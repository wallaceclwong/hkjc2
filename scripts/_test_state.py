"""Check page URL after login."""
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

        print(f"Before login URL: {page.url}")
        ok = await _hkjc_login(page, ctx)
        print(f"After login URL: {page.url[:120]}")
        print(f"Login: {ok}")

        if ok:
            # Now navigate to WP
            print("Going to WP R1...")
            await page.goto("https://bet.hkjc.com/en/racing/wp/2026-06-03/HV/1", timeout=30000)
            await asyncio.sleep(6)
            print(f"After WP goto URL: {page.url[:120]}")
            lines = await page.evaluate("() => document.body.innerText.split('\\n').map(l => l.trim())")
            print(f"Lines: {len(lines)}")

        await ctx.close()

asyncio.run(main())
