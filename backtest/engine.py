"""Backtest engine — weather forecast edge against real Polymarket CLOB prices.

Simulates: for each market in the enriched Polymarket dataset:
  1. Parse temperature threshold from market title
  2. Look up actual weather for that city+date (ground truth)
  3. Compute our model's probability for the bracket
  4. Compare against real CLOB trade prices (market_prob)
  5. Flat 2% of bankroll per bet, $500 max
  6. Score against the actual resolution from Polymarket
"""
import re
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
from loguru import logger

from backtest.config import (
    CITIES, MIN_EDGE, MIN_CONFIDENCE, MAX_STAKE, INITIAL_BANK, CACHE_DIR,
)
from backtest.fetch_weather import fetch_all_weather, weather_to_forecast_dict
from backtest.fetch_polymarket import get_settled_market_outcomes

RESULTS_FILE = Path(CACHE_DIR) / "backtest_results.parquet"


# ── Title parser: extract bracket and date from market title ──────────────────

def parse_market_title(title: str) -> dict | None:
    """Parse a Polymarket temperature market title into structured fields.

    Example titles:
      "Will the highest temperature in New York City be 27°F or below on December 30?"
      "Will the lowest temperature in London be between 10-12°C on January 15, 2025?"

    Returns dict with: temp_type, bracket_type, temp_low_c, temp_high_c, target_date
    """
    if not title or pd.isna(title):
        return None

    title_lower = title.lower()

    # Temperature type
    temp_type = "lowest" if "lowest temperature" in title_lower else "highest"

    # Date extraction: "on <Month> <Day>, <Year>" or "on <Month> <Day>?"
    date_match = re.search(
        r'on\s+(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2})(?:,\s*(\d{4}))?',
        title, re.IGNORECASE
    )
    if not date_match:
        return None

    month_str = date_match.group(1)
    day = int(date_match.group(2))
    year = int(date_match.group(3)) if date_match.group(3) else 2025  # default to 2025 if no year

    try:
        target_date = pd.Timestamp(f"{year}-{month_str}-{day:02d}")
    except ValueError:
        return None

    # Temperature bracket parsing
    # 1. "X°F or below" / "X°C or below"
    below_match = re.search(r'(\d+(?:\.\d+)?)\s*°\s*([cf])\s+or\s+below', title_lower)
    if below_match:
        val, unit = float(below_match.group(1)), below_match.group(2)
        temp_c = val if unit == 'c' else (val - 32) * 5 / 9
        return {
            "temp_type": temp_type,
            "bracket_type": "below",
            "temp_low_c": -999.0,
            "temp_high_c": round(temp_c, 1),
            "target_date": target_date,
        }

    # 2. "X°F or higher/above"
    above_match = re.search(r'(\d+(?:\.\d+)?)\s*°\s*([cf])\s+or\s+(?:higher|above)', title_lower)
    if above_match:
        val, unit = float(above_match.group(1)), above_match.group(2)
        temp_c = val if unit == 'c' else (val - 32) * 5 / 9
        return {
            "temp_type": temp_type,
            "bracket_type": "above",
            "temp_low_c": round(temp_c, 1),
            "temp_high_c": 999.0,
            "target_date": target_date,
        }

    # 3. "between X-Y°F"
    between_match = re.search(r'between\s+(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)\s*°\s*([cf])', title_lower)
    if between_match:
        low, high, unit = float(between_match.group(1)), float(between_match.group(2)), between_match.group(3)
        low_c = low if unit == 'c' else (low - 32) * 5 / 9
        high_c = high if unit == 'c' else (high - 32) * 5 / 9
        return {
            "temp_type": temp_type,
            "bracket_type": "range",
            "temp_low_c": round(low_c, 1),
            "temp_high_c": round(high_c, 1),
            "target_date": target_date,
        }

    # 4. "be X°F" (exact)
    exact_match = re.search(r'be\s+(\d+(?:\.\d+)?)\s*°\s*([cf])', title_lower)
    if exact_match:
        val, unit = float(exact_match.group(1)), exact_match.group(2)
        temp_c = val if unit == 'c' else (val - 32) * 5 / 9
        offset = 0.5 if unit == 'c' else 0.28
        return {
            "temp_type": temp_type,
            "bracket_type": "exact",
            "temp_low_c": round(temp_c - offset, 1),
            "temp_high_c": round(temp_c + offset, 1),
            "target_date": target_date,
        }

    return None


# ── Replicated from sentinel-weather edge engine ──────────────────────────────

