"""Fetch settled daily 'Highest temperature in [City]' markets via Gamma API + CLOB.

Two bracket structures:
  Celsius (international): "X°C or below", "Y°C" (exact), "Z°C or higher" — 9 markets/event
  Fahrenheit (US): "X°F or below", "between Y-Z°F", "W°F or higher" — 7 markets/event
"""
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import pandas as pd
import requests
from loguru import logger

sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

from backtest.config import CACHE_DIR

GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"

CACHE_FILE = Path(CACHE_DIR) / "daily_temperature_markets.parquet"
ENRICHED_FILE = Path(CACHE_DIR) / "daily_temperature_enriched.parquet"
PRICES_FILE = Path(CACHE_DIR) / "daily_temperature_prices.parquet"

# City name → (lat, lon, units) — Celsius unless F noted
CITY_COORDS = {
    "London": (51.5074, -0.1278, "C"),
    "Paris": (48.8566, 2.3522, "C"),
    "Tokyo": (35.6762, 139.6503, "C"),
    "Seoul": (37.5665, 126.9780, "C"),
    "Sao Paulo": (-23.5505, -46.6333, "C"),
    "Buenos Aires": (-34.6037, -58.3816, "C"),
    "Toronto": (43.6532, -79.3832, "C"),
    "Ankara": (39.9334, 32.8597, "C"),
    "Wellington": (-41.2865, 174.7762, "C"),
    "Zhengzhou": (34.7473, 113.6253, "C"),
    "Guangzhou": (23.1291, 113.2644, "C"),
    "Hong Kong": (22.3193, 114.1694, "C"),
    "Shanghai": (31.2304, 121.4737, "C"),
    "Singapore": (1.3521, 103.8198, "C"),
    "Milan": (45.4642, 9.1900, "C"),
    "Madrid": (40.4168, -3.7038, "C"),
    "Warsaw": (52.2297, 21.0122, "C"),
    "Taipei": (25.0330, 121.5654, "C"),
    "Chongqing": (29.4316, 106.9123, "C"),
    "Beijing": (39.9042, 116.4074, "C"),
    "Wuhan": (30.5928, 114.3055, "C"),
    "Chengdu": (30.5728, 104.0668, "C"),
    "Shenzhen": (22.5431, 114.0579, "C"),
    "Moscow": (55.7558, 37.6173, "C"),
    "Istanbul": (41.0082, 28.9784, "C"),
    "Jinan": (36.6512, 117.1201, "C"),
    "Mexico City": (19.4326, -99.1332, "C"),
    "Busan": (35.1796, 129.0756, "C"),
    "Amsterdam": (52.3676, 4.9041, "C"),
    "Helsinki": (60.1699, 24.9384, "C"),
    "Panama City": (8.9824, -79.5199, "C"),
    "Qingdao": (36.0671, 120.3826, "C"),
    "Kuala Lumpur": (3.1390, 101.6869, "C"),
    "Jakarta": (-6.2088, 106.8456, "C"),
    "Jeddah": (21.5433, 39.1728, "C"),
    "Karachi": (24.8607, 67.0011, "C"),
    "Lagos": (6.5244, 3.3792, "C"),
    "Cape Town": (-33.9249, 18.4241, "C"),
    "Lucknow": (26.8467, 80.9462, "C"),
    "Manila": (14.5995, 120.9842, "C"),
    "Munich": (48.1351, 11.5820, "C"),
    "Tel Aviv": (32.0853, 34.7818, "C"),
    "Los Angeles": (34.0522, -118.2437, "F"),
    "New York City": (40.7128, -74.0060, "F"),
    "NYC": (40.7128, -74.0060, "F"),
    "Chicago": (41.8781, -87.6298, "F"),
    "Dallas": (32.7767, -96.7970, "F"),
    "Atlanta": (33.7490, -84.3880, "F"),
    "Miami": (25.7617, -80.1918, "F"),
    "Seattle": (47.6062, -122.3321, "F"),
    "Austin": (30.2672, -97.7431, "F"),
    "Denver": (39.7392, -104.9903, "F"),
    "Houston": (29.7604, -95.3698, "F"),
    "San Francisco": (37.7749, -122.4194, "F"),
}

