"""
Premier League table and gameweek fixtures, derived from cached FPL data.

The FPL fixtures endpoint carries final scores for every played match, so
the league table is computed from it rather than scraped from elsewhere.
Both functions are pure over (bootstrap, fixtures) so they can be tested
without the network; views pass in the cached payloads.
"""

BADGE_URL = "https://resources.premierleague.com/premierleague/badges/70/t{code}.png"


def _team_info(bootstrap: dict) -> dict:
    return {
        t["id"]: {
            "id": t["id"],
            "name": t.get("name", ""),
            "short_name": t.get("short_name", ""),
            "badge": BADGE_URL.format(code=t.get("code", 0)),
        }
        for t in bootstrap.get("teams", [])
    }


def _is_played(f: dict) -> bool:
    return (
        (f.get("finished") or f.get("finished_provisional"))
        and f.get("team_h_score") is not None
        and f.get("team_a_score") is not None
    )


def _kickoff_key(f: dict) -> str:
    # ISO-8601 strings sort chronologically; unscheduled fixtures go last.
    return f.get("kickoff_time") or "9999"


def compute_league_table(bootstrap: dict, fixtures: list) -> list:
    """
    Return the league table as a list of rows sorted by
    points, goal difference, goals scored, then name.
    """
    teams = _team_info(bootstrap)
    rows = {
        tid: {**info, "played": 0, "won": 0, "drawn": 0, "lost": 0,
              "gf": 0, "ga": 0, "gd": 0, "points": 0, "form": []}
        for tid, info in teams.items()
    }

    for f in sorted((f for f in fixtures if _is_played(f)), key=_kickoff_key):
        h, a = f["team_h"], f["team_a"]
        if h not in rows or a not in rows:
            continue
        hs, as_ = int(f["team_h_score"]), int(f["team_a_score"])
        for tid, gf, ga in ((h, hs, as_), (a, as_, hs)):
            r = rows[tid]
            r["played"] += 1
            r["gf"] += gf
            r["ga"] += ga
            if gf > ga:
                r["won"] += 1
                r["points"] += 3
                r["form"].append("W")
            elif gf == ga:
                r["drawn"] += 1
                r["points"] += 1
                r["form"].append("D")
            else:
                r["lost"] += 1
                r["form"].append("L")

    for r in rows.values():
        r["gd"] = r["gf"] - r["ga"]
        r["form"] = r["form"][-5:]

    table = sorted(
        rows.values(),
        key=lambda r: (-r["points"], -r["gd"], -r["gf"], r["name"]),
    )
    for i, r in enumerate(table, start=1):
        r["position"] = i
    return table


def default_gameweek(bootstrap: dict, fixtures: list) -> int:
    """
    The gameweek being played "this week": the current GW while any of its
    matches are still to finish, otherwise the next one. FPL keeps is_current
    on a GW until the next deadline (and only sets the event's `finished` flag
    after bonus processing), so we check the matches themselves.
    """
    events = bootstrap.get("events", [])
    current = next((e["id"] for e in events if e.get("is_current")), None)
    nxt = next((e["id"] for e in events if e.get("is_next")), None)
    if current:
        gw_matches = [f for f in fixtures if f.get("event") == current]
        all_done = gw_matches and all(
            f.get("finished") or f.get("finished_provisional") for f in gw_matches
        )
        if not (all_done and nxt):
            return current
    return nxt or 1


def get_gameweek_fixtures(bootstrap: dict, fixtures: list, gw: int | None = None) -> dict:
    """Return the fixtures for one gameweek (current GW by default), in kickoff order."""
    teams = _team_info(bootstrap)
    events = bootstrap.get("events", [])
    max_gw = max((e["id"] for e in events), default=38)
    if gw is None:
        gw = default_gameweek(bootstrap, fixtures)
    gw = max(1, min(int(gw), max_gw))

    matches = []
    for f in sorted((f for f in fixtures if f.get("event") == gw), key=_kickoff_key):
        if f.get("finished") or f.get("finished_provisional"):
            status = "finished"
        elif f.get("started"):
            status = "live"
        else:
            status = "upcoming"
        matches.append({
            "id": f.get("id"),
            "kickoff_time": f.get("kickoff_time"),
            "status": status,
            "minutes": f.get("minutes", 0),
            "home": teams.get(f.get("team_h"), {}),
            "away": teams.get(f.get("team_a"), {}),
            "home_score": f.get("team_h_score"),
            "away_score": f.get("team_a_score"),
            "home_difficulty": f.get("team_h_difficulty"),
            "away_difficulty": f.get("team_a_difficulty"),
        })

    event = next((e for e in events if e["id"] == gw), {})
    return {
        "gameweek": gw,
        "max_gameweek": max_gw,
        "name": event.get("name") or f"Gameweek {gw}",
        "deadline_time": event.get("deadline_time"),
        "fixtures": matches,
    }

