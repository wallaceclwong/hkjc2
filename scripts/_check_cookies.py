"""Check trust-browser cookie expiration."""
import asyncio
from datetime import datetime
from playwright.async_api import async_playwright
from pathlib import Path
import sys

SESSION_DIR = sys.argv[1] if len(sys.argv) > 1 else "data/browser_session_odds"

async def check():
    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(SESSION_DIR, headless=True)
        cookies = await ctx.cookies()
        for c in cookies:
            name = c.get("name", "")
            domain = c.get("domain", "")
            if any(kw in name or kw in domain for kw in ["HKJC", "ark", "SSO", "TBR", "iPlanet"]):
                exp = c.get("expires", -1)
                if exp > 0:
                    exp_dt = datetime.fromtimestamp(exp)
                    print(f"{domain:30s} {name:25s} expires={exp_dt.strftime('%Y-%m-%d %H:%M')}")
                else:
                    print(f"{domain:30s} {name:25s} expires=session")
        await ctx.close()

asyncio.run(check())
