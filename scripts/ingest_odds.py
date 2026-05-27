#!/usr/bin/env python3
"""Ingest live HKJC odds by intercepting GraphQL responses from bet.hkjc.com.

The HKJC GraphQL API (info.cld.hkjc.com/graphql/base/) has a WHITELIST_ERROR
block on direct HTTP calls — only browser-originated requests from bet.hkjc.com
are accepted. So we use Playwright to navigate the SPA, intercept the racingBlock
GraphQL response, and extract structured WIN/PLA odds without fragile HTML scraping.

Usage:
    python scripts/ingest_odds.py --date 2026-05-28 --venue HV
    python scripts/ingest_odds.py --date 2026-05-28 --venue ST --race 3 --audit
"""

import argparse
import asyncio
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from playwright.async_api import async_playwright
from loguru import logger
from config import DATA_DIR
from db import init_db, save_odds_snapshot, get_race_ids_for_date, get_racecard, is_race_day, get_venue_for_date

GRAPHQL_PATH = "/graphql/base/"
ODDS_PAGE = "https://bet.hkjc.com/en/racing/wp/{date_path}/{venue}/{race_no}"
HOME_PAGE = "https://bet.hkjc.com/en/racing"


async def _wait_for_graphql(page, timeout: float = 30.0) -> dict | None:
    """Wait for the racingBlock GraphQL response and return parsed JSON."""
    graphql_data = None
    event = asyncio.Event()

    async def on_response(response):
        nonlocal graphql_data
        if event.is_set():
            return
        if GRAPHQL_PATH in response.url:
            try:
                data = await response.json()
                if data.get("data", {}).get("raceMeetings"):
                    graphql_data = data
                    event.set()
            except Exception:
                pass

    page.on("response", on_response)

    try:
        await asyncio.wait_for(event.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        logger.warning(f"Timeout ({timeout}s) waiting for GraphQL racingBlock response")

    page.remove_listener("response", on_response)
    await asyncio.sleep(0.5)  # let any in-flight handler finish
    return graphql_data


def _extract_odds(data: dict, date_str: str, venue: str, race_no: int) -> dict | None:
    """Extract WIN/PLA odds for a specific race from GraphQL response.

    Returns {"race_id": str, "win_odds": dict, "place_odds": dict} or None.
    """
    meetings = data.get("data", {}).get("raceMeetings", [])
    if not meetings:
        logger.warning("No raceMeetings in GraphQL response")
        return None

    # Find the meeting matching date + venue
    meeting = None
    for m in meetings:
        m_date = (m.get("date") or "").replace("/", "-")
        m_venue = (m.get("venueCode") or "").strip().upper()
        if m_date == date_str and m_venue == venue.upper():
            meeting = m
            break

    # Fallback: match by venue only (API may use different date format)
    if not meeting:
        for m in meetings:
            m_venue = (m.get("venueCode") or "").strip().upper()
            if m_venue == venue.upper():
                meeting = m
                break

    if not meeting:
        avail = [(m.get("date"), m.get("venueCode")) for m in meetings]
        logger.warning(f"No meeting found for {date_str} {venue} (available: {avail})")
        return None

    # Find the target race within the meeting
    target_race = None
    for r in meeting.get("races", []):
        if r.get("no") == race_no:
            target_race = r
            break

    if not target_race:
        logger.warning(f"Race {race_no} not found in meeting races")
        return None

    if target_race.get("status") == "Voided":
        logger.info(f"R{race_no}: voided — skipping")
        return None

    win_odds = {}
    place_odds = {}

    for pool in meeting.get("pmPools", []):
        odds_type = pool.get("oddsType")
        leg = pool.get("leg", {}) or {}
        leg_races = leg.get("races", []) if leg else []

        # Some pools have leg.races specifying which races they cover;
        # others are blank (meaning all races). Match accordingly.
        if leg_races and race_no not in leg_races:
            continue

        for odd in pool.get("odds", []):
            runner_no = str(odd.get("runnerNo", ""))
            odds_val = odd.get("odds")
            if not runner_no or odds_val is None:
                continue
            try:
                val = float(odds_val)
            except (ValueError, TypeError):
                continue
            if val <= 0:
                continue

            if odds_type == "WIN":
                win_odds[runner_no] = val
            elif odds_type == "PLA":
                place_odds[runner_no] = val

    if not win_odds:
        logger.warning(f"R{race_no}: no WIN odds extracted from {len(meeting.get('pmPools', []))} pools")
        return None

    return {
        "race_id": f"{date_str}_{venue}_R{race_no}",
        "win_odds": win_odds,
        "place_odds": place_odds,
    }


def _minutes_to_jump(jump_time_str: str) -> int | None:
    """Parse jump time and return minutes until jump. Returns None if unparseable."""
    now = datetime.now()
    for fmt in ("%H:%M", "%H:%M%p", "%I:%M%p", "%I:%M %p"):
        try:
            jt = datetime.strptime(jump_time_str.strip(), fmt)
            jt = jt.replace(year=now.year, month=now.month, day=now.day)
            return int((jt - now).total_seconds() / 60)
        except ValueError:
            continue
    return None


async def _prewarm(page):
    """Visit home page first to establish session cookies."""
    try:
        await page.goto(HOME_PAGE, wait_until="domcontentloaded", timeout=30000)
        logger.debug("Pre-warmed session on bet.hkjc.com")
    except Exception as e:
        logger.debug(f"Pre-warm failed (non-fatal): {e}")


async def main():
    parser = argparse.ArgumentParser(description="Ingest HKJC live odds via GraphQL")
    parser.add_argument("--date", default=datetime.now().strftime("%Y-%m-%d"))
    parser.add_argument("--venue", default=None, help="ST or HV (auto-resolved from DB if omitted)")
    parser.add_argument("--race", type=int, default=0, help="Single race #, or 0 for all")
    parser.add_argument("--audit", action="store_true", help="Trigger audit.py when T-15 detected")
    args = parser.parse_args()

    init_db()

    if not args.venue:
        args.venue = get_venue_for_date(args.date) or "ST"
        logger.info(f"Auto-resolved venue for {args.date} to: {args.venue}")

    if not is_race_day(args.date):
        logger.info(f"{args.date}: not a race day — skipping")
        return

    now_utc = datetime.now(timezone.utc).hour
    if not (3 <= now_utc <= 15):
        logger.info(f"UTC {now_utc:02d}:00 — outside HK racing window (03:00-15:00 UTC), skipping")
        return

    if args.race > 0:
        races_to_scrape = [
            {"race_id": f"{args.date}_{args.venue}_R{args.race}", "race_no": args.race, "jump_time": "13:00"}
        ]
    else:
        races_to_scrape = []
        for rid in get_race_ids_for_date(args.date, args.venue):
            rc = get_racecard(rid)
            if rc:
                races_to_scrape.append({
                    "race_id": rid,
                    "race_no": rc["race_no"],
                    "jump_time": rc.get("jump_time", "13:00"),
                })
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

        await _prewarm(page)

        # Start GraphQL interceptor BEFORE navigating — the SPA fires the
        # racingBlock query on page load and we must have the handler
        # registered before goto triggers the network request.
        graphql_task = asyncio.create_task(_wait_for_graphql(page))

        date_path = args.date.replace("-", "/")
        first_race_no = races_to_scrape[0]["race_no"]
        url = ODDS_PAGE.format(date_path=date_path, venue=args.venue, race_no=first_race_no)

        logger.debug(f"Navigating to {url}")
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            logger.warning(f"Page load error (non-fatal, checking GraphQL data): {e}")

        graphql_data = await graphql_task
        await context.close()

    if not graphql_data:
        logger.error("Failed to capture GraphQL response — no odds data available")
        sys.exit(1)

    logger.debug(f"Captured GraphQL response with {len(graphql_data.get('data', {}).get('raceMeetings', []))} meetings")

    ok = 0
    for r in races_to_scrape:
        try:
            result = _extract_odds(graphql_data, args.date, args.venue, r["race_no"])
            if result:
                save_odds_snapshot(result["race_id"], result["win_odds"], result["place_odds"])
                top3 = sorted(result["win_odds"].items(), key=lambda x: x[1])[:3]
                logger.info(f"R{r['race_no']}: {len(result['win_odds'])} horses — favs: {top3}")
                ok += 1

                if args.audit:
                    mins = _minutes_to_jump(r.get("jump_time", "13:00"))
                    if mins is not None and 10 <= mins <= 20:
                        logger.info(f"R{r['race_no']}: T-{mins}min — triggering audit...")
                        audit_script = str(Path(__file__).parent / "audit.py")
                        proc = subprocess.Popen(
                            [sys.executable, audit_script, "--race-id", result["race_id"]],
                            stdout=subprocess.DEVNULL,
                        )
                        logger.info(f"Audit PID {proc.pid} spawned for {result['race_id']}")

        except Exception as e:
            logger.error(f"R{r['race_no']}: {e}")

    logger.success(f"Odds scrape complete: {ok}/{len(races_to_scrape)} races")


if __name__ == "__main__":
    asyncio.run(main())
