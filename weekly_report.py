"""Generate and optionally email the Bat's Pool Sunday report.

Reads the validated, decrypted live checkpoint. NHL player game logs provide
per-player game counts, daily points, and draft-round weekly scores.
"""

import argparse
import concurrent.futures
import datetime as dt
import html
import imaplib
from collections import defaultdict
import json
import os
import re
import smtplib
import ssl
import subprocess
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo

from scripts.player_board import fetch
from render_chart import live_names, relabel

TZ = ZoneInfo("America/Toronto")
SENDER = "mcfarlane.donnie@gmail.com"
RECIPIENTS = tuple(os.environ.get("WEEKLY_RECIPIENTS", "").replace(",", " ").split())
DASHBOARD = "https://batspool2027-private.pages.dev/"
SEASON = "20262027"



SUBJECTS = {2: "The SMT Report", 4: "The Friday Check-Up", 6: "The Sunday Review"}
DAY = dt.timedelta(days=1)
API = "https://api-web.nhle.com/v1/"

def dates(first, last):
    return [first + i*DAY for i in range(max(0,(last-first).days+1))]

def windows(day):
    if day.weekday() not in SUBJECTS:
        raise ValueError("Report date must be Wednesday, Friday or Sunday")
    return day-(3 if day.weekday()==2 else 2)*DAY, day-DAY, day+(2 if day.weekday()==6 else 1)*DAY

def game_log(player):
    ident = player["playerId"]
    games = fetch(API+f"player/{ident}/game-log/{SEASON}/2")["gameLog"]
    if not isinstance(games,list):
        raise ValueError("Invalid player game log")
    return ident,games

def score_day(day):
    data=fetch(API+f"score/{day}")
    if data["currentDate"]!=str(day):
        raise ValueError("Wrong score date")
    return [g for g in data["games"] if g["gameType"]==2 and g.get("gameScheduleState") not in ("PPD","CNCL")]

def ranks(values):
    return {n:1+sum(v>x for v in values.values()) for n,x in values.items()}

def player_key(p):
    parts=p["name"].casefold().split()
    return (-p["points"],-p["goals"],p["season"],parts[-1],p["name"].casefold())

def top_picks(players):
    """Select point tiers, retaining the existing deterministic tie breakers.

    Never include zero-point players or a tier below half the leader's points.
    A tier is all players with the same point total. At most five appear.
    """
    ordered = sorted((p for p in players if p["points"] > 0), key=player_key)
    if not ordered:
        return []
    leader = ordered[0]["points"]
    tiers = []
    for p in ordered:
        if 2 * p["points"] < leader:
            break
        if not tiers or tiers[-1][0]["points"] != p["points"]:
            tiers.append([])
        tiers[-1].append(p)
    top = tiers[0]
    if len(top) >= 3:
        return top[:5]
    if len(tiers) == 1:
        return top
    second = tiers[1]
    if len(top) == 1 and len(second) >= 2:
        return (top + second)[:5]
    selected = (top + second)[:5]
    if len(tiers) >= 3 and len(selected) == len(top) + len(second):
        third = tiers[2]
        if len(third) <= 3 and len(selected) + len(third) <= 5:
            selected += third
    return selected[:5]

def summarize(pool, logs, first, last, draft):
    names=[c["name"] for c in pool["contestants"]]
    history={x["date"]:x for x in pool["history"]}
    before={n:history[str(first-DAY)][n] for n in names}
    after={n:history[str(last)][n] for n in names}
    delta={n:after[n]-before[n] for n in names}
    rb,ra=ranks(before),ranks(after)
    moves={n:rb[n]-ra[n] for n in names}
    players=[]; pd=[]; cd=defaultdict(int)
    for c in pool["contestants"]:
        for p in c["players"]:
            ident=p["playerId"]; games=logs[ident]
            eligible=[g for g in games if str(first)<=g["gameDate"]<=str(last)]
            daily=defaultdict(int)
            for g in eligible:
                daily[g["gameDate"]]+=int(g["points"])
            for d,pts in daily.items():
                pd.append((pts,p["name"],c["name"],d))
                cd[c["name"],d]+=pts
            round_no,_,team=draft[str(ident)]
            prior=[g for g in games if g["gameDate"]<=str(last)]
            players.append(dict(name=p["name"],owner=c["name"],round=int(round_no),team=prior[0].get("teamAbbrev",team) if prior else team,points=sum(daily.values()),goals=sum(int(g["goals"]) for g in eligible),season=sum(int(g["points"]) for g in prior)))
    for n in names:
        if delta[n]!=sum(p["points"] for p in players if p["owner"]==n):
            raise ValueError("Player totals disagree with checkpoint")
    ordered=sorted(names,key=lambda n:(-delta[n],n.casefold()))
    rounds=[min((p for p in players if p["round"]==i),key=player_key,default=None) for i in range(1,13)]
    elapsed=(first-DAY-dt.date.fromisoformat(pool["history"][0]["date"])).days+1
    duration=(last-first).days+1
    pace={n:after[n]/(elapsed+duration)-before[n]/elapsed for n in names} if elapsed>0 else {}
    return locals()

