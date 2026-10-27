import time
import threading
from unittest.mock import patch, MagicMock

from django.test import TestCase, Client, override_settings


class SmokeTests(TestCase):
    def setUp(self):
        self.client = Client()

    @override_settings(SECURE_SSL_REDIRECT=False)
    def test_index_page_renders(self):
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"FPL Dashboard", resp.content)

    @override_settings(SECURE_SSL_REDIRECT=False)
    def test_index_page_has_polygon_tab(self):
        resp = self.client.get("/")
        self.assertIn(b"drawPolygonBtn", resp.content)
        self.assertIn(b"radarChart", resp.content)

    @override_settings(SECURE_SSL_REDIRECT=False)
    def test_index_page_has_smart_picks(self):
        resp = self.client.get("/")
        self.assertIn(b"Smart Picks", resp.content)
        self.assertIn(b"smart-picks-list", resp.content)


class CacheTests(TestCase):
    """Unit-test the in-process TTL cache without hitting the network."""

    def setUp(self):
        # Reset cache store between tests
        from fpldash import cache as c
        with c._lock:
            c._store.clear()

    def _make_mock_response(self, payload):
        mock = MagicMock()
        mock.json.return_value = payload
        mock.raise_for_status.return_value = None
        return mock

    def test_cache_returns_same_object_on_second_call(self):
        from fpldash import cache as c
        payload = {"elements": [], "teams": [], "events": []}
        with patch("fpldash.cache.requests.get", return_value=self._make_mock_response(payload)) as mock_get:
            first = c.get_bootstrap()
            second = c.get_bootstrap()
        # Network should only be called once
        self.assertEqual(mock_get.call_count, 1)
        self.assertIs(first, second)

    def test_cache_refetches_after_ttl(self):
        from fpldash import cache as c
        payload = {"elements": [], "teams": [], "events": []}
        with patch("fpldash.cache.requests.get", return_value=self._make_mock_response(payload)) as mock_get:
            c.get_bootstrap()
            # Artificially expire the cache entry
            with c._lock:
                c._store["bootstrap"]["ts"] = time.time() - c._TTL - 1
            c.get_bootstrap()
        self.assertEqual(mock_get.call_count, 2)

    def test_compute_team_fdr_empty_fixtures(self):
        from fpldash.cache import compute_team_fdr
        bootstrap = {"events": [{"id": 30, "is_current": True}], "teams": []}
        with patch("fpldash.cache.get_fixtures", return_value=[]):
            result = compute_team_fdr(bootstrap)
        self.assertEqual(result, {})

    def test_compute_team_fdr_calculates_averages(self):
        from fpldash.cache import compute_team_fdr
        # Current GW = 30, so we want fixtures for GW 31-33
        bootstrap = {"events": [{"id": 30, "is_current": True}], "teams": []}
        fixtures = [
            {"event": 31, "team_h": 1, "team_a": 2, "team_h_difficulty": 2, "team_a_difficulty": 4},
            {"event": 32, "team_h": 3, "team_a": 1, "team_h_difficulty": 3, "team_a_difficulty": 5},
        ]
        with patch("fpldash.cache.get_fixtures", return_value=fixtures):
            result = compute_team_fdr(bootstrap)
        # Team 1: home GW31 FDR=2, away GW32 FDR=5 → avg 3.5
        self.assertAlmostEqual(result[1], 3.5)
        # Team 2: away GW31 FDR=4
        self.assertAlmostEqual(result[2], 4.0)
        # Team 3: home GW32 FDR=3
        self.assertAlmostEqual(result[3], 3.0)


