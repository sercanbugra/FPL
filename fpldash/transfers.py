"""
Single-transfer suggestions for a manager's current squad.

Each player gets an expected-points outlook over the next HORIZON gameweeks:

    xpts = predicted score  ×  fixture factor  ×  availability  ×  HORIZON

  * predicted score — the ML ensemble from ml_predictions.py (the same
    "Predicted Score" as the GW Forecast tab), falling back to FPL's own
    ep_next / form before the ensemble can train
  * fixture factor  — 1 + FDR_WEIGHT × (3 − avg FDR of the next 3 GWs),
    so FDR 2 → ×1.15, FDR 3 → ×1.0, FDR 4 → ×0.85
  * availability    — chance_of_playing_next_round (100% when FPL has no flag)

A swap's gain is the change in squad_value(): the best formation-legal XI plus
a captain bonus plus a lightly weighted bench, so upgrading a backup who never
starts is worth little. Suggestions are built greedily: for every player still
in the squad, the best
affordable replacement is the highest-xpts player in the same position that fits
the budget (bank + outgoing price) and the 3-per-club rule, and who has played
enough minutes for the per-90 prediction to be trustworthy. The swap with the
biggest gain (at least MIN_GAIN) is taken and applied, and the search
repeats, so the N suggestions can be combined (no player bought twice, max 3 per
club). Each one is priced against the manager's current bank only, never against
money freed by an earlier suggestion, so none of them depends on another.

Prices are current prices: the public API doesn't expose a manager's selling
prices (which can be lower after a price rise), so budgets are approximate.
"""

import requests

from .cache import get_bootstrap, compute_team_fdr
from .ml_predictions import get_ml_predicted_scores

API = "https://fantasy.premierleague.com/api"
HORIZON = 3
FDR_WEIGHT = 0.15
MIN_CHANCE = 75          # % chance of playing for an incoming player
MIN_MINUTES_SHARE = 0.5  # incoming players need ≥50% of available minutes
MIN_GAIN = 0.5           # squad-value gain over HORIZON below which a swap is pure noise
HIT_GAIN = 4.0           # a gain above this outweighs a -4 transfer hit
GOOD_GAIN = 2.0          # below this, only worth it with a spare free transfer
BANK_SLACK = 2.0         # £m left unspent; bigger downgrades must at least be "good"
BENCH_WEIGHT = 0.1       # bench players rarely score; keep a little value for depth
POSITIONS = {1: "GK", 2: "DEF", 3: "MID", 4: "FWD"}
# Formation limits per element_type: (min, max) in a starting XI
XI_LIMITS = {1: (1, 1), 2: (3, 5), 3: (2, 5), 4: (1, 3)}


def _chance(e: dict) -> int:
    c = e.get("chance_of_playing_next_round")
    if c is None:
        return 100 if e.get("status", "a") == "a" else 0
    return int(c)


def player_outlook(e: dict, ml_scores: dict, team_fdr: dict, teams: dict) -> dict:
    pred = ml_scores.get(e["id"])
    if pred is None:
        pred = float(e.get("ep_next") or e.get("form") or 0)
    fdr = team_fdr.get(e["team"], 3.0)
    chance = _chance(e)
    fixture = 1 + FDR_WEIGHT * (3 - fdr)
    xpts = max(0.0, pred) * fixture * (chance / 100) * HORIZON
    return {
        "id": e["id"],
        "name": e.get("web_name", ""),
        "team": teams.get(e["team"], ""),
        "team_id": e["team"],
        "position": POSITIONS.get(e.get("element_type"), ""),
        "element_type": e.get("element_type"),
        "price": (e.get("now_cost") or 0) / 10.0,
        "pred": round(pred, 2),
        "fdr": fdr,
        "chance": chance,
        "news": e.get("news") or "",
        "form": float(e.get("form") or 0),
        "minutes": int(e.get("minutes") or 0),
        "photo": str(e.get("photo", "")).split(".")[0],
        "xpts": round(xpts, 1),
    }


def squad_value(players: list) -> float:
    """
    Expected points of a 15-man squad: the best formation-legal XI, plus the
    captain's extra share (the top XI player counts twice), plus the bench at
    BENCH_WEIGHT. Filling the minimums first, then the best of the rest, is
    optimal because the only constraints are per-position min/max counts.
    """
    by_pos: dict = {}
    for p in players:
        by_pos.setdefault(p["element_type"], []).append(p)
    for lst in by_pos.values():
        lst.sort(key=lambda p: -p["xpts"])

    xi, taken = [], {}
    for pos, (lo, _hi) in XI_LIMITS.items():
        xi += by_pos.get(pos, [])[:lo]
        taken[pos] = min(lo, len(by_pos.get(pos, [])))
    rest = sorted((p for pos, lst in by_pos.items() for p in lst[taken.get(pos, 0):]),
                  key=lambda p: -p["xpts"])
    for p in rest:
        if len(xi) >= 11:
            break
        pos = p["element_type"]
        if taken.get(pos, 0) < XI_LIMITS.get(pos, (0, 0))[1]:
            xi.append(p)
            taken[pos] = taken.get(pos, 0) + 1

    xi_ids = {p["id"] for p in xi}
    bench = sum(p["xpts"] for p in players if p["id"] not in xi_ids)
    captain = max((p["xpts"] for p in xi), default=0.0)
    return sum(p["xpts"] for p in xi) + captain + BENCH_WEIGHT * bench


