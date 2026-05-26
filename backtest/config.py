"""Backtest configuration — Google Weather API → Polymarket edge strategy."""
from datetime import datetime

# Target cities with Polymarket weather liquidity
CITIES = {
    "New York":      (40.7128, -74.0060),
    "London":        (51.5074, -0.1278),
    "Tokyo":         (35.6762, 139.6503),
    "Los Angeles":   (34.0522, -118.2437),
    "Chicago":       (41.8781, -87.6298),
}

# Backtest date range
START_DATE = "2024-01-01"
END_DATE   = datetime.now().strftime("%Y-%m-%d")

# Edge thresholds (same as what live scanner will use)
MIN_EDGE       = 0.05   # 5% probability gap to consider a bet
MIN_CONFIDENCE = 0.65   # 65% confidence in weather forecast
KELLY_FRACTION = 0.10   # Fractional Kelly (conservative)
MAX_STAKE      = 500    # Max stake per bet in dollars
INITIAL_BANK   = 10000  # Paper bankroll

# Market types
MARKET_TYPES = ["temperature", "precipitation"]

# Cache
CACHE_DIR = "backtest/cache"