def _synthetic_bootstrap():
    """
    6 teams, 1 GK + 2 DEF + 2 MID + 1 FWD per team (36 players total) —
    enough spread to satisfy the 2-5-5-3 squad quotas and the 3-per-club
    cap with room to spare. ep_next varies by player so the ILP has a
    real optimisation to do; ep_next/ep_this are set (not points_per_game)
    so this doubles as a test of the cold-start (preseason) fallback path.
    """
    elements = []
    pid = 1

    def add(pos, team, price, ep):
        nonlocal pid
        elements.append({
            "id": pid,
            "first_name": "Test",
            "second_name": f"Player{pid}",
            "web_name": f"P{pid}",
            "team": team,
            "element_type": pos,
            "now_cost": int(round(price * 10)),
            "selected_by_percent": str(5 + (pid % 40)),
            "chance_of_playing_next_round": None,
            "ep_next": str(ep),
            "ep_this": str(ep),
            "points_per_game": "0.0",
            "photo": f"{pid}.jpg",
        })
        pid += 1

    teams = list(range(1, 7))
    for t in teams:
        add(1, t, 4.0 + (t % 3) * 0.5, 3.0 + (t % 3))       # 1 GK per team
    for t in teams:
        for k in range(2):
            add(2, t, 4.0 + k * 1.5, 2.5 + k * 1.5)         # 2 DEF per team
    for t in teams:
        for k in range(2):
            add(3, t, 4.5 + k * 2.0, 3.0 + k * 2.0)         # 2 MID per team
    for t in teams:
        add(4, t, 4.5 + (t % 3) * 2.5, 2.0 + (t % 3) * 2.5)  # 1 FWD per team

    return {
        "elements": elements,
        "teams": [{"id": t, "name": f"Team{t}"} for t in teams],
        "events": [{"id": 1, "is_current": True}],
    }


class SquadBuilderTests(TestCase):
    """Unit-test the squad-building ILP without hitting the network or the ML models."""

    def setUp(self):
        from fpldash import squad_builder as sb
        with sb._lock:
            sb._cache.clear()

    def _build(self, budget=100.0):
        from fpldash import squad_builder as sb
        bootstrap = _synthetic_bootstrap()
        with patch("fpldash.squad_builder.get_bootstrap", return_value=bootstrap), \
             patch("fpldash.squad_builder.compute_team_fdr", return_value={t: 3.0 for t in range(1, 7)}), \
             patch("fpldash.squad_builder.get_ml_predicted_scores", return_value={}):
            return sb.build_squads(budget=budget)

    def test_squads_are_budget_and_position_legal(self):
        result = self._build(budget=100.0)
        self.assertGreater(len(result["squads"]), 0)

        for squad in result["squads"]:
            starters = [p for grp in squad["starters"].values() for p in grp]
            all_players = starters + squad["bench"]
            self.assertEqual(len(all_players), 15)
            self.assertEqual(len(starters), 11)
            self.assertEqual(len(squad["bench"]), 4)
            self.assertLessEqual(squad["total_cost"], 100.0)
            self.assertAlmostEqual(squad["budget_left"], round(100.0 - squad["total_cost"], 1))

            pos_counts = {"GK": 0, "DEF": 0, "MID": 0, "FWD": 0}
            team_counts = {}
            for p in all_players:
                pos_counts[p["position"]] += 1
                team_counts[p["team_id"]] = team_counts.get(p["team_id"], 0) + 1
                # Cold-start fallback: predicted score must come from ep_next, never 0
                self.assertGreater(p["predicted"], 0)

            self.assertEqual(pos_counts, {"GK": 2, "DEF": 5, "MID": 5, "FWD": 3})
            self.assertTrue(all(c <= 3 for c in team_counts.values()))

            starter_ids = {p["id"] for p in starters}
            self.assertIn(squad["captain"]["id"], starter_ids)
            self.assertIn(squad["vice_captain"]["id"], starter_ids)
            self.assertNotEqual(squad["captain"]["id"], squad["vice_captain"]["id"])

    def test_build_squads_result_is_cached(self):
        from fpldash import squad_builder as sb
        bootstrap = _synthetic_bootstrap()
        with patch("fpldash.squad_builder.get_bootstrap", return_value=bootstrap) as mock_bs, \
             patch("fpldash.squad_builder.compute_team_fdr", return_value={t: 3.0 for t in range(1, 7)}), \
             patch("fpldash.squad_builder.get_ml_predicted_scores", return_value={}):
            sb.build_squads(budget=100.0)
            sb.build_squads(budget=100.0)
        self.assertEqual(mock_bs.call_count, 1)


