"""
Mini-league report: weekly stats, awards and season records for every
manager in an FPL classic league.

Data sources (all public FPL endpoints, no auth):
  leagues-classic/{id}/standings/   members + current rank
  entry/{id}/history/               per-GW points, rank, value, transfers, chips
  entry/{id}/event/{gw}/picks/      captain choice for each GW
  event/{gw}/live/                  every player's points for that GW

A 12-manager league over N gameweeks costs roughly 12 + 12*N + N requests,
so responses are cached: anything about a *finished* gameweek never changes
and is kept for the life of the process; everything else uses a 30-min TTL
like the rest of the app. The award/record builders are pure functions over
the assembled data so they can be tested without the network.
"""

import statistics
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import requests

from .cache import get_bootstrap

API = "https://fantasy.premierleague.com/api"
_TTL = 1800  # 30 minutes, aligns with the other caches
MAX_MANAGERS = 50  # bounds the fan-out of per-manager requests

_lock = threading.Lock()
_ttl_cache: dict = {}
_perm_cache: dict = {}


def _get_json(url: str, permanent: bool = False):
    with _lock:
        if url in _perm_cache:
            return _perm_cache[url]
        entry = _ttl_cache.get(url)
        if entry and time.time() - entry["ts"] < _TTL:
            return entry["data"]
    r = requests.get(url, timeout=20)
    r.raise_for_status()
    data = r.json()
    with _lock:
        if permanent:
            _perm_cache[url] = data
        else:
            _ttl_cache[url] = {"data": data, "ts": time.time()}
    return data


def _get_or_none(url: str, permanent: bool = False):
    try:
        return _get_json(url, permanent)
    except Exception:
        return None


def _fetch_standings(league_id: int) -> tuple[dict, list]:
    results, page = [], 1
    while True:
        data = _get_json(f"{API}/leagues-classic/{league_id}/standings/?page_standings={page}")
        results += data["standings"]["results"]
        if not data["standings"].get("has_next") or len(results) >= MAX_MANAGERS:
            return data["league"], results[:MAX_MANAGERS]
        page += 1


# ---------- Pure builders ----------

def build_weeks(managers: list, histories: dict, captains: dict, gameweeks: list) -> dict:
    """
    Return {entry: [week, ...]} with one week dict per gameweek, including
    the manager's league position that week (by cumulative net points).
    `captains` is {(entry, gw): {"name", "points"}}.
    """
    weeks: dict = {}
    for m in managers:
        eid = m["entry"]
        hist = histories.get(eid) or {}
        by_gw = {h["event"]: h for h in hist.get("current", [])}
        chips = {c["event"]: c["name"] for c in hist.get("chips", [])}
        rows, running = [], 0
        for gw in gameweeks:
            h = by_gw.get(gw)
            if h:
                hit = h.get("event_transfers_cost", 0) or 0
                net = (h.get("points") or 0) - hit
                running = h.get("total_points", running + net)
                rows.append({
                    "gw": gw,
                    "points": net,
                    "total": running,
                    "overall_rank": h.get("overall_rank"),
                    "value": (h.get("value") or 0) / 10.0,
                    "bank": (h.get("bank") or 0) / 10.0,
                    "transfers": h.get("event_transfers", 0) or 0,
                    "hit": hit,
                    "bench": h.get("points_on_bench", 0) or 0,
                    "chip": chips.get(gw),
                    "captain": captains.get((eid, gw)),
                })
            else:
                # Joined the game after this GW
                rows.append({"gw": gw, "points": None, "total": running, "overall_rank": None,
                             "value": None, "bank": None, "transfers": 0, "hit": 0,
                             "bench": 0, "chip": None, "captain": None})
        weeks[eid] = rows

    # League position per GW from cumulative totals
    for idx, _gw in enumerate(gameweeks):
        order = sorted(weeks, key=lambda e: -weeks[e][idx]["total"])
        for pos, eid in enumerate(order, start=1):
            weeks[eid][idx]["league_rank"] = pos
    return weeks


def _award(key, title, emoji, m, value, detail):
    return {"key": key, "title": title, "emoji": emoji, "entry": m["entry"],
            "team": m["team"], "player": m["player"], "value": value, "detail": detail}


