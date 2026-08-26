"""
HF Football pool — reads every team's REGULAR SEASON record from ESPN and writes
standings.json. Bowl games, the college football playoff and the NFL playoffs do not count.

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


def score_value(side):
    """ESPN sends a score as a plain string in some feeds and an object in others."""
    raw = (side or {}).get("score")
    if isinstance(raw, dict):
        raw = raw.get("value", raw.get("displayValue"))
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return None


CHAMPIONSHIP_WORDS = ("championship", "champ game", "title game")


def is_championship(event, comp):
    """Conference title games are filed as regular season by ESPN. They do not count."""
    blobs = [event.get("name", ""), event.get("shortName", "")]
    for note in (comp.get("notes") or []):
        blobs.append(note.get("headline", ""))
    text = " ".join(blobs).lower()
    if "championship subdivision" in text:  # that's just FCS in a team's name
        text = text.replace("championship subdivision", "")
    return any(word in text for word in CHAMPIONSHIP_WORDS)


def team_games(league, team_id):
    """Every game on a team's card, with a flag for whether it counts in the pool.

    Counting games are season type 2 (regular season) and not a conference title game.
    Preseason (type 1), bowls, the college playoff and the NFL playoffs (type 3) are
    kept in the list so the site can show a full season, but they do not count.
    """
    data = get_json("/%s/teams/%s/schedule" % (league, team_id))
    rows = []
    for event in data.get("events", []):
        comp = (event.get("competitions") or [{}])[0]
        kind = event.get("seasonType") or {}
        stype = str(kind.get("type", kind.get("id", 2)))
        title = is_championship(event, comp)

        sides = comp.get("competitors") or []
        mine = next((s for s in sides if str((s.get("team") or {}).get("id")) == str(team_id)), None)
        theirs = next((s for s in sides if s is not mine), None)
        if not mine or not theirs:
            continue

        status = (comp.get("status") or {}).get("type") or {}
        state = status.get("state", "pre")
        opp = theirs.get("team") or {}
        rows.append({
            "id": str(event.get("id", "")),
            "wk": (event.get("week") or {}).get("number"),
            "date": event.get("date", ""),
            "opp": opp.get("shortDisplayName") or opp.get("displayName") or "TBD",
            "oppId": str(opp.get("id", "")),
            "home": mine.get("homeAway") == "home",
            "state": state,
            "detail": status.get("shortDetail", ""),
            "us": score_value(mine),
            "them": score_value(theirs),
            "counts": stype == "2" and not title,
            "why": "title game" if title else ("preseason" if stype == "1" else
                   ("postseason" if stype == "3" else "")),
        })
    return rows


def record_from(rows):
    wins = losses = ties = 0
    for row in rows:
        if not row["counts"] or row["state"] != "post":
            continue
        ours, theirs = row["us"], row["them"]
        if ours is None or theirs is None:
            continue
        if ours > theirs:
            wins += 1
        elif ours < theirs:
            losses += 1
        else:
            ties += 1
    return wins, losses, ties


def current_odds():
    """One snapshot of this week's posted lines, so the site still shows a number
    even when a phone or laptop can't reach ESPN directly."""
    out = {}
    for path in ("/nfl/scoreboard?seasontype=2",
                 "/college-football/scoreboard?groups=80&limit=300&seasontype=2"):
        try:
            data = get_json(path)
        except Exception as err:  # noqa: BLE001
            print("  no odds from %s (%s)" % (path.split("/")[1], err))
            continue
        for event in data.get("events", []):
            comp = (event.get("competitions") or [{}])[0]
            odds = (comp.get("odds") or [{}])[0]
            if odds.get("details") or odds.get("overUnder") is not None:
                out[str(event.get("id", ""))] = {
                    "d": odds.get("details"),
                    "ou": odds.get("overUnder"),
                }
    return out


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

    results, unmatched, no_record, espn_ids, schedules = {}, [], [], {}, {}
    for entry in roster["teams"]:
        league = entry["league"]
        found = lookup[league].get(norm(entry["espn"])) or lookup[league].get(norm(entry["label"]))
        if not found:
            unmatched.append(entry["label"])
            results[entry["label"]] = {"w": 0, "l": 0, "t": 0}
            schedules[entry["label"]] = []
            continue

        if found.get("id"):
            espn_ids[entry["label"]] = str(found["id"])

        rec, rows = None, []
        if found.get("id"):
            try:
                rows = team_games(league, found["id"])
                rec = record_from(rows)
            except Exception as err:  # noqa: BLE001 — fall back to the posted record
                print("  schedule unavailable for %s (%s)" % (entry["label"], err))
        schedules[entry["label"]] = rows
        if rec is None:  # last resort: ESPN's posted record, which does include bowls
            rec = overall_record(found) or (0, 0, 0)
            no_record.append(entry["label"])
        results[entry["label"]] = {"w": rec[0], "l": rec[1], "t": rec[2]}

    if unmatched:
        print("\nNot found on ESPN (fix the 'espn' name in teams.json): %s" % ", ".join(unmatched))
    if no_record:
        print("Fell back to the posted record (may include bowls) for: %s" % ", ".join(no_record))

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

    with open(os.path.join(HERE, "schedules.json"), "w", encoding="utf-8") as fh:
        json.dump({
            "updated": payload["updated"],
            "leagues": {e["label"]: e["league"] for e in roster["teams"]},
            "owners": {e["label"]: e["owner"] for e in roster["teams"]},
            "teams": schedules,
            "odds": current_odds(),
        }, fh, separators=(",", ":"))

    print("\nWeek %d standings:" % week)
    for owner in sorted(totals, key=lambda o: -totals[o]):
        print("  %-11s %3d wins  (%+d this week)" % (owner, totals[owner], gained[owner]))
    print("\nWrote standings.json and schedules.json")

    if unmatched:
        sys.exit(0)  # still publish — just leave the warning in the log


if __name__ == "__main__":
    main()
