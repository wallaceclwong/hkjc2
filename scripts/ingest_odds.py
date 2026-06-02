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
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
from playwright.async_api import async_playwright
from loguru import logger
from config import BASE_DIR, DATA_DIR
from db import init_db, save_odds_snapshot, get_race_ids_for_date, get_racecard, is_race_day, get_venue_for_date
from notify import send_telegram_sync

load_dotenv(BASE_DIR / ".env")

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
ODDS_PAGE = "https://bet.hkjc.com/en/racing/wp/{date}/{venue}/{race_no}"

# WebSocket hijack init script
WS_HIJACK_SCRIPT = """
    window.__ws_instance = null;
    window.__ws_recv_raw = [];
    window.__ws_odds_data = {};
    window.__ws_sub_topic_map = {};

    const OrigWebSocket = window.WebSocket;
    window.WebSocket = function(...args) {
        const ws = new OrigWebSocket(...args);
        if (args[0] && args[0].includes('ueb.hkjc.com')) {
            // Always update to the LATEST Solace WS instance
            window.__ws_instance = ws;
            window.__ws_url = args[0];

            const origAddEventListener = ws.addEventListener.bind(ws);
            ws.addEventListener = function(type, handler, ...rest) {
                if (type === 'message') {
                    const wrappedHandler = function(event) {
                        let arr = null;
                        if (event.data instanceof ArrayBuffer) {
                            arr = Array.from(new Uint8Array(event.data));
                        } else if (typeof event.data === 'string') {
                            arr = Array.from(new TextEncoder().encode(event.data));
                        } else if (event.data && typeof event.data === 'object' && event.data.constructor === Uint8Array) {
                            arr = Array.from(event.data);
                        }
                        if (arr && arr.length > 0) {
                            const now = Date.now();
                            // Full hex dump for analysis
                            const hex = arr.map(b => (b < 16 ? '0' : '') + b.toString(16)).join(' ');
                            // ASCII-readable text only
                            const text = arr.filter(b => b >= 32 && b < 127)
                                .map(b => String.fromCharCode(b)).join('');
                            // Classify: ACK (0-50 bytes), data (50+ bytes)
                            const cls = arr.length <= 50 ? 'ack' : 'data';
                            window.__ws_recv_raw.push({
                                ts: now,
                                len: arr.length,
                                cls: cls,
                                bytes: arr,
                                hex: hex,
                                text: text
                            });
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

        const corrId1 = Math.floor(Math.random() * 256);
        const corrId2 = Math.floor(Math.random() * 256);
        window.__ws_sub_topic_map[corrId1] = topic;
        window.__ws_sub_topic_map[corrId2] = topic;

        // SMF subscribe: 2-part message (cache request + live sub)
        const msg = new Uint8Array(6 + 1 + topicLen + 6 + 1 + topicLen);
        let pos = 0;
        // Part 1: Cache/last-value request (0xa2 0x49)
        msg[pos++] = 0xa2; msg[pos++] = 0x49;
        msg[pos++] = 0x2d;  // subscribe operation
        msg[pos++] = corrId1;
        msg[pos++] = 0x00; msg[pos++] = 0x00;
        msg[pos++] = topicLen;
        for (let i = 0; i < topicBytes.length; i++) msg[pos++] = topicBytes[i];
        // Part 2: Live subscription (0x82 0x4a)
        msg[pos++] = 0x82; msg[pos++] = 0x4a;
        msg[pos++] = 0x2d;  // subscribe operation
        msg[pos++] = corrId2;
        msg[pos++] = 0x00; msg[pos++] = 0x00;
        msg[pos++] = topicLen;
        for (let i = 0; i < topicBytes.length; i++) msg[pos++] = topicBytes[i];

        ws.send(msg.buffer);
        return 'sent:' + corrId1 + ',' + corrId2;
    };

    // Helper: get captured data messages (skip ACKs)
    window.__getDataMessages = function() {
        return window.__ws_recv_raw.filter(m => m.cls === 'data');
    };

    // Helper: get message count by class
    window.__getMessageStats = function() {
        const stats = {total: window.__ws_recv_raw.length, ack: 0, data: 0};
        for (const m of window.__ws_recv_raw) {
            if (m.cls === 'ack') stats.ack++;
            else stats.data++;
        }
        return stats;
    };
"""