def h(v): return html.escape(str(v))
def para(s): return "<p>"+h(s)+"</p>"
def section(s): return "<hr><h2>"+h(s)+"</h2>"
def field(label,value): return "<h3>"+h(label)+"</h3>"+para(value)

class Markup(str):
    """Trusted, internally generated HTML for a table cell."""

def logo(team, size=19):
    if not isinstance(team, str) or not re.fullmatch(r"[A-Z]{2,4}", team):
        return ""
    url = f"https://assets.nhle.com/logos/nhl/svg/{team}_light.svg"
    return (f'<img src="{url}" alt="{h(team)} logo" width="{size}" height="{size}" '
            'style="vertical-align:middle;margin-right:4px" />')

def player_label(name, team):
    return Markup(logo(team) + h(name) + " (" + h(team) + ")")

def ordinal(value):
    if not isinstance(value, int) or not 1 <= value <= 32:
        return "N/A"
    suffix = "th" if 11 <= value % 100 <= 13 else {1:"st",2:"nd",3:"rd"}.get(value % 10, "th")
    return str(value) + suffix

def table(headers, rows, heat=False, compact_header=False):
    out='<table style="border-collapse:collapse;width:100%;color:#000">'
    if compact_header:
        # Keep the visual divider between successive matchups without
        # repeating column labels (roughly half the normal header height).
        out += '<tr>' + ''.join(
            '<th style="padding:2px 5px;height:7px;font-size:8px;line-height:7px;background:#e8edf2">&nbsp;</th>'
            for _ in headers) + '</tr>'
    else:
        out += '<tr>' + ''.join(
            '<th style="padding:5px;background:#e8edf2;text-align:left;color:#000">'+h(x)+'</th>'
            for x in headers) + '</tr>'
    for i,row in enumerate(rows):
        out += "<tr>"
        for j,v in enumerate(row):
            bg = "#f4f6f8" if i%2 else "#fff"
            if heat and j and isinstance(v,tuple):
                count,injured=v
                bg=f"rgb({255-int(min(count,12)*6)},{255-int(min(count,12)*2)},{255-int(min(count,12)*3)})"
                v=str(count)+(f" ({injured})" if injured else "")
            cell = str(v) if isinstance(v,Markup) else h(v)
            out+=f'<td style="padding:5px;border-bottom:1px solid #ddd;background:{bg};color:#000">'+cell+'</td>'
        out += "</tr>"
    return out+"</table>"

def leaders(values, positive=False):
    if not values or (positive and max(values.values())<=0): return "N/A"
    best=max(values.values())
    return " & ".join(sorted(n for n,v in values.items() if v==best)) + f" — {best} pts"

def move_text(s, all_moves=False):
    moves=s["moves"]
    selected=[n for n,v in moves.items() if v!=0 and (all_moves or v==max(moves.values())>0)]
    return "; ".join(f"{n}: {s['rb'][n]} → {s['ra'][n]} ({moves[n]:+d})" for n in sorted(selected,key=lambda n:(-moves[n],n))) or "N/A"

def peak(rows):
    if not rows or max(x[0] for x in rows)<=0: return "N/A"
    best=max(x[0] for x in rows)
    return "; ".join(f"{x[1]} — {x[0]} pts ({x[-1]})" for x in sorted(rows) if x[0]==best)

