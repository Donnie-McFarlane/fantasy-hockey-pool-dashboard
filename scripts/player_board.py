"""Build daily player statistics for the fixed DonniesPool rosters."""
import concurrent.futures
import datetime as dt
import json
from pathlib import Path
import re
import subprocess
import unicodedata
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent


def fetch(url):
    result = subprocess.run(["curl", "-fsSL", "--retry", "2", "--connect-timeout", "8", "--max-time", "30", url],
                            check=True, capture_output=True, text=True)
    return json.loads(result.stdout)


def name_key(name):
    return re.sub("[^a-z]", "", unicodedata.normalize("NFKD", name.lower()).encode("ascii", "ignore").decode())


def build(pool, source_dir):
    draft = json.loads((ROOT / "draft.json").read_text())
    end = dt.date.fromisoformat(pool["end"])
    today = dt.datetime.now(ZoneInfo("America/Toronto")).date()
    start = end - dt.timedelta(days=6)
    schedule = fetch(f"https://api-web.nhle.com/v1/score/{today}")
    assert schedule["currentDate"] == str(today)
    playing = {game[side]["abbrev"] for game in schedule["games"] if game["gameType"] == 2
               and game.get("gameScheduleState") not in ("PPD", "CNCL") for side in ("homeTeam", "awayTeam")}
    report = fetch("https://site.api.espn.com/apis/site/v2/sports/hockey/nhl/injuries")
    if not report["injuries"] or not report["timestamp"]:
        raise ValueError("Empty NHL injury report")
    injury = {}
    for club in report["injuries"]:
        for item in club["injuries"]:
            if item.get("status") and item["status"].lower() not in ("active","healthy"):
                injury[name_key(item["athlete"]["displayName"])] = item["status"]
    players = [p for c in pool["contestants"] for p in c["players"]]
    # Read the same validated scoring lines as the standings, including zero-point appearances.
    import importlib.util
    spec = importlib.util.spec_from_file_location("donniespool_scores", source_dir / "update_pool_day.py")
    scoring = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(scoring)
    player_ids = {p["playerId"] for p in players}
    dates = [start + dt.timedelta(days=i) for i in range(7)]
    def scored_day(day):
        return scoring.boxscore_rows(day, player_ids)
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        day_rows = list(executor.map(scored_day, dates))
    night = {}
    week = {}
    for day, records in zip(dates, day_rows):
        seen = set()
        for row in records:
            key = (row["playerId"], row["gameId"])
            if key in seen:
                raise ValueError(f"Duplicate player game on {day}: {key}")
            seen.add(key)
            ident = row["playerId"]
            week[ident] = week.get(ident, 0) + row["points"]
            if day == end:
                night[ident] = night.get(ident, 0) + row["points"]

    def player_row(p):
        ident = str(p["playerId"])
        round_no, position, team = draft[ident]
        profile = fetch(f"https://api-web.nhle.com/v1/player/{ident}/landing")
        assert profile["playerId"] == p["playerId"]
        team = profile.get("currentTeamAbbrev") or team
        names = [p["name"],p["nhlName"],profile["firstName"]["default"]+" "+profile["lastName"]["default"]]
        status = next((injury[name_key(n)] for n in names if name_key(n) in injury), "")
        return dict(id=p["playerId"], name=p["name"], owner=p["contestant"], team=team, pos=profile.get("position") or position,
                    round=round_no, total=p["nhlPoints"], night=night.get(p["playerId"]), playedLastNight=p["playerId"] in night,
                    week=week.get(p["playerId"]), injury=status, tonight=team in playing)
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        rows = list(executor.map(player_row, players))
    history = {r["date"]:r for r in pool["history"]}
    for c in pool["contestants"]:
        name = c["name"]
        group = [r for r in rows if r["owner"] == name]
        assert sorted(r["round"] for r in group) == list(range(1,13))
        for key,days in (("night",1),("week",7)):
            base = history.get(str(end-dt.timedelta(days=days)),{}).get(name,0)
            if sum((r[key] or 0) for r in group) != c["actualPoints"]-base:
                raise ValueError(f"{name}: {key} player totals disagree with standings")
    return dict(end=str(end), start=str(start), today=str(today), injuryDate=report["timestamp"][:10], rows=rows)


