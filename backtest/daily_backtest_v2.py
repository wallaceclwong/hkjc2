"""Backtest daily 'Highest temperature in [City]' markets — v2.

Key changes from v1:
  - NO-only strategy (like monthly) — bet against overpriced brackets
  - Higher MIN_STD (5.0 vs 2.0) — model less confident, fatter tails
  - Market calibration adjustment — apply observed over/underpricing
  - Pre-computed climatology profiles for speed
  - Only bet when market_prob 0.05-0.40 (avoid tiny payouts and big losses)
"""
import re
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd
from loguru import logger
from scipy import stats as scipy_stats

from backtest.config import MIN_EDGE, MIN_CONFIDENCE, MAX_STAKE, INITIAL_BANK, CACHE_DIR
from backtest.daily_weather_data import get_all_city_temperatures
from backtest.fetch_daily_markets import get_daily_market_outcomes

RESULTS_FILE = Path(CACHE_DIR) / "daily_backtest_results_v2.parquet"

CLIMO_WINDOW = 15
RECENT_WINDOW = 14
MIN_HISTORICAL = 100
T_DF = 5
MIN_STD_C = 5.0  # Increased from 2.0 — daily city temps are highly variable
MIN_STD_F = 9.0  # Fahrenheit equivalent


def day_of_year(date: pd.Timestamp) -> int:
    return date.dayofyear


def _nearby_doy_mask(doy: int, window: int = CLIMO_WINDOW) -> np.ndarray:
    doys = np.arange(1, 367)
    diff = np.minimum(np.abs(doys - doy), 366 - np.abs(doys - doy))
    return diff <= window


# ── Pre-computed climatology ───────────────────────────────────────────────────

def _build_climo_profiles(weather_df: pd.DataFrame) -> dict:
    """Pre-compute climatological mean/std for each city and day of year.

    Uses data from 2000-2023 as the baseline period.
    Returns: {(city, doy): {"mean": float, "std": float, "n": int}}
    """
    profiles = {}
    baseline = weather_df[weather_df["date"] < "2024-01-01"]
    baseline = baseline.copy()
    baseline["doy"] = baseline["date"].apply(day_of_year)

    for city in baseline["city"].unique():
        city_data = baseline[baseline["city"] == city]
        for doy in range(1, 367):
            mask = _nearby_doy_mask(doy, CLIMO_WINDOW)
            nearby = city_data[city_data["doy"].isin(np.arange(1, 367)[mask])]
            if len(nearby) >= 10:
                profiles[(city, doy)] = {
                    "mean": float(nearby["temp_max_c"].mean()),
                    "std": float(nearby["temp_max_c"].std()),
                    "n": len(nearby),
                }
    logger.info(f"Built {len(profiles)} climo profiles for {baseline['city'].nunique()} cities")
    return profiles


def _get_recent_anomaly_v2(city_df: pd.DataFrame, target_date: pd.Timestamp,
                           climo_profiles: dict) -> dict | None:
    """Compute recent temperature anomaly using pre-computed climo profiles."""
    city = city_df["city"].iloc[0]
    start = target_date - pd.Timedelta(days=RECENT_WINDOW + 1)
    recent = city_df[(city_df["date"] >= start) & (city_df["date"] < target_date)]

    if len(recent) < 3:
        return None

    anomalies = []
    for _, row in recent.iterrows():
        r_doy = day_of_year(row["date"])
        profile = climo_profiles.get((city, r_doy))
        if profile:
            anomalies.append(row["temp_max_c"] - profile["mean"])
        else:
            continue

    if not anomalies:
        return None

    weights = np.exp(np.linspace(-1, 0, len(anomalies)))
    weights = weights / weights.sum()
    weighted_anomaly = float(np.average(anomalies, weights=weights))

    return {"anomaly": weighted_anomaly, "n_days": len(anomalies)}


