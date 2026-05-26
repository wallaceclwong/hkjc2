"""Fetch historical weather from Open-Meteo Archive API (free, no key)."""
import time
from pathlib import Path

import numpy as np
import pandas as pd
import requests
from loguru import logger

from backtest.config import CITIES, START_DATE, END_DATE, CACHE_DIR

OPEN_METEO_ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"


def _city_cache_path(city: str) -> Path:
    Path(CACHE_DIR).mkdir(parents=True, exist_ok=True)
    return Path(CACHE_DIR) / f"weather_{city.replace(' ', '_')}_{START_DATE}_{END_DATE}.parquet"


def fetch_city_weather(city: str) -> pd.DataFrame:
    """Fetch daily historical weather for a city. Cached to parquet."""
    cache_path = _city_cache_path(city)
    if cache_path.exists():
        logger.info(f"Loading cached weather for {city}")
        return pd.read_parquet(cache_path)

    lat, lng = CITIES[city]
    params = {
        "latitude": lat,
        "longitude": lng,
        "start_date": START_DATE,
        "end_date": END_DATE,
        "daily": [
            "temperature_2m_max",
            "temperature_2m_min",
            "temperature_2m_mean",
            "apparent_temperature_max",
            "apparent_temperature_min",
            "apparent_temperature_mean",
            "precipitation_sum",
            "precipitation_hours",
            "precipitation_probability_max",
            "wind_speed_10m_max",
            "wind_gusts_10m_max",
            "relative_humidity_2m_mean",
            "surface_pressure_mean",
            "shortwave_radiation_sum",
        ],
        "timezone": "auto",
    }

    logger.info(f"Fetching {city} weather {START_DATE} → {END_DATE}...")
    resp = requests.get(OPEN_METEO_ARCHIVE, params=params, timeout=30)
    data = resp.json()

    if "error" in data:
        logger.error(f"Open-Meteo error for {city}: {data}")
        return pd.DataFrame()

    df = pd.DataFrame(data["daily"])
    df["date"] = pd.to_datetime(df["time"])
    df["city"] = city
    df["lat"] = lat
    df["lng"] = lng

    df.to_parquet(cache_path, index=False)
    logger.success(f"Saved {len(df)} days for {city} → {cache_path}")
    return df


def fetch_all_weather() -> pd.DataFrame:
    """Fetch weather for all 5 cities, return combined DataFrame."""
    frames = []
    for city in CITIES:
        df = fetch_city_weather(city)
        if not df.empty:
            frames.append(df)
        time.sleep(0.5)  # Be polite to free API

    df_all = pd.concat(frames, ignore_index=True)
    logger.success(f"Total: {len(df_all)} rows across {len(CITIES)} cities")
    return df_all


def weather_to_forecast_dict(row: pd.Series) -> dict:
    """Convert a weather row into the forecast dict expected by the edge engine."""
    def _f(val, default=0.0):
        """Safely cast to float, handling None/NaN."""
        try:
            if val is None or (isinstance(val, float) and pd.isna(val)):
                return default
            return float(val)
        except (ValueError, TypeError):
            return default

    return {
        "location": row["city"],
        "current_temp": _f(row.get("temperature_2m_mean"), 15.0),
        "feels_like": _f(row.get("apparent_temperature_mean"), 15.0),
        "humidity": _f(row.get("relative_humidity_2m_mean"), 50),
        "pressure": _f(row.get("surface_pressure_mean"), 1013),
        "description": "historical",
        "forecast_temps": [
            _f(row.get("temperature_2m_max"), 20.0),
            _f(row.get("temperature_2m_min"), 10.0),
        ],
        "wind_speed": _f(row.get("wind_speed_10m_max"), 0),
        "visibility": 10.0,
        "precip_prob": _f(row.get("precipitation_probability_max"), 0) / 100.0,
        "precip_amount": _f(row.get("precipitation_sum"), 0),
        "data_source": "open-meteo",
        "city_type": "general",
        "region": "North America" if row["city"] not in ("London", "Tokyo") else (
            "Europe" if row["city"] == "London" else "Asia"
        ),
    }


if __name__ == "__main__":
    fetch_all_weather()
