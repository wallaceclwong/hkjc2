import sqlite3
import json
from datetime import datetime
from pathlib import Path
from config import DB_PATH, DATA_DIR

SCHEMA = """
CREATE TABLE IF NOT EXISTS racecards (
    race_id TEXT PRIMARY KEY,
    date TEXT NOT NULL,
    venue TEXT NOT NULL,
    race_no INTEGER NOT NULL,
    distance INTEGER,
    track_type TEXT,
    course TEXT,
    race_class TEXT,
    track_condition TEXT DEFAULT 'Good',
    jump_time TEXT,
    data_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS horses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    race_id TEXT NOT NULL REFERENCES racecards(race_id),
    horse_no TEXT NOT NULL,
    horse_name TEXT,
    horse_id TEXT,
    jockey TEXT,
    trainer TEXT,
    draw INTEGER,
    weight REAL,
    gear TEXT,
    training_location TEXT DEFAULT 'HK',
    last_6_json TEXT,
    odds_json TEXT,
    weight_allowance REAL DEFAULT 0,
    UNIQUE(race_id, horse_no)
);

CREATE TABLE IF NOT EXISTS odds_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    race_id TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    win_odds_json TEXT NOT NULL,
    place_odds_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_odds_race ON odds_snapshots(race_id, timestamp);

CREATE TABLE IF NOT EXISTS predictions (
    race_id TEXT NOT NULL,
    horse_no TEXT NOT NULL,
    pred_prob REAL,
    model_prob_pure REAL,
    fair_odds REAL,
    pure_ev REAL,
    value_edge REAL,
    market_prob REAL,
    win_odds REAL,
    ensemble_score REAL,
    rank INTEGER,
    created_at TEXT NOT NULL,
    PRIMARY KEY (race_id, horse_no)
);

CREATE TABLE IF NOT EXISTS audits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    race_id TEXT NOT NULL,
    horse_no TEXT NOT NULL,
    verdict TEXT NOT NULL,
    conviction_grade TEXT,
    reasoning TEXT,
    market_signal TEXT,
    tactical_scenario TEXT,
    expert_note TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS results (
    race_id TEXT PRIMARY KEY,
    fetched_at TEXT NOT NULL,
    data_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    race_id TEXT NOT NULL,
    horse_no TEXT NOT NULL,
    stake REAL DEFAULT 0,
    odds_taken REAL,
    result TEXT,
    pnl REAL DEFAULT 0,
    settled_at TEXT
);

CREATE TABLE IF NOT EXISTS bankroll (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    balance REAL DEFAULT 10000,
    high_water_mark REAL DEFAULT 10000,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bankroll_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    description TEXT,
    pnl REAL,
    balance_after REAL
);

CREATE TABLE IF NOT EXISTS weather (
    venue TEXT NOT NULL,
    date TEXT NOT NULL,
    max_temp_c REAL,
    prob_rain REAL,
    humidity_pct REAL,
    wind_speed_kmh REAL,
    track_condition_forecast TEXT,
    reasoning TEXT,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (venue, date)
);

CREATE TABLE IF NOT EXISTS sentiment (
    horse_id TEXT NOT NULL,
    race_id TEXT,
    unluckiness_score REAL DEFAULT 5.0,
    analysis TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS fixtures (
    date TEXT NOT NULL,
    venue TEXT NOT NULL,
    day_night TEXT,
    race_type TEXT DEFAULT 'Local',
    status TEXT DEFAULT 'Scheduled',
    PRIMARY KEY (date, venue)
);
"""

def get_db() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn

def init_db():
    conn = get_db()
    conn.executescript(SCHEMA)
    # Seed bankroll if empty
    cur = conn.execute("SELECT 1 FROM bankroll WHERE id = 1")
    if not cur.fetchone():
        conn.execute(
            "INSERT INTO bankroll (id, balance, high_water_mark, updated_at) VALUES (1, 10000, 10000, ?)",
            (datetime.now().isoformat(),)
        )
    # Migrate: add weight_allowance to existing horses tables
    try:
        conn.execute("ALTER TABLE horses ADD COLUMN weight_allowance REAL DEFAULT 0")
    except Exception:
        pass
    conn.commit()
    conn.close()

# ── Racecard helpers ──