def _parse_smf_header(msg_bytes: list) -> dict:
    """Parse SMF (Solace Message Format) binary header.

    SMF header structure (minimal):
      Byte 0-1: Frame marker (e.g. 0xa2 0x49 for cache-req, 0x82 0x4a for live-sub)
      Byte 2:   Operation code (0x2d = subscribe, varies for data)
      Byte 3:   Correlation ID
      Byte 4-5: Flags/reserved
      Byte 6:   Topic length (if present)
      Byte 7+:  Topic bytes (if present)

    Returns dict with parsed fields or None if not a valid SMF frame.
    """
    if len(msg_bytes) < 7:
        return None

    header = {
        "marker": f"{msg_bytes[0]:02x} {msg_bytes[1]:02x}",
        "op": msg_bytes[2],
        "corr_id": msg_bytes[3],
        "flags": (msg_bytes[4], msg_bytes[5]),
        "total_len": len(msg_bytes),
    }

    # Check if this looks like an SMF data frame
    # Common patterns: 0xa2 0x49 = cache req, 0x82 0x4a = live sub
    # Data frames might have different markers
    if msg_bytes[0] in (0xa2, 0x82, 0x00, 0x01) and msg_bytes[1] in (0x49, 0x4a, 0x48, 0x4b):
        header["is_smf"] = True
    else:
        header["is_smf"] = False

    # Try to extract topic if this is a data message containing one
    # Topic often starts at byte 6-7 with a length prefix
    topic_len = msg_bytes[6] if len(msg_bytes) > 6 else 0
    if topic_len > 0 and topic_len < 200 and 7 + topic_len <= len(msg_bytes):
        try:
            topic_bytes = bytes(msg_bytes[7:7 + topic_len])
            topic = topic_bytes.decode("ascii", errors="replace")
            if topic.startswith("hk/") and "/" in topic:
                header["topic"] = topic
                header["payload_start"] = 7 + topic_len
        except Exception:
            pass

    return header


def _parse_ws_odds_messages(recv_messages: list) -> dict:
    """Parse WebSocket received messages for WIN/PLA odds data.

    Each SMF data message contains:
    - SMF header (7+ bytes)
    - Topic (length-prefixed ASCII string)
    - Payload: runner-number -> odds mappings in key=value format

    Returns dict: {race_no: {win_odds: {runner_no: price}, place_odds: {runner_no: price}}}
    """
    import re

    results = {}

    for msg in recv_messages:
        if msg.get("cls") != "data":
            continue

        arr = msg.get("bytes", [])
        text = msg.get("text", "")
        msg_len = len(arr)

        if msg_len < 50 or len(text) < 20:
            continue

        # Parse SMF header
        header = _parse_smf_header(arr)

        # Determine topic (race_no + odds_type) from header or text
        topic = header.get("topic", "") if header else ""
        race_no = None
        odds_type = None  # 'win' or 'pla'

        # Try to extract topic info from the ASCII text
        # Topic format: hk/d/prdt/wager/evt/01/upd/racing/{YYYYMMDD}/{venue}/{race_no}/{win|pla}/odds/full
        topic_match = re.search(r'hk/[a-z/]*racing/\d+/\w+/(\d+)/(win|pla)/odds', text)
        if topic_match:
            race_no = int(topic_match.group(1))
            odds_type = topic_match.group(2)

        # Extract runner odds from payload
        # The payload typically contains ASCII like "1=3.5,2=12.0,..." or similar
        # Look for patterns: digit(s) followed by = or : then decimal number
        odds_entries = re.findall(r'(\d{1,2})\s*[=:]\s*(\d+\.\d+)', text)
        if not odds_entries:
            # Try looser pattern: look for decimal numbers near small integers
            decimals = re.findall(r'(\d+\.\d+)', text)
            small_ints = re.findall(r'\b(\d{1,2})\b', text)
            # Only match if counts are similar (one odds value per runner)
            if len(decimals) == len(small_ints) and 4 <= len(decimals) <= 14:
                odds_entries = list(zip(small_ints, decimals))
            elif len(decimals) >= 4:
                # Fallback: assign sequential runner numbers
                odds_entries = [(str(i + 1), d) for i, d in enumerate(decimals[:14])]

        if odds_entries and (race_no or odds_type):
            # Infer race_no from topic text if not found
            if not race_no:
                race_match = re.search(r'/racing/\d+/\w+/(\d+)/', text)
                if race_match:
                    race_no = int(race_match.group(1))

            if not odds_type:
                if 'pla' in text.lower() or '/pla/' in text:
                    odds_type = 'pla'
                elif 'win' in text.lower() or '/win/' in text:
                    odds_type = 'win'

            if race_no:
                if race_no not in results:
                    results[race_no] = {"win_odds": {}, "place_odds": {}}

                odds_dict = {}
                for runner, price_str in odds_entries:
                    try:
                        odds_dict[str(int(runner))] = float(price_str)
                    except (ValueError, TypeError):
                        pass

                if odds_dict:
                    if odds_type == 'pla':
                        results[race_no]["place_odds"].update(odds_dict)
                    else:
                        results[race_no]["win_odds"].update(odds_dict)

    return results


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


