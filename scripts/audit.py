#!/usr/bin/env python3
"""DeepSeek-R1 War Room audit + Telegram alert.

Usage:
    python scripts/audit.py --race-id 2026-05-25_ST_R1
    python scripts/audit.py --date 2026-05-25 --venue ST
"""

import argparse, asyncio, json, os, re, sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
import pandas as pd
from loguru import logger
from openai import AsyncOpenAI
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from openai import APIError, APITimeoutError

from config import DEEPSEEK_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, DATA_DIR
from db import init_db, get_racecard, get_predictions, get_race_ids_for_date, save_audit, get_results, get_latest_odds

PEDIGREE_FILE = DATA_DIR / "pedigree_cache.json"


def _safe_int(val, default=0):
    try:
        if pd.isna(val) or val is None:
            return default
        return int(float(val))
    except (ValueError, TypeError):
        return default


class WarRoom:
    def __init__(self):
        self.model = "deepseek-reasoner"
        self.client = AsyncOpenAI(api_key=DEEPSEEK_API_KEY, base_url="https://api.deepseek.com")
        self.pedigree = {}
        if PEDIGREE_FILE.exists():
            try:
                with open(PEDIGREE_FILE) as f:
                    self.pedigree = json.load(f)
            except Exception:
                pass

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=4, max=10),
        retry=retry_if_exception_type((APIError, APITimeoutError)),
    )
    async def audit(self, race_id: str, tip_horse_no: str) -> dict:
        rc = get_racecard(race_id)
        preds = get_predictions(race_id)
        if not rc or not preds:
            return {"verdict": "VETO", "reasoning": "Missing racecard or predictions"}

        # Build field context from predictions
        field = []
        target = None
        for p in preds:
            h = {"no": p["horse_no"], "odds": round(p.get("win_odds", 10), 1),
                 "rank": p.get("rank", 99), "fair_odds": round(p.get("fair_odds", 10), 1),
                 "value_mult": round(p.get("value_edge", 0) + 1, 2)}
            field.append(h)
            if str(p["horse_no"]) == str(tip_horse_no):
                target = p
                target["horse_name"] = ""
                # Enrich with racecard data
                for hh in rc.get("horses", []):
                    if str(hh.get("saddle_number")) == str(tip_horse_no):
                        target["horse_name"] = hh.get("horse_name", "")
                        target["horse_id"] = hh.get("horse_id", "")
                        target["draw"] = hh.get("draw", 0)
                        target["gear"] = hh.get("gear", "")
                        target["training_location"] = hh.get("training_location", "HK")
                        break

        if target is None:
            return {"verdict": "VETO", "reasoning": "Horse not found in predictions"}

        horse_id = target.get("horse_id", "")
        pedigree = self.pedigree.get(horse_id, {"sire": "Unknown", "dam": "Unknown"})

        # Market context
        latest_odds = get_latest_odds(race_id)
        market_str = "No live odds data."

        prompt = f"""
Act as the 'LUNAR LEAP' STRATEGIC ADVISORY for HKJC.
Audit the following High-Value Trade using a MULTI-AGENT simulation.

### THE TARGET (Statistical Favorite)
- Horse: {target.get('horse_name', 'Unknown')} (#{target.get('horse_no', '?')})
- ID: {horse_id}
- Lineage: Sire: {pedigree['sire']} | Dam: {pedigree['dam']}
- Stats: Odds {target.get('win_odds', 10):.1f} (Fair: {target.get('fair_odds', 10):.1f}), EV: {target.get('pure_ev', 1.0):.2f}
- Logistics: Draw {target.get('draw', '?')}, Race at {rc.get('venue', '?')}

### MARKET MOMENTUM
{market_str}

### FIELD CONTEXT
{json.dumps(field[:14], indent=2)}

### WAR ROOM ROLES:
1. **AGENT TACTICIAN**: Analyze the 'Pace' and 'Draw'. Can this horse stay clear or will it be trapped?
2. **AGENT GENETICIST**: Analyze the Sire/Dam. Is this horse bred for {rc.get('distance', 'N/A')}m?
3. **AGENT MARKET-ANALYST**: Analyze odds. Smart money or noise?
4. **AGENT STEWARD**: Any historical red flags?
5. **AGENT VALUE-ORACLE**: Compare Public Odds vs Fair Odds. Is the profit margin worth the risk?

### OUTPUT FORMAT:
Respond with a JSON block followed by a brief Expert Note.
{{
  "verdict": "CONFIRMED" | "CAUTION" | "VETO",
  "conviction_grade": "S" | "A" | "B",
  "market_signal": "Brief analysis of the odds movement validity",
  "tactical_scenario": "2-sentence prediction of the race jump",
  "reasoning_path": "3-sentence chain of thought",
  "expert_note": "A final 1-sentence tactical summary for the user"
}}
"""

        try:
            resp = await self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": "You are a Multi-Agent Strategic Auditor for HKJC. Output JSON then analysis."},
                    {"role": "user", "content": prompt},
                ],
                timeout=55,
            )

            content = resp.choices[0].message.content
            clean = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL)

            json_block = re.search(r"```json\s*(\{.*?\})\s*```", clean, re.DOTALL)
            if json_block:
                result_str = json_block.group(1)
            else:
                json_match = re.search(r"\{.*\}", clean, re.DOTALL)
                result_str = json_match.group(0) if json_match else None

            if result_str:
                result = json.loads(result_str)
                result["raw"] = content
                return result
            else:
                logger.warning(f"Failed to parse DeepSeek response: {content[:200]}...")
                return {"verdict": "CAUTION", "reasoning": "Failed to parse strategic JSON.", "raw": content}

        except Exception as e:
            logger.error(f"DeepSeek API error: {e}")
            return {"verdict": "ERROR", "reasoning": str(e)}


