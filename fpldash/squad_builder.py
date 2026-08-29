"""
Initial-squad builder.

Formulates 15-man FPL squad selection as a small integer program and solves
it once per "strategy" (see STRATEGIES below) to produce a list of
alternative, budget-legal squads for a manager building their very first
team. Each strategy weights player quality differently, so the results are
genuinely different squads rather than near-duplicates of a single answer.

Why an ILP (via PuLP + the bundled CBC solver) instead of a greedy pick
------------------------------------------------------------------------
Squad selection is a constrained knapsack: 15 players, a fixed 2-5-5-3
position split, a budget, and a 3-per-club cap. A greedy "best value first"
fill can't see far enough ahead to spend the budget optimally across
positions at once (e.g. it may starve midfield to over-invest in defence).
An ILP solves the whole allocation jointly and is guaranteed optimal for
whatever objective a strategy encodes, and the problem is tiny (~700
players -> ~1400 binary variables), so it solves in well under a second.

We also jointly pick the best starting XI (formation-legal) from within the
15, since squad value that never gets a start is wasted budget — the
objective rewards starters far more than bench players.

Cold-start handling
--------------------
At the very start of a season nobody has kicked a ball yet, so
ml_predictions.get_ml_predicted_scores() (trained on this season's own
per-90 stats) has nothing to train on and returns {}. We fall back to the
FPL API's own forward-looking estimate (`ep_next`, then `ep_this`, then
last season's points-per-game) so squad building works before GW1, and
switches over to our own ML ensemble automatically once enough of the
season has been played for it to train.
"""

import threading
import time

import pulp

from .cache import get_bootstrap, compute_team_fdr
from .ml_predictions import get_ml_predicted_scores

SQUAD_SIZE = 15
POSITION_QUOTAS = {1: 2, 2: 5, 3: 5, 4: 3}  # GK, DEF, MID, FWD
POSITION_NAMES = {1: "GK", 2: "DEF", 3: "MID", 4: "FWD"}
MAX_PER_TEAM = 3
STARTING_XI = 11
FORMATION_MIN = {1: 1, 2: 3, 3: 2, 4: 1}
FORMATION_MAX = {1: 1, 2: 5, 3: 5, 4: 3}
BENCH_WEIGHT = 0.15  # bench value counts for cover, but starters are what score points

_lock = threading.Lock()
_cache: dict = {}
_TTL = 1800  # aligns with the bootstrap/ML caches this module reads from


def _score_balanced(p):
    return p["predicted"]


def _score_fixture(p):
    # (6 - fdr) normalised so an average fixture (fdr=3) leaves the score unchanged
    return p["predicted"] * max(6.0 - p["fdr"], 0.5) / 3.0


def _score_template(p):
    # Reward popular picks — lower rank-risk for a first squad
    return p["predicted"] * (1 + p["ownership"] / 100.0)


def _score_differential(p):
    # Reward low-ownership players, mildly fade heavily-owned ones
    if p["ownership"] < 15.0:
        return p["predicted"] * (1 + (15.0 - p["ownership"]) / 15.0)
    return p["predicted"] * 0.85


STRATEGIES = [
    {
        "key": "balanced",
        "name": "Balanced (ML Optimal)",
        "description": "Maximises predicted points with no other bias — the mathematically strongest squad.",
        "score_fn": _score_balanced,
    },
    {
        "key": "fixture",
        "name": "Fixture-Friendly",
        "description": "Leans towards players whose teams have the easiest run of fixtures over the next 3 gameweeks.",
        "score_fn": _score_fixture,
    },
    {
        "key": "template",
        "name": "Safe / Template",
        "description": "Favours widely-owned players so your rank stays close to the pack if a pick misfires.",
        "score_fn": _score_template,
    },
    {
        "key": "differential",
        "name": "Differential",
        "description": "Favours low-ownership players who can jump you up the rankings if they come off.",
        "score_fn": _score_differential,
    },
    {
        "key": "stars",
        "name": "Star-Studded",
        "description": "Locks in the 3 most expensive players in the game, then fills out the squad as efficiently as possible.",
        "score_fn": _score_balanced,
        "force_top_expensive": 3,
    },
]


def _player_pool(budget: float) -> list:
    data = get_bootstrap()
    elements = data["elements"]
    teams = {t["id"]: t["name"] for t in data["teams"]}
    team_fdr = compute_team_fdr(data, n_gws=3)
    ml_scores = get_ml_predicted_scores()

    pool = []
    for e in elements:
        price = e.get("now_cost", 0) / 10.0
        if price <= 0 or price > budget:
            continue
        pos = e.get("element_type")
        if pos not in POSITION_NAMES:
            continue
        # Skip players ruled out for the next match (injury/suspension/loan/etc.)
        cop = e.get("chance_of_playing_next_round")
        if cop is not None and cop == 0:
            continue

        ml = ml_scores.get(e["id"])
        if ml:
            predicted = float(ml)
        else:
            # Cold start (preseason / GW0): fall back to FPL's own forward estimate
            predicted = float(e.get("ep_next") or e.get("ep_this") or e.get("points_per_game") or 0)

        photo_slug = str(e.get("photo", "")).split(".")[0]
        pool.append({
            "id": e["id"],
            "web_name": e.get("web_name"),
            "name": f"{e.get('first_name', '')} {e.get('second_name', '')}".strip(),
            "team_id": e.get("team"),
            "team": teams.get(e.get("team"), "?"),
            "position": POSITION_NAMES[pos],
            "pos_code": pos,
            "price": round(price, 1),
            "predicted": round(predicted, 2),
            "ownership": float(e.get("selected_by_percent") or 0),
            "fdr": team_fdr.get(e.get("team"), 3.0),
            "photo": (
                f"https://resources.premierleague.com/premierleague/photos/players/250x250/p{photo_slug}.png"
                if photo_slug else None
            ),
        })
    return pool


