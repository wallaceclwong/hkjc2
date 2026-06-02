"""Test: does _fetch_metadata break WP navigation?"""
import asyncio, sys
from pathlib import Path
sys.path.insert(0, "/opt/hkjc2")
from dotenv import load_dotenv
from playwright.async_api import async_playwright
from config import BASE_DIR, DATA_DIR
from scripts.ingest_odds import _hkjc_login, _fetch_metadata

load_dotenv(BASE_DIR / ".env")

async def main():
    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            str(DATA_DIR / "browser_session_odds"), headless=True,
            viewport={"width": 1280, "height": 800})
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await _hkjc_login(page, ctx)

        # First call _fetch_metadata (like the pipeline does)
        print("Fetching GraphQL metadata...")
        meeting = await _fetch_metadata(page, "2026-06-03", "HV")
        if meeting:
            print(f"  Got meeting: {meeting.get('venueCode')}")

        # Then navigate to WP
        print("Going to WP R1...")
        await page.goto("https://bet.hkjc.com/en/racing/wp/2026-06-03/HV/1", timeout=30000)
        await asyncio.sleep(6)
        print(f"URL: {page.url[:120]}")
        lines = await page.evaluate("() => document.body.innerText.split('\\n').map(l => l.trim())")
        print(f"Lines: {len(lines)}")
        for i, l in enumerate(lines):
            if "Horse Name" in l and "Draw" in l:
                print(f"Header at line {i}: '{l[:150]}'")
                break

        await ctx.close()

asyncio.run(main())