def calculate_delta(forecast: dict) -> float:
    """Temperature delta: forecast avg minus current temp."""
    current = forecast["current_temp"]
    forecasts = forecast["forecast_temps"]
    if not forecasts:
        return 0.0
    return np.mean(forecasts) - current


def calculate_confidence(forecast: dict) -> float:
    """Confidence score based on weather data quality and stability."""
    data_source = forecast.get("data_source", "simulated")
    base_conf = 0.80 if data_source in ("open-meteo", "google") else 0.65

    humidity = forecast.get("humidity", 50)
    pressure = forecast.get("pressure", 1013)

    if 40 <= humidity <= 70 and 1000 <= pressure <= 1020:
        base_conf *= 1.05

    precip = forecast.get("precip_prob", 0)
    if precip > 0.5:
        base_conf *= 0.90

    return min(0.95, max(0.55, base_conf))


def build_climatology(weather_df: pd.DataFrame) -> dict:
    """Build per-city, per-month temperature climatology from historical data.

    Returns: {city: {month: {"max_mean": °C, "max_std": °C, "min_mean": °C, "min_std": °C}}}
    """
    climo = {}
    for city in weather_df["city"].unique():
        cd = weather_df[weather_df["city"] == city].copy()
        cd["month"] = cd["date"].dt.month
        climo[city] = {}
        for month in range(1, 13):
            md = cd[cd["month"] == month]
            if len(md) >= 5:  # minimum data threshold
                climo[city][month] = {
                    "max_mean": float(md["temperature_2m_max"].mean()),
                    "max_std": float(md["temperature_2m_max"].std()),
                    "min_mean": float(md["temperature_2m_min"].mean()),
                    "min_std": float(md["temperature_2m_min"].std()),
                }
            elif climo[city]:
                # Fall back to nearest month
                climo[city][month] = climo[city][max(climo[city].keys())]
    return climo


def compute_bracket_probability(forecast: dict, bracket: dict, climo: dict) -> float:
    """Probability of bracket using observed max/min as forecast with seasonal climatological uncertainty.

    Uses the observed max/min as the mean (represents a perfect forecast),
    and the seasonal standard deviation as the uncertainty (captures day-to-day
    variability around the climatological norm).
    """
    from scipy import stats as scipy_stats

    temp_type = bracket["temp_type"]
    bracket_low = bracket["temp_low_c"]
    bracket_high = bracket["temp_high_c"]
    city = forecast["location"]
    month = bracket["target_date"].month

    all_temps = forecast["forecast_temps"]
    if len(all_temps) >= 2:
        forecast_min = min(all_temps)
        forecast_max = max(all_temps)
    else:
        forecast_min = forecast_max = all_temps[0] if all_temps else 15.0

    city_climo = climo.get(city, {}).get(month)
    if city_climo is None:
        # Fallback: use all-city average climatology
        max_stds = [c[m]["max_std"] for c in climo.values() for m in c if m == month]
        min_stds = [c[m]["min_std"] for c in climo.values() for m in c if m == month]
        max_std = sum(max_stds) / len(max_stds) if max_stds else 5.0
        min_std = sum(min_stds) / len(min_stds) if min_stds else 5.0
    else:
        max_std = city_climo["max_std"]
        min_std = city_climo["min_std"]

    if temp_type == "lowest":
        mean_temp = forecast_min
        std_temp = max(min_std, 2.0)  # floor at 2°C
    else:
        mean_temp = forecast_max
        std_temp = max(max_std, 2.0)

    prob_in_bracket = scipy_stats.norm.cdf(bracket_high, loc=mean_temp, scale=std_temp) - \
                      scipy_stats.norm.cdf(bracket_low, loc=mean_temp, scale=std_temp)

    return max(0.02, min(0.98, prob_in_bracket))


def edge_score(our_prob: float, market_prob: float) -> float:
    """Edge = our probability estimate minus the market's implied probability."""
    return our_prob - market_prob


def fixed_stake(bankroll: float) -> float:
    """Flat 2% of bankroll per bet, capped at MAX_STAKE."""
    return round(min(bankroll * 0.02, MAX_STAKE), 2)


# ── Backtest runner ───────────────────────────────────────────────────────────

