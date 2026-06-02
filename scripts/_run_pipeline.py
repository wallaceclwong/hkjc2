"""Run full odds pipeline with trusted session."""
import asyncio, sys, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
from playwright.async_api import async_playwright
from config import BASE_DIR, DATA_DIR
from db import init_db
from scripts.ingest_odds import (
    _hkjc_login, _fetch_metadata, _subscribe_odds, _collect_ws_odds,
    _resolve_race_targets, WS_HIJACK_SCRIPT, HOME_PAGE, ODDS_PAGE
)

load_dotenv(BASE_DIR / ".env")

async def main():
    init_db()
    date_str = sys.argv[1] if len(sys.argv) > 1 else "2026-06-03"
    venue = sys.argv[2] if len(sys.argv) > 2 else "HV"

    races = _resolve_race_targets(date_str, venue, 0)
    print(f"Races: {len(races)}")

    # Use trusted session
    user_data = DATA_DIR / "browser_session_odds"

    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            str(user_data.absolute()), headless=True,
            viewport={"width": 1280, "height": 800})
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.set_extra_http_headers({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
        })
        await page.add_init_script(WS_HIJACK_SCRIPT)

        # Login
        ok = await _hkjc_login(page, ctx)
        print(f"Login: {ok}")
        if not ok:
            await ctx.close()
            return

        # Load SPA
        await page.goto(HOME_PAGE, wait_until="domcontentloaded", timeout=30000)
        date_path = date_str.replace("-", "/")
        odds_url = ODDS_PAGE.format(date_path=date_path, venue=venue, race_no=1)
        await page.goto(odds_url, wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(5)

        # GraphQL
        print("GraphQL metadata...")
        meeting = await _fetch_metadata(page, date_str, venue)
        if meeting:
            active = [p for p in meeting.get("poolInvs", []) if p.get("status") == "START_SELL"]
            print(f"  Meeting: {meeting.get('venueCode')} {meeting.get('date')}")
            print(f"  Status: {meeting.get('status')} Races: {meeting.get('totalNumberOfRace')}")
            print(f"  Active pools: {len(active)}")
            for p in active[:8]:
                print(f"    {p.get('oddsType')}: {p.get('status')}")
        else:
            print("  No metadata")

        # Subscribe
        print(f"Subscribing to {len(races) * 2} topics (WIN+PLA)...")
        await _subscribe_odds(page, races, date_str, venue)
        await asyncio.sleep(3)

        # Collect
        print("Collecting odds (45s)...")
        results = await _collect_ws_odds(page, races, wait_seconds=45)

        stats = await page.evaluate("() => window.__getMessageStats()")
        print(f"WS: total={stats['total']} ack={stats['ack']} data={stats['data']}")

        if results:
            for race_no, data in sorted(results.items()):
                win = data.get("win_odds", {})
                pla = data.get("place_odds", {})
                if win:
                    top = sorted(win.items(), key=lambda x: x[1])[:5]
                    print(f"R{race_no} WIN ({len(win)}): {top}")
                if pla:
                    top = sorted(pla.items(), key=lambda x: x[1])[:5]
                    print(f"R{race_no} PLA ({len(pla)}): {top}")
        else:
            data_msgs = await page.evaluate("() => window.__getDataMessages()")
            print(f"No odds parsed. {len(data_msgs)} data messages:")
            for i, msg in enumerate(data_msgs[:5]):
                print(f"  [{i}] {msg['len']}b: {msg.get('text','')[:300]}")

        await ctx.close()

if __name__ == "__main__":
    asyncio.run(main())