def build_awards(managers: list, weeks: dict, gameweeks: list, gw: int) -> list:
    """Weekly awards for one gameweek, in presentation order."""
    if gw not in gameweeks:
        return []
    idx = gameweeks.index(gw)
    played = [(m, weeks[m["entry"]][idx]) for m in managers
              if weeks[m["entry"]][idx]["points"] is not None]
    if not played:
        return []

    awards = []
    top_m, top_w = max(played, key=lambda p: p[1]["points"])
    avg = sum(w["points"] for _, w in played) / len(played)
    awards.append(_award("motw", "Manager of the Week", "🏆", top_m, top_w["points"],
                         f"{top_w['points']} pts · league average {avg:.1f}"))

    low_m, low_w = min(played, key=lambda p: p[1]["points"])
    awards.append(_award("spoon", "Wooden Spoon", "🥄", low_m, low_w["points"],
                         f"{low_w['points']} pts, {top_w['points'] - low_w['points']} behind the top score"))

    if idx > 0:
        moves = [(m, weeks[m["entry"]][idx - 1]["league_rank"] - w["league_rank"]) for m, w in played]
        up_m, up = max(moves, key=lambda p: p[1])
        if up > 0:
            now = weeks[up_m["entry"]][idx]["league_rank"]
            awards.append(_award("climber", "Biggest Climber", "🚀", up_m, up,
                                 f"Up {up} place{'s' if up > 1 else ''} to #{now}"))
        down_m, down = min(moves, key=lambda p: p[1])
        if down < 0:
            now = weeks[down_m["entry"]][idx]["league_rank"]
            awards.append(_award("faller", "Free Fall", "📉", down_m, -down,
                                 f"Down {-down} place{'s' if down < -1 else ''} to #{now}"))

    capped = [(m, w) for m, w in played if w["captain"]]
    if capped:
        cap_m, cap_w = max(capped, key=lambda p: p[1]["captain"]["points"])
        awards.append(_award("captain", "Captain Fantastic", "🎯", cap_m, cap_w["captain"]["points"],
                             f"{cap_w['captain']['name']} returned {cap_w['captain']['points']} pts"))

    bench_m, bench_w = max(played, key=lambda p: p[1]["bench"])
    if bench_w["bench"] > 0:
        awards.append(_award("bench", "Bench Disaster", "🪑", bench_m, bench_w["bench"],
                             f"{bench_w['bench']} pts left on the bench"))

    hit_m, hit_w = max(played, key=lambda p: p[1]["hit"])
    if hit_w["hit"] > 0:
        awards.append(_award("hit", "Transfer Gambler", "🎲", hit_m, -hit_w["hit"],
                             f"Took a -{hit_w['hit']} hit for {hit_w['transfers']} transfers"))

    for m, w in played:
        if w["chip"]:
            awards.append(_award("chip", "Chip Played", "🃏", m, w["chip"],
                                 f"Activated {CHIP_NAMES.get(w['chip'], w['chip'])} → {w['points']} pts"))
    return awards


CHIP_NAMES = {"wildcard": "Wildcard", "freehit": "Free Hit", "bboost": "Bench Boost",
              "3xc": "Triple Captain", "manager": "Assistant Manager"}
CHIP_ORDER = ["wildcard", "freehit", "bboost", "3xc", "manager"]


def build_chips(managers: list, histories: dict, weeks: dict, gameweeks: list,
                chip_defs: list, current_gw: int) -> dict:
    """
    Chip usage grid: one slot per chip *window* from bootstrap `chips`
    (each chip comes once per half-season), and per manager a cell per slot:
    used (with GW and that GW's net points), available, upcoming or expired.
    """
    slots = sorted(
        ({"name": c["name"], "label": CHIP_NAMES.get(c["name"], c["name"]),
          "start": c.get("start_event", 1), "stop": c.get("stop_event", 38)}
         for c in chip_defs),
        key=lambda s: (CHIP_ORDER.index(s["name"]) if s["name"] in CHIP_ORDER else 99, s["start"]),
    )
    if not slots:
        # Older bootstrap without chip windows: one season-long slot per chip seen
        seen = {c["name"] for h in histories.values() if h for c in h.get("chips", [])}
        slots = [{"name": n, "label": CHIP_NAMES.get(n, n), "start": 1, "stop": 38}
                 for n in sorted(seen, key=lambda n: CHIP_ORDER.index(n) if n in CHIP_ORDER else 99)]

    rows = []
    for m in managers:
        eid = m["entry"]
        used = [(c["name"], c["event"]) for c in ((histories.get(eid) or {}).get("chips") or [])]
        cells = []
        for s in slots:
            gw = next((ev for name, ev in used if name == s["name"] and s["start"] <= ev <= s["stop"]), None)
            if gw is not None:
                pts = None
                if gw in gameweeks:
                    pts = weeks[eid][gameweeks.index(gw)]["points"]
                cells.append({"status": "used", "gw": gw, "points": pts})
            elif current_gw is not None and current_gw > s["stop"]:
                cells.append({"status": "expired"})
            elif current_gw is not None and current_gw < s["start"]:
                cells.append({"status": "upcoming"})
            else:
                cells.append({"status": "available"})
        rows.append({"entry": eid, "used": sum(c["status"] == "used" for c in cells), "cells": cells})
    return {"slots": slots, "rows": rows}


