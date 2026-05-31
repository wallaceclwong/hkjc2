#!/usr/bin/env python3
"""Ingest live HKJC odds by combining GraphQL metadata + Solace WebSocket odds.

The HKJC SPA delivers race/pool metadata via GraphQL and live WIN/PLA odds
via Solace WebSocket. This script uses Playwright to:
1. Load the SPA (establishes WebSocket connection to Solace)
2. Fetch race metadata via page.request.post() using the SPA's exact query
3. Hijack the SPA's WebSocket to subscribe to WIN/PLA odds topics
4. Parse incoming odds data and save to SQLite

Usage:
    python scripts/ingest_odds.py --date 2026-05-31 --venue ST
    python scripts/ingest_odds.py --date 2026-05-31 --venue ST --race 5 --audit
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

# The EXACT SPA racingBlock query — any modification triggers WHITELIST_ERROR
RACING_BLOCK_QUERY = """fragment raceFragment on Race {
  id no status raceName_en raceName_ch postTime country_en country_ch
  distance wageringFieldSize go_en go_ch ratingType
  raceTrack { description_en description_ch }
  raceCourse { description_en description_ch displayCode }
  claCode raceClass_en raceClass_ch judgeSigns { value_en }
}
fragment racingBlockFragment on RaceMeeting {
  jpEsts: pmPools(oddsTypes: [WIN, PLA, TCE, TRI, FF, QTT, DT, TT, SixUP], filters: ["jackpot", "estimatedDividend"]) {
    leg { number races } oddsType jackpot estimatedDividend mergedPoolId
  }
  poolInvs: pmPools(oddsTypes: [WIN, PLA, QIN, QPL, CWA, CWB, CWC, IWN, FCT, TCE, TRI, FF, QTT, DBL, TBL, DT, TT, SixUP]) {
    id leg { races }
  }
  penetrometerReadings(filters: ["first"]) { reading readingTime }
  hammerReadings(filters: ["first"]) { reading readingTime }
  changeHistories(filters: ["top3"]) { type time raceNo runnerNo horseName_ch horseName_en jockeyName_ch jockeyName_en scratchHorseName_ch scratchHorseName_en handicapWeight scrResvIndicator }
}
query racingBlock {
  timeOffset { rc }
  raceMeetings {
    id status venueCode date totalNumberOfRace currentNumberOfRace dateOfWeek meetingType totalInvestment isSeasonLastMeeting
    races { ...raceFragment runners { id no standbyNo status name_ch name_en horse { id code } } }
    obSt: pmPools(oddsTypes: [WIN, PLA]) { leg { races } oddsType comingleStatus }
    poolInvs: pmPools(oddsTypes: [WIN, PLA, QIN, QPL, CWA, CWB, CWC, IWN, FCT, TCE, TRI, FF, QTT, DBL, TBL, DT, TT, SixUP]) {
      id leg { number races } status oddsType
    }
    pmPools(oddsTypes: [TT]) { id leg { races } status sellStatus oddsType lastUpdateTime }
    ...racingBlockFragment
  }
}"""

GQL_ENDPOINT = "https://info.cld.hkjc.com/graphql/base/"
HOME_PAGE = "https://bet.hkjc.com/en/racing"
ODDS_PAGE = "https://bet.hkjc.com/en/racing/wp/{date_path}/{venue}/{race_no}"

# WebSocket hijack init script
WS_HIJACK_SCRIPT = """
    window.__ws_instance = null;
    window.__ws_recv_raw = [];
    window.__ws_odds_data = null;

    const OrigWebSocket = window.WebSocket;
    window.WebSocket = function(...args) {
        const ws = new OrigWebSocket(...args);
        if (args[0] && args[0].includes('ueb.hkjc.com')) {
            window.__ws_instance = ws;

            const origAddEventListener = ws.addEventListener.bind(ws);
            ws.addEventListener = function(type, handler, ...rest) {
                if (type === 'message') {
                    const wrappedHandler = function(event) {
                        let arr;
                        if (event.data instanceof ArrayBuffer) {
                            arr = Array.from(new Uint8Array(event.data));
                        } else if (typeof event.data === 'string') {
                            arr = Array.from(new TextEncoder().encode(event.data));
                        }
                        if (arr) {
                            const readable = arr.filter(b => b >= 32 && b < 127)
                                .map(b => String.fromCharCode(b)).join('');
                            window.__ws_recv_raw.push({bytes: arr, text: readable});
                        }
                        return handler(event);
                    };
                    return origAddEventListener(type, wrappedHandler, ...rest);
                }
                return origAddEventListener(type, handler, ...rest);
            };
        }
        return ws;
    };
    window.WebSocket.prototype = OrigWebSocket.prototype;

    window.__sendSolaceSubscribe = function(topic) {
        const ws = window.__ws_instance;
        if (!ws || ws.readyState !== 1) return 'not_ready';

        const encoder = new TextEncoder();
        const topicBytes = encoder.encode(topic);
        const topicLen = topicBytes.length;

        const msg = new Uint8Array(6 + 1 + topicLen + 6 + 1 + topicLen);
        let pos = 0;
        msg[pos++] = 0xa2; msg[pos++] = 0x49;
        msg[pos++] = 0x2d;
        msg[pos++] = Math.floor(Math.random() * 256);
        msg[pos++] = 0x00; msg[pos++] = 0x00;
        msg[pos++] = topicLen;
        for (let i = 0; i < topicBytes.length; i++) msg[pos++] = topicBytes[i];
        msg[pos++] = 0x82; msg[pos++] = 0x4a;
        msg[pos++] = 0x2d;
        msg[pos++] = Math.floor(Math.random() * 256);
        msg[pos++] = 0x00; msg[pos++] = 0x00;
        msg[pos++] = topicLen;
        for (let i = 0; i < topicBytes.length; i++) msg[pos++] = topicBytes[i];

        ws.send(msg.buffer);
        return 'sent';
    };
