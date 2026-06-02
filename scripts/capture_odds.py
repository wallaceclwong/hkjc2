#!/usr/bin/env python3
"""Race-day Solace WebSocket message capture for SMF parser development.

Two modes:
  capture  — Login, subscribe to odds topics, save all WS messages to disk
  analyze  — Read saved captures and decode/display WIN/PLA odds

Usage:
  # Capture mode (run during live races):
  python scripts/capture_odds.py capture --date 2026-06-03 --venue HV --duration 60

  # Analyze mode (run after capture):
  python scripts/capture_odds.py analyze --file data/ws_capture_2026-06-03_HV.json

  # Live analyze during capture (run in separate terminal):
  python scripts/capture_odds.py live --date 2026-06-03 --venue HV --duration 120
"""
import argparse
import asyncio
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
from playwright.async_api import async_playwright
from loguru import logger
from config import BASE_DIR, DATA_DIR
from db import init_db, get_race_ids_for_date, get_racecard, is_race_day, get_venue_for_date
from scripts.ingest_odds import (
    _hkjc_login,
    _subscribe_odds,
    _parse_ws_odds_messages,
    _resolve_race_targets,
    WS_HIJACK_SCRIPT,
    HOME_PAGE,
    ODDS_PAGE,
)

load_dotenv(BASE_DIR / ".env")