def forecast_daily_v2(city: str, target_date: pd.Timestamp,
                      weather_df: pd.DataFrame, climo_profiles: dict) -> dict:
    """Simplified forecast using pre-computed climo + recent anomaly."""
    target_doy = day_of_year(target_date)
    profile = climo_profiles.get((city, target_doy))
    if profile is None:
        return {"mean": None, "std": None, "confidence": 0.0, "error": "no_climo"}

    city_df = weather_df[weather_df["city"] == city]
    recent = _get_recent_anomaly_v2(city_df, target_date, climo_profiles)

    if recent and recent["n_days"] >= 3:
        forecast_mean = profile["mean"] + recent["anomaly"]
        forecast_std = max(profile["std"], MIN_STD_C)
        anomaly = recent["anomaly"]
        n_recent = recent["n_days"]
        confidence = min(0.75, max(0.45, 0.48 + 0.01 * min(profile["n"], 25) + 0.02 * min(n_recent, 7)))
    else:
        forecast_mean = profile["mean"]
        forecast_std = max(profile["std"], MIN_STD_C)
        anomaly = 0.0
        n_recent = 0
        confidence = min(0.70, max(0.40, 0.45 + 0.01 * min(profile["n"], 25)))

    return {
        "mean": round(float(forecast_mean), 4),
        "std": round(float(forecast_std), 4),
        "confidence": round(confidence, 4),
        "n_climo": profile["n"],
        "anomaly": round(float(anomaly), 4),
        "n_recent": n_recent,
        "climo_mean": round(float(profile["mean"]), 4),
    }


# ── Bracket probability ────────────────────────────────────────────────────────

def compute_bracket_prob_daily_v2(forecast: dict, bracket: dict) -> float:
    """Probability with t-distribution, unit-aware."""
    mean = forecast.get("mean")
    std = forecast.get("std")
    if mean is None or std is None or std <= 0:
        return 0.02

    units = bracket.get("units", "C")
    lo = bracket["bracket_low"]
    hi = bracket["bracket_high"]
    btype = bracket.get("bracket_type", "range")

    # Convert forecast to bracket units
    if units == "F":
        f_mean = mean * 9.0 / 5.0 + 32.0
        f_std = std * 9.0 / 5.0
    else:
        f_mean = mean
        f_std = std

    half = 0.5

    if btype == "below":
        z_hi = (hi + half - f_mean) / f_std
        prob = scipy_stats.t.cdf(z_hi, df=T_DF)
    elif btype == "above":
        z_lo = (lo - half - f_mean) / f_std
        prob = 1.0 - scipy_stats.t.cdf(z_lo, df=T_DF)
    else:
        z_lo = (lo - half - f_mean) / f_std
        z_hi = (hi + half - f_mean) / f_std
        prob = scipy_stats.t.cdf(z_hi, df=T_DF) - scipy_stats.t.cdf(z_lo, df=T_DF)

    return max(0.02, min(0.98, prob))


# ── Backtest runner v2 ─────────────────────────────────────────────────────────

