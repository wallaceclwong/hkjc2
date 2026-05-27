"""Fetch historical daily max temperatures from Open-Meteo archive API.

Free, no API key required. Returns Celsius — conversion to Fahrenheit
handled downstream for US cities.
"""
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
import requests
from loguru import logger

sys.stdout.reconfigure(line_buffering=True)

from backtest.config import CACHE_DIR
from backtest.fetch_daily_markets import CITY_COORDS

WEATHER_CACHE = Path(CACHE_DIR) / "daily_city_temperatures.parquet"

OPEN_METEO_URL = "https://archive-api.open-meteo.com/v1/archive"


def fetch_city_temperature(lat: float, lon: float, city: str,
                           start_date: str = "2000-01-01",
                           end_date: str = "2026-04-15",
                           max_retries: int = 4) -> pd.DataFrame | None:
    """Fetch daily max temperature for a single city from Open-Meteo.

    Retries with exponential backoff on 429 rate limits.
    """
    params = {
        "latitude": lat,
        "longitude": lon,
        "start_date": start_date,
        "end_date": end_date,
        "daily": "temperature_2m_max",
        "timezone": "auto",
    }
    for attempt in range(max_retries):
        try:
            resp = requests.get(OPEN_METEO_URL, params=params, timeout=60)
            if resp.status_code == 200:
                data = resp.json()
                daily = data.get("daily", {})
                dates = daily.get("time", [])
                temps = daily.get("temperature_2m_max", [])

                if not dates or not temps:
                    logger.warning(f"Open-Meteo {city}: no data")
                    return None

                df = pd.DataFrame({
                    "date": pd.to_datetime(dates),
                    "temp_max_c": temps,
                    "city": city,
                })
                df["temp_max_c"] = df["temp_max_c"].replace({None: np.nan})
                df = df.dropna(subset=["temp_max_c"])

                logger.info(f"Open-Meteo {city}: {len(df)} days ({df['date'].min().date()} to {df['date'].max().date()})")
                return df
            elif resp.status_code == 429:
                wait = 3 * (2 ** attempt)
                logger.warning(f"Open-Meteo {city}: 429 rate limit, waiting {wait}s (attempt {attempt+1}/{max_retries})")
                time.sleep(wait)
            else:
                logger.warning(f"Open-Meteo {city}: HTTP {resp.status_code}")
                return None
        except Exception as e:
            if attempt < max_retries - 1:
                time.sleep(3 * (2 ** attempt))
            else:
                logger.warning(f"Open-Meteo {city} failed after {max_retries} attempts: {e}")
                return None
    return None


def get_all_city_temperatures(force_refresh: bool = False) -> pd.DataFrame:
    """Fetch daily max temperatures for all known cities, with caching."""
    if not force_refresh and WEATHER_CACHE.exists():
        cached = pd.read_parquet(WEATHER_CACHE)
        if len(cached) > 1000:
            logger.info(f"Loading {len(cached)} cached city temperatures from {WEATHER_CACHE}")
            return cached

    Path(CACHE_DIR).mkdir(parents=True, exist_ok=True)

    all_dfs = []
    cities = sorted(CITY_COORDS.items())

    for i, (city, (lat, lon, units)) in enumerate(cities):
        logger.info(f"Fetching {city} ({lat}, {lon}) [{i+1}/{len(cities)}]")
        df = fetch_city_temperature(lat, lon, city)
        if df is not None and not df.empty:
            df["units"] = units
            all_dfs.append(df)
        # Be polite to the API — archive endpoint is rate-limited
        if i < len(cities) - 1:
            time.sleep(5.0)

    if not all_dfs:
        logger.error("No city temperature data fetched")
        return pd.DataFrame()

    combined = pd.concat(all_dfs, ignore_index=True)
    combined.to_parquet(WEATHER_CACHE, index=False)
    logger.success(f"Saved {len(combined)} rows for {combined['city'].nunique()} cities -> {WEATHER_CACHE}")
    return combined


def get_city_weather(city: str) -> pd.DataFrame | None:
    """Get daily max temps for a specific city from cache (fetch if needed)."""
    df = get_all_city_temperatures()
    if df.empty:
        return None
    city_df = df[df["city"] == city].copy()
    if city_df.empty:
        # Try to fetch on demand
        info = CITY_COORDS.get(city)
        if info:
            lat, lon, units = info
            city_df = fetch_city_temperature(lat, lon, city)
            if city_df is not None:
                city_df["units"] = units
        else:
            logger.warning(f"Unknown city: {city}")
            return None
    return city_df.sort_values("date").reset_index(drop=True)


if __name__ == "__main__":
    Path(CACHE_DIR).mkdir(parents=True, exist_ok=True)
    df = get_all_city_temperatures()
    if not df.empty:
        print(f"Total: {len(df)} rows, {df['city'].nunique()} cities")
        print(f"Date range: {df['date'].min()} to {df['date'].max()}")
        for city in sorted(df["city"].unique()):
            cdf = df[df["city"] == city]
            print(f"  {city}: {len(cdf)} days, {cdf['temp_max_c'].min():.1f} to {cdf['temp_max_c'].max():.1f} C")
