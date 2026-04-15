"""
Simple thread-safe, in-process TTL cache for FPL API responses.

Bootstrap-static and fixtures data rarely change mid-day, so we cache
them for 30 minutes to avoid hammering the FPL API on every request.
"""

import threading
import time
import requests

_lock = threading.Lock()
_store: dict = {}
_TTL = 1800  # 30 minutes


def _fetch(url: str, timeout: int = 20):
    r = requests.get(url, timeout=timeout)
    r.raise_for_status()
    return r.json()


def _get_cached(key: str, url: str):
    now = time.time()
    with _lock:
        entry = _store.get(key)
        if entry and now - entry["ts"] < _TTL:
            return entry["data"]
    # Fetch outside the lock so other threads are not blocked during network IO.
    data = _fetch(url)
    with _lock:
        _store[key] = {"data": data, "ts": time.time()}
    return data


def get_bootstrap():
    """Return cached FPL bootstrap-static JSON."""
    return _get_cached(
        "bootstrap",
        "https://fantasy.premierleague.com/api/bootstrap-static/",
    )


def get_fixtures():
    """Return cached FPL fixtures JSON."""
    return _get_cached(
        "fixtures",
        "https://fantasy.premierleague.com/api/fixtures/",
    )


def compute_team_fdr(bootstrap_data: dict, n_gws: int = 3) -> dict:
    """
    Return {team_id: avg_fdr} for the next n_gws gameweeks.

    Reads live fixture data from the cache.  Teams with no upcoming
    fixtures in the window get a neutral score of 3.0.
    """
    events = bootstrap_data.get("events", [])
    current_gw = next((e["id"] for e in events if e.get("is_current")), 0)
    next_gw = current_gw + 1
    max_gw = current_gw + n_gws

    try:
        fixtures = get_fixtures()
    except Exception:
        return {}

    team_fdrs: dict[int, list] = {}
    for f in fixtures:
        gw = f.get("event")
        if gw is None or gw < next_gw or gw > max_gw:
            continue
        h, a = f["team_h"], f["team_a"]
        team_fdrs.setdefault(h, []).append(f.get("team_h_difficulty", 3))
        team_fdrs.setdefault(a, []).append(f.get("team_a_difficulty", 3))

    return {
        tid: round(sum(fdrs) / len(fdrs), 1)
        for tid, fdrs in team_fdrs.items()
        if fdrs
    }