class SquadBuilderViewTests(TestCase):
    def setUp(self):
        self.client = Client()

    @override_settings(SECURE_SSL_REDIRECT=False)
    def test_api_squad_builder_clamps_budget_and_returns_json(self):
        canned = {"budget": 110.0, "squads": []}
        with patch("fpldash.views.build_squads", return_value=canned) as mock_build:
            resp = self.client.get("/api/squad-builder", {"budget": "9999"})
        self.assertEqual(resp.status_code, 200)
        mock_build.assert_called_once_with(budget=110.0)
        self.assertEqual(resp.json(), canned)

    @override_settings(SECURE_SSL_REDIRECT=False)
    def test_api_squad_builder_defaults_budget_on_bad_input(self):
        canned = {"budget": 100.0, "squads": []}
        with patch("fpldash.views.build_squads", return_value=canned) as mock_build:
            resp = self.client.get("/api/squad-builder", {"budget": "not-a-number"})
        self.assertEqual(resp.status_code, 200)
        mock_build.assert_called_once_with(budget=100.0)


def _league_bootstrap():
    return {
        "teams": [
            {"id": 1, "name": "Arsenal", "short_name": "ARS", "code": 3},
            {"id": 2, "name": "Chelsea", "short_name": "CHE", "code": 8},
            {"id": 3, "name": "Liverpool", "short_name": "LIV", "code": 14},
        ],
        "events": [
            {"id": 1, "name": "Gameweek 1", "is_current": False, "finished": True},
            {"id": 2, "name": "Gameweek 2", "is_current": True, "deadline_time": "2026-08-22T10:00:00Z"},
            {"id": 3, "name": "Gameweek 3", "is_next": True},
        ],
    }


def _league_fixtures():
    return [
        # GW1: ARS 2-0 CHE, LIV 1-1 ARS
        {"id": 1, "event": 1, "team_h": 1, "team_a": 2, "team_h_score": 2, "team_a_score": 0,
         "finished": True, "started": True, "kickoff_time": "2026-08-15T14:00:00Z"},
        {"id": 2, "event": 1, "team_h": 3, "team_a": 1, "team_h_score": 1, "team_a_score": 1,
         "finished": True, "started": True, "kickoff_time": "2026-08-16T14:00:00Z"},
        # GW2: CHE 3-0 LIV live (not counted), LIV v CHE upcoming
        {"id": 3, "event": 2, "team_h": 2, "team_a": 3, "team_h_score": 3, "team_a_score": 0,
         "finished": False, "started": True, "minutes": 60, "kickoff_time": "2026-08-23T16:30:00Z"},
        {"id": 4, "event": 2, "team_h": 3, "team_a": 2, "team_h_score": None, "team_a_score": None,
         "finished": False, "started": False, "kickoff_time": "2026-08-23T11:30:00Z"},
    ]


class LeagueTests(TestCase):
    def test_table_counts_only_finished_matches_and_sorts(self):
        from fpldash.league import compute_league_table
        table = compute_league_table(_league_bootstrap(), _league_fixtures())
        self.assertEqual([r["short_name"] for r in table], ["ARS", "LIV", "CHE"])
        ars = table[0]
        self.assertEqual((ars["played"], ars["won"], ars["drawn"], ars["lost"]), (2, 1, 1, 0))
        self.assertEqual((ars["gf"], ars["ga"], ars["gd"], ars["points"]), (3, 1, 2, 4))
        self.assertEqual(ars["form"], ["W", "D"])
        self.assertEqual(table[2]["points"], 0)
        self.assertEqual([r["position"] for r in table], [1, 2, 3])

    def test_fixtures_default_to_current_gw_in_kickoff_order(self):
        from fpldash.league import get_gameweek_fixtures
        data = get_gameweek_fixtures(_league_bootstrap(), _league_fixtures())
        self.assertEqual(data["gameweek"], 2)
        self.assertEqual([m["id"] for m in data["fixtures"]], [4, 3])
        self.assertEqual([m["status"] for m in data["fixtures"]], ["upcoming", "live"])
        self.assertEqual(data["fixtures"][1]["home"]["short_name"], "CHE")

    def test_fixtures_default_moves_to_next_gw_once_current_matches_are_played(self):
        from fpldash.league import default_gameweek
        fixtures = _league_fixtures()
        for f in fixtures:
            if f["event"] == 2:
                f["finished_provisional"] = True
        self.assertEqual(default_gameweek(_league_bootstrap(), fixtures), 3)

    def test_fixtures_gw_is_clamped(self):
        from fpldash.league import get_gameweek_fixtures
        data = get_gameweek_fixtures(_league_bootstrap(), _league_fixtures(), gw=99)
        self.assertEqual(data["gameweek"], 3)
        self.assertEqual(data["fixtures"], [])


