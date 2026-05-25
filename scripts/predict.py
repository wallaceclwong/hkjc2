#!/usr/bin/env python3
"""Generate ML ensemble predictions for today's races.

Usage:
    python scripts/predict.py --date 2026-05-25 --venue ST
    python scripts/predict.py --date 2026-05-25 --venue HV --race 3
"""

import argparse, json, pickle, sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import lightgbm as lgb
import xgboost as xgb
from catboost import CatBoost
from loguru import logger

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import MODEL_DIR, DATA_DIR, ALL_FEATURES, RACE_TIME_BY_DIST, RACE_TIME_DEFAULT
from db import init_db, get_racecard, get_horses, get_latest_odds, save_predictions, get_race_ids_for_date, get_db

LGB_PATH    = MODEL_DIR / "model_lgb.txt"
XGB_PATH    = MODEL_DIR / "model_xgb.json"
CAT_PATH    = MODEL_DIR / "model_cat.cbm"
ENCODER_PATH = MODEL_DIR / "xgb_encoder.pkl"
META_PATH   = MODEL_DIR / "model_meta.json"

MATRIX_PATH = Path(__file__).resolve().parent.parent / "training_data" / "final_feature_matrix.parquet"
AI_CACHE = DATA_DIR / "ai_sentiment_cache.parquet"

TEMPERATURE = 0.55
MARKET_BLEND = 0.30


def _load_models():
    lgb_model = lgb.Booster(model_file=str(LGB_PATH))
    xgb_model = xgb.Booster()
    xgb_model.load_model(str(XGB_PATH))
    cat_model = CatBoost().load_model(str(CAT_PATH))
    xgb_enc = pickle.load(open(str(ENCODER_PATH), "rb"))
    with open(META_PATH) as f:
        meta = json.load(f)
    return lgb_model, xgb_model, cat_model, xgb_enc, meta["features"]


def _load_historical_stats():
    df = pd.read_parquet(MATRIX_PATH)
    j_stats = df.sort_values("date").groupby("jockey").tail(1)[["jockey", "jockey_win_rate", "jockey_place_rate", "jockey_rides"]]
    t_stats = df.sort_values("date").groupby("trainer").tail(1)[["trainer", "trainer_win_rate", "trainer_place_rate"]]
    h_cols = ["horse_id", "sec_pos_1", "sec_pos_2", "sec_pos_pre"]
    h_cols_exist = [c for c in h_cols if c in df.columns]
    h_stats = df.sort_values("date").groupby("horse_id").tail(1)[h_cols_exist]
    return j_stats, t_stats, h_stats


def _load_ai_scores(df_full):
    if not AI_CACHE.exists():
        return {}
    df_ai = pd.read_parquet(AI_CACHE)
    df_ai["horse_no"] = df_ai["horse_no"].astype(str)
    ai_map = df_full[["race_id", "horse_no", "horse_id"]].drop_duplicates()
    df_ai = df_ai.merge(ai_map, on=["race_id", "horse_no"], how="inner")
    return df_ai.sort_values("race_id").groupby("horse_id").tail(1).set_index("horse_id")["ai_unlucky_score"].to_dict()


def _safe_horse_no(val):
    try:
        return str(int(float(val)))
    except (ValueError, TypeError):
        return str(val)


def _normalize(s):
    mn, mx = s.min(), s.max()
    return (s - mn) / (mx - mn + 1e-9)


