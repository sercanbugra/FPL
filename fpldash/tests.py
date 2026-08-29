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