class LeagueViewTests(TestCase):
    def setUp(self):
        self.client = Client()

    @override_settings(SECURE_SSL_REDIRECT=False)
    def test_api_table_and_fixtures(self):
        with patch("fpldash.views.get_bootstrap", return_value=_league_bootstrap()), \
             patch("fpldash.views.get_fixtures", return_value=_league_fixtures()):
            table = self.client.get("/api/table").json()
            fixtures = self.client.get("/api/fixtures", {"gw": "1"}).json()
            bad_gw = self.client.get("/api/fixtures", {"gw": "abc"}).json()
        self.assertEqual(table[0]["name"], "Arsenal")
        self.assertEqual(fixtures["gameweek"], 1)
        self.assertEqual(len(fixtures["fixtures"]), 2)
        self.assertEqual(bad_gw["gameweek"], 2)

    @override_settings(SECURE_SSL_REDIRECT=False)
    def test_index_page_has_table_and_fixtures_tabs(self):
        resp = self.client.get("/")
        self.assertIn(b'data-tab="table"', resp.content)
        self.assertIn(b'id="fixtures-wrap"', resp.content)


def _mini_league_fixture():
    managers = [
        {"entry": 1, "team": "Alpha", "player": "A", "rank": 1, "last_rank": 2, "total": 0, "event_total": 0},
        {"entry": 2, "team": "Bravo", "player": "B", "rank": 2, "last_rank": 1, "total": 0, "event_total": 0},
        {"entry": 3, "team": "Late", "player": "C", "rank": 3, "last_rank": 3, "total": 0, "event_total": 0},
    ]

    def gw(event, points, total, hit=0, bench=0):
        return {"event": event, "points": points, "total_points": total, "overall_rank": 1000,
                "value": 1000, "bank": 5, "event_transfers": 1 if hit else 0,
                "event_transfers_cost": hit, "points_on_bench": bench}

    histories = {
        1: {"current": [gw(1, 40, 40), gw(2, 90, 126, hit=4, bench=12)], "chips": [{"name": "3xc", "event": 2}]},
        2: {"current": [gw(1, 60, 60), gw(2, 50, 110)], "chips": []},
        3: {"current": [gw(2, 30, 30)], "chips": []},  # joined in GW2
    }
    captains = {(1, 2): {"name": "Haaland", "points": 36}, (2, 2): {"name": "Salah", "points": 8}}
    return managers, histories, captains, [1, 2]


class MiniLeagueTests(TestCase):
    def test_weeks_net_points_and_league_rank(self):
        from fpldash.mini_league import build_weeks
        managers, histories, captains, gws = _mini_league_fixture()
        weeks = build_weeks(managers, histories, captains, gws)
        self.assertEqual(weeks[1][1]["points"], 86)  # 90 minus the -4 hit
        self.assertEqual([weeks[e][0]["league_rank"] for e in (1, 2, 3)], [2, 1, 3])
        self.assertEqual([weeks[e][1]["league_rank"] for e in (1, 2, 3)], [1, 2, 3])
        self.assertIsNone(weeks[3][0]["points"])  # not yet joined
        self.assertEqual(weeks[1][1]["chip"], "3xc")

    def test_weekly_awards(self):
        from fpldash.mini_league import build_weeks, build_awards
        managers, histories, captains, gws = _mini_league_fixture()
        weeks = build_weeks(managers, histories, captains, gws)
        awards = {a["key"]: a for a in build_awards(managers, weeks, gws, 2)}
        self.assertEqual(awards["motw"]["entry"], 1)
        self.assertEqual(awards["spoon"]["entry"], 3)
        self.assertEqual((awards["climber"]["entry"], awards["climber"]["value"]), (1, 1))
        self.assertEqual(awards["faller"]["entry"], 2)
        self.assertEqual(awards["captain"]["value"], 36)
        self.assertEqual(awards["bench"]["value"], 12)
        self.assertEqual(awards["hit"]["value"], -4)
        self.assertIn("chip", awards)
        self.assertEqual(build_awards(managers, weeks, gws, 99), [])

    def test_season_records(self):
        from fpldash.mini_league import build_weeks, build_records
        managers, histories, captains, gws = _mini_league_fixture()
        weeks = build_weeks(managers, histories, captains, gws)
        records = {r["key"]: r for r in build_records(managers, weeks, gws)}
        self.assertEqual((records["high"]["entry"], records["high"]["value"]), (1, 86))
        self.assertEqual(records["wins"]["value"], 1)
        self.assertEqual(records["capsum"]["entry"], 1)
        self.assertEqual(records["hitsum"]["value"], -4)

    @override_settings(SECURE_SSL_REDIRECT=False)
    def test_api_mini_league_rejects_bad_id(self):
        resp = Client().get("/api/mini-league", {"id": "abc"})
        self.assertEqual(resp.status_code, 400)


