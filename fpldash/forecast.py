import requests
import pandas as pd
from typing import List, Dict


def get_forecast_data(limit: int = 50) -> List[Dict]:
    """
    Build a compact forecast table with weekly columns like:
    Player, Team, Position, W1..Wn, Pred_LightGBM, Pred_XGBoost, Predicted_Avg

    Notes:
    - Uses bootstrap-static for base data (fast, single call).
    - Computes a lightweight heuristic standing in for the two models:
        Pred_LightGBM = form
        Pred_XGBoost  = points_per_game
        Predicted_Avg = (Pred_LightGBM + Pred_XGBoost) / 2
    - Fetches per-player weekly points only for the top `limit` players
      to keep network overhead reasonable.
    """
    base_bs = "https://fantasy.premierleague.com/api/bootstrap-static/"
    r = requests.get(base_bs, timeout=20)
    r.raise_for_status()
    data = r.json()

    elements = data["elements"]
    teams = {t["id"]: t["name"] for t in data["teams"]}
    positions = {1: "GK", 2: "DEF", 3: "MID", 4: "FWD"}

    # Determine the latest GW to display in columns (current if exists, otherwise finished/next)
    events = data.get("events", [])
    latest_gw = None
    for ev in events:
        if ev.get("is_current"):
            latest_gw = ev.get("id")
            break
    if latest_gw is None:
        # fallback: max finished or next
        ids = [ev.get("id") for ev in events if ev.get("finished") or ev.get("is_next")]
        latest_gw = max(ids) if ids else 1

    # Last fully finished GW for using recent realized points in predictions
    finished_ids = [ev.get("id") for ev in events if ev.get("finished")]
    last_finished_gw = max(finished_ids) if finished_ids else None

    df = pd.DataFrame(elements)
    df["form"] = pd.to_numeric(df["form"], errors="coerce").fillna(0.0)
    df["points_per_game"] = pd.to_numeric(df["points_per_game"], errors="coerce").fillna(0.0)

    df["Team"] = df["team"].map(teams)
    df["Position"] = df["element_type"].map(positions)

    # Initial heuristic predictions (will be refined with last finished GW points)
    df["Pred_LightGBM"] = df["form"]
    df["Pred_XGBoost"] = df["points_per_game"]
    df["Predicted_Avg"] = (df["Pred_LightGBM"] + df["Pred_XGBoost"]) / 2.0

    # Select top N to enrich with weekly history
    top = df.sort_values("Predicted_Avg", ascending=False).head(limit).copy()
    top["Player"] = top["web_name"]

    # Initialize weekly columns W1..Wn with blanks
    week_cols = [f"W{i}" for i in range(1, int(latest_gw) + 1)]
    for col in week_cols:
        top[col] = ""
    top["Last_GW_Points"] = 0.0

    # Fetch weekly totals for each selected player
    for idx, row in top.iterrows():
        pid = int(row["id"])
        try:
            u = f"https://fantasy.premierleague.com/api/element-summary/{pid}/"
            pr = requests.get(u, timeout=12)
            if pr.status_code != 200:
                continue
            js = pr.json()
            hist = js.get("history") or []
            for h in hist:
                rnd = h.get("round")
                pts = h.get("total_points")
                if isinstance(rnd, int) and 1 <= rnd <= latest_gw:
                    top.at[idx, f"W{rnd}"] = pts
                if (
                    last_finished_gw is not None
                    and isinstance(rnd, int)
                    and rnd == last_finished_gw
                ):
                    top.at[idx, "Last_GW_Points"] = float(pts or 0.0)
        except Exception:
            continue

    # Refine predictions by including the last finished GW points
    # This ensures the most recent completed week directly affects prediction output.
    top["Pred_LightGBM"] = (0.7 * top["form"]) + (0.3 * top["Last_GW_Points"])
    top["Pred_XGBoost"] = (0.6 * top["points_per_game"]) + (0.4 * top["Last_GW_Points"])
    top["Predicted_Avg"] = (top["Pred_LightGBM"] + top["Pred_XGBoost"]) / 2.0

    # Re-sort with updated predictions
    top = top.sort_values("Predicted_Avg", ascending=False).copy()

    # Round predictions for readability
    top["Last_GW_Points"] = top["Last_GW_Points"].round(2)
    top["Pred_LightGBM"] = top["Pred_LightGBM"].round(2)
    top["Pred_XGBoost"] = top["Pred_XGBoost"].round(2)
    top["Predicted_Avg"] = top["Predicted_Avg"].round(2)

    # Final column order
    cols = ["Player", "Team", "Position"] + week_cols + [
        "Pred_LightGBM", "Pred_XGBoost", "Predicted_Avg"
    ]
    present = [c for c in cols if c in top.columns]
    out = top[present]
    return out.to_dict(orient="records")
