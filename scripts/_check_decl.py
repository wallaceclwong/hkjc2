"""Check if R10/R11 have declarations published."""
import asyncio, sys
from pathlib import Path
sys.path.insert(0, "/opt/hkjc2")
from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()

        for race_no in [10, 11]:
            url = f"https://racing.hkjc.com/racing/information/English/racing/EntryList.aspx?RaceNo={race_no}&Venue=HV&Date=2026-06-03"
            await page.goto(url, timeout=30000)
            has_table = await page.evaluate("() => !!document.querySelector('.table_eng_text, .raceEntryList, [class*=\"horse\"], table[class*=\"entry\"]')")
            title = await page.title()
            print(f"R{race_no}: has_table={has_table} title={title[:80]}")

        await browser.close()

asyncio.run(main())