class MiniLeagueChipsTests(TestCase):
    def test_chip_grid_statuses(self):
        from fpldash.mini_league import build_weeks, build_chips
        managers, histories, captains, gws = _mini_league_fixture()
        weeks = build_weeks(managers, histories, captains, gws)
        chip_defs = [
            {"name": "3xc", "start_event": 1, "stop_event": 19},
            {"name": "wildcard", "start_event": 20, "stop_event": 38},
            {"name": "wildcard", "start_event": 2, "stop_event": 19},
        ]
        grid = build_chips(managers, histories, weeks, gws, chip_defs, current_gw=2)
        # Ordered wildcard (both halves, by start) then triple captain
        self.assertEqual([(s["name"], s["start"]) for s in grid["slots"]],
                         [("wildcard", 2), ("wildcard", 20), ("3xc", 1)])
        alpha = grid["rows"][0]
        self.assertEqual([c["status"] for c in alpha["cells"]], ["available", "upcoming", "used"])
        self.assertEqual((alpha["cells"][2]["gw"], alpha["cells"][2]["points"]), (2, 86))
        self.assertEqual(alpha["used"], 1)

        later = build_chips(managers, histories, weeks, gws, chip_defs, current_gw=25)
        self.assertEqual(later["rows"][1]["cells"][0]["status"], "expired")


class MiniLeagueInsightsTests(TestCase):
    def test_insights(self):
        from fpldash.mini_league import build_weeks, build_insights
        managers, histories, captains, gws = _mini_league_fixture()
        weeks = build_weeks(managers, histories, captains, gws)
        ins = build_insights(managers, weeks, gws)

        self.assertEqual(ins["form"][0]["entry"], 1)
        self.assertEqual(ins["form"][0]["last"], 126)
        self.assertEqual(ins["form"][0]["series"], [40, 86])

        share = {r["entry"]: r for r in ins["captain_share"]}
        self.assertEqual(share[1]["captain_points"], 36)
        self.assertEqual(share[1]["pct"], round(100 * 36 / 126, 1))

        ranges = {r["entry"]: r for r in ins["ranges"]}
        self.assertEqual((ranges[1]["min"], ranges[1]["max"], ranges[1]["avg"]), (40, 86, 63.0))


class GlobalCaptaincyTests(TestCase):
    def test_sample_pages_span_all_ranks(self):
        from fpldash.mini_league import sample_pages
        pages = sample_pages(1_000_000, n=5)
        self.assertEqual(pages[0], 1)
        self.assertEqual(pages[-1], 20_000)  # last page of 1M ranks at 50 per page
        self.assertEqual(len(pages), 5)
        self.assertEqual(sample_pages(30), [1])

    def test_summarize_captains(self):
        from fpldash.mini_league import summarize_captains
        players = {10: {"name": "Haaland", "team": "MCI"}, 20: {"name": "Salah", "team": "LIV"}}

        def picks(cap, chip=None):
            return {"active_chip": chip, "picks": [{"element": 99, "is_captain": False},
                                                   {"element": cap, "is_captain": True}]}

        rows, total, triple = summarize_captains(
            [picks(10), picks(10, "3xc"), picks(20), None, {"picks": []}], players)
        self.assertEqual(total, 3)  # inactive/missing teams are not counted
        self.assertEqual(triple, 1)
        self.assertEqual([(r["name"], r["count"], r["pct"]) for r in rows],
                         [("Haaland", 2, 66.7), ("Salah", 1, 33.3)])

    @override_settings(SECURE_SSL_REDIRECT=False)
    def test_api_captaincy_passes_through(self):
        canned = {"status": "pending", "gameweek": 8, "sample_target": 400}
        with patch("fpldash.views.get_global_captaincy", return_value=canned):
            resp = Client().get("/api/captaincy")
        self.assertEqual(resp.json(), canned)
