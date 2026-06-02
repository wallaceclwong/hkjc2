"""Debug: dump lines around horse numbers."""
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
        await page.goto("https://bet.hkjc.com/en/racing", timeout=30000)
        await asyncio.sleep(3)
        await page.goto("https://bet.hkjc.com/en/racing/wp/2026-06-03/HV/1", timeout=30000)
        await asyncio.sleep(5)

        lines = await page.evaluate("""() => {
            return document.body.innerText.split('\\n').map(l => l.trim());
        }""")

        # Search for header-like lines
        print("Lines containing Horse/Colour/No.:")
        for i, l in enumerate(lines):
            if "Horse" in l or "Colour" in l or l.startswith("No."):
                print(f"  Line {i}: '{l[:200]}'")

        print()
        print("Lines 60-85:")
        for i in range(60, min(len(lines), 85)):
            print(f"  {i}: '{lines[i][:200]}'")

        await ctx.close()

asyncio.run(main())
