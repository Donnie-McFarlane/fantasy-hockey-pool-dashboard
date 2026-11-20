"""Fallback: use official NHL final game box scores for the next day.

Calls the existing append_day validation and score aggregation, substituting
official box-score skater records for the unavailable NHL stats REST endpoint.
"""

import datetime as dt
import json
from pathlib import Path

import update_pool_day as updater


def boxscore_rows(date, drafted_ids):
    game_ids = updater.validated_game_ids(date)
    rows = []
    for game_id in sorted(game_ids):
        game = updater.fetch_json(f"https://api-web.nhle.com/v1/gamecenter/{game_id}/boxscore")
        if game.get("id") != game_id or game.get("gameState") not in ("FINAL", "OFF"):
            raise RuntimeError(f"Unfinalized or incorrect box score: {game_id}")
        for side in ("homeTeam", "awayTeam"):
            skaters = [p for group in ("forwards", "defense") for p in game["playerByGameStats"][side][group]]
            if len({p["playerId"] for p in skaters}) != len(skaters):
                raise RuntimeError(f"Duplicate skater in {game_id} {side}")
            scored = sum(p["goals"] for p in skaters)
            total_goals = game[side]["score"]
            if scored != total_goals:
                # Shootouts award one team goal not credited to a skater.
                if not (game.get("periodDescriptor", {}).get("periodType") == "SO" and scored + 1 == total_goals):
                    raise RuntimeError(f"Box score goal sum mismatch in {game_id} {side}: {scored} vs {total_goals}")
            for p in skaters:
                if p["points"] != p["goals"] + p["assists"]:
                    raise RuntimeError(f"Point arithmetic mismatch: {game_id}/{p['playerId']}")
                if p["playerId"] in drafted_ids:
                    rows.append({"playerId": p["playerId"], "gameId": game_id,
                                 "gameDate": date.isoformat(), "goals": p["goals"],
                                 "assists": p["assists"], "points": p["points"]})
        print(f"Verified official box score {game_id}", flush=True)
    return rows


if __name__ == "__main__":
    pool = json.loads(updater.DATA_PATH.read_text(encoding="utf-8"))
    date = dt.date.fromisoformat(pool["end"]) + dt.timedelta(days=1)
    yesterday = dt.datetime.now(updater.ZoneInfo("America/Toronto")).date() - dt.timedelta(days=1)
    if date != yesterday:
        raise SystemExit(f"Expected exactly yesterday: next day={date}, yesterday={yesterday}")
    updater.player_game_rows = boxscore_rows
    updater.main()