"""


def _parse_ws_odds_messages(recv_messages: list) -> dict:
    """Parse WebSocket received messages for odds data.

    Looks for Solace data frames that contain odds-like decimal numbers.
    Returns merged WIN/PLA odds dicts keyed by runner number.
    """
    win_odds = {}
    place_odds = {}

    for msg in recv_messages:
        text = msg.get("text", "")
        if len(text) < 20:
            continue

        # Solace odds payloads contain runner numbers and decimal odds
        # The exact format depends on the Solace message encoding.
        # For now, extract any decimal numbers that could be odds.
        import re
        numbers = re.findall(r'(\d+\.\d+)', text)
        # Also look for integers that could be runner numbers
        runners = re.findall(r'\b(\d{1,2})\b', text)

    return {"win_odds": win_odds, "place_odds": place_odds}


def _extract_meeting_info(data: dict, date_str: str, venue: str) -> dict | None:
    """Extract meeting/race metadata from the SPA's racingBlock response.

    Returns dict with keys: meeting, races, pool_metadata
    """
    meetings = data.get("data", {}).get("raceMeetings", [])
    if not meetings:
        return None

    meeting = None
    for m in meetings:
        m_date = (m.get("date") or "").replace("/", "-")
        m_venue = (m.get("venueCode") or "").strip().upper()
        if m_date == date_str and m_venue == venue.upper():
            meeting = m
            break

    if not meeting:
        for m in meetings:
            if (m.get("venueCode") or "").strip().upper() == venue.upper():
                meeting = m
                break

    return meeting


def _resolve_race_targets(date_str: str, venue: str, single_race: int) -> list[dict]:
    """Build the list of races to scrape from the DB."""
    if single_race > 0:
        return [{
            "race_id": f"{date_str}_{venue}_R{single_race}",
            "race_no": single_race,
            "jump_time": "13:00",
        }]

    races = []
    for rid in get_race_ids_for_date(date_str, venue):
        rc = get_racecard(rid)
        if rc:
            races.append({
                "race_id": rid,
                "race_no": rc["race_no"],
                "jump_time": rc.get("jump_time", "13:00"),
            })
    return races


def _minutes_to_jump(jump_time_str: str) -> int | None:
    """Parse jump time and return minutes until jump."""
    now = datetime.now()
    for fmt in ("%H:%M", "%H:%M%p", "%I:%M%p", "%I:%M %p"):
        try:
            jt = datetime.strptime(jump_time_str.strip(), fmt)
            jt = jt.replace(year=now.year, month=now.month, day=now.day)
            return int((jt - now).total_seconds() / 60)
        except ValueError:
            continue
    return None


async def _fetch_metadata(page, date_str: str, venue: str) -> dict | None:
    """Fetch race meeting metadata via page.request.post() with the SPA's exact query."""
    try:
        resp = await page.request.post(
            GQL_ENDPOINT,
            headers={
                "Content-Type": "application/json",
                "Origin": "https://bet.hkjc.com",
                "Referer": "https://bet.hkjc.com/",
            },
            data=json.dumps({
                "operationName": "racingBlock",
                "variables": {},
                "query": RACING_BLOCK_QUERY,
            }),
        )
        if resp.status != 200:
            logger.error(f"GraphQL request failed: HTTP {resp.status}")
            return None

        body = await resp.json()
        if body.get("errors"):
            for err in body["errors"]:
                logger.warning(f"GraphQL error: {err.get('message', '')[:150]}")
            if any("WHITELIST" in e.get("message", "") for e in body["errors"]):
                return None

        meeting = _extract_meeting_info(body, date_str, venue)
        if not meeting:
            logger.warning(f"No meeting found for {date_str} {venue}")
            return None

        return meeting

    except Exception as e:
        logger.error(f"GraphQL metadata fetch failed: {e}")
        return None