def save_racecard(race_id: str, date: str, venue: str, race_no: int,
                  distance: int, track_type: str, course: str, race_class: str,
                  track_condition: str, jump_time: str, horses: list):
    conn = get_db()
    data = {
        "race_id": race_id, "date": date, "venue": venue, "race_no": race_no,
        "distance": distance, "track_type": track_type, "course": course,
        "race_class": race_class, "track_condition": track_condition,
        "jump_time": jump_time, "horses": horses
    }
    conn.execute(
        "INSERT OR REPLACE INTO racecards VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (race_id, date, venue, race_no, distance, track_type, course,
         race_class, track_condition, jump_time, json.dumps(data, ensure_ascii=False))
    )
    for h in horses:
        last6 = json.dumps(h.get("last_6_runs", []))
        odds = json.dumps({"win": h.get("win_odds"), "place": h.get("place_odds")})
        conn.execute(
            "INSERT OR REPLACE INTO horses (race_id, horse_no, horse_name, horse_id, jockey, trainer, draw, weight, gear, training_location, last_6_json, odds_json, weight_allowance) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (race_id, str(h["saddle_number"]), h.get("horse_name", ""), h.get("horse_id", ""),
             h.get("jockey", ""), h.get("trainer", ""), h.get("draw", 0),
             h.get("weight", 0), h.get("gear", ""), h.get("training_location", "HK"),
             last6, odds, h.get("weight_allowance", 0))
        )
    conn.commit()
    conn.close()

def get_racecard(race_id: str) -> dict | None:
    conn = get_db()
    row = conn.execute("SELECT data_json FROM racecards WHERE race_id = ?", (race_id,)).fetchone()
    conn.close()
    if row:
        return json.loads(row["data_json"])
    return None