MONTH_NAMES = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]


def _safe_get(url: str, params: dict = None, retries: int = 3) -> dict | list | None:
    for attempt in range(retries):
        try:
            resp = requests.get(url, params=params, timeout=15)
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 429:
                wait = 2 ** attempt
                logger.warning(f"Rate limited, waiting {wait}s...")
                time.sleep(wait)
                continue
            logger.warning(f"{url[:80]} -> {resp.status_code}")
            return None
        except Exception as e:
            logger.warning(f"Request failed (attempt {attempt + 1}): {e}")
            time.sleep(1)
    return None


def parse_daily_title(question: str, event_title: str = "") -> dict | None:
    """Parse a daily temperature bracket title into structured data.

    Returns dict with: city, date, bracket_low, bracket_high, units, bracket_type
    or None if unparseable.
    """
    if not question or pd.isna(question):
        return None

    text = question.strip()

    # Extract date: "Month Day" or "Month Day?" or "Month Day, Year"
    date_str = None
    year = None
    m = re.search(r'on\s+(' + '|'.join(MONTH_NAMES) + r')\s+(\d{1,2})(?:,\s*(\d{4}))?', text)
    if m:
        month_name = m.group(1)
        day = int(m.group(2))
        month = MONTH_NAMES.index(month_name) + 1
        year = int(m.group(3)) if m.group(3) else None
        date_str = f"{month_name} {day}"

    if date_str is None:
        return None

    # Determine units from the title
    units = "F" if "°F" in text else "C"

    # Extract city from the question
    city = None
    m = re.search(r'in\s+(.+?)\s+(?:be\s+|on\s+)', text)
    if m:
        city = m.group(1).strip()

        # Also try from event_title if city extraction is weak
    if city is None and event_title:
        m = re.search(r'in\s+(.+?)\s+on\s+', event_title)
        if m:
            city = m.group(1).strip()

    if city is None:
        return None

    # Normalize city names
    if city == "New York City":
        city = "NYC"

    # Parse bracket bounds
    low_val = None
    high_val = None
    bracket_type = "exact"

    # "X°C or below" / "X°F or below"
    m = re.search(r'be\s+(\d+(?:\.\d+)?)\s*°\s*[CF]\s+or\s+below', text)
    if m:
        low_val = -999.0
        high_val = float(m.group(1))
        bracket_type = "below"
        return _build_result(city, month, day, year, date_str, low_val, high_val, units, bracket_type)

    # "X°C or higher" / "X°F or higher"
    m = re.search(r'be\s+(\d+(?:\.\d+)?)\s*°\s*[CF]\s+or\s+higher', text)
    if m:
        low_val = float(m.group(1))
        high_val = 999.0
        bracket_type = "above"
        return _build_result(city, month, day, year, date_str, low_val, high_val, units, bracket_type)

    # "between X-Y°F" (Fahrenheit range brackets)
    m = re.search(r'between\s+(\d+(?:\.\d+)?)\s*[-–—]\s*(\d+(?:\.\d+)?)\s*°\s*F', text)
    if m:
        low_val = float(m.group(1))
        high_val = float(m.group(2))
        bracket_type = "range"
        return _build_result(city, month, day, year, date_str, low_val, high_val, units, bracket_type)

    # "between X and Y°F"
    m = re.search(r'between\s+(\d+(?:\.\d+)?)\s*°\s*F?\s*and\s+(\d+(?:\.\d+)?)\s*°\s*F', text)
    if m:
        low_val = float(m.group(1))
        high_val = float(m.group(2))
        bracket_type = "range"
        return _build_result(city, month, day, year, date_str, low_val, high_val, units, bracket_type)

    # "X°C" (exact single degree — Celsius only)
    m = re.search(r'be\s+(\d+(?:\.\d+)?)\s*°\s*C\b(?!\s+or\s+(?:below|higher))', text)
    if m:
        val = float(m.group(1))
        low_val = val
        high_val = val
        bracket_type = "exact"
        return _build_result(city, month, day, year, date_str, low_val, high_val, units, bracket_type)

    return None


