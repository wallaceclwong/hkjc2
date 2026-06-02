import sqlite3, json
conn = sqlite3.connect("/opt/hkjc2/data/engine.db")
conn.row_factory = sqlite3.Row
race = conn.execute("SELECT * FROM racecards WHERE race_id = ?", ["2026-06-03_HV_R9"]).fetchone()
data = json.loads(race["data_json"])
horses = data.get("horses", [])

print(f"Race: {race['race_id']}")
print(f"Distance: {race['distance']}m  Class: {race['race_class']}  Track: {race['track_condition']}")
print(f"Course: {race['course']}  Jump: {race['jump_time']}")
print()

print(f"{'No':<4} {'Draw':<5} {'Horse':<28} {'Jockey':<22} {'Trainer':<20} {'Wt':<6} {'Last 6'}")
print("-" * 110)
for h in horses:
    no = str(h.get("saddle_number", "?"))
    draw = str(h.get("draw", "?"))
    name = str(h.get("horse_name", "?"))[:27]
    jockey = str(h.get("jockey", "?"))[:21]
    trainer = str(h.get("trainer", "?"))[:19]
    wt = str(h.get("weight", "?"))
    last6 = " ".join(str(r) for r in h.get("last_6_runs", [])[:6])
    print(f"{no:<4} {draw:<5} {name:<28} {jockey:<22} {trainer:<20} {wt:<6} {last6}")

print(f"\n{len(horses)} runners")
conn.close()
