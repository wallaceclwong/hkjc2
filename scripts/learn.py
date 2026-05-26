#!/usr/bin/env python3
"""Scrape HKJC results, settle bets, update bankroll. Run nightly after races.

Usage:
    python scripts/learn.py --date 2026-05-25 --venue ST
    python scripts/learn.py --date 2026-05-25 --venue HV --race 3
"""

import argparse, asyncio, json, re, subprocess, sys
from datetime import datetime
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from playwright.async_api import async_playwright
from loguru import logger
from config import DATA_DIR
from db import (init_db, get_race_ids_for_date, save_results, settle_bet,
                get_bankroll, update_bankroll, get_db, get_venue_for_date)
from notify import send_telegram_sync

RESULT_LOG = DATA_DIR / "result_log.parquet"


async def scrape_results(date_str: str, venue: str, race_no: int, page) -> dict | None:
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    formatted = dt.strftime("%Y/%m/%d")
    url = f"https://racing.hkjc.com/en-us/local/information/localresults?racedate={formatted}&Racecourse={venue}&RaceNo={race_no}"
    race_id = f"{date_str}_{venue}_R{race_no}"

    logger.info(f"Fetching results: {race_id}")
    await page.goto(url, wait_until="domcontentloaded", timeout=60000)

    # Click race tab if needed
    try:
        await page.wait_for_selector("div.performance, table.performance", timeout=8000)
    except Exception:
        logger.info("Clicking race tab...")
        clicked = False
        all_links = await page.query_selector_all("a, button")
        for link in all_links:
            txt = (await link.inner_text()).strip()
            if txt == str(race_no):
                await link.click()
                await page.wait_for_timeout(2000)
                clicked = True
                break
        if not clicked:
            tab = await page.query_selector(f"a[href*='RaceNo={race_no}']")
            if tab:
                await tab.click()
                await page.wait_for_timeout(2000)
        try:
            await page.wait_for_selector("div.performance, table.performance", timeout=12000)
        except Exception:
            pass

    # Parse horse results
    results = []
    row_selectors = ["div.performance tbody tr", "table.performance tbody tr", "div[class*='performance'] tbody tr"]
    rows = []
    for sel in row_selectors:
        rows = await page.query_selector_all(sel)
        if rows:
            break

    for row in rows:
        cols = await row.query_selector_all("td")
        if len(cols) >= 10:
            try:
                results.append({
                    "plc": (await cols[0].inner_text()).strip(),
                    "horse_no": (await cols[1].inner_text()).strip(),
                    "horse": (await cols[2].inner_text()).strip(),
                    "jockey": (await cols[3].inner_text()).strip(),
                    "trainer": (await cols[4].inner_text()).strip(),
                    "actual_wt": (await cols[5].inner_text()).strip(),
                    "declar_wt": (await cols[6].inner_text()).strip(),
                    "draw": (await cols[7].inner_text()).strip(),
                    "lbw": (await cols[8].inner_text()).strip(),
                    "finish_time": (await cols[10].inner_text()).strip() if len(cols) > 10 else "",
                    "win_odds": (await cols[11].inner_text()).strip() if len(cols) > 11 else "",
                })
            except Exception:
                continue

    if not results:
        # Retry after wait
        await page.wait_for_timeout(3000)
        for sel in row_selectors:
            rows = await page.query_selector_all(sel)
            if rows:
                break
        for row in rows:
            cols = await row.query_selector_all("td")
            if len(cols) >= 10:
                try:
                    results.append({
                        "plc": (await cols[0].inner_text()).strip(),
                        "horse_no": (await cols[1].inner_text()).strip(),
                        "horse": (await cols[2].inner_text()).strip(),
                        "jockey": (await cols[3].inner_text()).strip(),
                        "trainer": (await cols[4].inner_text()).strip(),
                        "actual_wt": (await cols[5].inner_text()).strip(),
                        "declar_wt": (await cols[6].inner_text()).strip(),
                        "draw": (await cols[7].inner_text()).strip(),
                        "lbw": (await cols[8].inner_text()).strip(),
                        "finish_time": (await cols[10].inner_text()).strip() if len(cols) > 10 else "",
                        "win_odds": (await cols[11].inner_text()).strip() if len(cols) > 11 else "",
                    })
                except Exception:
                    continue

    # Parse dividends from text
    content = await page.inner_text("body")
    dividends = {"WIN": [], "PLACE": [], "QUINELLA": [], "QUINELLA PLACE": []}
    lines = content.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if line == "Dividend" and i + 1 < len(lines) and "Pool" in lines[i + 1]:
            i += 2
            while i < len(lines):
                row_text = lines[i].strip()
                if not row_text or row_text == "Dividend Note:":
                    break
                parts = row_text.split()
                if len(parts) >= 3 and parts[0] in dividends:
                    pool = parts[0]
                    if pool == "WIN":
                        dividends["WIN"].append({"combination": parts[1], "dividend": parts[2]})
                    elif pool == "PLACE":
                        j = 1
                        while j < len(parts) - 1:
                            dividends["PLACE"].append({"combination": parts[j], "dividend": parts[j + 1]})
                            j += 2
                    elif pool == "QUINELLA":
                        dividends["QUINELLA"].append({"combination": parts[1], "dividend": parts[2]})
                    elif pool == "QUINELLA PLACE":
                        j = 1
                        while j < len(parts) - 1:
                            dividends["QUINELLA PLACE"].append({"combination": parts[j], "dividend": parts[j + 1]})
                            j += 2
                i += 1
        else:
            i += 1

    # Stewards report
    stewards_report = ""
    incidents = []
    report_div = await page.query_selector("div.race_incident_report")
    if report_div:
        stewards_report = (await report_div.inner_text()).strip()
        inc_table = await report_div.query_selector("table")
        if inc_table:
            for r in await inc_table.query_selector_all("tr"):
                c = await r.query_selector_all("td")
                if len(c) >= 4:
                    h_no = (await c[1].inner_text()).strip()
                    if h_no.isdigit():
                        incidents.append({"horse_no": h_no, "incident": (await c[3].inner_text()).strip()})

    data = {
        "race_id": race_id,
        "results": results,
        "dividends": dividends,
        "incidents": incidents,
        "stewards_report": stewards_report,
    }
    save_results(race_id, data)
    logger.success(f"{race_id}: {len(results)} results scraped")
    return data


