"""
HF Football pool — reads every team's record from ESPN and writes standings.json.

Runs by itself once a day on GitHub. To try it on your own computer:  python update.py
"""

import json
import os
import re
import sys
import time
import datetime
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
HEADERS = {"User-Agent": "hf-football-pool/1.0 (+github pages)"}

# ESPN's public feeds. The first one that answers is used.
HOSTS = [
    "https://site.web.api.espn.com/apis/site/v2/sports/football",
    "https://site.api.espn.com/apis/site/v2/sports/football",
]


def get_json(path, tries=3):
    """Fetch one ESPN URL, trying both hosts and retrying if the network hiccups."""
    last = None
    for attempt in range(tries):
        for host in HOSTS:
            url = host + path
            try:
                req = urllib.request.Request(url, headers=HEADERS)
                with urllib.request.urlopen(req, timeout=30) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except Exception as err:  # noqa: BLE001 - any failure means try the next host
                last = err
        time.sleep(2 * (attempt + 1))
    raise RuntimeError("could not reach ESPN for %s (%s)" % (path, last))


def norm(text):
    return re.sub(r"[^a-z0-9]", "", str(text).lower())


def parse_record(summary):
    """'7-2' or '7-2-1' -> (7, 2, 1)"""
    m = re.match(r"^\s*(\d+)\s*-\s*(\d+)(?:\s*-\s*(\d+))?", str(summary))
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)


def overall_record(team):
    """Pull the overall (not conference-only) record out of an ESPN team object."""
    items = (team.get("record") or {}).get("items") or []
    ranked = []
    for item in items:
        kind = (item.get("type") or item.get("description") or "").lower()
        score = 0 if kind in ("total", "overall") else 1
        ranked.append((score, item))
    ranked.sort(key=lambda pair: pair[0])
    for _, item in ranked:
        rec = parse_record(item.get("summary", ""))
        if rec:
            return rec
    return None


def load_league(league, limit):
    """Return every team ESPN lists for a league, with its record when included."""
    data = get_json("/%s/teams?limit=%d" % (league, limit))
    out = []
    for group in data["sports"][0]["leagues"][0]["teams"]:
        team = group["team"]
        team["_league"] = league
        out.append(team)
    return out


def fetch_one(league, team_id):
    data = get_json("/%s/teams/%s" % (league, team_id))
    return data.get("team") or {}


def index_teams(teams):
    """Every name ESPN might call a team -> the team itself."""
    idx = {}
    for team in teams:
        keys = [
            team.get("displayName"),
            team.get("shortDisplayName"),
            team.get("nickname"),
            team.get("name"),
            team.get("location"),
            team.get("abbreviation"),
            team.get("slug"),
            "%s %s" % (team.get("location", ""), team.get("name", "")),
        ]
        for key in keys:
            if key:
                idx.setdefault(norm(key), team)
    return idx


def week_number(day, season_start):
    return max(1, ((day - season_start).days // 7) + 1)


def main():
    with open(os.path.join(HERE, "teams.json"), encoding="utf-8") as fh:
        roster = json.load(fh)

    season_start = datetime.date.fromisoformat(roster["seasonStart"])
    today = datetime.date.today()
    week = week_number(today, season_start)

    print("Reading ESPN...")
    espn = {
        "nfl": load_league("nfl", 50),
        "college-football": load_league("college-football", 900),
    }
    for league, teams in espn.items():
        print("  %s: %d teams listed" % (league, len(teams)))

    lookup = {league: index_teams(teams) for league, teams in espn.items()}

    results, unmatched, no_record, espn_ids = {}, [], [], {}
    for entry in roster["teams"]:
        league = entry["league"]
        found = lookup[league].get(norm(entry["espn"])) or lookup[league].get(norm(entry["label"]))
        if not found:
            unmatched.append(entry["label"])
            results[entry["label"]] = {"w": 0, "l": 0, "t": 0}
            continue

        if found.get("id"):
            espn_ids[entry["label"]] = str(found["id"])

        rec = overall_record(found)
        if rec is None:  # the team list left the record out — ask for that one team
            try:
                rec = overall_record(fetch_one(league, found.get("id")))
            except Exception as err:  # noqa: BLE001
                print("  could not read %s: %s" % (entry["label"], err))
        if rec is None:
            rec = (0, 0, 0)
            no_record.append(entry["label"])
        results[entry["label"]] = {"w": rec[0], "l": rec[1], "t": rec[2]}

    if unmatched:
        print("\nNot found on ESPN (fix the 'espn' name in teams.json): %s" % ", ".join(unmatched))
    if no_record:
        print("No record posted yet for: %s" % ", ".join(no_record))

    totals = {owner: 0 for owner in roster["owners"]}
    losses = {owner: 0 for owner in roster["owners"]}
    for entry in roster["teams"]:
        rec = results[entry["label"]]
        totals[entry["owner"]] += rec["w"]
        losses[entry["owner"]] += rec["l"]

    # keep the week-by-week history from the last run
    path = os.path.join(HERE, "standings.json")
    history = []
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                history = json.load(fh).get("weeks", [])
        except Exception:  # noqa: BLE001 - a broken file just starts the history over
            history = []

    previous = history[-2]["totals"] if len(history) >= 2 and history[-1]["week"] == week else (
        history[-1]["totals"] if history and history[-1]["week"] != week else None
    )
    gained = {o: totals[o] - previous[o] for o in roster["owners"]} if previous else dict(totals)

    row = {
        "week": week,
        "date": today.isoformat(),
        "totals": totals,
        "gained": gained,
    }
    if history and history[-1]["week"] == week:
        history[-1] = row
    else:
        history.append(row)

    payload = {
        "updated": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "week": week,
        "owners": roster["owners"],
        "groups": roster["groups"],
        "totals": totals,
        "losses": losses,
        "records": results,
        "roster": [
            {"label": e["label"], "owner": e["owner"], "group": e["group"]} for e in roster["teams"]
        ],
        "espnIds": espn_ids,
        "weeks": history,
        "problems": {"unmatched": unmatched, "noRecord": no_record},
    }

    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=1)

    print("\nWeek %d standings:" % week)
    for owner in sorted(totals, key=lambda o: -totals[o]):
        print("  %-11s %3d wins  (%+d this week)" % (owner, totals[owner], gained[owner]))
    print("\nWrote standings.json")

    if unmatched:
        sys.exit(0)  # still publish — just leave the warning in the log


if __name__ == "__main__":
    main()