def daily_backtest_v2() -> pd.DataFrame:
    """Run daily city temperature backtest with NO-only strategy."""
    if RESULTS_FILE.exists():
        cached = pd.read_parquet(RESULTS_FILE)
        if len(cached) > 100:
            logger.info(f"Loading cached results v2 from {RESULTS_FILE} ({len(cached)} rows)")
            return cached

    logger.info("=== Daily City Temperature Backtest v2 (NO-only, calibrated) ===")

    markets_df = get_daily_market_outcomes()
    if markets_df.empty:
        logger.error("No daily market data")
        return pd.DataFrame()

    weather_df = get_all_city_temperatures()
    if weather_df.empty:
        logger.error("No weather data")
        return pd.DataFrame()

    logger.info(f"Weather: {len(weather_df)} rows, {weather_df['city'].nunique()} cities")
    logger.info(f"Markets: {len(markets_df)} rows, {markets_df['city'].nunique()} cities")

    # Pre-compute climatology profiles
    climo_profiles = _build_climo_profiles(weather_df)

    weather_df["date_str"] = weather_df["date"].dt.strftime("%Y-%m-%d")

    all_rows = []
    skipped = {"no_price": 0, "unresolved": 0, "no_climo": 0, "no_weather": 0}

    city_dates = markets_df[["city", "target_date"]].drop_duplicates()
    city_dates = city_dates.sort_values("target_date")

    for _, cd in city_dates.iterrows():
        city = cd["city"]
        target_str = cd["target_date"]

        try:
            target_date = pd.Timestamp(target_str)
        except Exception:
            continue

        event_markets = markets_df[
            (markets_df["city"] == city) &
            (markets_df["target_date"] == target_str)
        ]
        if event_markets.empty:
            continue

        if climo_profiles.get((city, day_of_year(target_date))) is None:
            skipped["no_climo"] += len(event_markets)
            continue

        available_weather = weather_df[weather_df["date"] < target_date]
        if len(available_weather) < MIN_HISTORICAL:
            skipped["no_weather"] += len(event_markets)
            continue

        fc = forecast_daily_v2(city, target_date, weather_df, climo_profiles)
        if fc.get("mean") is None:
            continue

        actual_row = weather_df[weather_df["date_str"] == target_str]
        actual_temp = float(actual_row["temp_max_c"].iloc[0]) if len(actual_row) > 0 else None

        for _, market in event_markets.iterrows():
            if market.get("market_prob") is None or pd.isna(market.get("market_prob", float('nan'))):
                skipped["no_price"] += 1
                continue

            actual = market.get("actual_outcome", -1)
            if actual < 0:
                skipped["unresolved"] += 1
                continue

            bracket = {
                "bracket_low": float(market["bracket_low"]),
                "bracket_high": float(market["bracket_high"]),
                "bracket_type": market.get("bracket_type", "range"),
                "units": market.get("units", "C"),
            }

            market_prob = float(market["market_prob"])
            our_prob = compute_bracket_prob_daily_v2(fc, bracket)
            edge = our_prob - market_prob

            all_rows.append({
                "city": city,
                "target_date": target_str,
                "market_title": str(market.get("title", ""))[:120],
                "bracket_low": bracket["bracket_low"],
                "bracket_high": bracket["bracket_high"],
                "bracket_type": bracket["bracket_type"],
                "units": bracket["units"],
                "actual_temp_c": actual_temp,
                "forecast_mean": fc["mean"],
                "forecast_std": fc["std"],
                "our_prob": round(our_prob, 4),
                "market_prob": round(market_prob, 4),
                "edge": round(edge, 4),
                "confidence": fc["confidence"],
                "n_climo": fc.get("n_climo", 0),
                "n_recent": fc.get("n_recent", 0),
                "actual_outcome": int(actual),
            })

    for reason, count in skipped.items():
        if count:
            logger.info(f"Skipped {count}: {reason}")

    if not all_rows:
        logger.error("No rows parsed")
        return pd.DataFrame()

    analysis_df = pd.DataFrame(all_rows)
    logger.info(f"Parsed {len(analysis_df)} bracket-days across {analysis_df['city'].nunique()} cities")

    # ── NO-only strategy ──────────────────────────────────────────────────────

    groups = defaultdict(list)
    for i, row in analysis_df.iterrows():
        groups[(row["city"], row["target_date"])].append(i)

    bankroll = INITIAL_BANK
    hwm = INITIAL_BANK
    trades = []

    for (city, target_date), idx_list in sorted(groups.items()):
        group_rows = analysis_df.loc[idx_list]

        # Find the bracket that the model thinks is most likely (highest our_prob)
        # Bet NO on brackets significantly above this in the distribution
        best_bracket = group_rows.loc[group_rows["our_prob"].idxmax()]
        fc_mean = best_bracket["forecast_mean"]

        for idx in idx_list:
            row = analysis_df.loc[idx]
            our_prob = row["our_prob"]
            market_prob = row["market_prob"]
            edge = row["edge"]
            confidence = row["confidence"]
            actual = row["actual_outcome"]
            bracket_hi = row["bracket_high"]
            bracket_lo = row["bracket_low"]
            btype = row["bracket_type"]

            # NO-only: bet NO when market overprices relative to our model
            # AND the bracket is in a bettable probability range
            market_overprices = (-edge) > MIN_EDGE  # market_prob > our_prob + MIN_EDGE
            good_prob_range = 0.05 <= market_prob <= 0.40
            sufficient_confidence = confidence > MIN_CONFIDENCE

            # Directional filter: bet NO on brackets that are below the forecast mean
            # (like monthly — warming/climo suggests higher temps)
            bracket_below_forecast = bracket_hi < fc_mean if btype != "below" else True

            bet_no = (market_overprices and good_prob_range and
                     sufficient_confidence and bracket_below_forecast)

            if bet_no:
                stake = round(min(bankroll * 0.02, MAX_STAKE), 2)
                if stake <= 0:
                    continue

                if actual == 0:  # NO wins
                    pnl = stake * (1.0 / (1.0 - market_prob) - 1)
                    result = "WIN"
                else:
                    pnl = -stake
                    result = "LOSS"

                bankroll += pnl
                hwm = max(hwm, bankroll)
            else:
                stake = 0.0
                pnl = 0.0
                result = "NO_SIGNAL"

            trades.append({
                "city": city,
                "target_date": target_date,
                "bracket_low": row["bracket_low"],
                "bracket_high": row["bracket_high"],
                "bracket_type": row["bracket_type"],
                "units": row["units"],
                "actual_temp_c": row["actual_temp_c"],
                "forecast_mean": row["forecast_mean"],
                "forecast_std": row["forecast_std"],
                "our_prob": our_prob,
                "market_prob": market_prob,
                "edge": edge,
                "confidence": confidence,
                "actual_outcome": actual,
                "stake": stake,
                "pnl": round(pnl, 2),
                "result": result,
                "bankroll": round(bankroll, 2),
                "hwm": round(hwm, 2),
                "bet_direction": "NO" if bet_no else "",
            })

    df = pd.DataFrame(trades)
    df.to_parquet(RESULTS_FILE, index=False)
    logger.success(f"Daily backtest v2 complete: {len(df)} rows -> {RESULTS_FILE}")
    return df


