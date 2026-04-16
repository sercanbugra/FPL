"""
Next-GW forecast builder.

Performance notes
-----------------
* Uses the shared bootstrap-static cache (30-min TTL) for base data.
* Per-player element-summary calls are executed in parallel with
  ThreadPoolExecutor, reducing wall-clock time from ~N×0.5 s to
  roughly max(individual latency) ≈ 1-2 s for 50 players.
* Predicted Score is the average of three ML model predictions
  (Ridge, Random Forest, Gradient Boosting) trained on per-90 stats.
  See ml_predictions.py for details.
"""

import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Dict

import pandas as pd

from .cache import get_bootstrap, compute_team_fdr
from .ml_predictions import get_ml_predicted_scores


def _fetch_player_history(player_id: int) -> tuple[int, list]:
    """Return (player_id, history_list) or (player_id, []) on failure."""
    try:
        url = f"https://fantasy.premierleague.com/api/element-summary/{player_id}/"
        r = requests.get(url, timeout=12)
        if r.status_code == 200:
            return player_id, r.json().get("history") or []
    except Exception:
        pass
    return player_id, []


def get_forecast_data(limit: int = 50) -> List[Dict]:
    """
    Build a compact forecast table with columns:
      Player, Team, Position, Last GW Pts, Form Score, PPG Score,
      Predicted Score, W1..Wn

    Column definitions:
      Form Score      = 0.7 × form + 0.3 × last-GW points
      PPG Score       = 0.6 × points_per_game + 0.4 × last-GW points
      Predicted Score = average of Ridge / Random Forest / Gradient
                        Boosting predictions (see ml_predictions.py)
    """
    data = get_bootstrap()

    elements = data["elements"]
    teams = {t["id"]: t["name"] for t in data["teams"]}
    positions = {1: "GK", 2: "DEF", 3: "MID", 4: "FWD"}

    # Determine the latest GW to display in columns
    events = data.get("events", [])
    latest_gw = None
    for ev in events:
        if ev.get("is_current"):
            latest_gw = ev.get("id")
            break
    if latest_gw is None:
        ids = [ev.get("id") for ev in events if ev.get("finished") or ev.get("is_next")]
        latest_gw = max(ids) if ids else 1

    # Last fully finished GW (for Last GW Pts)
    finished_ids = [ev.get("id") for ev in events if ev.get("finished")]
    last_finished_gw = max(finished_ids) if finished_ids else None

    df = pd.DataFrame(elements)
    df["form"] = pd.to_numeric(df["form"], errors="coerce").fillna(0.0)
    df["points_per_game"] = pd.to_numeric(df["points_per_game"], errors="coerce").fillna(0.0)

    df["Team"] = df["team"].map(teams)
    df["Position"] = df["element_type"].map(positions)

    team_fdr = compute_team_fdr(data, n_gws=3)
    df["FDR Next 3"] = df["team"].map(team_fdr).fillna(3.0).round(1)

    # ── ML Predicted Score ────────────────────────────────────────────
    # Train three models on the full player pool and take their average.
    # Players are pre-selected by ML rank so the forecast already favours
    # players the models rate highly — not just raw form/PPG.
    ml_scores = get_ml_predicted_scores()
    df["Predicted Score"] = df["id"].astype(int).map(ml_scores).fillna(0.0)

    top = df.sort_values("Predicted Score", ascending=False).head(limit).copy()
    top["Player"] = top["web_name"]

    week_cols = [f"W{i}" for i in range(1, int(latest_gw) + 1)]
    for col in week_cols:
        # None → object dtype so pandas 3.x accepts int assignment later
        top[col] = None
    top["Last GW Pts"] = 0.0

    # --- Parallel fetch of per-player weekly history ---
    pid_to_idx = {int(row["id"]): idx for idx, row in top.iterrows()}

    with ThreadPoolExecutor(max_workers=20) as pool:
        futures = {
            pool.submit(_fetch_player_history, pid): pid
            for pid in pid_to_idx
        }
        for future in as_completed(futures):
            pid, history = future.result()
            idx = pid_to_idx.get(pid)
            if idx is None:
                continue
            for h in history:
                rnd = h.get("round")
                pts = h.get("total_points")
                if isinstance(rnd, int) and 1 <= rnd <= latest_gw:
                    top.at[idx, f"W{rnd}"] = pts
                if (
                    last_finished_gw is not None
                    and isinstance(rnd, int)
                    and rnd == last_finished_gw
                ):
                    top.at[idx, "Last GW Pts"] = float(pts or 0.0)

    # Context scores (informational — Predicted Score comes from ML only)
    top["Form Score"] = (0.7 * top["form"] + 0.3 * top["Last GW Pts"]).round(2)
    top["PPG Score"]  = (0.6 * top["points_per_game"] + 0.4 * top["Last GW Pts"]).round(2)
    top["Last GW Pts"] = top["Last GW Pts"].round(2)

    # Final ML predicted score (already set; re-map in case cache refreshed)
    top["Predicted Score"] = top["id"].astype(int).map(ml_scores).fillna(0.0).round(2)

    top = top.sort_values("Predicted Score", ascending=False).copy()

    cols = ["Player", "Team", "Position", "FDR Next 3",
            "Last GW Pts", "Form Score", "PPG Score", "Predicted Score"] + week_cols
    present = [c for c in cols if c in top.columns]
    return top[present].to_dict(orient="records")