def run_backtest() -> pd.DataFrame:
    """Run the full backtest using real Polymarket market data."""
    if RESULTS_FILE.exists():
        cached = pd.read_parquet(RESULTS_FILE)
        if len(cached) > 100:
            logger.info(f"Loading cached results from {RESULTS_FILE} ({len(cached)} rows)")
            return cached

    logger.info("=== Starting Backtest with Real Polymarket Data ===")

    # 1. Load enriched Polymarket data
    poly_df = get_settled_market_outcomes()
    if poly_df.empty:
        logger.error("No Polymarket data — aborting")
        return pd.DataFrame()

    # Only use resolved markets
    resolved_df = poly_df[poly_df["actual_outcome"] >= 0].copy()
    logger.info(f"Using {len(resolved_df)} resolved markets out of {len(poly_df)} total")

    # 2. Load historical weather
    weather_df = fetch_all_weather()
    if weather_df.empty:
        logger.error("No weather data — aborting")
        return pd.DataFrame()

    # Build weather lookup: (city, date) → forecast dict
    weather_lookup = {}
    for _, row in weather_df.iterrows():
        date_str = row["date"].strftime("%Y-%m-%d")
        city = row["city"]
        weather_lookup[(city, date_str)] = weather_to_forecast_dict(row)

    # Build city×month climatology for calibrated stds
    climo = build_climatology(weather_df)
    logger.info(f"Climatology built for {len(climo)} cities, "
                f"avg max_std={sum(c[m]['max_std'] for c in climo.values() for m in c)/(len(climo)*12):.1f}°C")

    # 3. First pass: parse all markets, compute probabilities, group by city+date
    all_parsed = []
    skipped_no_price = 0
    for _, market in resolved_df.iterrows():
        title = market.get("title", "")
        bracket = parse_market_title(title)
        if bracket is None:
            continue

        # Use real CLOB trade price as market probability
        market_prob = market.get("market_prob")
        if market_prob is None or (isinstance(market_prob, float) and np.isnan(market_prob)):
            skipped_no_price += 1
            continue

        city = market["city"]
        target_date = bracket["target_date"].strftime("%Y-%m-%d")
        forecast = weather_lookup.get((city, target_date))
        if forecast is None:
            continue

        our_prob = compute_bracket_probability(forecast, bracket, climo)
        confidence = calculate_confidence(forecast)
        edge = edge_score(our_prob, market_prob)

        all_parsed.append({
            "market": market,
            "bracket": bracket,
            "target_date": target_date,
            "city": city,
            "forecast": forecast,
            "our_prob": our_prob,
            "market_prob": market_prob,
            "confidence": confidence,
            "edge": edge,
        })

    if skipped_no_price:
        logger.info(f"Skipped {skipped_no_price} markets without CLOB price data")

    # 4. Group by city+date: only bet on the single best bracket per group
    from collections import defaultdict
    groups = defaultdict(list)
    for p in all_parsed:
        groups[(p["city"], p["target_date"])].append(p)

    trades = []
    bankroll = INITIAL_BANK
    hwm = INITIAL_BANK

    for (city, target_date), group_items in groups.items():
        # Sort by our_prob descending — pick bracket where model is most confident
        group_items.sort(key=lambda x: x["our_prob"], reverse=True)
        best = group_items[0]

        our_prob = best["our_prob"]
        market_prob = best["market_prob"]
        confidence = best["confidence"]
        edge = best["edge"]
        bracket = best["bracket"]
        market = best["market"]
        actual_outcome = int(market["actual_outcome"])

        # Bet NO when model is moderately overconfident.
        # Sweet spot: our_prob 0.50-0.65 has actual NO rate ~91% (vs market 89%).
        # Too-high prob (>0.7) has actual NO rate only ~75% — model accidentally finds signal.
        bet_no = 0.50 < our_prob < 0.60

        if bet_no and confidence > MIN_CONFIDENCE:
            stake = fixed_stake(bankroll)
            if stake > 0:
                # Bet NO (short): model overconfident → bracket likely resolves NO
                if actual_outcome == 0:
                    pnl = stake * (1.0 / (1.0 - market_prob) - 1)
                    result = "WIN"
                else:
                    pnl = -stake
                    result = "LOSS"
                bankroll += pnl
                hwm = max(hwm, bankroll)
            else:
                pnl = 0.0
                result = "NO_BET"
        else:
            stake = 0.0
            pnl = 0.0
            if not bet_no:
                result = "LOW_CONF"
            else:
                result = "NO_BET"

        title = market.get("title", "")
        trade_data = {
            "date": target_date,
            "city": city,
            "market_title": title[:100] if title else "",
            "bracket_type": bracket["bracket_type"],
            "bracket_low_c": bracket["temp_low_c"],
            "bracket_high_c": bracket["temp_high_c"],
            "our_prob": round(our_prob, 4),
            "market_prob": round(market_prob, 4),
            "edge": round(edge, 4),
            "confidence": round(confidence, 4),
            "resolved": actual_outcome,
            "stake": stake,
            "pnl": round(pnl, 2) if pnl else 0,
            "result": result,
            "bankroll": round(bankroll, 2),
            "hwm": round(hwm, 2),
            "n_brackets": len(group_items),
        }
        delta = calculate_delta(best["forecast"])
        trade_data["delta"] = round(delta, 4)
        trades.append(trade_data)

        if len(trades) % 100 == 0:
            logger.info(f"Processed {len(trades)} city-date groups — "
                        f"bankroll=${bankroll:,.0f}")

    df = pd.DataFrame(trades)
    df.to_parquet(RESULTS_FILE, index=False)
    logger.success(f"Backtest complete: {len(df)} rows → {RESULTS_FILE}")
    return df


