"""Fetch historical Polymarket weather markets via Gamma API events endpoint.

Two-phase approach:
  1. Discover markets via /events?tag_slug=daily-temperature (cached)
  2. Enrich each market with resolution data via /markets/{id} (cached separately)
"""
import json
import time
from pathlib import Path

import pandas as pd
import requests
from loguru import logger

from backtest.config import CITIES, CACHE_DIR

GAMMA_BASE = "https://gamma-api.polymarket.com"

CACHE_FILE = Path(CACHE_DIR) / "polymarket_weather_markets.parquet"
ENRICHED_FILE = Path(CACHE_DIR) / "polymarket_enriched.parquet"


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


def enrich_resolutions(df: pd.DataFrame) -> pd.DataFrame:
    """Query each market individually to get resolution from outcomePrices.

    For neg-risk temperature markets, outcomePrices after resolution will be
    ["1", "0"] if YES won or ["0", "1"] if NO won.
    """
    if ENRICHED_FILE.exists():
        logger.info(f"Loading enriched data from {ENRICHED_FILE}")
        return pd.read_parquet(ENRICHED_FILE)

    if df.empty:
        return df

    results = []
    total = len(df)
    batch_start = time.time()

    for i, (_, row) in enumerate(df.iterrows()):
        mid = row["market_id"]

        market = _safe_get(f"{GAMMA_BASE}/markets/{mid}")
        if not market or not isinstance(market, dict):
            # Market not found — skip
            results.append({
                "market_id": mid,
                "yes_price": None,
                "actual_outcome": -1,
                "resolution_source": "",
            })
            continue

        prices = market.get("outcomePrices", [])
        if isinstance(prices, str):
            try:
                prices = json.loads(prices)
            except Exception:
                prices = []

        yes_price = float(prices[0]) if len(prices) > 0 else None

        # Resolution: for neg-risk markets, outcomePrices reflect the final state.
        # ["1", "0"] = YES won, ["0", "1"] = NO won
        outcome = -1
        if len(prices) >= 2:
            if prices[0] == "1":
                outcome = 1
            elif prices[1] == "1":
                outcome = 0

        results.append({
            "market_id": mid,
            "yes_price": yes_price,
            "actual_outcome": outcome,
            "resolution_source": market.get("resolutionSource", ""),
        })

        # Progress every 100 markets
        if (i + 1) % 100 == 0:
            elapsed = time.time() - batch_start
            rate = (i + 1) / elapsed
            eta = (total - i - 1) / rate
            resolved_count = sum(1 for r in results if r["actual_outcome"] >= 0)
            logger.info(
                f"Enriching {i + 1}/{total} ({rate:.1f}/s) — "
                f"{resolved_count} resolved so far, ETA {eta:.0f}s"
            )

        time.sleep(0.15)  # Rate limit: ~6-7 req/s

    enrich_df = pd.DataFrame(results)
    merged = df.merge(enrich_df, on="market_id", how="left")
    merged.to_parquet(ENRICHED_FILE, index=False)

    resolved = merged[merged["actual_outcome"] >= 0]
    logger.success(
        f"Enriched {len(merged)} markets: {len(resolved)} resolved "
        f"(YES={len(resolved[resolved['actual_outcome']==1])}, "
        f"NO={len(resolved[resolved['actual_outcome']==0])})"
    )
    return merged


def get_settled_market_outcomes() -> pd.DataFrame:
    """Build a dataset of settled weather market outcomes."""
    markets_df = fetch_settled_weather_markets()
    if markets_df.empty:
        return markets_df

    enriched = enrich_resolutions(markets_df)

    # For markets where yes_price is None (couldn't fetch), use 0.5
    # Handle merge column suffix: use yes_price_y (from enrichment) if available
    price_col = "yes_price_y" if "yes_price_y" in enriched.columns else "yes_price"
    enriched["implied_prob"] = enriched[price_col].fillna(0.5)

    logger.info(
        f"Final dataset: {len(enriched)} markets\n"
        f"  Resolved YES: {(enriched['actual_outcome'] == 1).sum()}\n"
        f"  Resolved NO:  {(enriched['actual_outcome'] == 0).sum()}\n"
        f"  Unresolved:   {(enriched['actual_outcome'] == -1).sum()}"
    )

    return enriched


if __name__ == "__main__":
    Path(CACHE_DIR).mkdir(parents=True, exist_ok=True)
    df = get_settled_market_outcomes()
    if not df.empty:
        print(df[["city", "title", "implied_prob", "actual_outcome"]].head(20))
