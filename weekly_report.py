"""Generate and optionally email the Bat's Pool Sunday report.

Reads the validated, decrypted live checkpoint. NHL player game logs provide
per-player game counts, daily points, and draft-round weekly scores.
"""

import argparse
import concurrent.futures
import datetime as dt
import html
import json
import os
import smtplib
import ssl
import subprocess
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo

from scripts.player_board import fetch

TZ = ZoneInfo("America/Toronto")
SENDER = "mcfarlane.donnie@gmail.com"
RECIPIENTS = tuple(os.environ.get("WEEKLY_RECIPIENTS", "").replace(",", " ").split())
if len(RECIPIENTS) != 2 or len(set(RECIPIENTS)) != 2 or SENDER not in RECIPIENTS:
    raise ValueError("WEEKLY_RECIPIENTS must contain exactly two distinct addresses, including the sender")
DASHBOARD = "https://batspool2027-private.pages.dev/"
SEASON = "20262027"


def report_dates(now):
    local = now.astimezone(TZ)
    if local.weekday() != 6 or local.hour < 3:
        raise ValueError("Weekly report must run on Sunday after 3 a.m. Toronto time")
    last_day = local.date() - dt.timedelta(days=1)
    first_day = last_day - dt.timedelta(days=6)
    baseline = first_day - dt.timedelta(days=1)
    return first_day, last_day, baseline


def game_log(player):
    ident = player["playerId"]
    data = fetch(f"https://api-web.nhle.com/v1/player/{ident}/game-log/{SEASON}/2")
    games = data.get("gameLog")
    if not isinstance(games, list):
        raise ValueError(f"Missing regular-season game log for {ident}")
    return ident, games


def build_report(pool, now, test=False):
    if test:
        last = dt.date.fromisoformat(pool["end"])
        first = last - dt.timedelta(days=6)
        baseline = first - dt.timedelta(days=1)
    else:
        first, last, baseline = report_dates(now)
    if not test and pool["end"] != last.isoformat():
        raise ValueError(f"Checkpoint ends {pool['end']}; expected {last}. Refusing stale or future results")
    roster = [p for c in pool["contestants"] for p in c["players"]]
    if len(roster) != 132 or len({p["playerId"] for p in roster}) != 132:
        raise ValueError("Expected 132 uniquely drafted players")
    draft = json.loads((Path(__file__).parent / "scripts/draft.json").read_text())
    current_teams = {row["id"]: row["team"] for row in pool.get("playerBoard", {}).get("rows", [])}
    history = {x["date"]: x for x in pool["history"]}
    if baseline.isoformat() not in history or last.isoformat() not in history:
        raise ValueError("Missing standings boundary date")
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        logs = dict(ex.map(game_log, roster))

    by_player = []
    per_day = {}
    game_counts = {c["name"]: [0, 0] for c in pool["contestants"]}
    for p in roster:
        name = p["contestant"]
        games = logs[p["playerId"]]
        before_games = after_games = week_points = 0
        for g in games:
            day = dt.date.fromisoformat(g["gameDate"])
            points = int(g["points"])
            if day <= baseline:
                before_games += 1
            if day <= last:
                after_games += 1
            if first <= day <= last:
                week_points += points
                key = (day.isoformat(), name)
                per_day[key] = per_day.get(key, 0) + points
        game_counts[name][0] += before_games
        game_counts[name][1] += after_games
        round_no = int(draft[str(p["playerId"])][0])
        team = current_teams.get(p["playerId"]) or draft[str(p["playerId"])][2]
        by_player.append({"name": p["name"], "owner": name, "round": round_no,
                          "points": week_points, "team": team, "games": games})

    contestants = []
    for c in pool["contestants"]:
        name = c["name"]
        before = history[baseline.isoformat()][name]
        after = history[last.isoformat()][name]
        weekly = after - before
        if weekly != sum(p["points"] for p in by_player if p["owner"] == name):
            raise ValueError(f"Weekly NHL player points disagree with checkpoint for {name}")
        prior_games, current_games = game_counts[name]
        if prior_games == 0 or current_games == 0:
            raise ValueError(f"Cannot compute scoring pace for {name}")
        contestants.append({"name": name, "weekly": weekly,
                            "ppg_jump": after / current_games - before / prior_games})

    top_three = sorted(contestants, key=lambda x: (-x["weekly"], x["name"]))[:3]
    top_rounds = []
    for r in range(1, 13):
        options = [p for p in by_player if p["round"] == r]
        if not options:
            raise ValueError(f"Missing draft round {r}")
        top_rounds.append(sorted(options, key=lambda p: (-p["points"], p["name"]))[0])
    top_rounds.sort(key=lambda p: (-p["points"], p["round"]))

    player_days = []
    for p in by_player:
        totals = {}
        for g in p["games"]:
            d = g["gameDate"]
            if first.isoformat() <= d <= last.isoformat():
                totals[d] = totals.get(d, 0) + int(g["points"])
        player_days.extend((n, p["name"], p["owner"], d) for d, n in totals.items())
    if not player_days or not per_day:
        raise ValueError("No completed player scoring records for reporting week")
    best_player = sorted(player_days, key=lambda x: (-x[0], x[1], x[3]))[0]
    best_contestant = sorted(((n, owner, day) for (day, owner), n in per_day.items()),
                             key=lambda x: (-x[0], x[1], x[2]))[0]
    pace = sorted(contestants, key=lambda x: (-x["ppg_jump"], x["name"]))[0]
    return dict(first=first, last=last, top_three=top_three, top_rounds=top_rounds,
                best_player=best_player, best_contestant=best_contestant, pace=pace)


