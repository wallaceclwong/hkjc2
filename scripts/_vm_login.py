"""VM-friendly login: reads OTP from file, stays alive polling for updates.
Usage on VM: /opt/hkjc2/.venv/bin/python scripts/_vm_login.py
Write OTP to /tmp/hkjc_otp.txt and the script will detect and submit it.
"""
import asyncio, sys, os, json, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dotenv import load_dotenv
from playwright.async_api import async_playwright

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
OTP_FILE = "/tmp/hkjc_otp.txt"


async def main():
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

        # Check if already authenticated
        try:
            resp = await page.request.post(
                "https://txn01.hkjc.com/BetslipIB/services/SSOService.svc/DoCheckSSOSignInStatusTR",
                headers={"Content-Type": "application/json"},
                data='{"knownWebID":"","knownSSOGUID":"","isNew":true}'
            )
            if "SSO_SIGN_IN" in await resp.text() and "NOT_SIGN_IN" not in await resp.text():
                print("Already authenticated!", flush=True)
                return
        except:
            pass

        # Track ForgeRock state
        prefix = "?"
        otp_reached = False
        saved_auth_id = ""

        async def on_response(response):
            nonlocal prefix, otp_reached, saved_auth_id
            if "auth.ark.hkjc.com" in response.url and "authenticate" in response.url:
                try:
                    body = await response.text()
                    data = json.loads(body)
                    if data.get("stage") == "otp":
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
                except:
                    pass

        page.on("response", on_response)

        # Submit credentials
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

        for i in range(15):
            await asyncio.sleep(1)
            if otp_reached:
                break

        if not otp_reached:
            print("ERROR: OTP stage not reached", flush=True)
            await context.close()
            return

        print(f"\n*** OTP SENT! Prefix: {prefix} ***", flush=True)
        print(f"*** Write code to {OTP_FILE} ***", flush=True)
        print(f"*** echo 'XXXXXX' > {OTP_FILE} ***\n", flush=True)

        # Clear old OTP file
        open(OTP_FILE, 'w').close()

        # Poll OTP file
        last_otp = ""
        deadline = time.time() + 90

        while time.time() < deadline:
            try:
                with open(OTP_FILE, 'r') as f:
                    otp = f.read().strip()
            except:
                otp = ""

            if otp != last_otp and len(otp) == 6 and otp.isdigit():
                elapsed = int(time.time() - (deadline - 90))
                print(f"[{elapsed}s] Got OTP: {otp}", flush=True)

                # Type digits using real Playwright keyboard
                boxes = await page.query_selector_all("input.otp-input")
                if len(boxes) >= 4:
                    for i, digit in enumerate(otp[:len(boxes)]):
                        await boxes[i].click()
                        await page.keyboard.type(digit)
                        await asyncio.sleep(0.05)
                    print(f"  Typed {min(len(otp), len(boxes))} digits", flush=True)
                    await asyncio.sleep(1)
                    await page.keyboard.press("Enter")
                    await asyncio.sleep(3)

                    try:
                        resp = await page.request.post(
                            "https://txn01.hkjc.com/BetslipIB/services/SSOService.svc/DoCheckSSOSignInStatusTR",
                            headers={"Content-Type": "application/json"},
                            data='{"knownWebID":"","knownSSOGUID":"","isNew":true}'
                        )
                        sso = await resp.text()
                        if "SSO_SIGN_IN" in sso and "NOT_SIGN_IN" not in sso:
                            print("\n*** LOGIN SUCCESS! ***", flush=True)
                            await context.close()
                            return
                        else:
                            print(f"  SSO: not signed in", flush=True)
                    except Exception as e:
                        print(f"  Error: {e}", flush=True)
                else:
                    print(f"  Boxes gone ({len(boxes)})", flush=True)

                last_otp = otp

            await asyncio.sleep(2)

        print("Timeout", flush=True)
        await context.close()


if __name__ == "__main__":
    asyncio.run(main())
