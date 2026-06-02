"""Login with OTP passed as CLI argument — fastest path, no polling.
Usage: python scripts/_login_cli.py --otp 123456
"""
import asyncio, sys, os, json, argparse
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dotenv import load_dotenv
from playwright.async_api import async_playwright

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"

async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--otp", required=True, help="6-digit OTP code from SMS")
    args = parser.parse_args()
    otp_code = args.otp.strip()

    if len(otp_code) != 6 or not otp_code.isdigit():
        print(f"ERROR: OTP must be 6 digits, got: {otp_code}")
        return

    load_dotenv(ENV_PATH)
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

        # Check already authenticated
        try:
            resp = await page.request.post(
                "https://txn01.hkjc.com/BetslipIB/services/SSOService.svc/DoCheckSSOSignInStatusTR",
                headers={"Content-Type": "application/json"},
                data='{"knownWebID":"","knownSSOGUID":"","isNew":true}')
            sso_text = await resp.text()
            if "SSO_SIGN_IN" in sso_text and "NOT_SIGN_IN" not in sso_text:
                print("Already authenticated!", flush=True)
                await context.close()
                return
        except:
            pass

        # Track ForgeRock responses
        otp_reached = False
        saved_auth_id = ""
        prefix = "?"
        fr_errors = []

        async def on_response(response):
            nonlocal otp_reached, saved_auth_id, prefix
            if "auth.ark.hkjc.com" in response.url and "authenticate" in response.url:
                try:
                    body = await response.text()
                    data = json.loads(body)
                    stage = data.get("stage", "")
                    if stage == "otp":
                        otp_reached = True
                        saved_auth_id = data.get("authId", "")
                        for cb in data.get("callbacks", []):
                            for o in cb.get("output", []):
                                v = str(o.get("value", ""))
                                if "prefixCode" in v:
                                    try:
                                        prefix = json.loads(v).get("prefixCode", "?")
                                    except:
                                        pass
                    # Capture errors
                    if data.get("code"):
                        fr_errors.append(f"code={data['code']} msg={data.get('message','')[:100]}")
                except:
                    pass

        page.on("response", on_response)

        # Step 1: Submit credentials
        print("Submitting credentials...", flush=True)
        await page.goto("https://bet.hkjc.com/en/racing/login",
                        wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(3)

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

        # Wait for OTP stage
        for i in range(20):
            await asyncio.sleep(1)
            if otp_reached:
                break

        if not otp_reached:
            print(f"ERROR: OTP stage not reached after 20s", flush=True)
            if fr_errors:
                print(f"ForgeRock errors: {fr_errors}", flush=True)
            await context.close()
            return

        print(f"OTP stage reached. Prefix: {prefix}", flush=True)

        # Step 2: Type OTP via real keyboard
        boxes = await page.query_selector_all("input.otp-input")
        box_count = len(boxes)
        print(f"Typing OTP into {box_count} boxes...", flush=True)

        if box_count >= 4:
            for i, digit in enumerate(otp_code[:box_count]):
                await boxes[i].click()
                await page.keyboard.type(digit)
                await asyncio.sleep(0.03)
            print(f"Typed {min(len(otp_code), box_count)} digits", flush=True)
            await asyncio.sleep(0.5)
            await page.keyboard.press("Enter")
            await asyncio.sleep(4)
        else:
            print(f"ERROR: only {box_count} OTP boxes found", flush=True)
            await context.close()
            return

        # Step 3: Check SSO
        try:
            resp = await page.request.post(
                "https://txn01.hkjc.com/BetslipIB/services/SSOService.svc/DoCheckSSOSignInStatusTR",
                headers={"Content-Type": "application/json"},
                data='{"knownWebID":"","knownSSOGUID":"","isNew":true}')
            sso = await resp.text()
            if "SSO_SIGN_IN" in sso and "NOT_SIGN_IN" not in sso:
                print("\n*** LOGIN SUCCESS! Cookies saved to browser_session_odds ***", flush=True)
            else:
                print(f"\nFAILED. SSO: {sso[:300]}", flush=True)
                if fr_errors:
                    print(f"ForgeRock errors: {fr_errors}", flush=True)
        except Exception as e:
            print(f"SSO check error: {e}", flush=True)

        await context.close()

if __name__ == "__main__":
    asyncio.run(main())