def get_horses(race_id: str) -> list[dict]:
    conn = get_db()
    rows = conn.execute(
        "SELECT horse_no, horse_name, horse_id, jockey, trainer, draw, weight, gear, training_location, last_6_json, odds_json FROM horses WHERE race_id = ?",
        (race_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def get_race_ids_for_date(date: str, venue: str = None) -> list[str]:
    conn = get_db()
    if venue:
        rows = conn.execute(
            "SELECT race_id FROM racecards WHERE date = ? AND venue = ? ORDER BY race_no",
            (date, venue)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT race_id FROM racecards WHERE date = ? ORDER BY race_no",
            (date,)
        ).fetchall()
    conn.close()
    return [r["race_id"] for r in rows]

# ── Odds helpers ──

def save_odds_snapshot(race_id: str, win_odds: dict, place_odds: dict):
    conn = get_db()
    conn.execute(
        "INSERT INTO odds_snapshots (race_id, timestamp, win_odds_json, place_odds_json) VALUES (?,?,?,?)",
        (race_id, datetime.now().isoformat(), json.dumps(win_odds), json.dumps(place_odds))
    )
    conn.commit()
    conn.close()

def get_latest_odds(race_id: str) -> dict:
    conn = get_db()
    row = conn.execute(
        "SELECT win_odds_json FROM odds_snapshots WHERE race_id = ? ORDER BY timestamp DESC LIMIT 1",
        (race_id,)
    ).fetchone()
    conn.close()
    if row:
        return json.loads(row["win_odds_json"])
    return {}

def get_odds_movement(race_id: str) -> dict:
    """Return per-horse odds movement: first snapshot vs latest snapshot."""
    conn = get_db()
    rows = conn.execute(
        "SELECT timestamp, win_odds_json FROM odds_snapshots WHERE race_id = ? ORDER BY timestamp",
        (race_id,)
    ).fetchall()
    conn.close()
    if len(rows) < 2:
        return {}
    first_snap = json.loads(rows[0]["win_odds_json"])
    latest_snap = json.loads(rows[-1]["win_odds_json"])
    movement = {}
    for horse_no, latest_val in latest_snap.items():
        first_val = first_snap.get(horse_no)
        if first_val:
            change = latest_val - first_val
            pct = change / first_val
            if pct < -0.12:
                direction = "STEAMING"
            elif pct > 0.12:
                direction = "DRIFTING"
            else:
                direction = "STABLE"
            movement[horse_no] = {"first": first_val, "latest": latest_val, "direction": direction}
    return movement

# ── Prediction helpers ──

def save_predictions(race_id: str, df):
    conn = get_db()
    now = datetime.now().isoformat()
    for _, row in df.iterrows():
        conn.execute(
            """INSERT OR REPLACE INTO predictions
               (race_id, horse_no, pred_prob, model_prob_pure, fair_odds, pure_ev,
                value_edge, market_prob, win_odds, ensemble_score, rank, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (race_id, str(row["horse_no"]), float(row.get("pred_prob", 0)), float(row.get("model_prob_pure", 0)),
             float(row.get("fair_odds", 0)), float(row.get("pure_ev", 0)), float(row.get("value_edge", 0)),
             float(row.get("market_prob", 0)), float(row.get("win_odds", 0)),
             float(row.get("ensemble_score", 0)), int(row.get("rank", 99)), now)
        )
    conn.commit()
    conn.close()

def get_predictions(race_id: str) -> list[dict]:
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM predictions WHERE race_id = ? ORDER BY rank",
        (race_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]

# ── Audit helpers ──

def save_audit(race_id: str, horse_no: str, verdict: str, conviction_grade: str,
               reasoning: str, market_signal: str = "", tactical_scenario: str = "",
               expert_note: str = ""):
    conn = get_db()
    conn.execute(
        "INSERT INTO audits (race_id, horse_no, verdict, conviction_grade, reasoning, market_signal, tactical_scenario, expert_note, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (race_id, horse_no, verdict, conviction_grade, reasoning, market_signal, tactical_scenario, expert_note, datetime.now().isoformat())
    )
    conn.commit()
    conn.close()

# ── Results helpers ──

def save_results(race_id: str, data: dict):
    conn = get_db()
    conn.execute(
        "INSERT OR REPLACE INTO results (race_id, fetched_at, data_json) VALUES (?,?,?)",
        (race_id, datetime.now().isoformat(), json.dumps(data, ensure_ascii=False))
    )
    conn.commit()
    conn.close()

def get_results(race_id: str) -> dict | None:
    conn = get_db()
    row = conn.execute("SELECT data_json FROM results WHERE race_id = ?", (race_id,)).fetchone()
    conn.close()
    if row:
        return json.loads(row["data_json"])
    return None

# ── Bet / Bankroll helpers ──

def save_bet(race_id: str, horse_no: str, stake: float, odds_taken: float):
    conn = get_db()
    conn.execute(
        "INSERT INTO bets (race_id, horse_no, stake, odds_taken) VALUES (?,?,?,?)",
        (race_id, horse_no, stake, odds_taken)
    )
    conn.commit()
    conn.close()

def settle_bet(race_id: str, horse_no: str, result: str, pnl: float):
    conn = get_db()
    conn.execute(
        "UPDATE bets SET result = ?, pnl = ?, settled_at = ? WHERE race_id = ? AND horse_no = ?",
        (result, pnl, datetime.now().isoformat(), race_id, horse_no)
    )
    conn.commit()
    conn.close()

def get_bankroll() -> dict:
    conn = get_db()
    row = conn.execute("SELECT balance, high_water_mark, updated_at FROM bankroll WHERE id = 1").fetchone()
    conn.close()
    return dict(row) if row else {"balance": 10000, "high_water_mark": 10000, "updated_at": ""}

def update_bankroll(new_balance: float, description: str = "", pnl: float = 0):
    conn = get_db()
    now = datetime.now().isoformat()
    cur = conn.execute("SELECT balance, high_water_mark FROM bankroll WHERE id = 1").fetchone()
    hwm = max(cur["high_water_mark"], new_balance)
    conn.execute("UPDATE bankroll SET balance = ?, high_water_mark = ?, updated_at = ? WHERE id = 1",
                 (new_balance, hwm, now))
    conn.execute(
        "INSERT INTO bankroll_history (timestamp, description, pnl, balance_after) VALUES (?,?,?,?)",
        (now, description, pnl, new_balance)
    )
    conn.commit()
    conn.close()

# ── Weather helpers ──

def save_weather(venue: str, date: str, forecast: dict):
    conn = get_db()
    conn.execute(
        "INSERT OR REPLACE INTO weather (venue, date, max_temp_c, prob_rain, humidity_pct, wind_speed_kmh, track_condition_forecast, reasoning, fetched_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (venue, date, forecast.get("max_temp_c"), forecast.get("prob_rain"),
         forecast.get("humidity_pct"), forecast.get("wind_speed_kmh"),
         forecast.get("track_condition", ""), forecast.get("reasoning", ""),
         datetime.now().isoformat())
    )
    conn.commit()
    conn.close()

# ── Fixture helpers ──

def get_upcoming_meetings(days: int = 7) -> list[dict]:
    conn = get_db()
    today = datetime.now().strftime("%Y-%m-%d")
    rows = conn.execute(
        "SELECT date, venue, day_night FROM fixtures WHERE date >= ? AND status = 'Scheduled' ORDER BY date LIMIT ?",
        (today, days * 2)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def is_race_day(date: str = None) -> bool:
    if date is None:
        date = datetime.now().strftime("%Y-%m-%d")
    conn = get_db()
    row = conn.execute("SELECT 1 FROM fixtures WHERE date = ?", (date,)).fetchone()
    conn.close()
    return row is not None
