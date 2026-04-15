import pandas as pd

from .cache import get_bootstrap, compute_team_fdr


def get_fpl_data() -> pd.DataFrame:
    """
    Fetch and process player statistics from the official FPL API.

    Uses the shared bootstrap-static cache (30-min TTL) so repeated
    requests within a half-hour window don't hit the network.

    Returns a DataFrame with one row per player, ready for JSON
    serialisation.  Key columns:
      PlayerId     — FPL element id (used by the player-summary modal)
      PlayerPhoto  — photo slug (e.g. "12345") for the CDN URL
      FDR Next 3   — average fixture difficulty for the next 3 GWs
    """
    data = get_bootstrap()

    elements = data["elements"]
    teams = {team["id"]: team["name"] for team in data["teams"]}
    positions = {1: "GK", 2: "DEF", 3: "MID", 4: "FWD"}

    df = pd.DataFrame(elements)

    # --- Fixture difficulty per team ---
    team_fdr = compute_team_fdr(data, n_gws=3)
    df["FDR Next 3"] = df["team"].map(team_fdr).fillna(3.0).round(1)

    # --- Derived columns ---
    df["Team"] = df["team"].map(teams)
    df["Position"] = df["element_type"].map(positions)
    df["Price (GBP m)"] = df["now_cost"] / 10.0

    df["Appearances"] = (df["minutes"] / 90).round(0).astype(int)

    # Players with 0 appearances have no meaningful FDR — sentinel 0 rendered as N/A
    # (None → NaN → invalid JSON "NaN"; 0.0 serialises cleanly and fdrBadge handles it)
    df.loc[df["Appearances"] == 0, "FDR Next 3"] = 0.0

    df["Median Points"] = df["points_per_game"].astype(float)
    df["Weighted Avg Points"] = (
        df["form"].astype(float) * 0.7
        + df["points_per_game"].astype(float) * 0.3
    )

    goal_points_map = {1: 6, 2: 6, 3: 5, 4: 4}
    df["_goal_pts_per"] = df["element_type"].map(goal_points_map)
    xg = pd.to_numeric(df.get("expected_goals", 0), errors="coerce").fillna(0.0)
    xa = pd.to_numeric(df.get("expected_assists", 0), errors="coerce").fillna(0.0)
    df["xG Points"] = (xg * df["_goal_pts_per"]) + (xa * 3.0)

    df["Goals per App"] = df.apply(
        lambda x: x["goals_scored"] / x["Appearances"] if x["Appearances"] > 0 else 0,
        axis=1,
    )
    df["Assists per App"] = df.apply(
        lambda x: x["assists"] / x["Appearances"] if x["Appearances"] > 0 else 0,
        axis=1,
    )

    df["Discipline Index"] = df["yellow_cards"] + 3 * df["red_cards"]

    df["Chance of Playing"] = df["chance_of_playing_next_round"].fillna(0).apply(
        lambda x: f"{int(x)}%" if x > 0 else "Unavailable"
    )

    # Photo slug — strip the ".jpg" extension for use in CDN URLs
    df["_photo_slug"] = df["photo"].astype(str).str.replace(r"\.\w+$", "", regex=True)

    # --- Output DataFrame ---
    df_out = pd.DataFrame({
        "PlayerId": df["id"].astype(int),
        "PlayerPhoto": df["_photo_slug"],
        "Player": df["web_name"],
        "Position": df["Position"],
        "Team": df["Team"],
        "Price (GBP m)": df["Price (GBP m)"].round(1),
        "FDR Next 3": df["FDR Next 3"],
        "Total Points": df["total_points"].astype(int),
        "Median": df["Median Points"].round(2),
        "Avg": df["Weighted Avg Points"].round(2),
        "xG Points": df["xG Points"].round(2),
        "Appearances": df["Appearances"],
        "Goals": df["goals_scored"],
        "Assists": df["assists"],
        "Goals per App": df["Goals per App"].round(2),
        "Assists per App": df["Assists per App"].round(2),
        "Y Cards": df["yellow_cards"],
        "R Cards": df["red_cards"],
        "Discip Index": df["Discipline Index"],
    })

    df_out.sort_values(by="Total Points", ascending=False, inplace=True)
    return df_out