def render(report):
    h = lambda s: html.escape(str(s))
    first, last = report["first"], report["last"]
    # The displayed week starts at the preceding Sunday 3 a.m. local time.
    start = first - dt.timedelta(days=1)
    label = f"{start:%B %d}–{last + dt.timedelta(days=1):%B %d, %Y}"
    short = f"{start:%b %d}–{last + dt.timedelta(days=1):%b %d, %Y}"
    tr = lambda cells: "<tr>" + "".join(f"<td>{h(cell)}</td>" for cell in cells) + "</tr>"
    three = "".join(tr([i, p["name"], p["weekly"]]) for i, p in enumerate(report["top_three"], 1))
    twelve = ""
    for p in report["top_rounds"]:
        team = h(p["team"])
        # The abbreviated team name is an accessible fallback when an email
        # client blocks remote SVG images.
        logo = (f"<img src='https://assets.nhle.com/logos/nhl/svg/{team}_light.svg' "
                f"alt='{team}' width='19' height='19' style='vertical-align:middle;margin-right:4px'>")
        twelve += ("<tr>" + f"<td>{p['round']}</td><td>{p['points']}</td>"
                   + f"<td>{logo}<span>{h(p['name'])}</span></td><td>{h(p['owner'])}</td></tr>")
    bp, bc, pace = report["best_player"], report["best_contestant"], report["pace"]
    date = lambda d: dt.date.fromisoformat(d).strftime("%B %d")
    style = "font-family:Arial,sans-serif;color:#203040;max-width:660px;margin:auto;line-height:1.5"
    table = "border-collapse:collapse;width:100%;text-align:left"
    css = "<style>td,th{border-bottom:1px solid #dde3e8;padding:6px 9px}th{background:#f3f6f9}h3{margin-bottom:8px}hr{border:0;border-top:1px solid #ccd6df;margin:24px 0}</style>"
    body = f"""<html><head>{css}</head><body><main style='{style}'>
<h1>BAT’S POOL 2027</h1><strong>Weekly Report | {h(label)}</strong>
<p>Another exciting week in hockey! Here’s what happened:</p><hr>
<h3>TOP 3 CONTESTANTS OF THE WEEK</h3>
<table style='{table}'><tr><th>Rank</th><th>Contestant</th><th>Points</th></tr>{three}</table><hr>
<h3>TOP 12 PICKS OF THE WEEK</h3>
<p><em>The highest-scoring player from each draft round, ranked by points earned this week.</em></p>
<table style='{table};table-layout:fixed;font-size:12px;overflow-wrap:anywhere'><tr><th style='width:12%'>Round</th><th style='width:15%'>Points</th><th style='width:40%'>Player</th><th style='width:33%'>Contestant</th></tr>{twelve}</table><hr>
<h3>WEEKLY HIGHLIGHTS</h3>
<p><strong>Best Day for a Player</strong><br>{h(bp[1])} ({h(bp[2])}) — <strong>{bp[0]} points</strong> on {h(date(bp[3]))}</p>
<p><strong>Best Day for a Contestant</strong><br>{h(bc[1])} — <strong>{bc[0]} points</strong> on {h(date(bc[2]))}</p>
<p><strong>Best Jump in Scoring Pace this Week</strong><br>{h(pace['name'])}</p><hr>
<h3>FULL STANDINGS &amp; ANALYSIS</h3>
<p>See who’s moving up, who’s falling behind, and how your picks compare.</p>
<p><a href='{DASHBOARD}' style='display:inline-block;background:#176ba0;color:white;padding:12px 20px;text-decoration:none;border-radius:5px;font-weight:bold'>VIEW THE LIVE DASHBOARD</a></p><hr>
<p style='font-size:12px;color:#536575'><em>For questions or feedback, please email {SENDER}</em></p>
</main></body></html>"""
    return f"Week in Review - Batspool2027 | {short}", body


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--checkpoint", required=True, type=Path)
    parser.add_argument("--send", action="store_true")
    parser.add_argument("--test", action="store_true", help="Use latest validated checkpoint even when week is incomplete")
    parser.add_argument("--preview", type=Path)
    args = parser.parse_args()
    report = build_report(json.loads(args.checkpoint.read_text()), dt.datetime.now(dt.timezone.utc), test=args.test)
    subject, body = render(report)
    if args.test:
        subject = "[TEST] " + subject
        body = body.replace("<h1>BAT’S POOL 2027</h1>",
            "<h1>BAT’S POOL 2027</h1><p style='background:#fff3cd;padding:10px'><strong>TEST REPORT</strong> — using only the latest validated data currently available. This is not the final weekly report.</p>")
    if args.preview:
        args.preview.write_text(body, encoding="utf-8")
    print(f"Prepared {subject} | recipient count: {len(RECIPIENTS)} | 12 rounds | 3 leaders")
    if args.send:
        password = os.environ.get("GMAIL_APP_PASSWORD")
        if not password:
            raise ValueError("GMAIL_APP_PASSWORD is missing")
        msg = EmailMessage()
        msg["From"] = SENDER
        msg["To"] = ", ".join(RECIPIENTS)
        msg["Subject"] = subject
        msg.set_content("View the weekly report in an HTML-capable email client: " + DASHBOARD)
        msg.add_alternative(body, subtype="html")
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context(), timeout=30) as smtp:
            smtp.login(SENDER, password)
            smtp.send_message(msg, to_addrs=RECIPIENTS)
        print("Email accepted by Gmail SMTP for both configured recipients")


if __name__ == "__main__":
    main()
