"""Regression tests for shared Wednesday/Friday/Sunday email formatting."""
import os
import unittest
from unittest.mock import patch

import weekly_report as wr


def player(points, goals=0, name="Example", season=0):
    return dict(points=points, goals=goals, name=name, season=season,
                team="TOR", round=1, owner="Contestant")


def group(*tiers):
    return [player(value, name=f"Player {i}") for i, value
            in enumerate((pts for score, count in tiers for pts in [score] * count))]


class PickRules(unittest.TestCase):
    def check(self, tiers, expected):
        data = group(*tiers)
        selected = wr.top_picks(data)
        self.assertEqual([p["points"] for p in selected], expected)
        self.assertLessEqual(len(selected), 5)

    def test_no_points(self):
        self.check([], [])
        self.check([(0, 11)], [])

    def test_top_tier_three_plus_only(self):
        self.check([(8, 3), (7, 2), (6, 1)], [8, 8, 8])
        self.check([(8, 7), (7, 1)], [8] * 5)

    def test_top_plus_second_tier(self):
        self.check([(8, 1), (6, 3), (5, 1)], [8, 6, 6, 6])
        self.check([(8, 1), (6, 7)], [8, 6, 6, 6, 6])

    def test_third_tier_and_limit(self):
        self.check([(9, 1), (8, 1), (7, 3)], [9, 8, 7, 7, 7])
        self.check([(9, 1), (8, 1), (7, 4)], [9, 8])
        self.check([(9, 2), (8, 1), (7, 2)], [9, 9, 8, 7, 7])
        self.check([(9, 2), (8, 1), (7, 3)], [9, 9, 8])
        self.check([(9, 2), (8, 2), (7, 1)], [9, 9, 8, 8, 7])

    def test_half_of_top_cutoff(self):
        self.check([(10, 1), (5, 2), (4, 3)], [10, 5, 5])
        self.check([(9, 1), (4, 8)], [9])
        self.check([(1, 2)], [1, 1])

    def test_tie_breaker(self):
        data=[player(5, goals=1, name="X"),player(5, goals=3, name="Y"),
              player(5, goals=2, name="Z")]
        self.assertEqual([x["name"] for x in wr.top_picks(data)], ["Y","Z","X"])


