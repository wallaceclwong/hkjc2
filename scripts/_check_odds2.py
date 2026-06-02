"""Check WP odds page through logged-in SPA navigation."""
import asyncio, json, sys, re
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

        # Step 1: Login
        ok = await _hkjc_login(page, ctx)
        print(f"Login: {ok}")
        if not ok:
            await ctx.close()
            return

        # Step 2: Load the SPA home page first
        print("Loading SPA...")
        await page.goto("https://bet.hkjc.com/en/racing", timeout=30000)
        await asyncio.sleep(5)

        # Step 3: Use SPA internal navigation (click the race link)
        # Instead of direct goto, try clicking through the UI
        # Or just goto and see what happens
        print("Navigating to WP R1...")
        await page.goto("https://bet.hkjc.com/en/racing/wp/2026-06-03/HV/1", timeout=30000)
        await asyncio.sleep(10)

        print(f"Final URL: {page.url[:150]}")

        # Check if we're on the odds page or redirected
        if "home" in page.url:
            print("REDIRECTED TO HOME — trying click navigation...")
            # Try clicking the race card link instead
            await page.goto("https://bet.hkjc.com/en/racing", timeout=30000)
            await asyncio.sleep(3)
            # Look for R1 link
            clicked = await page.evaluate("""() => {
                const links = document.querySelectorAll('a[href*=\"wp/2026/06/03/HV/1\"]');
                if (links.length > 0) {
                    links[0].click();
                    return 'clicked link';
                }
                // Try any link with HV/1
                const all = document.querySelectorAll('a');
                for (const a of all) {
                    if (a.href && a.href.includes('HV/1')) {
                        a.click();
                        return 'clicked: ' + a.href.slice(-40);
                    }
                }
                return 'no link found';
            }""")
            print(f"Click result: {clicked}")
            await asyncio.sleep(8)
            print(f"After click URL: {page.url[:150]}")

        # Dump page content
        body = await page.evaluate("() => document.body.innerText")
        decimals = re.findall(r"\d+\.\d+", body)
        unique = list(set(decimals))

        print(f"\nBody: {len(body)} chars, {len(unique)} unique odds values")
        if unique:
            print(f"Odds: {sorted(unique, key=float)[:30]}")

        # Show relevant lines
        for line in body.split("\n"):
            line = line.strip()
            if re.search(r"\d+\.\d+", line) and len(line) < 200:
                if any(kw in line.lower() for kw in ["win", "pla", "odds", "pool", "horse", "@"]):
                    print(f"  {line[:150]}")

        # Just show any lines with 3+ decimal numbers (likely odds table)
        for line in body.split("\n"):
            line = line.strip()
            if len(re.findall(r"\d+\.\d+", line)) >= 3:
                print(f"  [TABLE] {line[:200]}")

        await ctx.close()

asyncio.run(main())