def settle(race_id: str, results_data: dict):
    """Settle bets that were recorded by predict.py against scraped results."""
    conn = get_db()
    unsettled = conn.execute(
        "SELECT horse_no, stake, odds_taken FROM bets WHERE race_id = ? AND result IS NULL",
        (race_id,)
    ).fetchall()
    conn.close()

    if not unsettled:
        logger.info(f"{race_id}: no unsettled bets to settle")
        return 0

    results = results_data.get("results", [])
    if not results:
        logger.warning(f"{race_id}: no results to compare")
        return 0

    winner_no = None
    for r in results:
        if r.get("plc") == "1":
            winner_no = r.get("horse_no")
            break

    total_pnl = 0
    bankroll = get_bankroll()
    balance = bankroll["balance"]

    for row in unsettled:
        horse_no = str(row["horse_no"])
        stake = row["stake"]
        odds = row["odds_taken"]

        if horse_no == winner_no:
            pnl = stake * (odds - 1)
            result = "WIN"
        else:
            pnl = -stake
            result = "LOSS"

        settle_bet(race_id, horse_no, result, pnl)
        total_pnl += pnl
        logger.info(f"  #{horse_no}: {result} — stake={stake:.2f} pnl={pnl:+.2f}")

    if total_pnl != 0:
        new_balance = balance + total_pnl
        update_bankroll(new_balance, f"{race_id} settlement", total_pnl)
        logger.info(f"  Bankroll: {balance:.0f} → {new_balance:.0f} ({total_pnl:+.0f})")
        hwm = bankroll["high_water_mark"]
        if hwm > 0 and new_balance < hwm * 0.80:
            drawdown_pct = (1 - new_balance / hwm) * 100
            send_telegram_sync(
                f"⚠️ *DRAWDOWN ALERT*\n"
                f"Balance: {new_balance:.0f} ({drawdown_pct:.1f}% below HWM {hwm:.0f})\n"
                f"Last race: {race_id} PnL {total_pnl:+.0f}"
            )

    return total_pnl


