#!/usr/bin/env python3
"""Scrape HKJC season fixture schedule and populate SQLite.

Usage:
    python scripts/ingest_fixtures.py              # next 5 months
    python scripts/ingest_fixtures.py --months 3   # next 3 months
"""

import argparse, asyncio, sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from playwright.async_api import async_playwright
from loguru import logger
from db import init_db, get_db

FIXTURE_URL = "https://racing.hkjc.com/racing/information/English/Racing/Fixture.aspx"


async def fetch_month(page, month: int, year: int) -> list[dict]:
    fixtures = []
    # HKJC fixture page loads all months client-side — navigate once, extract by month
    # The calendar cells have class 'calendar' with data attributes
    cells = await page.query_selector_all("td.calendar")

    for cell in cells:
        day_elem = await cell.query_selector(".f_fl")
        if not day_elem:
            continue
        day_text = (await day_elem.inner_text()).strip()
        if not day_text.isdigit():
            continue

        venue = ""
        day_night = ""
        imgs = await cell.query_selector_all(".f_fr img")
        for img in imgs:
            alt = (await img.get_attribute("alt") or "").upper()
            if alt in ("ST", "HV"):
                venue = alt
            elif alt in ("D", "N"):
                day_night = alt

        if venue:
            date_str = f"{year}-{month:02d}-{int(day_text):02d}"
            fixtures.append({
                "date": date_str,
                "venue": venue,
                "day_night": day_night,
                "race_type": "Local",
                "status": "Scheduled",
            })

    return fixtures


async def main():
    parser = argparse.ArgumentParser(description="Scrape HKJC fixture schedule")
    parser.add_argument("--months", type=int, default=5, help="Number of months to fetch (default 5)")
    args = parser.parse_args()

    init_db()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
        )
        page = await context.new_page()

        logger.info(f"Navigating to {FIXTURE_URL}...")
        await page.goto(FIXTURE_URL, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(4000)
        await page.wait_for_selector("td.calendar", timeout=15000)

        now = datetime.now()
        total = 0
        conn = get_db()
        try:
            for i in range(args.months):
                m = (now.month + i - 1) % 12 + 1
                y = now.year + (now.month + i - 1) // 12

                # Navigate to the target month (HKJC page has month navigation)
                month_name = datetime(y, m, 1).strftime("%B")
                logger.info(f"Fetching {month_name} {y}...")

                # Try clicking the month tab
                try:
                    month_links = await page.query_selector_all("a")
                    for link in month_links:
                        txt = (await link.inner_text()).strip()
                        if month_name[:3].upper() in txt.upper() and str(y) in txt:
                            await link.click()
                            await page.wait_for_timeout(2000)
                            break
                except Exception:
                    pass

                fixtures = await fetch_month(page, m, y)
                for f in fixtures:
                    cur = conn.execute(
                        "INSERT OR IGNORE INTO fixtures (date, venue, day_night, race_type, status) VALUES (?,?,?,?,?)",
                        (f["date"], f["venue"], f["day_night"], f["race_type"], f["status"]),
                    )
                    if cur.rowcount:
                        total += 1
                        logger.info(f"  {f['date']}  {f['venue']}  {f['day_night']}")

            conn.commit()
        finally:
            conn.close()
        await browser.close()

    logger.success(f"Done: {total} fixtures saved")

    # Show upcoming
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT date, venue, day_night FROM fixtures WHERE date >= ? ORDER BY date LIMIT 10",
            (now.strftime("%Y-%m-%d"),)
        ).fetchall()
        if rows:
            logger.info("Upcoming race days:")
            for r in rows:
                logger.info(f"  {r['date']}  {r['venue']}  {r['day_night']}")
    finally:
        conn.close()


if __name__ == "__main__":
    asyncio.run(main())
