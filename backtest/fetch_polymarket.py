"""Fetch historical Polymarket weather markets via Gamma API + CLOB prices.

Three-phase approach:
  1. Discover markets via /events?tag_slug=daily-temperature (cached)
  2. Enrich each market with resolution + token IDs via /markets/{id} (cached)
  3. Fetch CLOB price history for real market probabilities (cached)
"""
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd
import requests
from loguru import logger

# Force unbuffered output in background tasks
sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

from backtest.config import CITIES, CACHE_DIR

GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"

CACHE_FILE = Path(CACHE_DIR) / "polymarket_weather_markets.parquet"
ENRICHED_FILE = Path(CACHE_DIR) / "polymarket_enriched.parquet"
PRICES_FILE = Path(CACHE_DIR) / "polymarket_prices.parquet"


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
            logger.warning(f"{url[:80]} → {resp.status_code}")
            return None
        except Exception as e:
            logger.warning(f"Request failed (attempt {attempt + 1}): {e}")
            time.sleep(1)
    return None


def fetch_settled_weather_markets() -> pd.DataFrame:
    """Fetch all closed/settled temperature markets via Gamma events endpoint."""
    if CACHE_FILE.exists():
        logger.info(f"Loading cached markets from {CACHE_FILE}")
        return pd.read_parquet(CACHE_FILE)

    all_markets = []
    seen_ids = set()
    tag_slugs = ["daily-temperature", "temperature"]

    for tag in tag_slugs:
        offset = 0
        while True:
            params = {
                "tag_slug": tag,
                "active": "false",
                "closed": "true",
                "limit": 100,
                "offset": offset,
            }
            events = _safe_get(f"{GAMMA_BASE}/events", params)
            if not events or not isinstance(events, list) or len(events) == 0:
                break

            for event in events:
                for m in event.get("markets", []):
                    mid = m.get("id", "")
                    if mid in seen_ids:
                        continue
                    seen_ids.add(mid)

                    question = m.get("question") or m.get("title", "")
                    title_lower = question.lower()

                    city_match = None
                    for city in CITIES:
                        if city.lower() in title_lower:
                            city_match = city
                            break
                    if not city_match:
                        continue

                    all_markets.append({
                        "market_id": mid,
                        "condition_id": m.get("conditionId", ""),
                        "title": question,
                        "slug": m.get("slug", ""),
                        "city": city_match,
                        "end_date": event.get("endDate", ""),
                        "volume": float(m.get("volume", 0) or 0),
                        "liquidity": float(m.get("liquidity", 0) or 0),
                    })

            logger.info(
                f"Tag '{tag}' offset {offset}: {len(events)} events, "
                f"{len(all_markets)} matched so far"
            )
            offset += 100
            if len(events) < 100:
                break
            time.sleep(0.3)

    df = pd.DataFrame(all_markets)
    if not df.empty:
        df.to_parquet(CACHE_FILE, index=False)
        logger.success(f"Saved {len(df)} weather markets → {CACHE_FILE}")
    return df


def _enrich_one(mid: str) -> dict:
    """Fetch resolution + token IDs for a single market."""
    market = _safe_get(f"{GAMMA_BASE}/markets/{mid}")
    if not market or not isinstance(market, dict):
        return {
            "market_id": mid,
            "yes_price": None,
            "actual_outcome": -1,
            "resolution_source": "",
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
        "resolution_source": market.get("resolutionSource", ""),
        "yes_token_id": tokens[0] if len(tokens) > 0 else "",
        "no_token_id": tokens[1] if len(tokens) > 1 else "",
    }


def enrich_resolutions(df: pd.DataFrame) -> pd.DataFrame:
    """Query each market individually to get resolution and token IDs (parallel)."""
    if ENRICHED_FILE.exists():
        cached = pd.read_parquet(ENRICHED_FILE)
        if "yes_token_id" in cached.columns:
            logger.info(f"Loading enriched data from {ENRICHED_FILE}")
            return cached
        logger.info("Enriched cache missing token IDs — re-enriching...")

    if df.empty:
        return df

    market_ids = df["market_id"].tolist()
    total = len(market_ids)
    msg = f"Starting enrichment of {total} markets with 5 workers..."
    logger.info(msg)
    print(msg, flush=True)

    results = []
    batch_start = time.time()

    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {executor.submit(_enrich_one, mid): mid for mid in market_ids}
        for i, future in enumerate(as_completed(futures), 1):
            results.append(future.result())

            if i % 200 == 0:
                elapsed = time.time() - batch_start
                rate = i / elapsed
                eta = (total - i) / rate
                msg = f"Enriching {i}/{total} ({rate:.1f}/s) — ETA {eta:.0f}s"
                logger.info(msg)
                print(msg, flush=True)

    enrich_df = pd.DataFrame(results)
    merged = df.merge(enrich_df, on="market_id", how="left")

    dup_cols = [c for c in merged.columns if c.endswith("_x") or c.endswith("_y")]
    if dup_cols:
        merged = merged.drop(columns=dup_cols)

    merged.to_parquet(ENRICHED_FILE, index=False)

    resolved = merged[merged["actual_outcome"] >= 0]
    logger.success(
        f"Enriched {len(merged)} markets: {len(resolved)} resolved "
        f"(YES={len(resolved[resolved['actual_outcome']==1])}, "
        f"NO={len(resolved[resolved['actual_outcome']==0])})"
    )
    return merged


