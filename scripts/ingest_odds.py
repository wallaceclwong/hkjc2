#!/usr/bin/env python3
"""Scrape live HKJC odds from bet.hkjc.com. Run every 3 min on race days via cron.

Usage:
    python scripts/ingest_odds.py --date 2026-05-25 --venue ST
    python scripts/ingest_odds.py --date 2026-05-25 --venue HV --race 3
"""

import argparse, asyncio, json, os, re, subprocess, sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from playwright.async_api import async_playwright
from loguru import logger
from config import DATA_DIR
from db import init_db, save_odds_snapshot, get_race_ids_for_date, get_racecard

HKJC_BET_URL = "https://bet.hkjc.com/en/racing/wp/{date_path}/{venue}/{race_no}"


def _scheduled_races(date_str: str, venue: str) -> list[dict]:
    """Get races with jump times from the DB."""
    race_ids = get_race_ids_for_date(date_str, venue)
    races = []
    for rid in race_ids:
        rc = get_racecard(rid)
        if rc:
            races.append({"race_id": rid, "race_no": rc["race_no"], "jump_time": rc.get("jump_time", "13:00")})
    return races


def _minutes_to_jump(jump_time_str: str) -> int | None:
    """Parse jump time like '13:00' or '1:30PM' and return minutes until jump."""
    now = datetime.now()
    for fmt in ("%H:%M", "%H:%M%p", "%I:%M%p", "%I:%M %p"):
        try:
            jt = datetime.strptime(jump_time_str.strip(), fmt)
            jt = jt.replace(year=now.year, month=now.month, day=now.day)
            return int((jt - now).total_seconds() / 60)
        except ValueError:
            continue
    return None


async def scrape_one(date_str: str, venue: str, race_no: int, page) -> dict | None:
    date_path = date_str.replace("-", "/")
    url = HKJC_BET_URL.format(date_path=date_path, venue=venue, race_no=race_no)
    race_id = f"{date_str}_{venue}_R{race_no}"

    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
    except Exception:
        logger.warning(f"R{race_no}: page load timeout, trying to parse anyway")

    # Wait for odds elements
    selectors = [f"#wpleg_WIN_{race_no}_1", f".winOdds_{race_no}", "table.win-odds", '[data-type="win-odds"]', "table"]
    found = False
    for sel in selectors:
        try:
            await page.wait_for_selector(sel, timeout=10000)
            found = True
            break
        except Exception:
            continue

    if not found:
        logger.warning(f"R{race_no}: no odds table found")
        return None

    win_odds = {}
    place_odds = {}

    for horse_num in range(1, 15):
        # Win odds
        for sel in [f"#odds_WIN_{race_no}_{horse_num} a", f"#win_{race_no}_{horse_num}",
                     f'[data-horse="{horse_num}"] .win-odds', f".horse-{horse_num} .win"]:
            try:
                el = await page.query_selector(sel)
                if el:
                    val = (await el.inner_text()).strip()
                    if val and val not in ("-", "", "SCR"):
                        try:
                            win_odds[str(horse_num)] = float(val)
                            break
                        except ValueError:
                            continue
            except Exception:
                continue

        # Place odds
        for sel in [f"#odds_PLA_{race_no}_{horse_num} a", f"#pla_{race_no}_{horse_num}",
                     f'[data-horse="{horse_num}"] .place-odds', f".horse-{horse_num} .place"]:
            try:
                el = await page.query_selector(sel)
                if el:
                    val = (await el.inner_text()).strip()
                    if val and val not in ("-", "", "SCR"):
                        try:
                            place_odds[str(horse_num)] = float(val)
                            break
                        except ValueError:
                            continue
            except Exception:
                continue

    if win_odds:
        save_odds_snapshot(race_id, win_odds, place_odds)
        top3 = sorted(win_odds.items(), key=lambda x: x[1])[:3]
        logger.info(f"R{race_no}: {len(win_odds)} horses — favs: {top3}")
        return {"race_id": race_id, "win_odds": win_odds, "place_odds": place_odds}
    else:
        logger.warning(f"R{race_no}: no odds extracted")
        return None


async def main():
    parser = argparse.ArgumentParser(description="Scrape HKJC live odds")
    parser.add_argument("--date", default=datetime.now().strftime("%Y-%m-%d"))
    parser.add_argument("--venue", default="ST")
    parser.add_argument("--race", type=int, default=0, help="Single race #, or 0 for all")
    parser.add_argument("--audit", action="store_true", help="Trigger audit.py when T-15 detected")
    args = parser.parse_args()

    init_db()

    if args.race > 0:
        races_to_scrape = [{"race_no": args.race, "jump_time": "13:00"}]
    else:
        races_to_scrape = _scheduled_races(args.date, args.venue)
        if not races_to_scrape:
            logger.warning(f"No racecards found for {args.date} {args.venue}. Run ingest_racecards.py first.")
            return

    user_data = DATA_DIR / "browser_session_odds"
    user_data.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            str(user_data.absolute()),
            headless=True,
            viewport={"width": 1280, "height": 800},
        )
        page = context.pages[0] if context.pages else await context.new_page()
        await page.set_extra_http_headers({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
        })

        for r in races_to_scrape:
            try:
                result = await scrape_one(args.date, args.venue, r["race_no"], page)
                if result and args.audit:
                    mins = _minutes_to_jump(r.get("jump_time", "13:00"))
                    if mins is not None and 10 <= mins <= 20:
                        logger.info(f"R{r['race_no']}: T-{mins}min — triggering audit...")
                        script = str(Path(__file__).parent / "audit.py")
                        subprocess.Popen(
                            [sys.executable, script, "--race-id", result["race_id"]],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        )
            except Exception as e:
                logger.error(f"R{r['race_no']}: {e}")

        await context.close()

    logger.success("Odds scrape complete")


if __name__ == "__main__":
    asyncio.run(main())