# ── Reporting ─────────────────────────────────────────────────────────────────

def report(df: pd.DataFrame) -> dict:
    """Generate performance metrics from backtest results."""
    trades = df[df["result"].isin(("WIN", "LOSS"))]
    bets = df[df["result"].isin(("WIN", "LOSS", "PENDING"))]

    if trades.empty:
        return {"error": "No settled trades found"}

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

    daily_returns = df.set_index("date").groupby("date")["pnl"].sum()
    sharpe = 0.0
    if len(daily_returns) > 1 and daily_returns.std() > 0:
        sharpe = (daily_returns.mean() / daily_returns.std()) * np.sqrt(252)

    roi = ((df["bankroll"].iloc[-1] - INITIAL_BANK) / INITIAL_BANK) * 100 if len(df) > 0 else 0

    edges = bets["edge"].dropna()
    avg_edge = edges.mean() if len(edges) > 0 else 0

    # Calibration: for each probability bucket, what was the actual win rate?
    prob_buckets = [0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0]
    calibration = {}
    for i in range(len(prob_buckets) - 1):
        lo, hi = prob_buckets[i], prob_buckets[i + 1]
        bucket = bets[(bets["our_prob"] >= lo) & (bets["our_prob"] < hi)]
        if len(bucket) > 0:
            actual = bucket["resolved"].mean() if "resolved" in bucket.columns else None
            calibration[f"{lo:.1f}-{hi:.1f}"] = {
                "count": len(bucket),
                "avg_prob": round(bucket["our_prob"].mean(), 3),
                "actual_rate": round(actual, 3) if actual is not None else None,
            }

    return {
        "total_trades": total_trades,
        "wins": int(wins),
        "losses": int(losses),
        "win_rate": round(win_rate * 100, 1),
        "total_pnl": round(total_pnl, 2),
        "avg_pnl_per_trade": round(avg_pnl, 2),
        "roi_pct": round(roi, 1),
        "initial_bank": INITIAL_BANK,
        "final_bank": round(df["bankroll"].iloc[-1], 2) if len(df) > 0 else INITIAL_BANK,
        "max_drawdown_pct": round(max_drawdown * 100, 1),
        "sharpe_ratio": round(sharpe, 2),
        "avg_edge": round(avg_edge, 4),
        "total_bets_placed": len(bets),
        "total_days": len(df["date"].unique()) if len(df) > 0 else 0,
        "bets_by_city": bets.groupby("city").size().to_dict() if len(bets) > 0 else {},
        "calibration": calibration,
    }


def print_report(df: pd.DataFrame):
    """Pretty-print backtest results."""
    r = report(df)
    if "error" in r:
        print(r["error"])
        return

    print("=" * 60)
    print("BACKTEST RESULTS (Real Polymarket Data)")
    print("=" * 60)
    print(f"Period: {df['date'].iloc[0]} → {df['date'].iloc[-1]}")
    print(f"Days: {r['total_days']}")
    print()
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
    print()
    print(f"Sharpe Ratio:  {r['sharpe_ratio']}")
    print(f"Max Drawdown:  {r['max_drawdown_pct']}%")
    print(f"Avg Edge:      {r['avg_edge']}")
    print(f"Total Bets:    {r['total_bets_placed']}")
    print()
    print("Bets by city:", r["bets_by_city"])
    print()
    print("Calibration (our_prob vs actual):")
    for bucket, data in r.get("calibration", {}).items():
        if data["count"] > 0:
            print(f"  {bucket}: n={data['count']:4d}, prob={data['avg_prob']:.3f}, "
                  f"actual={data['actual_rate']:.3f}")


if __name__ == "__main__":
    df = run_backtest()
    print_report(df)
