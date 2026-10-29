"""Exact-time dispatcher for the private studio-engine repo.

GitHub's own scheduler can start runs hours late or drop them (on 28 Oct 2026 it skipped
7 hours of runs and a post went out 47 minutes late), so posting does not depend on any
single scheduled run. Three layers:

1. Relay: whenever a post is due within the next 24 hours, this job stays awake for its
   full run and, before it ends, starts its own next run (workflow_dispatch, which does not
   go through the scheduler). One run that starts any time in the 24 hours before a slot
   is enough; the relay carries it to the slot.
2. Catch-up: after each event it checks that the work actually happened (published.json
   for a post, a snapshot in insights.json for insights) and re-triggers if it did not,
   up to a limit, so a dropped or failed run is retried instead of skipped.
   The same applies, from 4 hours ahead, to insights checkpoints (on 29 Oct a 24 h snapshot
   ran 6 hours late when the scheduler dropped runs again).
3. The schedule below runs often, to start a relay (or catch up) as early as possible.

It triggers in studio-engine:
  - publish.yml 25 minutes before a slot (it prepares the reel and posts at the exact minute)
  - insights.yml 24 h and 72 h after a slot (numbers and learnings for the next reels)
Duplicate triggers are harmless: studio-engine skips reels already posted or snapshotted.

Logs here are public, so this prints only times, event types and reel IDs, never topics.
Needs the secret STUDIO_ENGINE_TOKEN: a fine-grained token on UAnjali/studio-engine with
Actions: read and write, Contents: read.
"""

import base64
import csv
import datetime as dt
import io
import json
import os
import sys
import time
import urllib.error
import urllib.request

REPO = "UAnjali/studio-engine"
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
PUBLISH_LEAD = dt.timedelta(minutes=25)
INSIGHTS_DELAYS = (dt.timedelta(hours=24), dt.timedelta(hours=72))
RUN_LENGTH = dt.timedelta(minutes=330)        # stays under GitHub's 6-hour job limit
RELAY_AHEAD = dt.timedelta(hours=24)          # keep the relay going while a post is due this soon
INSIGHTS_RELAY_AHEAD = dt.timedelta(hours=4)  # and while an insights checkpoint is this close
POLL = dt.timedelta(minutes=5)

# Catch-up: how late an event may still be done, how long to give a run before checking,
# and how many runs in total to try per event.
PUBLISH_CATCHUP = dt.timedelta(hours=6)       # later than this, a post would crowd the next slot
INSIGHTS_CATCHUP = dt.timedelta(hours=12)
SETTLE = dt.timedelta(minutes=10)
MAX_RUNS = 3


def api(path, token, body=None, repo=REPO):
    req = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/{path}",
        data=json.dumps(body).encode() if body is not None else None,
        method="POST" if body is not None else "GET",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        raw = resp.read()
        return json.loads(raw) if raw else None


def file_text(token, path):
    """A file from studio-engine's main branch, or None if it does not exist."""
    try:
        content = api(f"contents/{path}", token)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise
    return base64.b64decode(content["content"]).decode()


def all_events(token):
    """Every (when, kind, reel, slot) from the calendar."""
    rows = csv.DictReader(io.StringIO(file_text(token, "config/calendar.csv")))
    out = []
    for row in rows:
        hour, minute = map(int, row["time_ist"].split(":"))
        slot = dt.datetime.fromisoformat(row["date"]).replace(hour=hour, minute=minute, tzinfo=IST)
        out.append((slot - PUBLISH_LEAD, "publish", row["id"], slot))
        for delay in INSIGHTS_DELAYS:
            out.append((slot + delay, "insights", row["id"], slot))
    return sorted(out)


def is_done(token, kind, reel, when):
    if kind == "publish":
        return file_text(token, f"reels/{reel}/published.json") is not None
    raw = file_text(token, f"reels/{reel}/insights.json")
    if not raw:
        return False
    # A snapshot taken from 30 minutes before the checkpoint counts.
    cutoff = when - dt.timedelta(minutes=30)
    return any(dt.datetime.fromisoformat(s["taken_at"]) >= cutoff for s in json.loads(raw))


def runs_since(token, kind, since):
    """studio-engine's runs of publish.yml or insights.yml created since a time."""
    stamp = since.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    out = api(f"actions/workflows/{kind}.yml/runs?created=%3E%3D{stamp}&per_page=20", token)
    return out["workflow_runs"]