def build_records(managers: list, weeks: dict, gameweeks: list) -> list:
    """Season-long records across all gameweeks so far."""
    if not managers or not gameweeks:
        return []
    records = []

    best = None
    for m in managers:
        for w in weeks[m["entry"]]:
            if w["points"] is not None and (best is None or w["points"] > best[1]["points"]):
                best = (m, w)
    if best:
        records.append(_award("high", "Highest Gameweek", "🔥", best[0], best[1]["points"],
                              f"{best[1]['points']} pts in GW{best[1]['gw']}"))

    wins = {m["entry"]: 0 for m in managers}
    for idx in range(len(gameweeks)):
        scores = [(m["entry"], weeks[m["entry"]][idx]["points"]) for m in managers
                  if weeks[m["entry"]][idx]["points"] is not None]
        if scores:
            top = max(s for _, s in scores)
            for eid, s in scores:
                if s == top:
                    wins[eid] += 1
    by_entry = {m["entry"]: m for m in managers}
    w_eid = max(wins, key=wins.get)
    records.append(_award("wins", "Most Weekly Wins", "👑", by_entry[w_eid], wins[w_eid],
                          f"Top scorer in {wins[w_eid]} gameweek{'s' if wins[w_eid] != 1 else ''}"))

    def pts(m):
        return [w["points"] for w in weeks[m["entry"]] if w["points"] is not None]

    steady = [(m, statistics.pstdev(pts(m))) for m in managers if len(pts(m)) >= 2]
    if steady:
        s_m, sd = min(steady, key=lambda p: p[1])
        records.append(_award("steady", "Mr Consistent", "📏", s_m, round(sd, 1),
                              f"Weekly scores vary by only ±{sd:.1f} pts"))

    caps = [(m, sum(w["captain"]["points"] for w in weeks[m["entry"]] if w["captain"])) for m in managers]
    c_m, c_pts = max(caps, key=lambda p: p[1])
    if c_pts:
        records.append(_award("capsum", "Best Captaincy", "🎖️", c_m, c_pts,
                              f"{c_pts} pts from captains this season"))

    benches = [(m, sum(w["bench"] for w in weeks[m["entry"]])) for m in managers]
    b_m, b_pts = max(benches, key=lambda p: p[1])
    if b_pts:
        records.append(_award("benchsum", "Bench Warmer", "🛋️", b_m, b_pts,
                              f"{b_pts} pts wasted on the bench"))

    hits = [(m, sum(w["hit"] for w in weeks[m["entry"]])) for m in managers]
    h_m, h_pts = max(hits, key=lambda p: p[1])
    if h_pts:
        records.append(_award("hitsum", "Hit Addict", "💸", h_m, -h_pts,
                              f"-{h_pts} pts in transfer hits"))

    values = [(m, weeks[m["entry"]][-1]["value"]) for m in managers if weeks[m["entry"]][-1]["value"]]
    if values:
        v_m, v = max(values, key=lambda p: p[1])
        records.append(_award("value", "Richest Squad", "💰", v_m, v, f"Team value £{v:.1f}m"))
    return records


# ---------- Assembly ----------

def get_mini_league(league_id: int) -> dict:
    bootstrap = get_bootstrap()
    events = bootstrap.get("events", [])
    finished = {e["id"] for e in events if e.get("finished")}
    gameweeks = [e["id"] for e in events if e.get("finished") or e.get("is_current")]
    names = {e["id"]: e.get("web_name", "") for e in bootstrap.get("elements", [])}

    league, standings = _fetch_standings(league_id)
    managers = [{
        "entry": s["entry"],
        "team": s.get("entry_name", ""),
        "player": s.get("player_name", ""),
        "rank": s.get("rank"),
        "last_rank": s.get("last_rank"),
        "total": s.get("total", 0),
        "event_total": s.get("event_total", 0),
    } for s in standings]

    with ThreadPoolExecutor(max_workers=16) as pool:
        hist_f = {m["entry"]: pool.submit(_get_or_none, f"{API}/entry/{m['entry']}/history/")
                  for m in managers}
        live_f = {gw: pool.submit(_get_or_none, f"{API}/event/{gw}/live/", gw in finished)
                  for gw in gameweeks}
        pick_f = {(m["entry"], gw): pool.submit(
                      _get_or_none, f"{API}/entry/{m['entry']}/event/{gw}/picks/", gw in finished)
                  for m in managers for gw in gameweeks}
        histories = {eid: f.result() for eid, f in hist_f.items()}
        live = {gw: {e["id"]: e["stats"]["total_points"] for e in (f.result() or {}).get("elements", [])}
                for gw, f in live_f.items()}
        captains = {}
        for (eid, gw), f in pick_f.items():
            picks = (f.result() or {}).get("picks", [])
            cap = next((p for p in picks if p.get("is_captain")), None)
            if cap:
                captains[(eid, gw)] = {
                    "name": names.get(cap["element"], "?"),
                    "points": live.get(gw, {}).get(cap["element"], 0) * (cap.get("multiplier") or 1),
                }

    weeks = build_weeks(managers, histories, captains, gameweeks)
    return {
        "league": {"id": league.get("id"), "name": league.get("name", "")},
        "gameweeks": gameweeks,
        "current_gw": gameweeks[-1] if gameweeks else None,
        "managers": managers,
        "weeks": {str(eid): rows for eid, rows in weeks.items()},
        "awards": {str(gw): build_awards(managers, weeks, gameweeks, gw) for gw in gameweeks},
        "records": build_records(managers, weeks, gameweeks),
        "chips": build_chips(managers, histories, weeks, gameweeks, bootstrap.get("chips", []),
                             gameweeks[-1] if gameweeks else None),
    }