def predict_race(race_id: str, lgb_model, xgb_model, cat_model, xgb_enc, features,
                 j_stats, t_stats, h_stats, ai_scores) -> dict | None:
    rc = get_racecard(race_id)
    if not rc:
        logger.warning(f"{race_id}: racecard not found in DB")
        return None

    horses = rc.get("horses", [])
    if not horses:
        return None

    field_size = len(horses)
    venue = rc.get("venue", "ST")
    odds = get_latest_odds(race_id)

    rows = []
    for h in horses:
        horse_id = h.get("horse_id", "")
        jockey = h.get("jockey", "").strip()
        trainer = h.get("trainer", "").strip()
        horse_no = str(h.get("saddle_number", ""))

        js = j_stats[j_stats["jockey"] == jockey].iloc[0].to_dict() if jockey in j_stats["jockey"].values else {}
        ts = t_stats[t_stats["trainer"] == trainer].iloc[0].to_dict() if trainer in t_stats["trainer"].values else {}
        hs = h_stats[h_stats["horse_id"] == horse_id].iloc[0].to_dict() if horse_id in h_stats["horse_id"].values else {}

        last_6_raw = h.get("last_6_runs", [])
        last_6 = [int(r) for r in last_6_raw if str(r).isdigit()]

        win_odds_val = odds.get(horse_no, 10.0)
        win_odds = float(win_odds_val) if win_odds_val else 10.0

        row = {
            "horse_no": horse_no,
            "horse_name": h.get("horse_name"),
            "win_odds": win_odds,
            "market_implied_prob": 1.0 / win_odds if win_odds > 0 else 0.05,
            "actual_wt": float(h.get("weight", 120)),
            "draw": int(h.get("draw", 0) or 0),
            "field_size": field_size,
            "distance": float(rc.get("distance", 1200)),
            "venue": venue,
            "track_type": rc.get("track_type", "Turf"),
            "course": rc.get("course", "A"),
            "race_class": rc.get("race_class", "Class 4"),
            "track_condition": rc.get("track_condition", "Good"),
            "last_6_avg": np.mean(last_6) if last_6 else 7.0,
            "last_6_best": min(last_6) if last_6 else 5.0,
            "last_2_avg": np.mean(last_6[:2]) if len(last_6) >= 2 else 7.0,
            "last_6_trend": (np.mean(last_6[:3]) - np.mean(last_6[3:])) if len(last_6) >= 6 else 0.0,
            "gear_change": 0.0,
            "stable_change": 0,
            "ai_unluckiness": ai_scores.get(horse_id, 1.0),
            "jockey_win_rate": js.get("jockey_win_rate", 0.08),
            "jockey_place_rate": js.get("jockey_place_rate", 0.23),
            "jockey_rides": js.get("jockey_rides", 100),
            "trainer_win_rate": ts.get("trainer_win_rate", 0.08),
            "trainer_place_rate": ts.get("trainer_place_rate", 0.23),
            "sec_pos_1": hs.get("sec_pos_1", 6.0),
            "sec_pos_2": hs.get("sec_pos_2", 6.0),
            "sec_pos_pre": hs.get("sec_pos_pre", 6.0),
            "race_sec_sum": RACE_TIME_BY_DIST.get(int(rc.get("distance", 1200)), RACE_TIME_DEFAULT),
        }
        row["draw_relative"] = row["draw"] / field_size if field_size > 0 else 0.5
        rows.append(row)

    df = pd.DataFrame(rows)
    df["implied_prob_norm"] = df["market_implied_prob"] / df["market_implied_prob"].sum()
    df["odds_rank"] = df["win_odds"].rank(method="min")

    for col in ["venue", "track_type", "course", "race_class", "track_condition"]:
        df[col] = df[col].astype("category")

    X = df[features]

    lgb_scores = lgb_model.predict(X)
    xgb_scores = xgb_model.predict(xgb.DMatrix(X, enable_categorical=True))
    cat_scores = cat_model.predict(X)

    df["ensemble_score"] = (_normalize(lgb_scores) + _normalize(xgb_scores) + _normalize(cat_scores)) / 3.0
    df["rank"] = df["ensemble_score"].rank(ascending=False, method="first").astype(int)

    scores = df["ensemble_score"].values
    exp_scores = np.exp((scores - np.max(scores)) / TEMPERATURE)
    model_probs = exp_scores / exp_scores.sum()

    market_probs = df["implied_prob_norm"].values
    blended = (1 - MARKET_BLEND) * model_probs + MARKET_BLEND * market_probs
    blended = blended / blended.sum()

    df["model_prob_pure"] = model_probs
    df["pred_prob"] = blended
    df["fair_odds"] = 1.0 / df["pred_prob"]
    df["pure_ev"] = df["model_prob_pure"] * df["win_odds"]
    df["value_edge"] = (df["pred_prob"] - df["implied_prob_norm"]) / df["implied_prob_norm"].clip(lower=0.01)
    df["market_prob"] = market_probs

    save_predictions(race_id, df)

    # ── Bet signal ──────────────────────────────────────────────────────────
    track_cond = rc.get("track_condition", "Good").upper()
    wet = any(w in track_cond for w in ("WET", "SOFT", "YIELDING", "HEAVY", "SLOW"))
    odds_defaulted = (df["win_odds"] == 10.0).all()

    value = df[(df["win_odds"] >= 4.0) & (df["win_odds"] <= 15.0) & (df["pure_ev"] > 1.05) & (df["rank"] <= 4)]
    bet = None
    if not wet and not value.empty and not odds_defaulted:
        bet = value.sort_values("pure_ev", ascending=False).iloc[0]

    top = df.sort_values("rank").iloc[0]
    return {
        "race_id": race_id,
        "field_size": field_size,
        "top_pick": {"horse_no": _safe_horse_no(top["horse_no"]), "name": top["horse_name"],
                     "odds": float(top["win_odds"]), "rank": int(top["rank"]),
                     "fair_odds": float(top["fair_odds"]), "pure_ev": float(top["pure_ev"])},
        "bet": None if bet is None else {
            "horse_no": _safe_horse_no(bet["horse_no"]), "name": bet["horse_name"],
            "odds": float(bet["win_odds"]), "pure_ev": float(bet["pure_ev"]), "rank": int(bet["rank"]),
        },
        "wet_track": wet,
        "odds_defaulted": odds_defaulted,
    }


