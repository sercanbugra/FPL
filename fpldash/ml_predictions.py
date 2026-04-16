"""
ML-ensemble next-GW score predictions.

Three models are trained on the full current-season player pool using
underlying per-90-minute performance metrics as features and
points_per_game as the target:

  1. Ridge regression   — linear baseline
  2. Random Forest      — captures non-linear interactions, bagged
  3. Gradient Boosting  — sequential boosting, further non-linearity

The ensemble average of the three predictions is the "Predicted Score".

Why this is meaningful even though training == prediction set
------------------------------------------------------------
The models learn the *population-level* relationship between underlying
stats (xG rate, xA rate, influence, creativity, threat, clean sheets,
bonus) and realized FPL points.  Applied back to the same players, the
prediction acts as a "regularised estimate" of true quality:

  * Players outperforming their expected stats (high PPG vs low xG)
    will receive a lower predicted score → expect regression.
  * Players underperforming their expected stats (low PPG vs high xG)
    will receive a higher predicted score → expect improvement.

Predictions are cached for 30 minutes alongside the bootstrap data.
"""

import threading
import time

import numpy as np
import pandas as pd
from sklearn.ensemble import GradientBoostingRegressor, RandomForestRegressor
from sklearn.linear_model import Ridge
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from .cache import get_bootstrap

_lock = threading.Lock()
_cache: dict = {}
_TTL = 1800  # 30 minutes — aligns with bootstrap cache TTL

# Underlying-stat columns used as features (raw totals; normalised per-90 below)
_RAW_FEATURES = [
    "influence",
    "creativity",
    "threat",
    "expected_goals",
    "expected_assists",
    "expected_goal_involvements",
    "goals_scored",
    "assists",
    "clean_sheets",
    "bonus",
]


def get_ml_predicted_scores() -> dict:
    """
    Return {player_id (int): predicted_score (float)} for every player
    who has played at least one minute this season.

    Scores are in the same units as FPL points-per-game and are cached.
    """
    with _lock:
        entry = _cache.get("scores")
        if entry and (time.time() - entry["ts"]) < _TTL:
            return entry["data"]

    result = _train_and_predict()

    with _lock:
        _cache["scores"] = {"ts": time.time(), "data": result}

    return result


def _train_and_predict() -> dict:
    data = get_bootstrap()
    df = pd.DataFrame(data["elements"])

    # Only players who have actually played
    df["minutes"] = pd.to_numeric(df["minutes"], errors="coerce").fillna(0.0)
    df = df[df["minutes"] > 0].copy()
    if df.empty:
        return {}

    # Parse all numeric columns
    for col in _RAW_FEATURES + ["points_per_game"]:
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)

    # Appearances (90-min equivalents) — floor at 0.5 to prevent /0
    df["apps90"] = (df["minutes"] / 90.0).clip(lower=0.5)

    # Per-90 normalisation
    feat_cols = []
    for col in _RAW_FEATURES:
        c90 = col + "_90"
        df[c90] = df[col] / df["apps90"]
        feat_cols.append(c90)

    # Position (1 = GK … 4 = FWD) — captures position scoring differences
    df["pos"] = pd.to_numeric(df["element_type"], errors="coerce").fillna(2.0)
    feat_cols.append("pos")

    X = df[feat_cols].values.astype(float)
    y = df["points_per_game"].values.astype(float)

    models = [
        # 1. Ridge — regularised linear model (fast, interpretable baseline)
        Pipeline([
            ("scaler", StandardScaler()),
            ("ridge", Ridge(alpha=1.0)),
        ]),
        # 2. Random Forest — bagged trees, handles interactions & outliers
        RandomForestRegressor(
            n_estimators=150,
            max_depth=6,
            min_samples_leaf=5,
            random_state=42,
            n_jobs=-1,
        ),
        # 3. Gradient Boosting — sequential boosting for remaining residuals
        GradientBoostingRegressor(
            n_estimators=150,
            max_depth=4,
            learning_rate=0.05,
            min_samples_leaf=5,
            random_state=42,
        ),
    ]

    preds = []
    for m in models:
        m.fit(X, y)
        preds.append(m.predict(X))

    # Ensemble average, clamped to non-negative
    avg = np.clip(np.mean(preds, axis=0), 0.0, None)

    player_ids = df["id"].astype(int).values
    return {int(pid): round(float(score), 2) for pid, score in zip(player_ids, avg)}