def _build_result(city, month, day, year, date_str, low_val, high_val, units, bracket_type):
    return {
        "city": city,
        "month": month,
        "day": day,
        "year": year,
        "date_str": date_str,
        "bracket_low": low_val,
        "bracket_high": high_val,
        "units": units,
        "bracket_type": bracket_type,
        "bracket_mid": (low_val + high_val) / 2 if low_val > -900 and high_val < 900
        else (high_val if low_val <= -900 else low_val),
    }


def fetch_settled_daily_markets() -> pd.DataFrame:
    """Fetch all settled daily 'Highest temperature in [City]' markets."""
    if CACHE_FILE.exists():
        logger.info(f"Loading cached daily markets from {CACHE_FILE}")
        return pd.read_parquet(CACHE_FILE)

    all_markets = []
    seen_ids = set()

    for tag in ["daily-temperature", "temperature"]:
        offset = 0
        while True:
            events = _safe_get(f"{GAMMA_BASE}/events", {
                "tag_slug": tag,
                "active": "false",
                "closed": "true",
                "limit": 100,
                "offset": offset,
            })
            if not events or not isinstance(events, list) or len(events) == 0:
                break

            for event in events:
                title = event.get("title", "")
                if "ighest temperature" not in title and "emperature" not in title:
                    continue
                if "on " not in title:
                    continue

                for m in event.get("markets", []):
                    mid = m.get("id", "")
                    if mid in seen_ids:
                        continue
                    seen_ids.add(mid)

                    question = m.get("question") or m.get("title", "")
                    parsed = parse_daily_title(question, title)
                    if parsed is None:
                        continue

                    all_markets.append({
                        "market_id": mid,
                        "condition_id": m.get("conditionId", ""),
                        "title": question,
                        "event_title": title,
                        "city": parsed["city"],
                        "target_date": f"{parsed['year'] or 0}-{parsed['month']:02d}-{parsed['day']:02d}",
                        "target_month": parsed["month"],
                        "target_day": parsed["day"],
                        "target_year": parsed["year"],
                        "bracket_low": parsed["bracket_low"],
                        "bracket_high": parsed["bracket_high"],
                        "bracket_mid": parsed["bracket_mid"],
                        "bracket_type": parsed["bracket_type"],
                        "units": parsed["units"],
                        "end_date": event.get("endDate", ""),
                        "volume": float(m.get("volume", 0) or 0),
                    })

            offset += 100
            if len(events) < 100:
                break
            time.sleep(0.3)

    df = pd.DataFrame(all_markets)
    if not df.empty:
        # Infer year from end_date if missing
        missing_year = df["target_year"].isna() | (df["target_year"] == 0)
        if missing_year.any():
            df.loc[missing_year, "target_year"] = df.loc[missing_year, "end_date"].apply(
                lambda x: int(x[:4]) if isinstance(x, str) and len(x) >= 4 else None
            )
            df.loc[missing_year, "target_date"] = df.loc[missing_year].apply(
                lambda r: f"{int(r['target_year'])}-{int(r['target_month']):02d}-{int(r['target_day']):02d}"
                if r["target_year"] and r["target_year"] > 0 else r["target_date"], axis=1
            )

        df.to_parquet(CACHE_FILE, index=False)
        n_cities = df["city"].nunique()
        n_events = df["event_title"].nunique()
        logger.success(f"Saved {len(df)} daily markets ({n_events} events, {n_cities} cities) -> {CACHE_FILE}")
    return df


# ── Resolution enrichment ──────────────────────────────────────────────────────