def peak_players_html(rows, by_name):
    if not rows or max(x[0] for x in rows)<=0:
        return para("N/A")
    best=max(x[0] for x in rows)
    items=[logo(by_name.get(x[1]))+h(x[1])+f" — {x[0]} pts ("+h(x[-1])+")"
           for x in sorted(rows) if x[0]==best]
    return "<p>"+"; ".join(items)+"</p>"

def render_period(s,weekly=False,played=True):
    out=section("Week in Review" if weekly else f"{s['first']:%A}–{s['last']:%A} Review")+para(f"{s['first']:%b %d}–{s['last']:%b %d}")
    by_name={p["name"]:p["team"] for p in s["players"]}
    if weekly:
        order=s["ordered"]
        cutoff=s["delta"][order[min(2,len(order)-1)]] if order else 0
        out+="<h3>Top 3 Contestants of the Week</h3>"+(table(["Contestant","PTS"],[(n,s["delta"][n]) for n in order if s["delta"][n]>=cutoff]) if played else para("N/A"))
        picks=sorted((p for p in s["rounds"] if p and p["points"]>0),key=player_key)
        if picks:
            out+="<h3>Top 12 Picks of the Week</h3>"+para("Best player this week per draft round, sorted by points.")
    else:
        out+=field("Top Contestant",leaders(s["delta"],True) if played else "N/A")
        picks=top_picks(s["players"])
        if picks:
            out+=f"<h3>Top {len(picks)} Picks</h3>"
    if picks:
        out+=table(["Player","PTS","Round","Contestant"],[(player_label(p["name"],p["team"]),p["points"],p["round"],p["owner"]) for p in picks])
    if weekly:
        # Both highlights use player logo information from the reporting roster.
        out+="<h3>Most Points by a Player in One Day</h3>"+peak_players_html(s["pd"],by_name)
        out+=field("Most Points by a Contestant in One Day",peak([(v,n,d) for (n,d),v in s["cd"].items()]))
        pace=s["pace"]
        out+=field("Best Jump in Scoring Pace this Week"," & ".join(sorted(n for n,v in pace.items() if v==max(pace.values()))) if pace and max(pace.values())>0 else "N/A")
        out+=field("Biggest Standings Changes this Week",move_text(s,True))
        out+=section("Current Standings")+table(["Rank","Contestant","PTS","Week","+/-","Behind lead","Behind money"],[(s["ra"][n],n,s["after"][n],s["delta"][n],f"{s['moves'][n]:+d}" if s["moves"][n] else "—",max(s["after"].values())-s["after"][n],max(0,sorted(s["after"].values(),reverse=True)[min(2,len(s["after"])-1)]-s["after"][n])) for n in sorted(s["names"],key=lambda n:(s["ra"][n],n))])
    else:
        hits=[x for x in s["pd"] if x[0]>=4]
        if hits:
            parts=[h(x[3])+": "+logo(by_name.get(x[1]))+h(x[1])+f" — {x[0]} pts" for x in sorted(hits,key=lambda x:(x[3],-x[0],x[1]))]
            out+="<h3>4+ Point Performances</h3><p>"+"; ".join(parts)+"</p>"
        out+=field("Biggest Climb in Standings",move_text(s))
    return out

def box_highlights(g):
    b=fetch(API+f"gamecenter/{g['id']}/boxscore")
    rows=b["playerByGameStats"]
    out=[]
    for side in ("homeTeam","awayTeam"):
        for group in ("forwards","defense"):
            for p in rows[side][group]:
                goals=int(p["goals"]); pts=int(p["points"])
                if goals>=3 or pts>=4:
                    out.append((p["name"]["default"],goals,pts,g[side]["abbrev"]))
    return out

def team_stats(day, teams):
    standings=fetch(API+f"standings/{day}")["standings"]
    by={x["teamAbbrev"]["default"]:x for x in standings}
    def one(team):
        x=by.get(team,{})
        schedule=fetch(API+f"club-schedule-season/{team}/{SEASON}")["games"]
        games=sorted((g for g in schedule if g["gameType"]==2 and g["gameDate"]<=str(day) and g["gameState"] in ("OFF","FINAL")),key=lambda g:g["gameDate"])[-5:]
        w=l=o=0
        for g in games:
            a,b=g["awayTeam"],g["homeTeam"]
            own,opp=(a,b) if a["abbrev"]==team else (b,a)
            if own["score"]>opp["score"]: w+=1
            elif g.get("gameOutcome",{}).get("lastPeriodType") in ("OT","SO"): o+=1
            elif g.get("gameOutcome",{}).get("lastPeriodType")=="REG": l+=1
            else: return team,(x.get("points","N/A"),ordinal(x.get("leagueSequence")),"N/A")
        return team,(x.get("points","N/A"),ordinal(x.get("leagueSequence")),f"{w}-{l}-{o}" if games else "N/A")
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        return dict(ex.map(one,sorted(teams)))

