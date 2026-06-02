"""Test parsing logic directly against loaded page."""
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

        # Navigate and parse (replicate what _scrape_odds_from_page does)
        url = "https://bet.hkjc.com/en/racing/wp/2026/06/03/HV/1"
        await page.goto(url, timeout=30000)
        await asyncio.sleep(6)

        lines = await page.evaluate("""() => {
            return document.body.innerText.split('\\n').map(l => l.trim());
        }""")

        print(f"Total lines: {len(lines)}")

        # Find header
        header_idx = None
        for i, line in enumerate(lines):
            if "Horse Name" in line and "Draw" in line and "Win" in line:
                header_idx = i
                print(f"Header found at line {i}: '{line[:120]}'")
                break

        if header_idx is None:
            print("No header found!")
            # Show lines with Horse
            for i, l in enumerate(lines):
                if "Horse" in l or "horse" in l.lower():
                    print(f"  Line {i}: '{l[:150]}'")
            await ctx.close()
            return

        # Parse
        win_odds = {}
        place_odds = {}
        i = header_idx + 1
        count = 0
        while i < len(lines) - 3:
            horse_line = lines[i]
            if horse_line.isdigit() and 1 <= int(horse_line) <= 14:
                runner_no = horse_line
                name_line = lines[i + 1]
                win_line = lines[i + 2]
                place_line = lines[i + 3]

                print(f"  Trying runner {runner_no}: name='{name_line[:50]}' win='{win_line}' pla='{place_line}'")

                if (name_line and len(name_line) > 10 and
                    win_line.replace(".", "").replace(",", "").replace("-", "").isdigit() and
                    place_line.replace(".", "").replace(",", "").replace("-", "").isdigit()):

                    win_val = float(win_line.replace(",", ""))
                    place_val = float(place_line.replace(",", ""))
                    if 1.0 <= win_val <= 999 and 1.0 <= place_val <= 999:
                        win_odds[runner_no] = win_val
                        place_odds[runner_no] = place_val
                        print(f"    PARSED: WIN={win_val} PLA={place_val}")
                        count += 1
                        i += 5
                        continue
                    else:
                        print(f"    OUT OF RANGE: WIN={win_val} PLA={place_val}")
                else:
                    checks = {
                        "name_len": len(name_line) > 10 if name_line else False,
                        "win_isdigit": win_line.replace(".", "").replace(",", "").replace("-", "").isdigit(),
                        "pla_isdigit": place_line.replace(".", "").replace(",", "").replace("-", "").isdigit(),
                    }
                    print(f"    FAILED checks: {checks}")
            i += 1

        print(f"\nParsed: {len(win_odds)} WIN, {len(place_odds)} PLA")
        if win_odds:
            top = sorted(win_odds.items(), key=lambda x: x[1])[:5]
            print(f"WIN: {top}")
        if place_odds:
            top = sorted(place_odds.items(), key=lambda x: x[1])[:5]
            print(f"PLA: {top}")

        if not win_odds:
            # Dump lines 70-85
            print("\nLines 70-85:")
            for j in range(70, min(len(lines), 85)):
                print(f"  {j}: '{lines[j][:150]}'")

        await ctx.close()

asyncio.run(main())