async def send_telegram(text: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("Telegram not configured — skipping notification")
        return False

    prefixed = f"[LUNAR LEAP]\n{text}"
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

    async with httpx.AsyncClient() as client:
        try:
            resp = await client.post(url, json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": prefixed[:4000],
                "parse_mode": "Markdown",
            }, timeout=10)
            if resp.status_code == 200:
                return True
        except Exception:
            pass

        # Fallback: plain text
        plain = prefixed.replace("*", "").replace("_", "").replace("`", "")[:4096]
        try:
            resp = await client.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": plain}, timeout=10)
            return resp.status_code == 200
        except Exception as e:
            logger.error(f"Telegram send failed: {e}")
            return False


async def audit_race(race_id: str, war_room: WarRoom, send_tg: bool = True, force_horse: str = None):
    preds = get_predictions(race_id)
    if not preds:
        logger.warning(f"{race_id}: no predictions found")
        return None

    rc = get_racecard(race_id)
    wet = False
    if rc:
        track = rc.get("track_condition", "Good").upper()
        wet = any(w in track for w in ("WET", "SOFT", "YIELDING", "HEAVY", "SLOW"))

    # Find bet candidates: odds 4-15, pure_ev > 1.05, rank <= 4
    candidates = [p for p in preds
                  if 4.0 <= p.get("win_odds", 0) <= 15.0
                  and p.get("pure_ev", 0) > 1.05
                  and p.get("rank", 99) <= 4]

    if force_horse:
        candidates = [p for p in preds if str(p.get("horse_no")) == str(force_horse)]
        if not candidates:
            logger.error(f"{race_id}: forced horse #{force_horse} not found in predictions")
            return None

    if not candidates or wet:
        logger.info(f"{race_id}: no audit candidates{' (wet track)' if wet else ''}")
        return None

    # Audit the top candidate
    candidate = sorted(candidates, key=lambda x: x.get("pure_ev", 0), reverse=True)[0]
    horse_no = str(candidate["horse_no"])

    logger.info(f"{race_id}: auditing #{horse_no}...")
    result = await war_room.audit(race_id, horse_no)

    verdict = result.get("verdict", "CAUTION")
    grade = result.get("conviction_grade", "?")
    reasoning = result.get("reasoning_path", "")
    market_signal = result.get("market_signal", "")
    tactical = result.get("tactical_scenario", "")
    note = result.get("expert_note", "")

    full_reasoning = f"Grade [{grade}] — SIGNAL: {market_signal} — REASON: {reasoning} — NOTE: {note}"

    save_audit(race_id, horse_no, verdict, grade, full_reasoning, market_signal, tactical, note)

    # Build Telegram message
    horse_name = ""
    odds_val = candidate.get("win_odds", 0)
    ev_val = candidate.get("pure_ev", 0)
    if rc:
        for h in rc.get("horses", []):
            if str(h.get("saddle_number")) == horse_no:
                horse_name = h.get("horse_name", "")
                break

    emoji = {"CONFIRMED": "BET", "CAUTION": "WATCH", "VETO": "NO BET"}.get(verdict, verdict)
    msg = (
        f"*{emoji}: {race_id}*\n"
        f"Horse: #{horse_no} {horse_name}\n"
        f"Odds: {odds_val:.1f} | EV: {ev_val:.2f}\n"
        f"Grade: {grade}\n\n"
        f"{note}"
    )

    if send_tg and verdict in ("CONFIRMED", "CAUTION"):
        await send_telegram(msg)

    logger.success(f"{race_id}: verdict={verdict} grade={grade} — {note}")
    return {"race_id": race_id, "verdict": verdict, "grade": grade, "message": msg}


async def main():
    parser = argparse.ArgumentParser(description="DeepSeek-R1 War Room audit")
    parser.add_argument("--race-id", help="Single race ID (e.g. 2026-05-25_ST_R1)")
    parser.add_argument("--date", default=datetime.now().strftime("%Y-%m-%d"))
    parser.add_argument("--venue", default="ST")
    parser.add_argument("--no-telegram", action="store_true")
    parser.add_argument("--horse", help="Force audit a specific horse number (bypasses filters)")
    args = parser.parse_args()

    init_db()

    if not DEEPSEEK_API_KEY:
        logger.error("DEEPSEEK_API_KEY not set")
        return

    war_room = WarRoom()

    if args.race_id:
        await audit_race(args.race_id, war_room, send_tg=not args.no_telegram, force_horse=args.horse)
    else:
        race_ids = get_race_ids_for_date(args.date, args.venue)
        for rid in race_ids:
            try:
                await audit_race(rid, war_room, send_tg=not args.no_telegram, force_horse=args.horse)
            except Exception as e:
                logger.error(f"{rid}: audit failed — {e}")


if __name__ == "__main__":
    asyncio.run(main())