def trigger(token, kind, reel, on_time):
    if kind == "publish":
        # On time: the calendar run, which waits and posts at the exact minute.
        # Late or catch-up: name the reel, which posts straight away.
        inputs = {"dry_run": "false", "lookahead_minutes": "40"} if on_time else {"dry_run": "false", "reel": reel}
    else:
        inputs = {"reel": reel}
    api(f"actions/workflows/{kind}.yml/dispatches", token, {"ref": "main", "inputs": inputs})
    label = "trigger" if on_time else "catch-up trigger"
    print(f"{label}: {kind} {reel} at {dt.datetime.now(IST):%a %d %b %H:%M:%S IST}")


def handle(token, event, now):
    """Fire or catch up one event that is due. Returns True while it still needs watching."""
    when, kind, reel, slot = event
    limit = slot + PUBLISH_CATCHUP if kind == "publish" else when + INSIGHTS_CATCHUP
    if now > limit:
        return False
    if is_done(token, kind, reel, when):
        return False
    runs = runs_since(token, kind, when - dt.timedelta(minutes=5))
    if any(r["status"] != "completed" for r in runs):
        return True                                   # a run is queued or working; give it time
    tries = [r for r in runs if r["event"] == "workflow_dispatch"]
    if not tries:
        trigger(token, kind, reel, on_time=now < slot - dt.timedelta(minutes=2) or kind != "publish")
        return True
    last = max(tries, key=lambda r: r["created_at"])
    if kind == "publish" and now < slot + dt.timedelta(minutes=5):
        return True                                   # the first run posts at the slot; check after it
    if kind == "publish" and last["conclusion"] == "success" and reel in last.get("display_title", ""):
        # A run for this reel finished cleanly without posting: it is not approved, or its issue
        # was closed after a manual post. publish.py has already said so on the issue.
        print(f"publish {reel}: run finished without posting (not approved or posted by hand); no retry")
        return False
    finished = dt.datetime.fromisoformat(last["updated_at"].replace("Z", "+00:00"))
    if now - finished < SETTLE:
        return True
    if len(tries) >= MAX_RUNS:
        print(f"{kind} {reel}: {len(tries)} runs tried, giving up (the failure is on the review issue)")
        return False
    trigger(token, kind, reel, on_time=False)
    return True


def relay(now):
    """Start this workflow's next run directly, without waiting for the scheduler."""
    own = os.environ.get("GITHUB_REPOSITORY")
    token = os.environ.get("GITHUB_TOKEN")
    if not own or not token:
        print("relay: not on GitHub Actions, not chaining")
        return
    api("actions/workflows/dispatch.yml/dispatches", token,
        {"ref": "main", "inputs": {"check_only": "false"}}, repo=own)
    print(f"relay: started the next run at {now.astimezone(IST):%a %H:%M IST}")


def main():
    token = os.environ.get("STUDIO_ENGINE_TOKEN")
    if not token:
        sys.exit("STUDIO_ENGINE_TOKEN is not set; add it in this repo's Settings > Secrets and variables > Actions.")
    start = dt.datetime.now(dt.timezone.utc)

    if os.environ.get("CHECK_ONLY") == "true":
        # Token check: reading the calendar proves Contents access; listing workflows proves Actions access.
        names = [w["name"] for w in api("actions/workflows", token)["workflows"]]
        print(f"token OK: calendar readable, {len(names)} workflows visible; publish.yml and insights.yml "
              f"{'found' if any('Publish' in n for n in names) and any('insights' in n.lower() for n in names) else 'NOT found'}")
        events = all_events(token)
        for when, kind, reel, slot in events:
            if when <= start and start <= (slot + PUBLISH_CATCHUP if kind == "publish" else when + INSIGHTS_CATCHUP):
                print(f"open: {kind} {reel} due {when.astimezone(IST):%a %d %b %H:%M IST}, "
                      f"done={is_done(token, kind, reel, when)}")
        for when, kind, reel, _ in [e for e in events if e[0] > start][:4]:
            print(f"next: {kind} {reel} at {when.astimezone(IST):%a %d %b %H:%M IST}")
        return

    deadline = start + RUN_LENGTH
    while True:
        now = dt.datetime.now(dt.timezone.utc)
        events = all_events(token)                    # re-read each round: the calendar can change
        watching = [e for e in events if e[0] <= now and handle(token, e, now)]
        upcoming = [e for e in events if e[0] > now]
        post_soon = any(when - now <= (RELAY_AHEAD if kind == "publish" else INSIGHTS_RELAY_AHEAD)
                        for when, kind, _, _ in upcoming)
        if not watching and not post_soon:
            print("nothing due in the next 24 hours; the schedule will start the next run")
            return
        next_event = upcoming[0][0] if upcoming else deadline
        wake = min(next_event, now + POLL, deadline)
        if wake >= deadline:
            relay(now)
            return
        time.sleep(max(1, (wake - now).total_seconds()))


if __name__ == "__main__":
    main()
