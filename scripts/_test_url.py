"""Test slash vs dash date format in WP URL."""
import asyncio, sys
from pathlib import Path
sys.path.insert(0, "/opt/hkjc2")
from dotenv import load_dotenv
from playwright.async_api import async_playwright
from config import BASE_DIR, DATA_DIR
from scripts.ingest_odds import _hkjc_login
load_dotenv(BASE_DIR / ".env")

async def main():
    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            str(DATA_DIR / "browser_session_odds"), headless=True,
            viewport={"width": 1280, "height": 800})
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await _hkjc_login(page, ctx)

        # Try slash format
        print("Trying SLASH format (2026/06/03)...")
        await page.goto("https://bet.hkjc.com/en/racing/wp/2026/06/03/HV/1", timeout=30000)
        await asyncio.sleep(6)
        lines = await page.evaluate("() => document.body.innerText.split('\\n').map(l => l.trim())")
        print(f"  URL: {page.url[:120]}")
        print(f"  Lines: {len(lines)}")

        # Try dash format
        print("Trying DASH format (2026-06-03)...")
        await page.goto("https://bet.hkjc.com/en/racing/wp/2026-06-03/HV/1", timeout=30000)
        await asyncio.sleep(6)
        lines = await page.evaluate("() => document.body.innerText.split('\\n').map(l => l.trim())")
        print(f"  URL: {page.url[:120]}")
        print(f"  Lines: {len(lines)}")

        await ctx.close()

asyncio.run(main())
