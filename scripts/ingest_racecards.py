#!/usr/bin/env python3
"""Scrape HKJC racecards and write to SQLite. One-shot CLI — run on race day morning.

Usage:
    python scripts/ingest_racecards.py --date 2026-05-25 --venue ST
    python scripts/ingest_racecards.py --date 2026-05-25 --venue HV --races 1,2,3
"""

import argparse, asyncio, json, os, re, sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from playwright.async_api import async_playwright
from loguru import logger
from config import DATA_DIR
from db import save_racecard, init_db

GEAR_CODES = {
    "B": "Blinkers", "BO": "Blinkers Off", "CO": "Cap Off", "CP": "Cheek Pieces",
    "E": "Ear Muffs", "HS": "Hood (Start)", "P": "Pacifiers", "PC": "Pacifiers+Cheek Pieces",
    "SR": "Shadow Roll", "TT": "Tongue Tie", "V": "Visor", "VO": "Visor Off",
    "XB": "Cross Blinkers",
}

EXTRACTION_JS = r"""() => {
    const results = [];
    const tables = Array.from(document.querySelectorAll('table.starter, table.table_bd.racecard, #racecardlist table'));
    for (const table of tables) {
        const rows = Array.from(table.querySelectorAll('tr'));
        let headerFound = false;
        let mapping = { saddle: 0, last_6: 1, horse: 3, weight: 4, jockey: 5, draw: 6, trainer: 7, gear: -1 };
        for (const row of rows) {
            const cells = Array.from(row.querySelectorAll('td, th'));
            const cellTexts = cells.map(c => c.innerText.trim());
            if (!headerFound && cellTexts.includes('Horse No.') && cellTexts.includes('Jockey')) {
                headerFound = true;
                mapping.saddle = cellTexts.indexOf('Horse No.');
                mapping.horse  = cellTexts.indexOf('Horse');
                mapping.weight = cellTexts.indexOf('Wt.');
                mapping.jockey = cellTexts.indexOf('Jockey');
                mapping.draw   = cellTexts.indexOf('Draw');
                mapping.trainer = cellTexts.indexOf('Trainer');
                mapping.last_6 = cellTexts.indexOf('Last 6 Runs');
                const gearIdx = cellTexts.findIndex(t => t === 'Gear' || t === 'Equipment');
                if (gearIdx !== -1) mapping.gear = gearIdx;
                continue;
            }
            if (headerFound && cellTexts.length >= 8) {
                const saddle = cellTexts[mapping.saddle];
                if (!saddle || !/^\d+$/.test(saddle)) continue;
                const horse = cellTexts[mapping.horse] ? cellTexts[mapping.horse].split('\n')[0].trim() : "";
                if (!horse || horse === 'Horse') continue;
                let gearText = mapping.gear >= 0 ? (cellTexts[mapping.gear] || "") : "";
                if (!gearText && mapping.gear >= 0) {
                    const gearCell = cells[mapping.gear];
                    if (gearCell) {
                        const imgs = gearCell.querySelectorAll('img[alt]');
                        gearText = Array.from(imgs).map(i => i.alt.trim()).filter(Boolean).join(",");
                    }
                }
                results.push({
                    saddle: saddle, horse: horse, last_6: cellTexts[mapping.last_6] || "",
                    weight: cellTexts[mapping.weight] || "", jockey: cellTexts[mapping.jockey] || "",
                    draw: cellTexts[mapping.draw] || "", trainer: cellTexts[mapping.trainer] || "", gear: gearText,
                });
            }
        }
        if (results.length > 0) break;
    }
    return results;
}"""

GOING_KEYWORDS = {
    "WET FAST": "Wet", "WET SLOW": "Wet", "WET": "Wet", "SOFT": "Soft",
    "YIELDING": "Yielding", "GOOD TO YIELDING": "Yielding", "GOOD TO FIRM": "Good",
    "FIRM": "Good", "GOOD": "Good",
}

def _parse_header(text: str):
    distance, track_type, course, race_class, track_condition, jump_time = 1200, "Turf", "A", "Class 4", "Good", "13:00"

    dist_match = re.search(r'(\d{3,5})M', text, re.IGNORECASE)
    if dist_match:
        distance = int(dist_match.group(1))

    if "All Weather" in text or "AWT" in text:
        track_type = "All Weather Track"
    elif "Turf" in text:
        track_type = "Turf"

    course_match = re.search(r'"([A-C](?:\+\d)?)"\s*Course', text, re.IGNORECASE)
    if course_match:
        course = course_match.group(1).upper()

    class_match = re.search(r'(Class \d|Griffin|Group \d)', text)
    if class_match:
        race_class = class_match.group(1)

    for line in text.splitlines():
        if re.search(r'\d+M', line, re.IGNORECASE):
            tokens = [t.strip(' "') for t in line.split(',')]
            joined = " ".join(tokens).upper()
            for kw, norm in sorted(GOING_KEYWORDS.items(), key=lambda x: -len(x[0])):
                if kw in joined:
                    track_condition = norm
                    break
            break

    if track_condition == "Good":
        going_patterns = [
            r'Going\s*:?\s*([A-Z][A-Z\s]{2,20})',
            r'(?:^|,|\s)((?:WET FAST|WET SLOW|WET|GOOD TO YIELDING|GOOD TO FIRM|YIELDING|SOFT|FIRM|GOOD))(?=,|\s|$)',
        ]
        for pat in going_patterns:
            m = re.search(pat, text, re.IGNORECASE | re.MULTILINE)
            if m:
                raw = m.group(1).strip().upper()
                for kw, norm in sorted(GOING_KEYWORDS.items(), key=lambda x: -len(x[0])):
                    if kw in raw:
                        track_condition = norm
                        break
                break

    time_match = re.search(r'(\d{1,2}:\d{2}\s?(?:AM|PM)?)', text)
    if time_match:
        jump_time = time_match.group(1).strip()

    return distance, track_type, course, race_class, track_condition, jump_time

