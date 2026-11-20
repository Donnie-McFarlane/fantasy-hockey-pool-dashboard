"""Append finalized NHL regular-season game days to Batspool2027.

This fetches only the requested *new* day, in five batches of roster IDs,
and refuses to publish a partial game night. Run the renderer afterward.

    python3 update_pool_day.py --through 2026-11-15

With no --through argument, catches up through yesterday in Toronto time.
It is idempotent: dates already present in pool_data.json are never counted twice.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import statistics
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parent
DATA_PATH = ROOT / "pool_data.json"
SEASON = 20262027
REGULAR_SEASON_END = dt.date(2027, 4, 10)
STATS_URL = "https://api.nhle.com/stats/rest/en/skater/summary"


def fetch_json(url: str, params: dict[str, str] | None = None) -> dict:
    command = ["curl", "--fail", "--silent", "--show-error", "--retry", "2", "--max-time", "45"]
    if params:
        command.append("--get")
    command.append(url)
    for key, value in (params or {}).items():
        command.extend(("--data-urlencode", f"{key}={value}"))
    result = subprocess.run(command, check=True, capture_output=True, text=True, timeout=55)
    return json.loads(result.stdout)


def validated_game_ids(date: dt.date) -> set[int]:
    response = fetch_json(f"https://api-web.nhle.com/v1/score/{date.isoformat()}")
    if not isinstance(response.get("games"), list):
        raise RuntimeError(f"NHL scoreboard missing games array for {date}; no update")
    games = [g for g in response.get("games", []) if g.get("gameType") == 2]
    unfinished = [(g["id"], g.get("gameState")) for g in games if g.get("gameState") not in ("OFF", "FINAL")]
    if unfinished:
        raise RuntimeError(f"Refusing partial NHL results for {date}: {unfinished}")
    if games and len({g["id"] for g in games}) != len(games):
        raise RuntimeError(f"Duplicate game IDs in NHL schedule for {date}")
    return {g["id"] for g in games}


def player_game_rows(date: dt.date, player_ids: list[int]) -> list[dict]:
    # Final game box scores are more reliable here than the stats REST batches,
    # and require only one request per game rather than a query per player.
    # Use the REST batches only if the box-score transport itself fails.
    try:
        return boxscore_rows(date, set(player_ids))
    except (subprocess.TimeoutExpired, subprocess.CalledProcessError) as error:
        print(f"NHL box score endpoint failed ({type(error).__name__}: {error}); trying stats REST batches", flush=True)
    batches = [player_ids[i:i + 30] for i in range(0, len(player_ids), 30)]

    def batch_query(ids: list[int]) -> list[dict]:
        expression = (
            f"seasonId={SEASON} and gameTypeId=2 "
            f'and gameDate>="{date.isoformat()}" and gameDate<="{date.isoformat()}" '
            f"and playerId in ({','.join(map(str, ids))})"
        )
        response = fetch_json(STATS_URL, {
            "isAggregate": "false", "isGame": "true", "start": "0", "limit": "-1",
            "cayenneExp": expression,
        })
        if len(response.get("data", [])) != response.get("total"):
            raise RuntimeError(f"NHL stats batch truncated for {date}")
        return response["data"]

    try:
        with ThreadPoolExecutor(max_workers=3) as executor:
            return [record for rows in executor.map(batch_query, batches) for record in rows]
    except (subprocess.TimeoutExpired, subprocess.CalledProcessError) as error:
        print(f"NHL stats endpoint failed ({type(error).__name__}: {error}); verifying final game box scores instead", flush=True)
        return boxscore_rows(date, set(player_ids))


def boxscore_rows(date: dt.date, drafted_ids: set[int]) -> list[dict]:
    """Retrieve official final-game skater lines, verifying team goal sums."""
    game_ids = validated_game_ids(date)
    rows = []
    for game_id in sorted(game_ids):
        game = fetch_json(f"https://api-web.nhle.com/v1/gamecenter/{game_id}/boxscore")
        if game.get("id") != game_id or game.get("gameState") not in ("FINAL", "OFF"):
            raise RuntimeError(f"Unfinalized or wrong box score: {game_id}")
        for side in ("homeTeam", "awayTeam"):
            skaters = [p for group in ("forwards", "defense") for p in game["playerByGameStats"][side][group]]
            if len({p["playerId"] for p in skaters}) != len(skaters):
                raise RuntimeError(f"Duplicate skater in box score {game_id} {side}")
            goals_scored = sum(p["goals"] for p in skaters)
            team_goals = game[side]["score"]
            if goals_scored != team_goals and not (
                game.get("periodDescriptor", {}).get("periodType") == "SO" and goals_scored + 1 == team_goals
            ):
                raise RuntimeError(f"Box score goal sum mismatch: {game_id}/{side} {goals_scored}/{team_goals}")
            for p in skaters:
                if p["points"] != p["goals"] + p["assists"]:
                    raise RuntimeError(f"Goal/assist mismatch: {game_id}/{p['playerId']}")
                if p["playerId"] in drafted_ids:
                    rows.append({"playerId": p["playerId"], "gameId": game_id,
                                 "gameDate": date.isoformat(), "goals": p["goals"],
                                 "assists": p["assists"], "points": p["points"]})
        print(f"Verified final NHL box score: {game_id}", flush=True)
    return rows


def append_day(pool: dict, date: dt.date) -> tuple[dict, dict]:
    prev_date = dt.date.fromisoformat(pool["end"])
    if date != prev_date + dt.timedelta(days=1):
        raise ValueError(f"New day must be immediately after {prev_date}: got {date}")
    expected_games = validated_game_ids(date)
    ids = sorted({player["playerId"] for c in pool["contestants"] for player in c["players"]})
    rows = player_game_rows(date, ids) if expected_games else []
    allowed = set(ids)
    seen = set()
    points = {player_id: 0 for player_id in ids}
    actual_games = set()
    for row in rows:
        player_id, game_id = row["playerId"], row["gameId"]
        if player_id not in allowed or game_id not in expected_games or row["gameDate"] != date.isoformat():
            raise RuntimeError(f"Unexpected NHL player/game record: {player_id}/{game_id}")
        if (player_id, game_id) in seen:
            raise RuntimeError(f"Duplicate player/game: {player_id}/{game_id}")
        seen.add((player_id, game_id))
        if row["goals"] + row["assists"] != row["points"]:
            raise RuntimeError(f"Goals + assists do not match NHL points for {player_id}/{game_id}")
        points[player_id] += row["points"]
        actual_games.add(game_id)

    # All games must be represented by a roster skater before a day is accepted.
    # Otherwise cross-check their box scores, rather than quietly accepting a
    # possibly incomplete NHL stats response. There can be games with no drafted
    # players, so these are logged for review, not treated as automatic failures.
    uncovered_games = sorted(expected_games - actual_games)
    before = pool["history"][-1]
    daily = {}
    new_row = {"date": date.isoformat()}
    for contestant in pool["contestants"]:
        name = contestant["name"]
        if contestant["actualPoints"] != before[name]:
            raise RuntimeError(f"Latest history and contestant total differ for {name}")
        gained = sum(points[p["playerId"]] for p in contestant["players"])
        contestant["actualPoints"] += gained
        daily[name] = gained
        new_row[name] = contestant["actualPoints"]
        for player in contestant["players"]:
            player["nhlPoints"] += points[player["playerId"]]
            # PDF totals refer to the original Nov. 14 validation snapshot.
            player["difference"] = player["nhlPoints"] - player["pdfPoints"]
        if sum(p["nhlPoints"] for p in contestant["players"]) != contestant["actualPoints"]:
            raise RuntimeError(f"Player sum and contestant total differ for {name}")
    pool["days"] += 1
    pool["end"] = date.isoformat()
    new_row["Median Pace"] = round(pool["days"] * pool["benchmarkPointsPerDay"], 8)
    pool["history"].append(new_row)
    pool["medianPoints"] = statistics.median(c["actualPoints"] for c in pool["contestants"])
    pool["observedMedianPointsPerDay"] = pool["medianPoints"] / pool["days"]
    pool.setdefault("validatedSnapshotDate", "2026-11-14")
    pool.setdefault("updates", []).append({
        "date": date.isoformat(), "scheduledGameCount": len(expected_games),
        "gamesWithoutRosterPlayer": uncovered_games,
        "dailyContestantPoints": daily,
    })
    return daily, {"games": len(expected_games), "statsRows": len(rows), "uncoveredGames": uncovered_games}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--through", help="Last completed calendar date (YYYY-MM-DD); default yesterday Toronto")
    args = parser.parse_args()
    through = (dt.date.fromisoformat(args.through) if args.through else
               dt.datetime.now(ZoneInfo("America/Toronto")).date() - dt.timedelta(days=1))
    if through > REGULAR_SEASON_END:
        through = REGULAR_SEASON_END
    pool = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    date = dt.date.fromisoformat(pool["end"]) + dt.timedelta(days=1)
    if through < date:
        print(f"Already current through {pool['end']}; nothing to append")
        return
    while date <= through:
        daily, details = append_day(pool, date)
        # Each day is committed independently, so a later incomplete day
        # cannot corrupt the last successfully published date.
        DATA_PATH.write_text(json.dumps(pool, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        with (ROOT / "historical_standings.csv").open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=["date", "Median Pace"] + [c["name"] for c in pool["contestants"]])
            writer.writeheader()
            writer.writerows(pool["history"])
        print(f"Appended {date}: {details['games']} final games, {details['statsRows']} drafted-player game lines")
        print("Daily pool points:", ", ".join(f"{c['name']} +{daily[c['name']]}" for c in pool["contestants"]))
        if details["uncoveredGames"]:
            print("Note: games with no drafted skater line:", details["uncoveredGames"])
        date += dt.timedelta(days=1)


if __name__ == "__main__":
    main()
