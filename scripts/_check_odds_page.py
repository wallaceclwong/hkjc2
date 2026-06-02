"""Check if WIN/PLA odds are displayed on HKJC SPA."""
import asyncio, json, sys
from pathlib import Path
sys.path.insert(0, "/opt/hkjc2")

from dotenv import load_dotenv
from playwright.async_api import async_playwright
from config import BASE_DIR, DATA_DIR

load_dotenv(BASE_DIR / ".env")

async def check():
    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            str(DATA_DIR / "browser_session_odds"), headless=True,
            viewport={"width": 1280, "height": 800})
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        # Check SSO first
        resp = await page.request.post(
            "https://txn01.hkjc.com/BetslipIB/services/SSOService.svc/DoCheckSSOSignInStatusTR",
            headers={"Content-Type": "application/json"},
            data='{"knownWebID":"","knownSSOGUID":"","isNew":true}')
        sso = json.loads(await resp.text())
        level = "0"
        for item in sso.get("DoCheckSSOSignInStatusTRResult", []):
            if item["Key"] == "sso_sign_in_level":
                level = item["Value"]
        print(f"SSO level: {level}")

        # Load home page (SPA)
        print("Loading SPA home...")
        await page.goto("https://bet.hkjc.com/en/racing", timeout=30000)
        await asyncio.sleep(5)

        # Navigate to R1 odds page
        print("Going to R1 odds...")
        await page.goto("https://bet.hkjc.com/en/racing/wp/2026/06/03/HV/1", timeout=30000)
        await asyncio.sleep(8)
        print(f"URL: {page.url[:100]}")

        # Dump what's on the page
        result = await page.evaluate("""() => {
            const body = document.body ? document.body.innerText : '';
            // Find all decimal numbers (potential odds)
            const oddsMatches = body.match(/\\d+\\.\\d+/g) || [];
            const uniqueOdds = [...new Set(oddsMatches)].slice(0, 30);

            // Look for key sections
            const sections = [];
            const lines = body.split('\\n');
            for (let i = 0; i < lines.length; i++) {
                const l = lines[i].trim();
                if (l.includes('Pool') || l.includes('Odds') || l.includes('WIN') ||
                    l.includes('PLA') || l.includes('Dividend') || l.includes('Sell') ||
                    l.includes('win') || l.includes('pla')) {
                    sections.push(l.slice(0, 150));
                }
            }

            return {
                url: window.location.href,
                title: document.title,
                decimalCount: uniqueOdds.length,
                uniqueOdds: uniqueOdds,
                relevantSections: sections.slice(0, 20),
                bodyLength: body.length,
                bodyTop: body.slice(0, 1200)
            };
        }""")

        print(f"Title: {result['title']}")
        print(f"Body length: {result['bodyLength']}")
        print(f"Decimal numbers: {result['decimalCount']}")
        print(f"Unique odds: {result['uniqueOdds'][:20]}")
        print(f"\nRelevant sections:")
        for s in result['relevantSections'][:15]:
            print(f"  {s}")
        if not result['relevantSections']:
            print(f"\nBody top:\n{result['bodyTop']}")

        await ctx.close()

asyncio.run(check())