async def _scrape_odds_from_page(page, date_str: str, venue: str, race_no: int) -> dict:
    """Scrape WIN/PLA odds from the SPA WP page DOM.

    The WP page renders odds in a text table:
      Horse number (line)
      Horse name + draw + weight + jockey + trainer (line)
      WIN odds (line)
      PLACE odds (line)
      [empty line]

    Returns {"win_odds": {runner: price}, "place_odds": {runner: price}}
    """
    url = ODDS_PAGE.format(date=date_str, venue=venue, race_no=race_no)
    try:
        await page.goto(url, timeout=30000)
        await asyncio.sleep(8)  # Generous wait for SPA to render
    except Exception:
        logger.debug(f"R{race_no}: page load warning, continuing")

    lines = await page.evaluate("""() => {
        return document.body.innerText.split('\\n').map(l => l.trim());
    }""")

    if len(lines) < 50:
        logger.warning(f"R{race_no}: only {len(lines)} lines — page may have redirected")

    win_odds = {}
    place_odds = {}

    # Find the horse table header to anchor parsing
    # Format: "No. Colour Horse Name Draw Wt. Jockey Trainer Win Place Win & Place"
    header_idx = None
    for i, line in enumerate(lines):
        if "Horse Name" in line and "Draw" in line and "Win" in line:
            header_idx = i
            break

    if header_idx is None:
        # Debug: show lines that might contain part of the header
        candidates = [l for l in lines if "Horse" in l or "Colour" in l or "Draw" in l[:50]]
        logger.debug(
            f"R{race_no}: could not find horse table header "
            f"(lines={len(lines)}, candidates={len(candidates)}: {candidates[:3]})"
        )
        return {"win_odds": win_odds, "place_odds": place_odds}

    # Parse horse rows starting after the header
    # Pattern: horse_no -> name+details -> WIN odds -> PLACE odds -> [blank]
    i = header_idx + 1
    while i < len(lines) - 3:
        horse_line = lines[i]
        if horse_line.isdigit() and 1 <= int(horse_line) <= 14:
            runner_no = horse_line
            name_line = lines[i + 1]
            win_line = lines[i + 2]
            place_line = lines[i + 3]

            # Name line should be longer than ~10 chars (contains horse name + draw + wt + jockey + trainer)
            # Win/Place lines should be parseable as numbers
            if (name_line and len(name_line) > 10 and
                win_line.replace(".", "").replace(",", "").replace("-", "").isdigit() and
                place_line.replace(".", "").replace(",", "").replace("-", "").isdigit()):

                try:
                    win_val = float(win_line.replace(",", ""))
                    place_val = float(place_line.replace(",", ""))
                    # Sanity: odds should be reasonable (not pool totals in millions)
                    if 1.0 <= win_val <= 999 and 1.0 <= place_val <= 999:
                        win_odds[runner_no] = win_val
                        place_odds[runner_no] = place_val
                        i += 5  # Skip blank line after place odds
                        continue
                except ValueError:
                    pass
        i += 1

    logger.debug(
        f"R{race_no} scraped: {len(win_odds)} WIN, {len(place_odds)} PLA "
        f"from {len(lines)} lines"
    )
    return {"win_odds": win_odds, "place_odds": place_odds}