def _enrich_one(mid: str) -> dict:
    """Fetch resolution + token IDs for a single market."""
    market = _safe_get(f"{GAMMA_BASE}/markets/{mid}")
    if not market or not isinstance(market, dict):
        return {
            "market_id": mid,
            "yes_price": None,
            "actual_outcome": -1,
            "yes_token_id": "",
            "no_token_id": "",
        }

    prices = market.get("outcomePrices", [])
    if isinstance(prices, str):
        try:
            prices = json.loads(prices)
        except Exception:
            prices = []

    yes_price = float(prices[0]) if len(prices) > 0 else None

    outcome = -1
    if len(prices) >= 2:
        if prices[0] == "1":
            outcome = 1
        elif prices[1] == "1":
            outcome = 0

    tokens = market.get("clobTokenIds", [])
    if isinstance(tokens, str):
        try:
            tokens = json.loads(tokens)
        except Exception:
            tokens = []

    return {
        "market_id": mid,
        "yes_price": yes_price,
        "actual_outcome": outcome,
        "yes_token_id": tokens[0] if len(tokens) > 0 else "",
        "no_token_id": tokens[1] if len(tokens) > 1 else "",
    }


def enrich_daily_resolutions(df: pd.DataFrame) -> pd.DataFrame:
    """Query each market individually to get resolution and token IDs (parallel)."""
    if ENRICHED_FILE.exists():
        cached = pd.read_parquet(ENRICHED_FILE)
        if "yes_token_id" in cached.columns and cached["yes_token_id"].notna().sum() > 0:
            logger.info(f"Loading enriched daily data from {ENRICHED_FILE}")
            return cached

    if df.empty:
        return df

    market_ids = df["market_id"].tolist()
    total = len(market_ids)
    logger.info(f"Enriching {total} daily markets with 15 workers...")

    results = []
    batch_start = time.time()

    with ThreadPoolExecutor(max_workers=15) as executor:
        futures = {executor.submit(_enrich_one, mid): mid for mid in market_ids}
        for i, future in enumerate(as_completed(futures), 1):
            results.append(future.result())
            if i % 500 == 0 or i == total:
                elapsed = time.time() - batch_start
                rate = i / elapsed if elapsed > 0 else 0
                eta = (total - i) / rate if rate > 0 else 0
                logger.info(f"Enriching {i}/{total} ({rate:.1f}/s) — ETA {eta:.0f}s")

    enrich_df = pd.DataFrame(results)
    merged = df.merge(enrich_df, on="market_id", how="left")
    merged.to_parquet(ENRICHED_FILE, index=False)

    resolved = merged[merged["actual_outcome"] >= 0]
    logger.success(
        f"Enriched {len(merged)} daily markets: {len(resolved)} resolved "
        f"(YES={len(resolved[resolved['actual_outcome']==1])}, "
        f"NO={len(resolved[resolved['actual_outcome']==0])})"
    )
    return merged


# ── CLOB prices ─────────────────────────────────────────────────────────────────

def _fetch_one_clob(mid: str, yes_token: str, end_date: str) -> dict:
    """Fetch CLOB price history for a single market's YES token."""
    if not yes_token or pd.isna(yes_token) or yes_token == "":
        return {"market_id": mid, "market_prob": None, "num_trades": 0}

    try:
        end_dt = pd.to_datetime(end_date)
    except Exception:
        return {"market_id": mid, "market_prob": None, "num_trades": 0}

    # Daily markets: use 7 days of price data, cutoff 6 hours before close (noon)
    start_ts = int((end_dt - pd.Timedelta(days=7)).timestamp())
    end_ts = int(end_dt.timestamp())
    cutoff_ts = int((end_dt - pd.Timedelta(hours=6)).timestamp())

    data = _safe_get(f"{CLOB_BASE}/prices-history", {
        "market": yes_token,
        "interval": "max",
        "startTs": start_ts,
        "endTs": end_ts,
    })
    if not data:
        return {"market_id": mid, "market_prob": None, "num_trades": 0}

    history = data.get("history", [])
    if not history:
        return {"market_id": mid, "market_prob": None, "num_trades": 0}

    pre_cutoff = [h for h in history if h.get("t", 0) and h["t"] < cutoff_ts]
    trades = pre_cutoff if pre_cutoff else history

    prices = [float(h["p"]) for h in trades]
    avg_price = sum(prices) / len(prices) if prices else None
    market_prob = max(0.02, avg_price) if avg_price is not None else None

    return {
        "market_id": mid,
        "market_prob": round(market_prob, 4) if market_prob else None,
        "num_trades": len(trades),
    }


