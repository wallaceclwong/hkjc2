import os
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).parent.absolute()
load_dotenv(BASE_DIR / ".env")

DATA_DIR = BASE_DIR / "data"
MODEL_DIR = BASE_DIR / "models"
DB_PATH = DATA_DIR / "engine.db"

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
PROXY_URL = os.getenv("PROXY_URL", "")

RACE_TIME_BY_DIST = {
    1000: 56.69,
    1200: 69.08,
    1400: 82.20,
    1600: 94.72,
    1650: 97.00,
    2000: 121.70,
}
RACE_TIME_DEFAULT = 70.0

ALL_FEATURES = [
    "win_odds", "market_implied_prob", "implied_prob_norm", "odds_rank",
    "actual_wt", "draw", "draw_relative", "field_size",
    "jockey_win_rate", "jockey_place_rate", "jockey_rides",
    "trainer_win_rate", "trainer_place_rate",
    "last_6_avg", "last_6_best", "last_2_avg", "last_6_trend",
    "gear_change", "stable_change", "ai_unluckiness",
    "distance", "race_sec_sum",
    "sec_pos_1", "sec_pos_2", "sec_pos_pre",
    "venue", "track_type", "course", "race_class", "track_condition",
]