def main():
    parser = argparse.ArgumentParser(description="Generate ML predictions")
    parser.add_argument("--date", default=datetime.now().strftime("%Y-%m-%d"))
    parser.add_argument("--venue", default="ST")
    parser.add_argument("--race", type=int, default=0, help="Single race #, or 0 for all")
    args = parser.parse_args()

    init_db()

    if not MATRIX_PATH.exists():
        logger.error(f"Feature matrix not found at {MATRIX_PATH}")
        logger.error("Run training first to generate final_feature_matrix.parquet.")
        return

    logger.info("Loading models...")
    lgb_model, xgb_model, cat_model, xgb_enc, features = _load_models()
    logger.info("Loading historical stats...")
    df_full = pd.read_parquet(MATRIX_PATH)
    j_stats, t_stats, h_stats = _load_historical_stats()
    ai_scores = _load_ai_scores(df_full)

    if args.race > 0:
        race_ids = [f"{args.date}_{args.venue}_R{args.race}"]
    else:
        race_ids = get_race_ids_for_date(args.date, args.venue)

    if not race_ids:
        logger.warning(f"No racecards found for {args.date} {args.venue}")
        return

    results = []
    for rid in race_ids:
        try:
            r = predict_race(rid, lgb_model, xgb_model, cat_model, xgb_enc, features,
                             j_stats, t_stats, h_stats, ai_scores)
            if r:
                results.append(r)
                tp = r["top_pick"]
                bet_str = ""
                if r["bet"]:
                    b = r["bet"]
                    bet_str = f" | BET: #{b['horse_no']} {b['name']} (EV={b['pure_ev']:.2f}, odds={b['odds']:.1f})"
                elif r["odds_defaulted"]:
                    bet_str = " | NO BET (odds not available)"
                elif r["wet_track"]:
                    bet_str = " | NO BET (wet track)"
                else:
                    bet_str = " | NO BET (no value)"
                logger.info(f"{rid}: top #{tp['horse_no']} {tp['name']} (rank={tp['rank']}, odds={tp['odds']:.1f}){bet_str}")
        except Exception as e:
            logger.error(f"{rid}: prediction failed — {e}")

    bets = [r for r in results if r["bet"]]
    logger.success(f"Done: {len(results)} races predicted, {len(bets)} bets flagged")


if __name__ == "__main__":
    main()