class DisplayRules(unittest.TestCase):
    def test_ordinals(self):
        self.assertEqual(wr.ordinal(1), "1st")
        self.assertEqual(wr.ordinal(2), "2nd")
        self.assertEqual(wr.ordinal(3), "3rd")
        self.assertEqual(wr.ordinal(11), "11th")
        self.assertEqual(wr.ordinal(12), "12th")
        self.assertEqual(wr.ordinal(13), "13th")
        self.assertEqual(wr.ordinal(21), "21st")
        self.assertEqual(wr.ordinal(22), "22nd")
        self.assertEqual(wr.ordinal(23), "23rd")
        self.assertEqual(wr.ordinal(32), "32nd")
        self.assertEqual(wr.ordinal(None), "N/A")

    def test_blank_compact_header(self):
        txt=wr.table(["Matchup","PTS","LG","L5"],[[wr.Markup(wr.logo("MTL")+"MTL @"),42,"2nd","3-1-1"]],
                     compact_header=True)
        self.assertNotIn(">Matchup<",txt)
        self.assertNotIn(">PTS<",txt)
        self.assertIn("height:7px",txt)
        self.assertIn("MTL_light.svg",txt)
        self.assertIn("MTL @",txt)
        self.assertIn("color:#000",txt)

    def test_first_matchup_has_true_header(self):
        txt=wr.table(["Matchup","PTS","LG","L5"],[["TOR",30,"1st","4-0-1"]])
        self.assertIn(">Matchup<",txt)
        self.assertIn(">LG<",txt)
        self.assertNotIn("height:7px",txt)

    def test_bad_logo_token_ignored(self):
        self.assertEqual(wr.logo('<bad>'), "")
        self.assertIn('alt="TOR logo"',wr.logo("TOR"))

    def test_no_scorers_omits_top_picks(self):
        from datetime import date
        empty={"first":date(2026,11,25),"last":date(2026,11,26),
               "players":[player(0)],"delta":{"A":0},"moves":{"A":0},
               "rb":{"A":1},"ra":{"A":1},"pd":[]}
        txt=wr.render_period(empty)
        self.assertNotIn("Top 0 Picks",txt)
        self.assertNotIn("Top 3 Picks",txt)
        self.assertNotIn("Top 1 Picks",txt)

    def test_picks_count_and_logo_in_period(self):
        from datetime import date
        data={"first":date(2026,11,25),"last":date(2026,11,26),
              "players":[player(6,name="A"),player(4,name="B"),
                         player(3,name="C"),player(0,name="D")],
              "delta":{"A":13},"moves":{"A":0},
              "rb":{"A":1},"ra":{"A":1},"pd":[]}
        txt=wr.render_period(data)
        self.assertIn("Top 3 Picks",txt)
        self.assertIn("TOR_light.svg",txt)
        self.assertNotIn("D (TOR)",txt)

    def test_sunday_player_highlight_has_logo(self):
        data=[(5,"Sample Player","Contestant","2026-11-27")]
        txt=wr.peak_players_html(data,{"Sample Player":"MTL"})
        self.assertIn("MTL_light.svg",txt)
        self.assertIn("Sample Player",txt)

    def test_home_away_scoreboard_is_centered_and_headerless(self):
        games=[{"awayTeam":{"abbrev":"TOR","score":2},
                "homeTeam":{"abbrev":"MTL","score":4},
                "gameOutcome":{"lastPeriodType":"REG"}},
               {"awayTeam":{"abbrev":"BOS","score":3},
                "homeTeam":{"abbrev":"NYR","score":2},
                "gameOutcome":{"lastPeriodType":"OT"}},
               {"awayTeam":{"abbrev":"LAK","score":1},
                "homeTeam":{"abbrev":"SJS","score":2},
                "gameOutcome":{"lastPeriodType":"SO"}}]
        output=wr.render_results(games)
        self.assertNotIn("<th", output)
        self.assertNotIn("Winner", output)
        self.assertNotIn("Loser", output)
        self.assertNotIn("REG", output)
        self.assertIn(">OT</span>", output)
        self.assertIn(">SO</span>", output)
        self.assertIn("MTL_light.svg", output)
        self.assertIn('width="26" height="26"', output)
        self.assertIn("text-align:right", output)
        self.assertIn("text-align:left", output)
        self.assertIn("text-align:center", output)
        self.assertIn('width:27px;text-align:right', output)
        self.assertIn('width:16px;text-align:center', output)
        self.assertIn('width:27px;text-align:left', output)
        self.assertIn("<strong>4</strong>", output)
        self.assertIn("<strong>3</strong>", output)
        self.assertIn("<strong>2</strong>", output)
        self.assertNotIn(" (Home)", output)
        self.assertNotIn(" (Away)", output)
        # Home appears before Away, independently of who won.
        self.assertLess(output.index("MTL_light.svg"),output.index("TOR_light.svg"))
        self.assertLess(output.index("NYR_light.svg"),output.index("BOS_light.svg"))

    def test_tied_zero_score_does_not_invent_winner(self):
        game={"homeTeam":{"abbrev":"NYR","score":0},
              "awayTeam":{"abbrev":"BOS","score":0},
              "gameOutcome":{"lastPeriodType":"SO"}}
        output=wr.render_results([game])
        self.assertNotIn("<strong>0</strong>",output)
        self.assertIn(">SO</span>",output)

    def test_empty_scoreboard(self):
        self.assertIn("No NHL games played.",wr.render_results([]))

    def test_missing_result_scores_rejected(self):
        game={"awayTeam":{"abbrev":"TOR"},
              "homeTeam":{"abbrev":"MTL","score":2}}
        with self.assertRaises(ValueError):
            wr.render_results([game])

    def test_schedule_blue_bands_and_asterisks(self):
        rows = [
            ["A", (1,1), (4,2), (5,3)],
            ["B", (7,0), (10,1), (17,1)],
            ["C", (5,0), (8,0), (13,0)],
        ]
        markup = wr.scheduled_games_table(["Contestant","Fri","Sat","Total"],rows)
        for color in ("#dfedf9","#a6c8eb","#5e97cb"):
            self.assertIn(f"background:{color}",markup)
        self.assertNotIn("#f1a1a6",markup)
        self.assertIn(">1*</td>",markup)
        self.assertIn(">4**</td>",markup)
        self.assertIn(">5***</td>",markup)
        self.assertIn(">17*</td>",markup)
        self.assertNotIn("(3)",markup)
        self.assertIn('width:34.00%',markup)
        self.assertIn('width:24.00%',markup)
        self.assertIn('width:18.00%',markup)
        self.assertIn('background:#ffffff',markup)
        self.assertIn('background:#afb5be',markup)
        # Six daily cells, one shared range. No daily white cells.
        self.assertNotIn('background:#ffffff;color:#000;text-align:center">1',markup)

    def test_total_scale_stays_separate(self):
        rows=[["A",(1,0),(2,0),(3,0)],["B",(9,0),(10,0),(19,0)]]
        result=wr.scheduled_games_table(["Contestant","Fri","Sat","Total"],rows)
        self.assertIn('background:#ffffff',result)
        self.assertIn('background:#afb5be',result)
        self.assertIn('background:#dfedf9',result)
        self.assertIn('background:#5e97cb',result)

    def test_uniform_and_empty_schedule(self):
        self.assertIn("</table>",wr.scheduled_games_table(["Contestant","Fri","Total"],[]))
        markup=wr.scheduled_games_table(["Contestant","Fri","Total"],[["A",(0,0),(0,0)]])
        self.assertIn('background:#a6c8eb',markup)
        self.assertNotIn("#f1a1a6",markup)
        self.assertIn('background:#ffffff',markup)

    def test_upcoming_header_widths_and_labels(self):
        out=wr.table(["Matchup","Pts","Rank","Last 5"],
                     [[wr.Markup(wr.logo("MTL")+"MTL @"),42,"3rd","3-1-1"]])
        self.assertIn("width:43%",out)
        self.assertIn("width:12%",out)
        self.assertIn("width:17%",out)
        self.assertIn("width:28%",out)
        self.assertIn(">Pts<",out)
        self.assertIn(">Rank<",out)
        self.assertIn(">Last 5<",out)


    def test_heading_centering(self):
        self.assertIn('text-align:center',wr.section("Around the NHL"))
        self.assertIn('text-align:center',wr.field("Top Contestant", "A"))
        self.assertIn("font-weight:700",wr.subheading("Top Contestant"))
        self.assertIn("border-bottom:2px",wr.section("Around the NHL"))

    def test_four_discrete_day_shades(self):
        rows = [
            ["A", (1, 0), (2, 0), (3, 0)],
            ["B", (3, 0), (4, 0), (7, 0)],
            ["C", (5, 0), (5, 0), (10, 0)],
        ]
        result = wr.scheduled_games_table(["Contestant", "Fri", "Sat", "Total"], rows)
        colours = ("#f1a1a6", "#d3a8b0", "#b0aec8", "#89b3e4")
        # Five day cells reduce to four unique bins, never pure white.
        day_shades = [wr.scheduled_games_table(["Contestant", "Day", "Total"],
                      [["A", (x, 0), (1, 0)], ["B", (y, 0), (1, 0)]])
                      for x, y in ((1, 5), (2, 5), (3, 5), (4, 5))]
        self.assertTrue(all(c in result for c in colours))
        self.assertNotIn("background:#ffffff;text-align:center", result)

    def test_post_intro_centering_preserves_table_alignment(self):
        self.assertIn("text-align:center", wr.subheading("Scheduled Player Games"))
        self.assertIn("text-align:left", wr.table(["Name"], [["Example"]]))
        self.assertIn('text-align:center', wr.section("Looking Ahead"))

    def test_report_versions_are_shared(self):
        self.assertEqual(wr.SUBJECTS[2], "The SMT Report")
        self.assertEqual(wr.SUBJECTS[4], "The Friday Check-Up")
        self.assertEqual(wr.SUBJECTS[6], "The Sunday Review")
        from datetime import date
        self.assertEqual(wr.windows(date(2026,11,27))[0],date(2026,11,25))


if __name__ == "__main__":
    unittest.main()
