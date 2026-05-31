"""Interactive HKJC login — user types OTP directly when prompted."""
import asyncio, sys, os, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dotenv import load_dotenv
from playwright.async_api import async_playwright

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(ENV_PATH)


async def main():
    account = os.getenv("HKJC_ACCOUNT", "")
    password = os.getenv("HKJC_PASSWORD", "")

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            "./data/browser_session_odds",
            headless=True,
            viewport={"width": 1280, "height": 800},
        )
        page = context.pages[0] if context.pages else await context.new_page()
        await page.set_extra_http_headers({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
        })

        # Check if already authenticated
        try:
            resp = await page.request.post(
                "https://txn01.hkjc.com/BetslipIB/services/SSOService.svc/DoCheckSSOSignInStatusTR",
                headers={"Content-Type": "application/json"},
                data='{"knownWebID":"","knownSSOGUID":"","isNew":true}'
            )
            if "SSO_SIGN_IN" in await resp.text() and "NOT_SIGN_IN" not in await resp.text():
                print("Already authenticated!")
                return
        except:
            pass

        await page.goto("https://bet.hkjc.com/en/racing/login",
                        wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(2)

        # Submit credentials
        print("Submitting credentials...")
        await page.evaluate(f"""
            () => {{
                for (const [id, val] of [['#login-account-input', '{account}'], ['#login-password-input', '{password}']]) {{
                    const input = document.querySelector(id);
                    const ns = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
                    ns.call(input, val);
                    input.dispatchEvent(new Event('input', {{ bubbles: true }}));
                }}
            }}
        """)
        await asyncio.sleep(0.5)
        await page.focus("#login-password-input")
        await page.keyboard.press("Enter")

        # Wait for OTP boxes
        for i in range(15):
            await asyncio.sleep(1)
            boxes = await page.evaluate("""() => document.querySelectorAll('input.otp-input').length""")
            if boxes >= 4:
                break

        boxes = await page.evaluate("""() => document.querySelectorAll('input.otp-input').length""")
        if boxes < 4:
            print("ERROR: OTP prompt not detected")
            await context.close()
            return

        # Prompt user for OTP
        print(f"\n{'='*50}")
        print(f"OTP sent to +852-XXXX0738")
        print(f"Check your phone and type the 6-digit code below:")
        print(f"{'='*50}")

        # Read OTP from stdin (blocks until user types it)
        otp = input("OTP> ").strip()

        if len(otp) != 6 or not otp.isdigit():
            print(f"Invalid OTP: {otp}")
            await context.close()
            return

        # Fill OTP
        print(f"Submitting OTP: {otp}")
        await page.evaluate(f"""
            () => {{
                const boxes = document.querySelectorAll('input.otp-input');
                const digits = '{otp}';
                for (let i = 0; i < Math.min(digits.length, boxes.length); i++) {{
                    const ns = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
                    ns.call(boxes[i], digits[i]);
                    boxes[i].dispatchEvent(new Event('input', {{ bubbles: true }}));
                    boxes[i].dispatchEvent(new Event('change', {{ bubbles: true }}));
                    boxes[i].dispatchEvent(new Event('keyup', {{ bubbles: true }}));
                }}
            }}
        """)
        await asyncio.sleep(4)

        # Check result
        resp = await page.request.post(
            "https://txn01.hkjc.com/BetslipIB/services/SSOService.svc/DoCheckSSOSignInStatusTR",
            headers={"Content-Type": "application/json"},
            data='{"knownWebID":"","knownSSOGUID":"","isNew":true}'
        )
        sso = await resp.text()
        print(f"\nSSO: {sso[:300]}")

        if "SSO_SIGN_IN" in sso and "NOT_SIGN_IN" not in sso:
            print("*** LOGIN SUCCESS! Cookies saved to browser_session_odds ***")
        else:
            print("Login failed — wrong OTP or expired")

        await context.close()


if __name__ == "__main__":
    asyncio.run(main())