async def capture_mode(args):
    """Login, subscribe, capture all WS messages for --duration seconds."""
    init_db()

    if not args.venue:
        args.venue = get_venue_for_date(args.date) or "ST"

    if not is_race_day(args.date):
        logger.warning(f"{args.date}: not a race day — odds unlikely")
        if not args.force:
            logger.info("Use --force to capture anyway")
            return

    races_to_scrape = _resolve_race_targets(args.date, args.venue, args.race)
    if not races_to_scrape:
        logger.warning(f"No racecards for {args.date} {args.venue}. Run ingest_racecards.py first.")
        return

    user_data = DATA_DIR / "browser_session_capture"
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

        await page.add_init_script(WS_HIJACK_SCRIPT)

        # Login
        logged_in = await _hkjc_login(page, context)
        if not logged_in:
            logger.error("Login failed — cannot capture odds")
            await context.close()
            return

        # Load SPA and navigate to first race
        logger.info("Loading SPA...")
        await page.goto(HOME_PAGE, wait_until="domcontentloaded", timeout=30000)

        first_no = races_to_scrape[0]["race_no"]
        date_path = args.date.replace("-", "/")
        odds_url = ODDS_PAGE.format(date_path=date_path, venue=args.venue, race_no=first_no)
        await page.goto(odds_url, wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(5)

        # Subscribe to odds topics
        logger.info(f"Subscribing to {len(races_to_scrape)} WIN + {len(races_to_scrape)} PLA topics...")
        await _subscribe_odds(page, races_to_scrape, args.date, args.venue)

        # Capture loop
        deadline = asyncio.get_event_loop().time() + args.duration
        logger.info(f"Capturing for {args.duration}s (until T+{args.duration}s)...")

        # Poll for new messages and show stats
        last_count = 0
        while asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(2)
            stats = await page.evaluate("() => window.__getMessageStats()")
            if stats["total"] != last_count:
                logger.info(f"  Messages: {stats['total']} total, {stats['data']} data, {stats['ack']} ACKs")
                last_count = stats["total"]

        # Fetch all messages
        all_msgs = await page.evaluate("() => window.__ws_recv_raw")
        data_msgs = [m for m in all_msgs if m.get("cls") == "data"]
        ack_msgs = [m for m in all_msgs if m.get("cls") == "ack"]

        logger.info(f"Capture complete: {len(all_msgs)} total, {len(data_msgs)} data, {len(ack_msgs)} ACKs")

        # Save to file
        output_path = DATA_DIR / f"ws_capture_{args.date}_{args.venue}.json"
        capture_data = {
            "date": args.date,
            "venue": args.venue,
            "captured_at": datetime.now().isoformat(),
            "duration": args.duration,
            "races": [r["race_no"] for r in races_to_scrape],
            "stats": {"total": len(all_msgs), "data": len(data_msgs), "ack": len(ack_msgs)},
            "messages": all_msgs,  # Full raw messages with bytes, hex, text
        }
        with open(output_path, "w") as f:
            json.dump(capture_data, f, default=str)
        logger.success(f"Saved {len(all_msgs)} messages to {output_path}")

        # Quick parse attempt
        if data_msgs:
            logger.info("Attempting to parse odds...")
            odds = _parse_ws_odds_messages(data_msgs)
            if odds:
                for race_no, race_data in sorted(odds.items()):
                    win = race_data.get("win_odds", {})
                    pla = race_data.get("place_odds", {})
                    if win:
                        top3 = sorted(win.items(), key=lambda x: x[1])[:3]
                        logger.info(f"  R{race_no} WIN: {len(win)} horses, favs: {top3}")
                    if pla:
                        top3p = sorted(pla.items(), key=lambda x: x[1])[:3]
                        logger.info(f"  R{race_no} PLA: {len(pla)} horses, favs: {top3p}")
            else:
                logger.warning("No odds parsed — check data message format")
                for i, msg in enumerate(data_msgs[:5]):
                    logger.info(f"  msg[{i}]: {msg['len']}b text={msg.get('text','')[:200]}")

        await context.close()


def analyze_mode(args):
    """Analyze a saved capture file and decode odds."""
    capture_path = Path(args.file)
    if not capture_path.exists():
        logger.error(f"File not found: {args.file}")
        return

    with open(capture_path) as f:
        data = json.load(f)

    logger.info(f"Capture: {data['date']} {data['venue']} — {data['stats']['total']} messages")
    logger.info(f"Captured at: {data.get('captured_at', 'unknown')}")
    logger.info(f"Races: {data.get('races', [])}")

    messages = data.get("messages", [])
    data_msgs = [m for m in messages if m.get("cls") == "data"]
    ack_msgs = [m for m in messages if m.get("cls") == "ack"]

    # Show ACK pattern
    logger.info(f"\nACK messages: {len(ack_msgs)}")
    for msg in ack_msgs[:5]:
        logger.info(f"  {msg['len']}b: {msg.get('text','')[:100]}")

    # Show data message hex dumps
    logger.info(f"\nData messages: {len(data_msgs)}")
    if args.verbose:
        for i, msg in enumerate(data_msgs[:10]):
            logger.info(f"\n--- Data msg {i} ({msg['len']} bytes) ---")
            logger.info(f"  Hex (first 80): {msg.get('hex','')[:240]}")
            logger.info(f"  Text: {msg.get('text','')[:300]}")

    # Parse odds
    logger.info(f"\n=== Parsing odds ===")
    odds = _parse_ws_odds_messages(data_msgs)
    if odds:
        for race_no, race_data in sorted(odds.items()):
            win = race_data.get("win_odds", {})
            pla = race_data.get("place_odds", {})
            if win:
                sorted_win = sorted(win.items(), key=lambda x: x[1])
                logger.info(f"R{race_no} WIN: {sorted_win}")
            if pla:
                sorted_pla = sorted(pla.items(), key=lambda x: x[1])
                logger.info(f"R{race_no} PLA: {sorted_pla}")
    else:
        logger.warning("No odds could be parsed")
        # Show ASCII text of all data messages for manual analysis
        logger.info("\nAll data message ASCII text:")
        for i, msg in enumerate(data_msgs):
            text = msg.get("text", "")
            if len(text) > 20:
                logger.info(f"  [{i}] {text[:300]}")


async def live_mode(args):
    """Live capture with periodic odds parsing."""
    init_db()

    if not args.venue:
        args.venue = get_venue_for_date(args.date) or "ST"

    races_to_scrape = _resolve_race_targets(args.date, args.venue, args.race)
    if not races_to_scrape:
        logger.warning(f"No racecards for {args.date} {args.venue}.")
        return

    user_data = DATA_DIR / "browser_session_capture"
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

        await page.add_init_script(WS_HIJACK_SCRIPT)

        logged_in = await _hkjc_login(page, context)
        if not logged_in:
            logger.error("Login failed")
            await context.close()
            return

        await page.goto(HOME_PAGE, wait_until="domcontentloaded", timeout=30000)
        first_no = races_to_scrape[0]["race_no"]
        date_path = args.date.replace("-", "/")
        odds_url = ODDS_PAGE.format(date_path=date_path, venue=args.venue, race_no=first_no)
        await page.goto(odds_url, wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(5)

        await _subscribe_odds(page, races_to_scrape, args.date, args.venue)

        deadline = asyncio.get_event_loop().time() + args.duration
        logger.info(f"Live monitoring for {args.duration}s...")

        while asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(args.interval)

            # Get new data messages
            data_msgs = await page.evaluate("() => window.__getDataMessages()")
            stats = await page.evaluate("() => window.__getMessageStats()")

            if data_msgs:
                odds = _parse_ws_odds_messages(data_msgs)
                if odds:
                    logger.info(f"--- {datetime.now().strftime('%H:%M:%S')} ({stats['data']} data msgs) ---")
                    for race_no, race_data in sorted(odds.items()):
                        win = race_data.get("win_odds", {})
                        pla = race_data.get("place_odds", {})
                        if win:
                            top3 = sorted(win.items(), key=lambda x: x[1])[:3]
                            logger.info(f"  R{race_no} WIN: favs={top3} ({len(win)} horses)")
                        if pla:
                            top3p = sorted(pla.items(), key=lambda x: x[1])[:3]
                            logger.info(f"  R{race_no} PLA: favs={top3p} ({len(pla)} horses)")
                else:
                    logger.debug(f"  {stats['data']} data msgs, no odds parsed yet")
            else:
                logger.debug(f"  No data messages yet ({stats['total']} total, {stats['ack']} ACKs)")

        await context.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Capture and analyze HKJC Solace WebSocket odds")
    sub = parser.add_subparsers(dest="mode", required=True)

    # Capture mode
    cap = sub.add_parser("capture", help="Capture WS messages to file")
    cap.add_argument("--date", default=datetime.now().strftime("%Y-%m-%d"))
    cap.add_argument("--venue", default=None)
    cap.add_argument("--race", type=int, default=0)
    cap.add_argument("--duration", type=int, default=60, help="Capture duration in seconds")
    cap.add_argument("--force", action="store_true", help="Capture even on non-race days")

    # Analyze mode
    ana = sub.add_parser("analyze", help="Analyze saved capture file")
    ana.add_argument("--file", required=True, help="Path to capture JSON file")
    ana.add_argument("--verbose", "-v", action="store_true", help="Show hex dumps")

    # Live mode
    live = sub.add_parser("live", help="Live capture with periodic odds display")
    live.add_argument("--date", default=datetime.now().strftime("%Y-%m-%d"))
    live.add_argument("--venue", default=None)
    live.add_argument("--race", type=int, default=0)
    live.add_argument("--duration", type=int, default=300, help="Total monitoring duration in seconds")
    live.add_argument("--interval", type=int, default=10, help="Parse interval in seconds")

    args = parser.parse_args()

    if args.mode == "capture":
        asyncio.run(capture_mode(args))
    elif args.mode == "analyze":
        analyze_mode(args)
    elif args.mode == "live":
        asyncio.run(live_mode(args))
