"""Test _scrape_odds_from_page directly."""
import asyncio, sys
from pathlib import Path
sys.path.insert(0, "/opt/hkjc2")
from dotenv import load_dotenv
from playwright.async_api import async_playwright
from config import BASE_DIR, DATA_DIR
from scripts.ingest_odds import _hkjc_login, _scrape_odds_from_page

load_dotenv(BASE_DIR / ".env")

async def main():
    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            str(DATA_DIR / "browser_session_odds"), headless=True,
            viewport={"width": 1280, "height": 800})
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await _hkjc_login(page, ctx)
        await page.goto("https://bet.hkjc.com/en/racing", timeout=30000)
        await asyncio.sleep(3)

        print("Going to WP R1 directly...")
        await page.goto("https://bet.hkjc.com/en/racing/wp/2026-06-03/HV/1", timeout=30000)
        await asyncio.sleep(5)
        print(f"URL after goto: {page.url[:150]}")

        # Check header
        lines = await page.evaluate("""() => {
            return document.body.innerText.split('\\n').map(l => l.trim());
        }""")
        print(f"Lines: {len(lines)}")
        for i, l in enumerate(lines):
            if "Horse" in l and "Name" in l:
                print(f"Header at line {i}: '{l[:200]}'")
                break

        print()
        print("Calling _scrape_odds_from_page for R1...")
        result = await _scrape_odds_from_page(page, "2026-06-03", "HV", 1)
        print(f"WIN: {result['win_odds']}")
        print(f"PLA: {result['place_odds']}")

        await ctx.close()

asyncio.run(main())