def suggest_transfers(bootstrap: dict, picks: dict, ml_scores: dict, team_fdr: dict,
                      n: int = 5, alternatives: int = 2) -> dict:
    teams = {t["id"]: t.get("short_name", "") for t in bootstrap.get("teams", [])}
    elements = {e["id"]: e for e in bootstrap.get("elements", [])}
    outlook = {pid: player_outlook(e, ml_scores, team_fdr, teams) for pid, e in elements.items()}

    squad_ids = [p["element"] for p in picks.get("picks", []) if p["element"] in elements]
    starters = {p["element"] for p in picks.get("picks", []) if p.get("position", 99) <= 11}
    bank = (picks.get("entry_history", {}).get("bank") or 0) / 10.0
    club_count: dict = {}
    for pid in squad_ids:
        club_count[elements[pid]["team"]] = club_count.get(elements[pid]["team"], 0) + 1

    played_gws = sum(1 for ev in bootstrap.get("events", []) if ev.get("finished"))
    min_minutes = MIN_MINUTES_SHARE * 90 * max(1, played_gws)
    pool = [o for pid, o in outlook.items()
            if pid not in squad_ids and o["chance"] >= MIN_CHANCE and o["minutes"] >= min_minutes]

    pool.sort(key=lambda c: (-c["xpts"], c["price"]))

    # Greedy plan: take the single best swap, apply it to the squad / bank /
    # club counts, repeat. The suggestions are therefore valid *together*
    # (no player bought twice, never a 4th player from one club).
    squad = set(squad_ids)
    original = list(squad_ids)
    plan = []
    while len(plan) < n:
        best = None
        for pid in original:
            if pid not in squad:
                continue  # already sold earlier in the plan
            out = outlook[pid]
            budget = round(bank + out["price"], 1)
            fits = []
            for c in pool:
                if (c["id"] in squad or c["element_type"] != out["element_type"]
                        or c["price"] > budget + 1e-9
                        or club_count.get(c["team_id"], 0) - (c["team_id"] == out["team_id"]) >= 3):
                    continue
                fits.append(c)
                if len(fits) > alternatives:
                    break
            if not fits or fits[0]["xpts"] <= out["xpts"]:
                continue
            # Squad value is monotone in a player's xpts, so for this outgoing
            # player the highest-xpts fit is also the best for the whole squad.
            others = [outlook[x] for x in squad if x != pid]
            base = squad_value(others + [out])
            gain = squad_value(others + [fits[0]]) - base
            if budget - fits[0]["price"] > BANK_SLACK and gain < GOOD_GAIN:
                continue  # selling a premium for a marginal gain wastes the money
            if gain >= MIN_GAIN and (best is None or gain > best[0]):
                best = (gain, out, fits, budget, others, base)
        if best is None:
            break
        gain, out, fits, budget, others, base = best
        chosen = fits[0]
        plan.append({
            "out": out,
            "in": chosen,
            "gain": round(gain, 1),
            "strength": "strong" if gain >= HIT_GAIN else "good" if gain >= GOOD_GAIN else "marginal",
            "cost_change": round(chosen["price"] - out["price"], 1),
            "bank_after": round(budget - chosen["price"], 1),
            "starter": out["id"] in starters,
            "alternatives": [{**c, "gain": round(squad_value(others + [c]) - base, 1)}
                             for c in fits[1:] if squad_value(others + [c]) - base > 0],
        })
        squad.discard(out["id"])
        squad.add(chosen["id"])
        club_count[out["team_id"]] -= 1
        club_count[chosen["team_id"]] = club_count.get(chosen["team_id"], 0) + 1

    return {
        "bank": (picks.get("entry_history", {}).get("bank") or 0) / 10.0,
        "horizon": HORIZON,
        "squad": sorted(({**outlook[pid], "starter": pid in starters} for pid in squad_ids),
                        key=lambda o: o["xpts"]),
        "suggestions": plan,
    }


def get_transfer_suggestions(manager_id: str, n: int = 5) -> dict:
    bootstrap = get_bootstrap()
    events = bootstrap.get("events", [])
    gw = next((e["id"] for e in events if e.get("is_current")), None) or \
        next((e["id"] for e in events if e.get("is_next")), 1)
    r = requests.get(f"{API}/entry/{manager_id}/event/{gw}/picks/", timeout=15)
    r.raise_for_status()
    picks = r.json()
    result = suggest_transfers(bootstrap, picks, get_ml_predicted_scores(),
                               compute_team_fdr(bootstrap, n_gws=HORIZON), n=n)
    result["gameweek"] = gw
    result["next_gameweek"] = next((e["id"] for e in events if e.get("is_next")), None)
    return result