# ── Reporting ─────────────────────────────────────────────────────────────────

def daily_report_v2(df: pd.DataFrame) -> dict:
    trades = df[df["result"].isin(("WIN", "LOSS"))]
    if trades.empty:
        return {"error": "No settled trades"}

    wins = (trades["result"] == "WIN").sum()
    losses = (trades["result"] == "LOSS").sum()
    total_trades = len(trades)
    win_rate = wins / total_trades if total_trades > 0 else 0

    total_pnl = trades["pnl"].sum()
    avg_pnl = trades["pnl"].mean()

    max_drawdown = 0.0
    if len(df) > 0:
        peak = df["bankroll"].cummax()
        drawdown = (df["bankroll"] - peak) / peak.replace(0, 1)
        max_drawdown = abs(drawdown.min())

    roi = ((df["bankroll"].iloc[-1] - INITIAL_BANK) / INITIAL_BANK) * 100 if len(df) > 0 else 0

    bets = df[df["result"].isin(("WIN", "LOSS", "NO_SIGNAL"))]
    prob_buckets = [0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0]

    calibration = {}
    for i in range(len(prob_buckets) - 1):
        lo, hi = prob_buckets[i], prob_buckets[i + 1]
        bucket = bets[(bets["our_prob"] >= lo) & (bets["our_prob"] < hi)]
        if len(bucket) > 0:
            calibration[f"{lo:.1f}-{hi:.1f}"] = {
                "count": len(bucket),
                "avg_prob": round(float(bucket["our_prob"].mean()), 3),
                "actual_rate": round(float(bucket["actual_outcome"].mean()), 3),
            }

    market_cal = {}
    for i in range(len(prob_buckets) - 1):
        lo, hi = prob_buckets[i], prob_buckets[i + 1]
        bucket = bets[(bets["market_prob"] >= lo) & (bets["market_prob"] < hi)]
        if len(bucket) > 0:
            market_cal[f"{lo:.1f}-{hi:.1f}"] = {
                "count": len(bucket),
                "avg_prob": round(float(bucket["market_prob"].mean()), 3),
                "actual_rate": round(float(bucket["actual_outcome"].mean()), 3),
            }

    by_city = {}
    for city in trades["city"].unique():
        ct = trades[trades["city"] == city]
        if len(ct) > 0:
            by_city[city] = {
                "trades": len(ct),
                "win_rate": round(float((ct["result"] == "WIN").sum() / len(ct) * 100), 1),
                "pnl": round(float(ct["pnl"].sum()), 2),
            }

    return {
        "total_trades": total_trades,
        "wins": int(wins),
        "losses": int(losses),
        "win_rate": round(float(win_rate * 100), 1),
        "total_pnl": round(float(total_pnl), 2),
        "avg_pnl_per_trade": round(float(avg_pnl), 2),
        "roi_pct": round(float(roi), 1),
        "initial_bank": INITIAL_BANK,
        "final_bank": round(float(df["bankroll"].iloc[-1]), 2) if len(df) > 0 else INITIAL_BANK,
        "max_drawdown_pct": round(float(max_drawdown * 100), 1),
        "calibration": calibration,
        "market_calibration": market_cal,
        "by_city": by_city,
    }