def fetch_daily_clob_prices(enriched_df: pd.DataFrame) -> pd.DataFrame:
    """Fetch real CLOB trade prices for each market's YES token (parallel)."""
    if PRICES_FILE.exists():
        logger.info(f"Loading cached daily CLOB prices from {PRICES_FILE}")
        return enriched_df.merge(
            pd.read_parquet(PRICES_FILE), on="market_id", how="left"
        )

    if enriched_df.empty:
        return enriched_df

    total = len(enriched_df)
    logger.info(f"Fetching CLOB prices for {total} daily markets with 12 workers...")
    batch_start = time.time()

    tasks = []
    for _, row in enriched_df.iterrows():
        mid = row["market_id"]
        yes_token = row.get("yes_token_id", "")
        end_date = row.get("end_date", "")
        tasks.append((mid, yes_token, end_date))

    price_rows = []
    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = {executor.submit(_fetch_one_clob, mid, token, end_dt): mid
                   for mid, token, end_dt in tasks}
        for i, future in enumerate(as_completed(futures), 1):
            price_rows.append(future.result())
            if i % 500 == 0 or i == total:
                elapsed = time.time() - batch_start
                rate = i / elapsed if elapsed > 0 else 0
                eta = (total - i) / rate if rate > 0 else 0
                with_prices = sum(1 for r in price_rows if r.get("market_prob") is not None)
                logger.info(f"CLOB {i}/{total} ({rate:.1f}/s) — {with_prices} w/ prices, ETA {eta:.0f}s")

    prices_df = pd.DataFrame(price_rows)
    prices_df.to_parquet(PRICES_FILE, index=False)

    with_prices = prices_df["market_prob"].notna().sum()
    logger.success(f"CLOB prices: {with_prices}/{len(prices_df)} daily markets have trade data")
    return enriched_df.merge(prices_df, on="market_id", how="left")


def get_daily_market_outcomes() -> pd.DataFrame:
    """Build the full daily dataset: markets + resolutions + CLOB prices."""
    Path(CACHE_DIR).mkdir(parents=True, exist_ok=True)

    markets_df = fetch_settled_daily_markets()
    if markets_df.empty:
        logger.warning("No daily temperature markets found")
        return markets_df

    enriched = enrich_daily_resolutions(markets_df)
    with_prices = fetch_daily_clob_prices(enriched)

    has_prices = with_prices[with_prices["market_prob"].notna()]
    resolved = has_prices[has_prices["actual_outcome"] >= 0]

    logger.info(
        f"Daily dataset: {len(resolved)} markets with prices + resolution\n"
        f"  Resolved YES: {(resolved['actual_outcome'] == 1).sum()}\n"
        f"  Resolved NO:  {(resolved['actual_outcome'] == 0).sum()}\n"
        f"  Cities: {resolved['city'].nunique()}\n"
        f"  Avg market_prob: {resolved['market_prob'].mean():.4f}"
    )
    return resolved


if __name__ == "__main__":
    Path(CACHE_DIR).mkdir(parents=True, exist_ok=True)
    df = get_daily_market_outcomes()
    if not df.empty:
        print(df[["city", "target_date", "bracket_low", "bracket_high", "units",
                   "market_prob", "actual_outcome", "num_trades"]].head(30))
        print(f"\nTotal: {len(df)} markets, {df['city'].nunique()} cities")
        print(f"Date range: {df['target_date'].min()} to {df['target_date'].max()}")