def _fetch_one_clob(mid: str, yes_token: str, end_date: str) -> dict:
    """Fetch CLOB price history for a single market's YES token."""
    if not yes_token or pd.isna(yes_token) or yes_token == "":
        return {"market_id": mid, "market_prob": None, "num_trades": 0, "avg_yes_price": None}

    try:
        end_dt = pd.to_datetime(end_date)
    except Exception:
        return {"market_id": mid, "market_prob": None, "num_trades": 0, "avg_yes_price": None}

    start_ts = int((end_dt - pd.Timedelta(days=7)).timestamp())
    end_ts = int(end_dt.timestamp())
    cutoff_ts = int((end_dt - pd.Timedelta(hours=12)).timestamp())

    data = _safe_get(f"{CLOB_BASE}/prices-history", {
        "market": yes_token,
        "interval": "max",
        "startTs": start_ts,
        "endTs": end_ts,
    })
    if not data:
        return {"market_id": mid, "market_prob": None, "num_trades": 0, "avg_yes_price": None}

    history = data.get("history", [])
    if not history:
        return {"market_id": mid, "market_prob": None, "num_trades": 0, "avg_yes_price": None}

    pre_cutoff = [h for h in history if h.get("t", 0) and h["t"] < cutoff_ts]
    trades = pre_cutoff if pre_cutoff else history

    prices = [float(h["p"]) for h in trades]
    avg_price = sum(prices) / len(prices) if prices else None
    market_prob = max(0.02, avg_price) if avg_price is not None else None

    return {
        "market_id": mid,
        "market_prob": round(market_prob, 4) if market_prob else None,
        "num_trades": len(trades),
        "avg_yes_price": round(avg_price, 4) if avg_price else None,
    }


def fetch_clob_prices(enriched_df: pd.DataFrame) -> pd.DataFrame:
    """Fetch real CLOB trade prices for each market's YES token (parallel).

    Uses interval=max (raw trades), then computes the volume-weighted average
    price from trades at least 24h before the event end date.
    """
    if PRICES_FILE.exists():
        logger.info(f"Loading cached CLOB prices from {PRICES_FILE}")
        return enriched_df.merge(
            pd.read_parquet(PRICES_FILE), on="market_id", how="left"
        )

    if enriched_df.empty:
        return enriched_df

    total = len(enriched_df)
    msg = f"Starting CLOB price fetch for {total} markets with 8 workers..."
    logger.info(msg)
    print(msg, flush=True)
    batch_start = time.time()

    # Pre-extract all needed data to avoid per-row overhead
    tasks = []
    for _, row in enriched_df.iterrows():
        mid = row["market_id"]
        yes_token = row.get("yes_token_id", "")
        end_date = row.get("end_date", "")
        tasks.append((mid, yes_token, end_date))

    price_rows = []
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(_fetch_one_clob, mid, token, end_dt): mid
                   for mid, token, end_dt in tasks}
        for i, future in enumerate(as_completed(futures), 1):
            price_rows.append(future.result())

            if i % 200 == 0:
                elapsed = time.time() - batch_start
                rate = i / elapsed
                eta = (total - i) / rate
                with_prices = sum(1 for r in price_rows if r["market_prob"] is not None)
                msg = f"CLOB prices {i}/{total} ({rate:.1f}/s) — {with_prices} with prices, ETA {eta:.0f}s"
                logger.info(msg)
                print(msg, flush=True)

    prices_df = pd.DataFrame(price_rows)
    prices_df.to_parquet(PRICES_FILE, index=False)

    with_prices = prices_df["market_prob"].notna().sum()
    logger.success(
        f"CLOB prices: {with_prices}/{len(prices_df)} markets have trade data"
    )

    return enriched_df.merge(prices_df, on="market_id", how="left")


def get_settled_market_outcomes() -> pd.DataFrame:
    """Build the full dataset: markets + resolutions + CLOB prices."""
    markets_df = fetch_settled_weather_markets()
    if markets_df.empty:
        return markets_df

    enriched = enrich_resolutions(markets_df)
    with_prices = fetch_clob_prices(enriched)

    # Only keep markets with real CLOB prices
    has_prices = with_prices[with_prices["market_prob"].notna()]
    resolved = has_prices[has_prices["actual_outcome"] >= 0]

    logger.info(
        f"Final dataset: {len(resolved)} markets with prices + resolution\n"
        f"  Resolved YES: {(resolved['actual_outcome'] == 1).sum()}\n"
        f"  Resolved NO:  {(resolved['actual_outcome'] == 0).sum()}\n"
        f"  Avg market_prob: {resolved['market_prob'].mean():.4f}"
    )

    return resolved


if __name__ == "__main__":
    Path(CACHE_DIR).mkdir(parents=True, exist_ok=True)
    df = get_settled_market_outcomes()
    if not df.empty:
        print(df[["city", "title", "market_prob", "actual_outcome", "num_trades"]].head(20))