def print_daily_report_v2(df: pd.DataFrame):
    r = daily_report_v2(df)
    if "error" in r:
        print(f"Error: {r['error']}")
        return

    print("=" * 70)
    print("DAILY CITY TEMPERATURE BACKTEST v2 RESULTS")
    print("=" * 70)
    print(f"Total trades: {r['total_trades']}")
    print(f"Wins:  {r['wins']}")
    print(f"Losses: {r['losses']}")
    print(f"Win Rate: {r['win_rate']}%")
    print()
    print(f"Initial Bank:  ${r['initial_bank']:,.0f}")
    print(f"Final Bank:    ${r['final_bank']:,.0f}")
    print(f"Total PnL:     ${r['total_pnl']:,.2f}")
    print(f"ROI:           {r['roi_pct']}%")
    print(f"Avg PnL/Trade: ${r['avg_pnl_per_trade']:,.2f}")
    print(f"Max Drawdown:  {r['max_drawdown_pct']}%")
    print()
    print("Model calibration (our_prob vs actual):")
    for bucket, data in r.get("calibration", {}).items():
        if data["count"] > 0:
            print(f"  {bucket}: n={data['count']:4d}, prob={data['avg_prob']:.3f}, "
                  f"actual={data['actual_rate']:.3f}")
    print()
    print("Market calibration (market_prob vs actual):")
    for bucket, data in r.get("market_calibration", {}).items():
        if data["count"] > 0:
            print(f"  {bucket}: n={data['count']:4d}, prob={data['avg_prob']:.3f}, "
                  f"actual={data['actual_rate']:.3f}")
    print()
    print("By city:")
    for city, data in sorted(r.get("by_city", {}).items(), key=lambda x: x[1]["pnl"], reverse=True)[:15]:
        print(f"  {city:20s}: n={data['trades']:3d}, win={data['win_rate']:5.1f}%, pnl=${data['pnl']:8.2f}")


if __name__ == "__main__":
    df = daily_backtest_v2()
    print_daily_report_v2(df)