def _solve_squad(pool: list, budget: float, score_fn, force_top_expensive: int = 0):
    """Solve one strategy's ILP. Returns a squad dict, or None if infeasible."""
    prob = pulp.LpProblem("fpl_squad", pulp.LpMaximize)

    x = {p["id"]: pulp.LpVariable(f"x_{p['id']}", cat="Binary") for p in pool}  # in squad
    y = {p["id"]: pulp.LpVariable(f"y_{p['id']}", cat="Binary") for p in pool}  # starting XI
    scores = {p["id"]: score_fn(p) for p in pool}

    prob += (
        pulp.lpSum(scores[pid] * y[pid] for pid in x)
        + BENCH_WEIGHT * pulp.lpSum(scores[pid] * (x[pid] - y[pid]) for pid in x)
    )

    prob += pulp.lpSum(x.values()) == SQUAD_SIZE
    prob += pulp.lpSum(y.values()) == STARTING_XI
    prob += pulp.lpSum(p["price"] * x[p["id"]] for p in pool) <= budget

    for pid in x:
        prob += y[pid] <= x[pid]  # can only start if picked for the squad

    for pos, quota in POSITION_QUOTAS.items():
        ids = [p["id"] for p in pool if p["pos_code"] == pos]
        prob += pulp.lpSum(x[i] for i in ids) == quota
        prob += pulp.lpSum(y[i] for i in ids) >= FORMATION_MIN[pos]
        prob += pulp.lpSum(y[i] for i in ids) <= FORMATION_MAX[pos]

    team_ids = {p["team_id"] for p in pool}
    for tid in team_ids:
        ids = [p["id"] for p in pool if p["team_id"] == tid]
        prob += pulp.lpSum(x[i] for i in ids) <= MAX_PER_TEAM

    if force_top_expensive:
        priciest = sorted(pool, key=lambda p: -p["price"])[:force_top_expensive]
        for p in priciest:
            prob += x[p["id"]] == 1

    prob.solve(pulp.PULP_CBC_CMD(msg=False, timeLimit=15))
    if pulp.LpStatus[prob.status] != "Optimal":
        return None

    by_id = {p["id"]: p for p in pool}
    squad_ids = [pid for pid in x if x[pid].value() == 1]
    starter_ids = [pid for pid in squad_ids if y[pid].value() == 1]
    bench_ids = [pid for pid in squad_ids if pid not in starter_ids]

    # Captaincy, bench order and the displayed points always rank by *actual*
    # predicted points (not the strategy's weighted score) — a template pick's
    # ownership bonus, say, shouldn't be able to make it captain over a player
    # who is simply expected to score more.
    raw = {pid: by_id[pid]["predicted"] for pid in squad_ids}

    starters = sorted((by_id[i] for i in starter_ids), key=lambda p: -raw[p["id"]])
    captain, vice = starters[0], starters[1]

    bench_outfield = sorted(
        (by_id[i] for i in bench_ids if by_id[i]["pos_code"] != 1),
        key=lambda p: -raw[p["id"]],
    )
    bench_gk = [by_id[i] for i in bench_ids if by_id[i]["pos_code"] == 1]
    bench = bench_outfield + bench_gk  # GK last — matches standard bench-order convention

    starters_by_pos = {name: [] for name in POSITION_NAMES.values()}
    for i in starter_ids:
        starters_by_pos[by_id[i]["position"]].append(by_id[i])
    for grp in starters_by_pos.values():
        grp.sort(key=lambda p: -raw[p["id"]])

    total_cost = round(sum(by_id[i]["price"] for i in squad_ids), 1)
    formation = f"{len(starters_by_pos['DEF'])}-{len(starters_by_pos['MID'])}-{len(starters_by_pos['FWD'])}"
    predicted_points = round(sum(raw[i] for i in starter_ids), 1)

    return {
        "formation": formation,
        "total_cost": total_cost,
        "budget_left": round(budget - total_cost, 1),
        "predicted_points": predicted_points,
        "captain": captain,
        "vice_captain": vice,
        "starters": starters_by_pos,
        "bench": bench,
    }


def build_squads(budget: float = 100.0) -> dict:
    cache_key = round(budget, 1)
    with _lock:
        entry = _cache.get(cache_key)
        if entry and (time.time() - entry["ts"]) < _TTL:
            return entry["data"]

    pool = _player_pool(budget)

    squads = []
    for strat in STRATEGIES:
        squad = _solve_squad(
            pool,
            budget,
            strat["score_fn"],
            force_top_expensive=strat.get("force_top_expensive", 0),
        )
        if squad is None:
            continue
        squads.append({
            "key": strat["key"],
            "name": strat["name"],
            "description": strat["description"],
            **squad,
        })

    result = {"budget": budget, "squads": squads}

    with _lock:
        _cache[cache_key] = {"ts": time.time(), "data": result}

    return result