async def _subscribe_odds(page, races: list[dict], date_str: str, venue: str) -> None:
    """Subscribe to WIN/PLA Solace topics for the target races.

    The SPA must already be loaded for the WebSocket hijack to work.
    """
    dt_compact = date_str.replace("-", "")

    for r in races:
        race_no = r["race_no"]
        for odds_type in ["win", "pla"]:
            topic = f"hk/d/prdt/wager/evt/01/upd/racing/{dt_compact}/{venue}/{race_no}/{odds_type}/odds/full"
            # Retry on 'not_ready' (WebSocket still opening or busy)
            for attempt in range(10):
                result = await page.evaluate(f"window.__sendSolaceSubscribe('{topic}')")
                if result == "not_ready":
                    await asyncio.sleep(0.3)
                else:
                    break
            if result == "not_ready":
                logger.warning(f"Solace sub R{race_no} {odds_type}: failed after 10 retries")
            else:
                logger.debug(f"Solace sub R{race_no} {odds_type}: {result}")
            await asyncio.sleep(0.05)


async def _collect_ws_odds(page, races: list[dict], wait_seconds: int = 10) -> dict:
    """Wait for WebSocket odds data, parse SMF messages, return per-race odds.

    Returns dict of race_no -> {win_odds: {runner: price}, place_odds: {runner: price}}
    """
    await asyncio.sleep(wait_seconds)

    # Get message stats from browser
    stats = await page.evaluate("() => window.__getMessageStats()")
    logger.debug(f"WS messages: {stats['total']} total, {stats['data']} data, {stats['ack']} ACKs")

    if stats["data"] == 0:
        logger.debug("No WebSocket data messages received yet")
        return {}

    # Get all received messages (data only to save transfer)
    messages = await page.evaluate("() => window.__getDataMessages()")

    # Parse odds from messages
    results = _parse_ws_odds_messages(messages)

    if not results:
        # Dump first few messages for debugging
        for i, msg in enumerate(messages[:3]):
            logger.debug(f"  msg[{i}]: {msg['len']} bytes, text: {msg.get('text', '')[:200]}")
        logger.debug("Could not parse odds from messages — see text dumps above")

    return results