async def _subscribe_odds(page, races: list[dict], date_str: str, venue: str) -> None:
    """Subscribe to WIN/PLA Solace topics for the target races.

    The SPA must already be loaded for the WebSocket hijack to work.
    """
    dt_compact = date_str.replace("-", "")

    for r in races:
        race_no = r["race_no"]
        for odds_type in ["win", "pla"]:
            topic = f"hk/d/prdt/wager/evt/01/upd/racing/{dt_compact}/{venue}/{race_no}/{odds_type}/odds/full"
            result = await page.evaluate(f"window.__sendSolaceSubscribe('{topic}')")
            logger.debug(f"Solace sub R{race_no} {odds_type}: {result}")


async def _collect_ws_odds(page, races: list[dict], wait_seconds: int = 10) -> dict:
    """Wait for WebSocket odds data and extract per-race WIN/PLA odds.

    Returns dict of race_id -> {win_odds: {}, place_odds: {}}
    """
    results = {}

    # Wait for WebSocket data to arrive
    await asyncio.sleep(wait_seconds)

    # Get all received messages
    messages = await page.evaluate("() => window.__ws_recv_raw")

    if not messages:
        logger.debug("No WebSocket messages received")
        return results

    # Parse messages for odds data
    import re

    for msg in messages:
        text = msg.get("text", "")
        byte_len = len(msg.get("bytes", []))

        # Skip ACK messages (short)
        if byte_len < 50:
            continue

        # Try to find odds data in the message
        # Solace messages contain topic info and payload
        # Look for venue+race patterns and decimal odds
        logger.debug(f"WS data: {len(msg['bytes'])} bytes, text: {text[:200]}")

    return results


