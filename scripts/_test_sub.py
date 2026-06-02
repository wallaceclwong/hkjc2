"""Quick test: verify all Solace subscriptions succeed."""
import asyncio, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
from playwright.async_api import async_playwright
from config import BASE_DIR, DATA_DIR
from scripts.ingest_odds import (
    _hkjc_login, _subscribe_odds, _resolve_race_targets, WS_HIJACK_SCRIPT, HOME_PAGE, ODDS_PAGE
)

load_dotenv(BASE_DIR / ".env")

async def main():
    races = _resolve_race_targets("2026-05-31", "ST", 0)
    print(f"Races: {len(races)}")

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

        ok = await _hkjc_login(page, ctx)
        print(f"Login: {ok}")
        if not ok:
            await ctx.close()
            return

        await page.goto(HOME_PAGE, wait_until="domcontentloaded", timeout=30000)
        odds_url = ODDS_PAGE.format(date_path="2026/05/31", venue="ST", race_no=1)
        await page.goto(odds_url, wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(5)

        await _subscribe_odds(page, races, "2026-05-31", "ST")
        await asyncio.sleep(2)

        stats = await page.evaluate("() => window.__getMessageStats()")
        ack = stats["ack"]
        total = stats["total"]
        print(f"WS: total={total} ack={ack} data={stats['data']}")
        expected = len(races) * 2  # WIN + PLA per race
        print(f"Expected {expected} ACKs, got {ack} — {'PASS' if ack >= expected else 'MISSING ' + str(expected - ack)}")

        await ctx.close()

if __name__ == "__main__":
    asyncio.run(main())
