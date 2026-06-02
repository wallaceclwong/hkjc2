"""Show racecard details for a specific race."""
import sys, sqlite3
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import DB_PATH

race_id = sys.argv[1] if len(sys.argv) > 1 else "2026-06-03_HV_R9"

conn = sqlite3.connect(str(DB_PATH))
conn.row_factory = sqlite3.Row

race = conn.execute("SELECT * FROM racecards WHERE race_id = ?", [race_id]).fetchone()
if not race:
    print(f"Race {race_id} not found")
    conn.close()
    sys.exit(1)

print(f"Race: {race['race_id']}")
print(f"Distance: {race['distance']}m  Track: {race['track']}  Class: {race['class']}")
print(f"Going: {race['going']}  Prize: {race['prize']}")
print(f"Status: {race.get('status', '?')}")
print()

runners = conn.execute(
    "SELECT * FROM runners WHERE race_id = ? ORDER BY CAST(horse_no AS INTEGER)",
    [race_id]
).fetchall()

print(f"{'No':<4} {'Horse':<28} {'Jockey':<22} {'Trainer':<22} {'Wt':<5} {'Draw':<5}")
print("-" * 90)
for r in runners:
    no = r["horse_no"] or "?"
    horse = (r["horse_name_en"] or "?")[:27]
    jockey = (r["jockey_name_en"] or "?")[:21]
    trainer = (r["trainer_name_en"] or "?")[:21]
    wt = r["declared_weight"] or "?"
    draw = r["draw"] or "?"
    print(f"{no:<4} {horse:<28} {jockey:<22} {trainer:<22} {wt:<5} {draw:<5}")

print(f"\n{runners.__len__()} runners total")
conn.close()