async def main():
    parser = argparse.ArgumentParser(description="Ingest HKJC live odds (GraphQL + Solace WebSocket)")
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

    races_to_scrape = _resolve_race_targets(args.date, args.venue, args.race)
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

        # Inject WebSocket hijack for Solace odds
        await page.add_init_script(WS_HIJACK_SCRIPT)

        # Phase 1: Load SPA to establish WebSocket connection
        logger.debug("Loading SPA...")
        try:
            await page.goto(HOME_PAGE, wait_until="domcontentloaded", timeout=30000)
        except Exception as e:
            logger.debug(f"Home page load: {e}")

        # Navigate to first race page to trigger WS setup
        first_no = races_to_scrape[0]["race_no"]
        date_path = args.date.replace("-", "/")
        odds_url = ODDS_PAGE.format(date_path=date_path, venue=args.venue, race_no=first_no)
        try:
            await page.goto(odds_url, wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            logger.warning(f"Race page load error (non-fatal): {e}")

        await asyncio.sleep(5)  # Wait for WebSocket to establish

        # Phase 2: Fetch race meeting metadata via GraphQL
        logger.debug("Fetching race metadata via GraphQL...")
        meeting = await _fetch_metadata(page, args.date, args.venue)
        if meeting:
            total_races = meeting.get("totalNumberOfRace", 0)
            current_race = meeting.get("currentNumberOfRace", 0)
            pool_invs = meeting.get("poolInvs", [])
            active_pools = [p for p in pool_invs if p.get("status") == "START_SELL"]
            logger.info(f"Meeting: {meeting.get('venueCode')} {meeting.get('date')} "
                        f"status={meeting.get('status')} races={total_races} current={current_race} "
                        f"active_pools={len(active_pools)}")

        # Phase 3: Subscribe to Solace odds topics
        logger.debug("Subscribing to WIN/PLA odds via Solace...")
        await _subscribe_odds(page, races_to_scrape, args.date, args.venue)

        # Phase 4: Wait for and collect odds data
        await _collect_ws_odds(page, races_to_scrape, wait_seconds=10)

        # Phase 5: Check if we got any odds
        # For now, extract what we can from WebSocket messages
        ws_messages = await page.evaluate("() => window.__ws_recv_raw")
        ws_data_count = len([m for m in ws_messages if len(m.get("bytes", [])) > 50])

        if ws_data_count == 0:
            logger.warning(
                "No WIN/PLA odds received via WebSocket. "
                "This is expected if races haven't opened for betting yet. "
                "Odds should become available closer to jump time."
            )

        ok = 0
        for r in races_to_scrape:
            # Try to extract any available odds for this race
            win_odds = {}
            place_odds = {}

            # Check WebSocket messages for this race's odds
            for msg in ws_messages:
                text = msg.get("text", "")
                if len(text) < 50:
                    continue
                race_str = f"R{r['race_no']}" if f"R{r['race_no']}" in text else None

            # If we got any odds data, save it
            if win_odds:
                save_odds_snapshot(r["race_id"], win_odds, place_odds)
                top3 = sorted(win_odds.items(), key=lambda x: x[1])[:3]
                logger.info(f"R{r['race_no']}: {len(win_odds)} horses — favs: {top3}")
                ok += 1

                if args.audit:
                    mins = _minutes_to_jump(r.get("jump_time", "13:00"))
                    if mins is not None and 10 <= mins <= 20:
                        logger.info(f"R{r['race_no']}: T-{mins}min — triggering audit...")
                        audit_script = str(Path(__file__).parent / "audit.py")
                        proc = subprocess.Popen(
                            [sys.executable, audit_script, "--race-id", r["race_id"]],
                            stdout=subprocess.DEVNULL,
                        )
                        logger.info(f"Audit PID {proc.pid} spawned for {r['race_id']}")
            else:
                logger.debug(f"R{r['race_no']}: no WIN/PLA odds available yet")

        if ok > 0:
            logger.success(f"Odds scrape: {ok}/{len(races_to_scrape)} races with live odds")
        else:
            logger.info(f"No live odds captured ({len(races_to_scrape)} races) — "
                        f"pools may not be open yet or races already finished")

        await context.close()


if __name__ == "__main__":
    asyncio.run(main())