async def _hkjc_login(page, context) -> bool:
    """Log into HKJC betting account via SPA's ForgeRock SSO flow.

    Handles both trusted-browser (fast path, no OTP) and OTP flows.
    Trust-browser dialog is handled with Playwright native clicks.
    """
    account = os.getenv("HKJC_ACCOUNT", "")
    password = os.getenv("HKJC_PASSWORD", "")
    otp_code = os.getenv("HKJC_OTP", "")

    if not account or not password or account == "YOUR_ACCOUNT_ID":
        logger.debug("No HKJC credentials set — skipping login")
        return False

    # Quick SSO check
    try:
        resp = await page.request.post(
            "https://txn01.hkjc.com/BetslipIB/services/SSOService.svc/DoCheckSSOSignInStatusTR",
            headers={"Content-Type": "application/json"},
            data='{"knownWebID":"","knownSSOGUID":"","isNew":true}',
        )
        sso_text = await resp.text()
        sso = json.loads(sso_text)
        for item in sso.get("DoCheckSSOSignInStatusTRResult", []):
            if item["Key"] == "sso_sign_in_level" and item["Value"] != "0":
                logger.info("HKJC: already authenticated")
                return True
    except Exception:
        pass

    logger.info("Logging into HKJC (ForgeRock SSO)...")

    # ForgeRock state tracking
    got_token = False
    otp_needed = False
    trust_stage = False

    async def on_response(response):
        nonlocal got_token, otp_needed, trust_stage
        if "auth.ark.hkjc.com" in response.url and "authenticate" in response.url:
            try:
                data = json.loads(await response.text())
                if data.get("tokenId"):
                    got_token = True
                if data.get("stage") == "otp":
                    otp_needed = True
                elif data.get("stage") == "trust-browser-confirm":
                    trust_stage = True
            except Exception:
                pass

    page.on("response", on_response)

    try:
        # Load login page
        await page.goto("https://bet.hkjc.com/en/racing/login",
                        wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(3)

        # Check if login form exists
        account_input = await page.query_selector("#login-account-input")
        if not account_input:
            logger.debug("No login form — may already be authenticated")
            return True

        # Fill credentials
        logger.debug("Filling credentials...")
        await page.evaluate(f"""
            () => {{
                for (const [id, val] of [
                    ['#login-account-input', '{account}'],
                    ['#login-password-input', '{password}']
                ]) {{
                    const input = document.querySelector(id);
                    const ns = Object.getOwnPropertyDescriptor(
                        window.HTMLInputElement.prototype, 'value').set;
                    ns.call(input, val);
                    input.dispatchEvent(new Event('input', {{ bubbles: true }}));
                    input.dispatchEvent(new Event('change', {{ bubbles: true }}));
                }}
            }}
        """)
        await asyncio.sleep(1)

        # Submit credentials
        await page.focus("#login-password-input")
        await page.keyboard.press("Enter")

        # Wait for ForgeRock response
        for i in range(30):
            await asyncio.sleep(1)
            if got_token:
                break
            if otp_needed:
                break

        # PATH A: Trusted browser — token received directly
        if got_token and not otp_needed:
            logger.info("Trusted browser: token received, establishing SSO...")
            for i in range(20):
                await asyncio.sleep(1)
                try:
                    resp = await page.request.post(
                        "https://txn01.hkjc.com/BetslipIB/services/SSOService.svc/DoCheckSSOSignInStatusTR",
                        headers={"Content-Type": "application/json"},
                        data='{"knownWebID":"","knownSSOGUID":"","isNew":true}')
                    sso = json.loads(await resp.text())
                    for item in sso.get("DoCheckSSOSignInStatusTRResult", []):
                        if item["Key"] == "sso_sign_in_level" and item["Value"] != "0":
                            logger.success("HKJC login successful (trusted browser)!")
                            return True
                except Exception:
                    pass
            logger.warning("Token received but SSO not established")
            return False

        # PATH B: OTP required
        if otp_needed and not got_token:
            if not otp_code or len(otp_code) != 6 or not otp_code.isdigit():
                logger.error("HKJC_OTP not set or invalid in .env — cannot complete 2FA")
                # Rate-limited alert: max once per 30 min
                alert_file = Path("/tmp/hkjc_otp_alerted")
                now = datetime.now()
                if not alert_file.exists() or (now - datetime.fromtimestamp(alert_file.stat().st_mtime)).seconds > 1800:
                    send_telegram_sync("HKJC OTP NEEDED — trust cookie expired. Update HKJC_OTP in .env and restart cp-odds.")
                    alert_file.write_text(now.isoformat())
                return False

            # Check OTP boxes
            await asyncio.sleep(2)
            boxes = await page.query_selector_all("input.otp-input")
            logger.info(f"OTP required — {len(boxes)} boxes, filling...")

            if len(boxes) >= 4:
                # Type OTP via real keyboard
                for i, digit in enumerate(otp_code[:len(boxes)]):
                    await boxes[i].click()
                    await page.keyboard.type(digit)
                    await asyncio.sleep(0.03)
                logger.debug("OTP typed via keyboard")

                # Wait for trust-browser or token
                for i in range(15):
                    await asyncio.sleep(1)
                    if got_token:
                        break
                    if trust_stage:
                        break

                # Handle trust-browser dialog
                if trust_stage and not got_token:
                    logger.debug("Trust-browser dialog detected, clicking Trust + Next...")
                    await asyncio.sleep(3)

                    # Click "Trust this browser"
                    try:
                        btn = await page.query_selector('#notTrustButton')
                        if btn:
                            await btn.click(force=True)
                            logger.debug("  Clicked Trust option")
                    except Exception as e:
                        logger.debug(f"  Trust click: {e}")

                    await asyncio.sleep(1)

                    # Click Next
                    try:
                        next_btn = await page.query_selector('.trustbrowser-btn-group')
                        if next_btn:
                            await next_btn.click(force=True)
                            logger.debug("  Clicked Next")
                    except Exception as e:
                        logger.debug(f"  Next click: {e}")

                    # Wait for token
                    for i in range(20):
                        await asyncio.sleep(1)
                        if got_token:
                            break

                if got_token:
                    # Wait for SSO establishment
                    for i in range(20):
                        await asyncio.sleep(1)
                        try:
                            resp = await page.request.post(
                                "https://txn01.hkjc.com/BetslipIB/services/SSOService.svc/DoCheckSSOSignInStatusTR",
                                headers={"Content-Type": "application/json"},
                                data='{"knownWebID":"","knownSSOGUID":"","isNew":true}')
                            sso = json.loads(await resp.text())
                            for item in sso.get("DoCheckSSOSignInStatusTRResult", []):
                                if item["Key"] == "sso_sign_in_level" and item["Value"] != "0":
                                    logger.success("HKJC login successful (OTP + trust)!")
                                    send_telegram_sync("HKJC login OK — trust cookie renewed for 24h")
                                    return True
                        except Exception:
                            pass
                else:
                    logger.warning("OTP submitted but no token received")
                    return False

        logger.warning(f"HKJC login failed: got_token={got_token} otp={otp_needed}")
        send_telegram_sync(f"HKJC login FAILED — token={got_token} otp_needed={otp_needed}")
        return False

    except Exception as e:
        logger.error(f"HKJC login error: {e}")
        return False


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

        # Authenticate with HKJC to access live odds
        await _hkjc_login(page, context)

        # Phase 1: Fetch race meeting metadata via GraphQL (no page load needed)
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

        # Phase 3: Scrape odds from WP pages (primary method — always works)
        logger.debug("Scraping WIN/PLA odds from SPA pages...")
        ok = 0
        for r in races_to_scrape:
            race_no = r["race_no"]
            race_odds = await _scrape_odds_from_page(page, args.date, args.venue, race_no)

            win_odds = race_odds.get("win_odds", {})
            place_odds = race_odds.get("place_odds", {})

            if win_odds:
                save_odds_snapshot(r["race_id"], win_odds, place_odds)
                top3 = sorted(win_odds.items(), key=lambda x: x[1])[:3]
                logger.info(f"R{race_no}: {len(win_odds)} horses WIN — favs: {top3}")
                if place_odds:
                    top3p = sorted(place_odds.items(), key=lambda x: x[1])[:3]
                    logger.info(f"R{race_no}: {len(place_odds)} horses PLA — favs: {top3p}")
                ok += 1

                if args.audit:
                    mins = _minutes_to_jump(r.get("jump_time", "13:00"))
                    if mins is not None and 10 <= mins <= 20:
                        logger.info(f"R{race_no}: T-{mins}min — triggering audit...")
                        audit_script = str(Path(__file__).parent / "audit.py")
                        proc = subprocess.Popen(
                            [sys.executable, audit_script, "--race-id", r["race_id"]],
                            stdout=subprocess.DEVNULL,
                        )
                        logger.info(f"Audit PID {proc.pid} spawned for {r['race_id']}")
            else:
                logger.debug(f"R{race_no}: no WIN/PLA odds available yet")

        # Also subscribe to Solace for live updates (non-blocking)
        try:
            await _subscribe_odds(page, races_to_scrape, args.date, args.venue)
        except Exception:
            pass

        if ok > 0:
            logger.success(f"Odds scrape: {ok}/{len(races_to_scrape)} races with live odds")
        else:
            logger.info(f"No live odds captured ({len(races_to_scrape)} races) — "
                        f"pools may not be open yet or races already finished")

        await context.close()


if __name__ == "__main__":
    asyncio.run(main())
