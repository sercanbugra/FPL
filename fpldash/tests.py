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
        self.assertIn(b"FPL Top Players", resp.content)

    @override_settings(SECURE_SSL_REDIRECT=False)
    def test_index_page_has_polygon_panel(self):
        resp = self.client.get("/")
        self.assertIn(b"polygon-panel", resp.content)
        self.assertIn(b"drawPolygonBtn", resp.content)

    @override_settings(SECURE_SSL_REDIRECT=False)
    def test_index_page_has_smart_picks_tab(self):
        resp = self.client.get("/")
        self.assertIn(b"Smart Picks", resp.content)
        self.assertIn(b"tab-picks", resp.content)


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
