"""Login + check odds page in one shot."""
import asyncio, json, sys
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

        ok = await _hkjc_login(page, ctx)
        print(f"Login: {ok}")

        if ok:
            await page.goto("https://bet.hkjc.com/en/racing", timeout=30000)
            await asyncio.sleep(3)
            await page.goto("https://bet.hkjc.com/en/racing/wp/2026/06/03/HV/1", timeout=30000)
            await asyncio.sleep(8)
            print(f"URL: {page.url[:120]}")

            r = await page.evaluate("""() => {
                const b = document.body.innerText;
                const m = b.match(/\\d+\\.\\d+/g) || [];
                const unique = [...new Set(m)];
                return {
                    len: b.length,
                    odds: unique.slice(0, 30),
                    hasHorse: b.includes("SMART AVENUE") || b.includes("MISTER DAPPER"),
                    top: b.slice(0, 800)
                };
            }""")
            print(f"Body: {r['len']} chars, {len(r['odds'])} unique decimals")
            print(f"Odds: {r['odds'][:25]}")
            print(f"Has horse names: {r['hasHorse']}")
            if r['len'] < 500:
                print(f"Body: {r['top']}")

        await ctx.close()

asyncio.run(main())
