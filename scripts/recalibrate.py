#!/usr/bin/env python3
"""Ensemble recalibration engine.
Optimizes model weights, temperature, and market blending to minimize Brier Score.
Runs automatically post-race or manually.
"""

import json
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import lightgbm as lgb
import xgboost as xgb
from catboost import CatBoost
from loguru import logger

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import MODEL_DIR, ALL_FEATURES
from db import init_db

LGB_PATH  = MODEL_DIR / "model_lgb.txt"
XGB_PATH  = MODEL_DIR / "model_xgb.json"
CAT_PATH  = MODEL_DIR / "model_cat.cbm"
META_PATH = MODEL_DIR / "model_meta.json"

MATRIX_PATH = Path(__file__).resolve().parent.parent / "training_data" / "final_feature_matrix.parquet"


def _normalize(s):
    mn, mx = s.min(), s.max()
    return (s - mn) / (mx - mn + 1e-9)


def main():
    logger.info("=== Starting Ensemble Recalibration Engine ===")
    init_db()

    if not MATRIX_PATH.exists():
        logger.error(f"Feature matrix not found at {MATRIX_PATH}")
        return

    # Load models
    logger.info("Loading models for predictions...")
    try:
        lgb_model = lgb.Booster(model_file=str(LGB_PATH))
        xgb_model = xgb.Booster()
        xgb_model.load_model(str(XGB_PATH))
        cat_model = CatBoost().load_model(str(CAT_PATH))
    except Exception as e:
        logger.error(f"Failed to load models: {e}")
        return

    # Load metadata
    with open(META_PATH) as f:
        meta = json.load(f)
    features = meta["features"]

    # Load feature matrix
    logger.info("Loading historical feature matrix...")
    df_full = pd.read_parquet(MATRIX_PATH)

    # Clean is_win / plc target
    if "is_win" in df_full.columns:
        df_full["is_win_num"] = df_full["is_win"].astype(float)
    elif "plc" in df_full.columns:
        df_full["is_win_num"] = (df_full["plc"].astype(str) == "1").astype(float)
    else:
        logger.error("No win indicator target found in feature matrix.")
        return

    # Sort chronologically to select recent races
    if "date" in df_full.columns:
        df_full = df_full.sort_values("date")
    
    unique_races = df_full["race_id"].unique()
    num_races_val = min(500, len(unique_races))
    logger.info(f"Selecting the most recent {num_races_val} races for calibration...")
    val_races = unique_races[-num_races_val:]
    
    df_val = df_full[df_full["race_id"].isin(val_races)].copy()
    
    for col in ["venue", "track_type", "course", "race_class", "track_condition"]:
        if col in df_val.columns:
            df_val[col] = df_val[col].astype("category")

    # Map ai_unluckiness from cache
    AI_CACHE = Path(__file__).resolve().parent.parent / "data" / "ai_sentiment_cache.parquet"
    if AI_CACHE.exists():
        try:
            df_ai = pd.read_parquet(AI_CACHE)
            df_ai["horse_no"] = df_ai["horse_no"].astype(str)
            df_val["_horse_no_str"] = df_val["horse_no"].astype(str)
            df_val = df_val.merge(df_ai, left_on=["race_id", "_horse_no_str"], right_on=["race_id", "horse_no"], how="left", suffixes=("", "_ai"))
            df_val["ai_unluckiness"] = df_val["ai_unlucky_score"].fillna(1.0)
            df_val = df_val.drop(columns=["_horse_no_str", "horse_no_ai", "ai_unlucky_score"], errors="ignore")
        except Exception as e:
            logger.warning(f"Failed to merge AI scores, using default 1.0: {e}")
            df_val["ai_unluckiness"] = 1.0
    else:
        df_val["ai_unluckiness"] = 1.0

    # Generate predictions
    logger.info("Generating predictions for recent races...")
    X = df_val[features]
    
    lgb_scores = lgb_model.predict(X)
    xgb_scores = xgb_model.predict(xgb.DMatrix(X, enable_categorical=True))
    cat_scores = cat_model.predict(X)

    df_val["lgb_score_raw"] = lgb_scores
    df_val["xgb_score_raw"] = xgb_scores
    df_val["cat_score_raw"] = cat_scores

    # Pre-normalize scores per race
    logger.info("Pre-normalizing model scores and market-implied probabilities...")
    df_val["lgb_score_norm"] = df_val.groupby("race_id")["lgb_score_raw"].transform(_normalize)
    df_val["xgb_score_norm"] = df_val.groupby("race_id")["xgb_score_raw"].transform(_normalize)
    df_val["cat_score_norm"] = df_val.groupby("race_id")["cat_score_raw"].transform(_normalize)
    
    # Pre-normalize market implied probabilities
    df_val["implied_prob_norm"] = df_val.groupby("race_id")["market_implied_prob"].transform(lambda x: x / (x.sum() + 1e-9))

    # Extract NumPy arrays for ultra-fast vector evaluation
    lgb_norm = df_val["lgb_score_norm"].values
    xgb_norm = df_val["xgb_score_norm"].values
    cat_norm = df_val["cat_score_norm"].values
    m_prob_norm = df_val["implied_prob_norm"].values
    y_true = df_val["is_win_num"].values

    # Quick baseline calculation using standard settings (equal weights, T=0.55, B=0.30)
    def compute_brier(w_l, w_x, w_c, temp, blend):
        w_sum = w_l + w_x + w_c
        if w_sum > 0:
            wl, wx, wc = w_l / w_sum, w_x / w_sum, w_c / w_sum
        else:
            wl, wx, wc = 0.33, 0.33, 0.34
            
        ensemble = wl * lgb_norm + wx * xgb_norm + wc * cat_norm
        
        # Softmax grouped by race_id
        df_val["_ens"] = ensemble
        df_val["_exp"] = np.exp((df_val["_ens"] - df_val.groupby("race_id")["_ens"].transform("max")) / temp)
        df_val["_model_prob"] = df_val["_exp"] / (df_val.groupby("race_id")["_exp"].transform("sum") + 1e-9)
        
        # Blended with market implied prob
        blended = (1.0 - blend) * df_val["_model_prob"].values + blend * m_prob_norm
        
        # Standardize blend to sum to 1.0 per race
        df_val["_blended"] = blended
        df_val["_blended_norm"] = df_val["_blended"] / (df_val.groupby("race_id")["_blended"].transform("sum") + 1e-9)
        
        # Calculate Brier score
        diff = df_val["_blended_norm"].values - y_true
        return np.mean(diff ** 2)

    # Initial Brier Score
    temp_init = meta.get("temperature", 0.55)
    blend_init = meta.get("market_blend", 0.30)
    wl_init = meta.get("lgb_weight", 0.33)
    wx_init = meta.get("xgb_weight", 0.33)
    wc_init = meta.get("cat_weight", 0.34)
    
    brier_init = compute_brier(wl_init, wx_init, wc_init, temp_init, blend_init)
    logger.info(f"Baseline Brier Score: {brier_init:.6f} (using LGB={wl_init:.2f}, XGB={wx_init:.2f}, CAT={wc_init:.2f}, T={temp_init:.2f}, Blend={blend_init:.2f})")

    # Coarse-to-fine Optimizer (Random search + local grid polish)
    np.random.seed(42)  # Set seed for reproducible calibration
    logger.info("Phase 1: Running coarse randomized search (1,500 trials)...")
    
    best_brier = brier_init
    best_params = (wl_init, wx_init, wc_init, temp_init, blend_init)
    
    for _ in range(1500):
        # Sample parameters
        w_raw = np.random.dirichlet(np.ones(3)) # sums to 1.0
        t = np.random.uniform(0.35, 1.10)
        b = np.random.uniform(0.05, 0.55)
        
        score = compute_brier(w_raw[0], w_raw[1], w_raw[2], t, b)
        if score < best_brier:
            best_brier = score
            best_params = (w_raw[0], w_raw[1], w_raw[2], t, b)

    logger.info(f"Phase 1 best Brier Score: {best_brier:.6f}")
    
    # Phase 2: Fine local grid search around best params
    logger.info("Phase 2: Fine-tuning locally around best parameters (250 grid points)...")
    wl_b, wx_b, wc_b, temp_b, blend_b = best_params
    
    for _ in range(250):
        # Sample close perturbations
        wl_p = max(0.0, wl_b + np.random.normal(0, 0.05))
        wx_p = max(0.0, wx_b + np.random.normal(0, 0.05))
        wc_p = max(0.0, wc_b + np.random.normal(0, 0.05))
        w_sum = wl_p + wx_p + wc_p
        if w_sum > 0:
            wl_p, wx_p, wc_p = wl_p/w_sum, wx_p/w_sum, wc_p/w_sum
            
        temp_p = min(max(temp_b + np.random.normal(0, 0.04), 0.30), 1.15)
        blend_p = min(max(blend_b + np.random.normal(0, 0.03), 0.0), 0.60)
        
        score = compute_brier(wl_p, wx_p, wc_p, temp_p, blend_p)
        if score < best_brier:
            best_brier = score
            best_params = (wl_p, wx_p, wc_p, temp_p, blend_p)

    wl_opt, wx_opt, wc_opt, temp_opt, blend_opt = best_params
    
    improvement_pct = ((brier_init - best_brier) / brier_init) * 100
    logger.success(f"Recalibration Completed!")
    logger.info(f"  Optimized Brier Score: {best_brier:.6f} (Initial: {brier_init:.6f})")
    logger.info(f"  Total Calibration Improvement: {improvement_pct:.3f}%")
    logger.info(f"  Optimal Weights: LGB={wl_opt:.3f}, XGB={wx_opt:.3f}, CAT={wc_opt:.3f}")
    logger.info(f"  Optimal Temperature scaling (Entropy): {temp_opt:.3f}")
    logger.info(f"  Optimal Market Blend percentage: {blend_opt*100:.1f}%")

    # Update metadata file
    meta["temperature"] = round(float(temp_opt), 4)
    meta["market_blend"] = round(float(blend_opt), 4)
    meta["lgb_weight"] = round(float(wl_opt), 4)
    meta["xgb_weight"] = round(float(wx_opt), 4)
    meta["cat_weight"] = round(float(wc_opt), 4)
    meta["calibrated_at"] = datetime.now().isoformat()
    meta["brier_score_before"] = round(float(brier_init), 6)
    meta["brier_score_after"] = round(float(best_brier), 6)

    try:
        with open(META_PATH, "w") as f:
            json.dump(meta, f, indent=2)
        logger.success("Optimized parameters committed successfully to model_meta.json!")
    except Exception as e:
        logger.error(f"Failed to save optimized parameters to metadata: {e}")


if __name__ == "__main__":
    main()
