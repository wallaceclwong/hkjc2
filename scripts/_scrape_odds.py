"""Scrape WIN/PLA odds from HKJC SPA WP page DOM."""
import asyncio, json, re, sys
from pathlib import Path
sys.path.insert(0, "/opt/hkjc2")
from dotenv import load_dotenv
from playwright.async_api import async_playwright
from config import BASE_DIR, DATA_DIR
from scripts.ingest_odds import _hkjc_login

load_dotenv(BASE_DIR / ".env")

async def scrape_race(page, date_str, venue, race_no):
    """Navigate to race odds page and extract WIN/PLA odds from DOM."""
    date_path = date_str.replace("-", "/")
    url = f"https://bet.hkjc.com/en/racing/wp/{date_path}/{venue}/{race_no}"
    await page.goto(url, timeout=30000)
    await asyncio.sleep(5)

    result = await page.evaluate("""() => {
        const body = document.body.innerText;
        const lines = body.split('\\n').map(l => l.trim()).filter(l => l);

        let winOdds = {};
        let plaOdds = {};
        let foundWin = false;
        let foundPla = false;

        // Look for WIN/PLA odds sections and parse runner:price pairs
        for (let i = 0; i < lines.length; i++) {
            const line = lines[i];

            // Detect WIN odds section
            if (/^WIN$/i.test(line) || /^Win$/i.test(line)) {
                foundWin = true;
                foundPla = false;
                continue;
            }
            if (/^PLA$/i.test(line) || /^Place$/i.test(line) || /^PLACE$/i.test(line)) {
                foundWin = false;
                foundPla = true;
                continue;
            }
            if (/^QIN$/i.test(line) || /^Quinella$/i.test(line)) {
                foundWin = false;
                foundPla = false;
                continue;
            }

            // Parse odds line: "1 3.5" or "1 @ 3.5" or "1 = 3.5"
            if (foundWin || foundPla) {
                const match = line.match(/^(\\d{1,2})\\s+[@=]?\\s*(\\d+\\.?\\d*)$/);
                if (match) {
                    const runner = match[1];
                    const price = parseFloat(match[2]);
                    if (foundWin) winOdds[runner] = price;
                    if (foundPla) plaOdds[runner] = price;
                }
            }
        }

        // Fallback: search by known patterns in the page
        // Look for horse rows with odds
        const allText = body;
        // Pattern: horse number followed by odds nearby
        const horseRows = [];
        const horsePattern = /(\\d{1,2})\\s+([A-Za-z\\s]+)\\s+.*?(\\d+\\.\\d+)/g;
        let m;
        while ((m = horsePattern.exec(allText)) !== null) {
            horseRows.push({no: m[1], name: m[2].trim(), odds: parseFloat(m[3])});
        }

        return {
            winOdds: winOdds,
            plaOdds: plaOdds,
            horseRows: horseRows.slice(0, 14),
            lineCount: lines.length,
            foundWinSection: foundWin,
            foundPlaSection: foundPla,
            // Return raw lines around WIN/PLA for debugging
            sampleLines: lines.filter((l, i) => {
                for (let j = Math.max(0, i-2); j <= Math.min(lines.length-1, i+2); j++) {
                    if (/^(WIN|PLA|Place|Win)$/i.test(lines[j])) return true;
                }
                return false;
            }).slice(0, 40)
        };
    }""")

    return result


async def main():
    date_str = sys.argv[1] if len(sys.argv) > 1 else "2026-06-03"
    venue = sys.argv[2] if len(sys.argv) > 2 else "HV"
    race_no = int(sys.argv[3]) if len(sys.argv) > 3 else 1

    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            str(DATA_DIR / "browser_session_odds"), headless=True,
            viewport={"width": 1280, "height": 800})
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        ok = await _hkjc_login(page, ctx)
        if not ok:
            print("Login failed")
            await ctx.close()
            return

        # Load SPA first
        await page.goto("https://bet.hkjc.com/en/racing", timeout=30000)
        await asyncio.sleep(3)

        # Scrape
        print(f"Scraping R{race_no}...")
        result = await scrape_race(page, date_str, venue, race_no)

        print(f"Page lines: {result['lineCount']}")
        print(f"WIN section found: {result['foundWinSection']}")
        print(f"PLA section found: {result['foundPlaSection']}")
        print(f"WIN odds: {json.dumps(result['winOdds'], indent=2)}")
        print(f"PLA odds: {json.dumps(result['plaOdds'], indent=2)}")
        print(f"Horse rows: {json.dumps(result['horseRows'], indent=2)}")

        if not result['winOdds'] and not result['plaOdds']:
            print("\nSample lines around WIN/PLA:")
            for l in result['sampleLines'][:50]:
                print(f"  '{l}'")

        await ctx.close()

if __name__ == "__main__":
    asyncio.run(main())
