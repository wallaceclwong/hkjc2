"""Backtest daily 'Highest temperature in [City]' markets.

Model: day-of-year climatology + recent anomaly persistence.
Walk-forward (no lookahead) — at each target date, only data before that
date is used for training.

Structure:
  Celsius cities: 9 markets/event — below, exact degrees, above
  Fahrenheit cities: 7 markets/event — below, 2°F ranges, above

Strategy: symmetric edge — bet YES when our prob > market, bet NO when
market prob > ours, with minimum edge and confidence thresholds.
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

RESULTS_FILE = Path(CACHE_DIR) / "daily_backtest_results.parquet"

# Climatology window: ±15 days around target day of year
CLIMO_WINDOW = 15
# Recent trend window: past N days
RECENT_WINDOW = 14
# Minimum historical observations required
MIN_HISTORICAL = 30
# T-distribution DF (fatter tails than normal)
T_DF = 5
# Minimum forecast std (degrees C or F)
MIN_STD = 2.0


# ── Climatology + trend model ──────────────────────────────────────────────────

def day_of_year(date: pd.Timestamp) -> int:
    """Day of year (1-366)."""
    return date.dayofyear


def _nearby_doy_mask(doy: int, window: int = CLIMO_WINDOW) -> np.ndarray:
    """Boolean mask for days within ±window of the given day of year."""
    doys = np.arange(1, 367)
    # Circular distance
    diff = np.minimum(np.abs(doys - doy), 366 - np.abs(doys - doy))
    return diff <= window


def _get_climatology(city_df: pd.DataFrame, target_date: pd.Timestamp) -> dict | None:
    """Compute climatological stats for a city's day of year.

    Uses all historical data BEFORE target_date.
    Returns: mean, std, n_samples for the target day ± CLIMO_WINDOW.
    """
    # Only data before target date
    hist = city_df[city_df["date"] < target_date].copy()
    if len(hist) < MIN_HISTORICAL:
        return None

    target_doy = day_of_year(target_date)
    hist["doy"] = hist["date"].apply(day_of_year)
    mask = _nearby_doy_mask(target_doy, CLIMO_WINDOW)
    nearby = hist[hist["doy"].isin(np.arange(1, 367)[mask])]

    if len(nearby) < 10:
        return None

    temps = nearby["temp_max_c"].values
    return {
        "mean": float(np.mean(temps)),
        "std": float(np.std(temps)),
        "n": len(nearby),
    }


def _get_recent_anomaly(city_df: pd.DataFrame, target_date: pd.Timestamp,
                        climo_mean: float, window: int = RECENT_WINDOW) -> dict | None:
    """Compute recent temperature anomaly relative to climatology.

    Uses the past `window` days before target_date.
    """
    start = target_date - pd.Timedelta(days=window + 1)
    recent = city_df[(city_df["date"] >= start) & (city_df["date"] < target_date)]

    if len(recent) < 3:
        return None

    # Compute anomaly for each recent day relative to its own day-of-year climo
    recent = recent.copy()
    anomalies = []
    for _, row in recent.iterrows():
        r_doy = day_of_year(row["date"])
        r_mask = _nearby_doy_mask(r_doy, CLIMO_WINDOW)
        r_climo = city_df[
            (city_df["date"] < row["date"]) &
            (city_df["date"].apply(day_of_year).isin(np.arange(1, 367)[r_mask]))
        ]["temp_max_c"]
        if len(r_climo) >= 10:
            anomalies.append(row["temp_max_c"] - r_climo.mean())
        else:
            anomalies.append(row["temp_max_c"] - climo_mean)

    if not anomalies:
        return None

    # Exponential weighting: recent days matter more
    weights = np.exp(np.linspace(-1, 0, len(anomalies)))
    weights = weights / weights.sum()
    weighted_anomaly = float(np.average(anomalies, weights=weights))

    return {
        "anomaly": weighted_anomaly,
        "n_days": len(anomalies),
        "raw_mean_anomaly": float(np.mean(anomalies)),
    }


def forecast_daily_temp(city: str, target_date: pd.Timestamp,
                        weather_df: pd.DataFrame) -> dict:
    """Forecast daily max temperature distribution for a city on a target date.

    Blends climatology mean with recent anomaly. Uses historical std scaled
    by sqrt(1 + 1/n) for prediction interval.

    Returns: mean, std, confidence, n_climo, anomaly, n_recent
    """
    city_df = weather_df[weather_df["city"] == city].copy()
    if city_df.empty:
        return {"mean": None, "std": None, "confidence": 0.0, "error": "no_data"}

    climo = _get_climatology(city_df, target_date)
    if climo is None:
        return {"mean": None, "std": None, "confidence": 0.0, "error": "insufficient_climo"}

    recent = _get_recent_anomaly(city_df, target_date, climo["mean"])

    # Forecast mean: climatology + recent anomaly
    if recent and recent["n_days"] >= 3:
        forecast_mean = climo["mean"] + recent["anomaly"]
        # Blended std: climo uncertainty + prediction uncertainty
        forecast_std = max(climo["std"] * np.sqrt(1 + 1 / climo["n"]), MIN_STD)
        anomaly = recent["anomaly"]
        n_recent = recent["n_days"]
        # Confidence increases with historical data and recent data
        confidence = min(0.85, max(0.45, 0.50 + 0.01 * min(climo["n"], 30) + 0.02 * min(n_recent, 10)))
    else:
        forecast_mean = climo["mean"]
        forecast_std = max(climo["std"] * np.sqrt(1 + 1 / climo["n"]), MIN_STD)
        anomaly = 0.0
        n_recent = 0
        confidence = min(0.75, max(0.40, 0.45 + 0.01 * min(climo["n"], 30)))

    return {
        "mean": round(float(forecast_mean), 4),
        "std": round(float(forecast_std), 4),
        "confidence": round(confidence, 4),
        "n_climo": climo["n"],
        "anomaly": round(float(anomaly), 4),
        "n_recent": n_recent,
        "climo_mean": round(float(climo["mean"]), 4),
    }


# ── Bracket probability ────────────────────────────────────────────────────────

def compute_bracket_probability_daily(forecast: dict, bracket: dict) -> float:
    """Probability the daily max temp falls in this bracket.

    Uses t-distribution (T_DF degrees of freedom) for fatter tails.

    Bracket boundary rules:
      - below: (-inf, threshold + 0.5]
      - exact (C): [val - 0.5, val + 0.5]
      - range (F): [low - 0.5, high + 0.5]
      - above: [threshold - 0.5, +inf)
    """
    mean = forecast.get("mean")
    std = forecast.get("std")
    if mean is None or std is None or std <= 0:
        return 0.02

    units = bracket.get("units", "C")
    lo = bracket["bracket_low"]
    hi = bracket["bracket_high"]
    btype = bracket.get("bracket_type", "range")

    # Convert forecast (always Celsius from Open-Meteo) to bracket units
    if units == "F":
        f_mean = mean * 9.0 / 5.0 + 32.0
        f_std = std * 9.0 / 5.0
    else:
        f_mean = mean
        f_std = std

    # Half-degree boundary for integer thresholds
    half = 0.5

    if btype == "below":
        z_hi = (hi + half - f_mean) / f_std
        prob = scipy_stats.t.cdf(z_hi, df=T_DF)
    elif btype == "above":
        z_lo = (lo - half - f_mean) / f_std
        prob = 1.0 - scipy_stats.t.cdf(z_lo, df=T_DF)
    elif btype == "exact":
        z_lo = (lo - half - f_mean) / f_std
        z_hi = (hi + half - f_mean) / f_std
        prob = scipy_stats.t.cdf(z_hi, df=T_DF) - scipy_stats.t.cdf(z_lo, df=T_DF)
    else:  # range (F use case)
        z_lo = (lo - half - f_mean) / f_std
        z_hi = (hi + half - f_mean) / f_std
        prob = scipy_stats.t.cdf(z_hi, df=T_DF) - scipy_stats.t.cdf(z_lo, df=T_DF)

    return max(0.02, min(0.98, prob))


# ── Backtest runner ────────────────────────────────────────────────────────────

def daily_backtest() -> pd.DataFrame:
    """Run daily city temperature backtest with walk-forward climatology model."""
    if RESULTS_FILE.exists():
        cached = pd.read_parquet(RESULTS_FILE)
        if len(cached) > 100:
            logger.info(f"Loading cached daily results from {RESULTS_FILE} ({len(cached)} rows)")
            return cached

    logger.info("=== Daily City Temperature Backtest (Climatology + Trend) ===")

    # Load market data
    markets_df = get_daily_market_outcomes()
    if markets_df.empty:
        logger.error("No daily market data")
        return pd.DataFrame()

    # Load weather data
    weather_df = get_all_city_temperatures()
    if weather_df.empty:
        logger.error("No weather data")
        return pd.DataFrame()

    logger.info(f"Weather: {len(weather_df)} rows, {weather_df['city'].nunique()} cities")
    logger.info(f"Markets: {len(markets_df)} rows, {markets_df['city'].nunique()} cities")

    # Pre-compute actual max temps per city+date for quick lookup
    weather_df["date_str"] = weather_df["date"].dt.strftime("%Y-%m-%d")

    # Walk-forward: iterate through unique city-date combinations sorted by date
    all_rows = []
    skipped = {"no_parse": 0, "no_price": 0, "unresolved": 0, "no_weather": 0,
               "low_confidence": 0, "no_trades": 0}

    # Get unique city-dates sorted by date
    city_dates = markets_df[["city", "target_date"]].drop_duplicates()
    city_dates = city_dates.sort_values("target_date")

    for _, cd in city_dates.iterrows():
        city = cd["city"]
        target_str = cd["target_date"]

        try:
            target_date = pd.Timestamp(target_str)
        except Exception:
            continue

        # Get all markets for this city-date
        event_markets = markets_df[
            (markets_df["city"] == city) &
            (markets_df["target_date"] == target_str)
        ]

        if event_markets.empty:
            continue

        # Only use weather data available BEFORE target_date
        available_weather = weather_df[weather_df["date"] < target_date]
        if len(available_weather) < MIN_HISTORICAL:
            skipped["no_weather"] += len(event_markets)
            continue

        # Generate forecast
        fc = forecast_daily_temp(city, target_date, weather_df)
        if fc.get("mean") is None:
            skipped["low_confidence"] += len(event_markets)
            continue

        # Get actual temperature on this date
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
            our_prob = compute_bracket_probability_daily(fc, bracket)
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
                "climo_mean": fc.get("climo_mean"),
                "recent_anomaly": fc.get("anomaly", 0),
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

    # ── Run strategy ──────────────────────────────────────────────────────────

    # Group by city-date to know bracket groups (mutually exclusive)
    groups = defaultdict(list)
    for i, row in analysis_df.iterrows():
        groups[(row["city"], row["target_date"])].append(i)

    bankroll = INITIAL_BANK
    hwm = INITIAL_BANK
    trades = []

    for (city, target_date), idx_list in sorted(groups.items()):
        group_rows = analysis_df.loc[idx_list]

        for idx in idx_list:
            row = analysis_df.loc[idx]
            our_prob = row["our_prob"]
            market_prob = row["market_prob"]
            edge = row["edge"]
            confidence = row["confidence"]
            actual = row["actual_outcome"]

            # Symmetric edge strategy
            bet_yes = edge > MIN_EDGE and confidence > MIN_CONFIDENCE
            bet_no = (-edge) > MIN_EDGE and confidence > MIN_CONFIDENCE

            if bet_yes or bet_no:
                stake = round(min(bankroll * 0.02, MAX_STAKE), 2)
                if stake <= 0:
                    continue

                if bet_yes:
                    if actual == 1:  # YES wins
                        pnl = stake * (1.0 / market_prob - 1)
                        result = "WIN"
                    else:
                        pnl = -stake
                        result = "LOSS"
                else:  # bet NO
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
                if abs(edge) <= MIN_EDGE:
                    result = "LOW_EDGE"
                else:
                    result = "LOW_CONF"

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
                "bet_direction": "YES" if bet_yes else ("NO" if bet_no else ""),
            })

    df = pd.DataFrame(trades)
    df.to_parquet(RESULTS_FILE, index=False)
    logger.success(f"Daily backtest complete: {len(df)} rows -> {RESULTS_FILE}")
    return df


# ── Reporting ─────────────────────────────────────────────────────────────────

def daily_report(df: pd.DataFrame) -> dict:
    """Generate performance metrics for daily backtest."""
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

    # By direction
    yes_trades = trades[trades["bet_direction"] == "YES"]
    no_trades = trades[trades["bet_direction"] == "NO"]

    # Calibration
    bets = df[df["result"].isin(("WIN", "LOSS", "LOW_EDGE", "LOW_CONF"))]
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

    # Market calibration
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

    # By city
    by_city = {}
    for city in trades["city"].unique():
        ct = trades[trades["city"] == city]
        if len(ct) > 0:
            by_city[city] = {
                "trades": len(ct),
                "win_rate": round(float((ct["result"] == "WIN").sum() / len(ct) * 100), 1),
                "pnl": round(float(ct["pnl"].sum()), 2),
            }

    # By units
    celsius = trades[trades["units"] == "C"]
    fahrenheit = trades[trades["units"] == "F"]

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
        "yes_trades": {"count": len(yes_trades), "win_rate": round(float((yes_trades["result"] == "WIN").sum() / len(yes_trades) * 100), 1) if len(yes_trades) > 0 else 0, "pnl": round(float(yes_trades["pnl"].sum()), 2)},
        "no_trades": {"count": len(no_trades), "win_rate": round(float((no_trades["result"] == "WIN").sum() / len(no_trades) * 100), 1) if len(no_trades) > 0 else 0, "pnl": round(float(no_trades["pnl"].sum()), 2)},
        "calibration": calibration,
        "market_calibration": market_cal,
        "by_city": by_city,
        "celsius": {"trades": len(celsius), "pnl": round(float(celsius["pnl"].sum()), 2)},
        "fahrenheit": {"trades": len(fahrenheit), "pnl": round(float(fahrenheit["pnl"].sum()), 2)},
    }


def print_daily_report(df: pd.DataFrame):
    """Pretty-print daily backtest results."""
    r = daily_report(df)
    if "error" in r:
        print(f"Error: {r['error']}")
        return

    print("=" * 70)
    print("DAILY CITY TEMPERATURE BACKTEST RESULTS")
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
    print(f"By direction:")
    print(f"  YES: n={r['yes_trades']['count']}, win={r['yes_trades']['win_rate']}%, pnl=${r['yes_trades']['pnl']:,.2f}")
    print(f"  NO:  n={r['no_trades']['count']}, win={r['no_trades']['win_rate']}%, pnl=${r['no_trades']['pnl']:,.2f}")
    print()
    print(f"By units:")
    print(f"  Celsius:    n={r['celsius']['trades']}, pnl=${r['celsius']['pnl']:,.2f}")
    print(f"  Fahrenheit: n={r['fahrenheit']['trades']}, pnl=${r['fahrenheit']['pnl']:,.2f}")
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
    for city, data in sorted(r.get("by_city", {}).items(), key=lambda x: x[1]["pnl"], reverse=True):
        print(f"  {city:20s}: n={data['trades']:3d}, win={data['win_rate']:5.1f}%, pnl=${data['pnl']:8.2f}")


if __name__ == "__main__":
    df = daily_backtest()
    print_daily_report(df)