def _parse_horses(raw_horses: list) -> list:
    horses = []
    for h in raw_horses:
        try:
            saddle_number = int(h["saddle"])
        except (ValueError, TypeError):
            continue

        horse_name = h["horse"].split("(")[0].strip()
        brand_id = "N/A"
        brand_match = re.search(r'\(([^)]+)\)', h["horse"])
        if brand_match:
            brand_id = brand_match.group(1)

        training_location = "CTC" if "CTC" in h["horse"].upper() else "HK"

        jockey_raw = h["jockey"]
        allowance = 0
        allow_match = re.search(r'\((-?\d+)\)', jockey_raw)
        if allow_match:
            allowance = int(allow_match.group(1))
        jockey = re.sub(r'\(-?\d+\)', '', jockey_raw).strip()

        last_6 = [r.strip() for r in h["last_6"].split("/") if r.strip()]

        raw_gear = h.get("gear", "").strip()
        if raw_gear:
            codes = [c.strip().upper() for c in re.split(r'[,/\s]+', raw_gear) if c.strip()]
            expanded = [GEAR_CODES.get(c, c) for c in codes if c]
            gear_str = ", ".join(expanded) if expanded else raw_gear
        else:
            gear_str = ""

        try:
            weight = float(h["weight"])
        except (ValueError, TypeError):
            weight = 133.0

        try:
            draw = int(h["draw"])
        except (ValueError, TypeError):
            draw = 0

        horses.append({
            "horse_id": brand_id, "horse_name": horse_name, "owner": "",
            "saddle_number": saddle_number, "draw": draw, "jockey": jockey,
            "weight_allowance": allowance, "trainer": h["trainer"],
            "weight": weight, "last_6_runs": last_6, "gear": gear_str,
            "training_location": training_location, "stable_change": False,
            "trial_comments": None, "synergy_score": 0.0,
        })
    return horses


async def scrape_one(race_no: int, date_str: str, venue: str, page):
    """Scrape a single racecard. Assumes page is already on the meeting page."""
    formatted = datetime.strptime(date_str, "%Y-%m-%d").strftime("%Y/%m/%d")
    url = f"https://racing.hkjc.com/en-us/local/information/racecard?racedate={formatted}&Racecourse={venue}&RaceNo={race_no}"
    race_id = f"{date_str}_{venue}_R{race_no}"

    logger.info(f"Scraping {race_id}...")
    await page.goto(url, wait_until="domcontentloaded", timeout=90000)

    # Wait for horse table
    table_selectors = ["table.starter", "table.table_bd.racecard", "#racecardlist table"]
    table_found = False
    for sel in table_selectors:
        try:
            await page.wait_for_selector(sel, timeout=10000)
            table_found = True
            break
        except Exception:
            continue

    if not table_found:
        # Try clicking "SETUP MY STARTER LIST" button (new HKJC layout)
        try:
            buttons = await page.query_selector_all("a, button, div[onclick], span")
            for btn in buttons:
                txt = await btn.inner_text()
                if "SETUP" in txt and "STARTER" in txt:
                    logger.info("Clicking SETUP MY STARTER LIST...")
                    await btn.click()
                    await page.wait_for_timeout(3000)
                    break
        except Exception as e:
            logger.warning(f"Setup button click failed: {e}")
        tables = await page.query_selector_all("table")
        for t in tables:
            txt = await t.inner_text()
            if "Horse No." in txt and "Jockey" in txt:
                table_found = True
                break

    if not table_found:
        logger.error(f"R{race_no}: could not locate horse table")
        return None

    # Header parsing
    header_text = ""
    try:
        header_el = await page.query_selector("div.f_fs13")
        if header_el:
            header_text = (await header_el.inner_text()).strip()
    except Exception:
        pass

    body_text = await page.inner_text("#innerContent, .p_line, body")
    full_text = header_text or body_text
    distance, track_type, course, race_class, track_condition, jump_time = _parse_header(full_text)

    # Extract horses
    raw = await page.evaluate(EXTRACTION_JS)
    horses = _parse_horses(raw)

    if not horses:
        logger.error(f"R{race_no}: no horses found")
        return None

    save_racecard(race_id, date_str, venue, race_no, distance, track_type,
                  course, race_class, track_condition, jump_time, horses)
    logger.success(f"R{race_no}: {len(horses)} horses — {distance}m {track_type} {race_class} ({track_condition})")
    return race_id


async def main():
    parser = argparse.ArgumentParser(description="Scrape HKJC racecards")
    parser.add_argument("--date", required=True, help="YYYY-MM-DD")
    parser.add_argument("--venue", default="ST", help="ST or HV")
    parser.add_argument("--races", default="1-11", help="e.g. '1-11' or '1,2,3'")
    args = parser.parse_args()

    init_db()

    # Parse race list
    races = []
    for part in args.races.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-", 1)
            races.extend(range(int(a), int(b) + 1))
        else:
            races.append(int(part))

    user_data = DATA_DIR / "browser_session_ingest"
    user_data.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            str(user_data.absolute()),
            headless=True,
            viewport={"width": 1280, "height": 800},
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = context.pages[0] if context.pages else await context.new_page()
        await page.set_extra_http_headers({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
        })

        ok = 0
        for r in races:
            try:
                result = await scrape_one(r, args.date, args.venue, page)
                if result:
                    ok += 1
            except Exception as e:
                logger.error(f"R{r} failed: {e}")

        await context.close()

    logger.success(f"Done: {ok}/{len(races)} racecards saved")


if __name__ == "__main__":
    asyncio.run(main())
