"""Exact-time dispatcher for the private studio-engine repo.

GitHub's own scheduler can start runs hours late, so studio-engine does not rely on it for
posting. This job runs every 2 hours here (a public repo, where waiting costs no Actions
minutes), reads studio-engine's calendar, sleeps until each event, and triggers:
  - publish.yml 25 minutes before a slot (it prepares the reel and posts at the exact minute)
  - insights.yml 24 h and 72 h after a slot (numbers and learnings for the next reels)
Duplicate triggers are harmless: studio-engine skips reels already posted or snapshotted.

Logs here are public, so this prints only times and event types, never reel topics.
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
import urllib.request

REPO = "UAnjali/studio-engine"
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
PUBLISH_LEAD = dt.timedelta(minutes=25)
INSIGHTS_DELAYS = (dt.timedelta(hours=24), dt.timedelta(hours=72))
WINDOW = dt.timedelta(minutes=330)   # stays under GitHub's 6-hour job limit
GRACE = dt.timedelta(minutes=40)     # still fire an event this late (the run itself may start late)


def api(path, token, body=None):
    req = urllib.request.Request(
        f"https://api.github.com/repos/{REPO}/{path}",
        data=json.dumps(body).encode() if body is not None else None,
        method="POST" if body is not None else "GET",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        raw = resp.read()
        return json.loads(raw) if raw else None


def events(token, now):
    content = api("contents/config/calendar.csv", token)
    rows = csv.DictReader(io.StringIO(base64.b64decode(content["content"]).decode()))
    found = []
    for row in rows:
        hour, minute = map(int, row["time_ist"].split(":"))
        slot = dt.datetime.fromisoformat(row["date"]).replace(hour=hour, minute=minute, tzinfo=IST)
        found.append((slot - PUBLISH_LEAD, "publish", {"dry_run": "false", "lookahead_minutes": "40"}))
        for delay in INSIGHTS_DELAYS:
            found.append((slot + delay, "insights", {"reel": row["id"]}))
    return sorted(e for e in found if now - GRACE <= e[0] <= now + WINDOW)


def _all(token):
    content = api("contents/config/calendar.csv", token)
    rows = csv.DictReader(io.StringIO(base64.b64decode(content["content"]).decode()))
    out = []
    for row in rows:
        hour, minute = map(int, row["time_ist"].split(":"))
        slot = dt.datetime.fromisoformat(row["date"]).replace(hour=hour, minute=minute, tzinfo=IST)
        out.append((slot - PUBLISH_LEAD, "publish", None))
        for delay in INSIGHTS_DELAYS:
            out.append((slot + delay, "insights", None))
    return out


def main():
    token = os.environ.get("STUDIO_ENGINE_TOKEN")
    if not token:
        sys.exit("STUDIO_ENGINE_TOKEN is not set; add it in this repo's Settings > Secrets and variables > Actions.")
    now = dt.datetime.now(dt.timezone.utc)
    todo = events(token, now)
    print(f"{len(todo)} event(s) in the next {WINDOW.seconds // 60} min")
    if os.environ.get("CHECK_ONLY") == "true":
        # Token check: reading the calendar above proves Contents access; listing workflows proves Actions access.
        names = [w["name"] for w in api("actions/workflows", token)["workflows"]]
        print(f"token OK: calendar readable, {len(names)} workflows visible; publish.yml and insights.yml "
              f"{'found' if any('Publish' in n for n in names) and any('insights' in n.lower() for n in names) else 'NOT found'}")
        horizon = sorted(e for e in _all(token) if e[0] >= now)[:4]
        for when, kind, _ in horizon:
            print(f"next: {kind} at {when.astimezone(IST):%a %d %b %H:%M IST}")
        return
    for when, kind, inputs in todo:
        wait = (when - dt.datetime.now(dt.timezone.utc)).total_seconds()
        if wait > 0:
            print(f"waiting until {when.astimezone(IST):%a %H:%M IST} for a {kind} trigger")
            time.sleep(wait)
        api(f"actions/workflows/{kind}.yml/dispatches", token, {"ref": "main", "inputs": inputs})
        print(f"triggered {kind} at {dt.datetime.now(IST):%a %H:%M:%S IST}")


if __name__ == "__main__":
    main()