def build_report(pool,day):
    first,last,future=windows(day)
    if pool["end"]<str(last): raise ValueError("Stale checkpoint")
    roster=[p for c in pool["contestants"] for p in c["players"]]
    if len(roster)!=132 or len({p["playerId"] for p in roster})!=132: raise ValueError("Invalid roster")
    draft=json.loads((Path(__file__).parent/"scripts/draft.json").read_text())
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        logs=dict(ex.map(game_log,roster))
        all_days=dates(first,future)
        slate=dict(zip(all_days,ex.map(score_day,all_days)))
        past=[(d,g) for d in dates(first,last) for g in slate[d]]
        if any(g["gameState"] not in ("OFF","FINAL") for d,g in past): raise ValueError("Incomplete results")
        highlights=list(ex.map(box_highlights,[g for d,g in past]))
    out=para(f"Good morning and Happy {day:%A}!")
    if day.weekday()==6: out+=para("(The Week in Review follows below)")
    night=slate[last]
    goals=sum(g["homeTeam"]["score"]+g["awayTeam"]["score"]-(g.get("gameOutcome",{}).get("lastPeriodType")=="SO") for g in night)
    intro=f"Last night there {'was' if len(night)==1 else 'were'} {len(night)} {'game' if len(night)==1 else 'games'} played and {goals} {'goal' if goals==1 else 'goals'} scored." if night else "There were no NHL games last night."
    for (d,g),hits in sorted(zip(past,highlights),key=lambda z:z[0][0],reverse=True):
        when="last night" if d==last else f"on {d:%A}"
        for name,goals,pts,team in hits:
            tag=logo(team)+h(name)
            if goals>=3: intro+=f" {tag} scored a hat trick {h(when)} ({goals} goals)."
            if pts>=4: intro+=f" {tag} recorded {pts} points {h(when)}."
    out+="<p>"+intro+"</p>"+para("Here’s what happened in the pool:")
    s=summarize(pool,logs,first,last,draft)
    out+=render_period(s,played=bool(past))
    if day.weekday()==6:
        week=summarize(pool,logs,day-7*DAY,last,draft)
        out+=render_period(week,True,any(p["points"] for p in week["players"]))
    out+=section("Around the NHL")
    for d in dates(first,last):
        scores=[]
        for g in slate[d]:
            a,b=g["awayTeam"],g["homeTeam"]
            if a["score"]<b["score"]: a,b=b,a
            typ=g.get("gameOutcome",{}).get("lastPeriodType")
            scores.append(logo(a["abbrev"],16)+h(a["abbrev"])+f" {a['score']} – "+logo(b["abbrev"],16)+h(b["abbrev"])+f" {b['score']}"+(f" ({typ})" if typ in ("OT","SO") else ""))
        out+="<h3>"+h(f"{d:%A}")+"</h3><p>"+("; ".join(scores) if scores else "No NHL games played.")+"</p>"
    out+=section("Looking Ahead")+para(f"{day:%b %d}–{future:%b %d}")
    upcoming=dates(day,future)
    teams_by_day={d:{g[side]["abbrev"] for g in slate[d] for side in ("homeTeam","awayTeam")} for d in upcoming}
    board={p["id"]:p for p in pool.get("playerBoard",{}).get("rows",[])}
    rows=[]
    for c in sorted(pool["contestants"],key=lambda c:(s["ra"][c["name"]],c["name"])):
        counts=[]
        for d in upcoming:
            eligible=[]
            for p in c["players"]:
                ident=p["playerId"]; old=[g for g in logs[ident] if g["gameDate"]<=str(last)]
                info=board.get(ident,{})
                team=info.get("team") if pool["end"]==str(last) else None
                team=team or (max(old,key=lambda g:g["gameDate"]).get("teamAbbrev") if old else None) or draft[str(ident)][2]
                if team in teams_by_day[d]: eligible.append(info)
            counts.append((len(eligible),sum(bool(p.get("injury")) for p in eligible) if pool["end"]==str(last) else 0))
        rows.append([c["name"],*counts,(sum(x[0] for x in counts),sum(x[1] for x in counts))])
    out+="<h3>Scheduled Player Games</h3>"+table(["Contestant",*[d.strftime("%a") for d in upcoming],"Total"],rows,True)+para("Parentheses: injured player appearances included. Team schedules do not guarantee player participation." if pool["end"]==str(last) else "Historical replay: injury status unavailable as of report date.")
    clubs=set().union(*teams_by_day.values())
    stats=team_stats(last,clubs) if clubs else {}
    out+="<h3>Upcoming NHL Games</h3>"+para("PTS = season standings points; LG = league rank (1–32); L5 = last five games (W-L-OTL).")
    first_matchup=True
    for d in upcoming:
        out+=f"<h4>{d:%A}</h4>"
        if not slate[d]: out+=para("No NHL games scheduled.")
        for g in slate[d]:
            rows=[]
            for side in ("awayTeam","homeTeam"):
                team=g[side]["abbrev"]
                text=logo(team)+h(team)+(" @" if side=="awayTeam" else "")
                rows.append((Markup(text),*stats.get(team,("N/A",)*3)))
            out+=table(["Matchup","PTS","LG","L5"],rows,compact_header=not first_matchup)
            first_matchup=False
    out+=f"<p><a href='{DASHBOARD}'>View the full Batspool2027 dashboard.</a></p>"
    return SUBJECTS[day.weekday()]+" | Batspool2027", "<html><body style='font-family:Arial,sans-serif;color:#000;max-width:660px;margin:auto'>"+out+"</body></html>"

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--checkpoint",required=True,type=Path)
    parser.add_argument("--report-date",type=dt.date.fromisoformat)
    parser.add_argument("--send",action="store_true")
    parser.add_argument("--test",action="store_true")
    parser.add_argument("--preview",type=Path)
    args=parser.parse_args()
    now=dt.datetime.now(TZ)
    pool=json.loads(args.checkpoint.read_text())
    day=args.report_date or now.date()
    if args.test and not args.report_date:
        day=dt.date.fromisoformat(pool["end"])+DAY
        while day.weekday() not in SUBJECTS: day-=DAY
    pool=relabel(pool,live_names(pool))
    subject,body=build_report(pool,day)
    if args.test: subject="[TEST] "+subject
    if args.preview: args.preview.write_text(body,encoding="utf-8")
    if not args.send: return
    if len(RECIPIENTS)!=2 or len(set(RECIPIENTS))!=2 or SENDER not in RECIPIENTS: raise ValueError("Invalid recipient configuration")
    password=os.environ["GMAIL_APP_PASSWORD"]
    if not args.test and not args.report_date and not (10 <= dt.datetime.now(TZ).hour < 11): raise ValueError("Outside 10–11 a.m. Toronto delivery window")
    mid=f"<batspool-{day}{'-test' if args.test else ''}@gmail.com>"
    with imaplib.IMAP4_SSL("imap.gmail.com",timeout=30) as imap:
        imap.login(SENDER,password)
        status,_=imap.select('"[Gmail]/Sent Mail"',readonly=True)
        if status!="OK": raise RuntimeError("Cannot verify prior delivery")
        status,data=imap.search(None,"HEADER","Message-ID",mid)
        if status!="OK": raise RuntimeError("Cannot verify prior delivery")
        if data[0] and not args.test:
            print("Report already sent; skipping")
            return
    msg=EmailMessage()
    msg["From"]=SENDER; msg["To"]=", ".join(RECIPIENTS); msg["Subject"]=subject; msg["Message-ID"]=mid
    msg.set_content("View the HTML report in your email client: "+DASHBOARD)
    msg.add_alternative(body,subtype="html")
    with smtplib.SMTP_SSL("smtp.gmail.com",465,context=ssl.create_default_context(),timeout=30) as smtp:
        smtp.login(SENDER,password)
        refused=smtp.send_message(msg,to_addrs=RECIPIENTS)
        if refused: raise RuntimeError("One or more recipients rejected")
    print("Email accepted by Gmail SMTP")

if __name__=="__main__":
    main()