def log_result_rows(race_id: str, results_data: dict):
    """Append settled race outcomes to result_log.parquet for future retraining."""
    results = results_data.get("results", [])
    if not results:
        return

    conn = get_db()
    bets_rows = conn.execute(
        "SELECT horse_no, stake, odds_taken, result, pnl FROM bets WHERE race_id = ?",
        (race_id,)
    ).fetchall()
    conn.close()
    bets_map = {str(r["horse_no"]): dict(r) for r in bets_rows}

    rows = []
    for r in results:
        horse_no = str(r.get("horse_no", ""))
        try:
            finish_pos = int(r.get("plc", 99))
        except (ValueError, TypeError):
            finish_pos = 99
        bet = bets_map.get(horse_no, {})
        rows.append({
            "race_id": race_id,
            "horse_no": horse_no,
            "horse": r.get("horse", ""),
            "finish_pos": finish_pos,
            "won": finish_pos == 1,
            "win_odds": r.get("win_odds", ""),
            "stake": bet.get("stake"),
            "odds_taken": bet.get("odds_taken"),
            "result": bet.get("result"),
            "pnl": bet.get("pnl"),
            "logged_at": datetime.now().isoformat(),
        })

    if not rows:
        return

    df_new = pd.DataFrame(rows)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if RESULT_LOG.exists():
        df_existing = pd.read_parquet(RESULT_LOG)
        df_existing = df_existing[df_existing["race_id"] != race_id]
        df_out = pd.concat([df_existing, df_new], ignore_index=True)
    else:
        df_out = df_new
    df_out.to_parquet(RESULT_LOG, index=False)
    logger.info(f"{race_id}: {len(rows)} result rows appended to result_log.parquet")


async def main():
    parser = argparse.ArgumentParser(description="Scrape results and settle bets")
    parser.add_argument("--date", default=datetime.now().strftime("%Y-%m-%d"))
    parser.add_argument("--venue", default=None, help="ST or HV (auto-resolved from DB if omitted)")
    parser.add_argument("--race", type=int, default=0)
    parser.add_argument("--scrape-only", action="store_true", help="Scrape results without settling")
    args = parser.parse_args()

    init_db()

    if not args.venue:
        args.venue = get_venue_for_date(args.date) or "ST"
        logger.info(f"Auto-resolved venue for {args.date} to: {args.venue}")

    if args.race > 0:
        race_ids = [f"{args.date}_{args.venue}_R{args.race}"]
    else:
        race_ids = get_race_ids_for_date(args.date, args.venue)

    if not race_ids:
        logger.warning(f"No racecards for {args.date} {args.venue}")
        return

    user_data = DATA_DIR / "browser_session_results"
    user_data.mkdir(parents=True, exist_ok=True)

    total_pnl = 0
    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            str(user_data.absolute()), headless=True,
            viewport={"width": 1280, "height": 800},
        )
        page = context.pages[0] if context.pages else await context.new_page()

        for rid in race_ids:
            try:
                parts = rid.split("_")
                race_no = int(parts[-1].replace("R", ""))
                data = await scrape_results(args.date, args.venue, race_no, page)
                if data:
                    if not args.scrape_only:
                        pnl = settle(rid, data)
                        total_pnl += pnl
                    log_result_rows(rid, data)
            except Exception as e:
                logger.error(f"{rid}: {e}")

        await context.close()

    if not args.scrape_only:
        bankroll = get_bankroll()
        logger.success(f"Learn complete. Total PnL: {total_pnl:+.0f}. Bankroll: {bankroll['balance']:.0f}")
        
        # Continuous self-correction trigger: Automatically recalibrate ensemble weights
        try:
            logger.info("Triggering automatic model weight and hyperparameter recalibration...")
            recal_script = str(Path(__file__).resolve().parent / "recalibrate.py")
            subprocess.run([sys.executable, recal_script], check=True)
            logger.success("Model recalibration completed successfully!")
        except Exception as e:
            logger.error(f"Automatic model recalibration failed: {e}")
    else:
        logger.success("Scrape-only complete — no bets settled")


if __name__ == "__main__":
    asyncio.run(main())
