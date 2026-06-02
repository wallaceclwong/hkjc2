"""Fast-polling login with full ForgeRock flow including trust-browser.
Usage: python scripts/_login_fast.py
Write the 6-digit OTP to C:\tmp\hkjc_otp.txt when prompted.
"""
import asyncio, sys, os, json, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dotenv import load_dotenv
from playwright.async_api import async_playwright

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
OTP_FILE = Path("/tmp/hkjc_otp.txt")

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

        # Check already authenticated
        try:
            resp = await page.request.post(
                "https://txn01.hkjc.com/BetslipIB/services/SSOService.svc/DoCheckSSOSignInStatusTR",
                headers={"Content-Type": "application/json"},
                data='{"knownWebID":"","knownSSOGUID":"","isNew":true}')
            if "SSO_SIGN_IN" in await resp.text() and "NOT_SIGN_IN" not in await resp.text():
                print("ALREADY_AUTHENTICATED", flush=True)
                await context.close()
                return
        except:
            pass

        # ForgeRock state
        otp_reached = False
        trust_stage = False
        got_token = False
        prefix = "?"

        async def on_response(response):
            nonlocal otp_reached, prefix, trust_stage, got_token
            if "auth.ark.hkjc.com" in response.url and "authenticate" in response.url:
                try:
                    data = json.loads(await response.text())
                    stage = data.get("stage", "")
                    if data.get("tokenId"):
                        got_token = True
                    if stage == "otp":
                        otp_reached = True
                        for cb in data.get("callbacks", []):
                            for o in cb.get("output", []):
                                v = str(o.get("value", ""))
                                if "prefixCode" in v:
                                    try:
                                        prefix = json.loads(v).get("prefixCode", "?")
                                    except:
                                        pass
                    elif stage == "trust-browser-confirm":
                        trust_stage = True
                except:
                    pass

        page.on("response", on_response)

        # Phase 1: Submit credentials via SPA
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

        for i in range(25):
            await asyncio.sleep(1)
            if otp_reached:
                break

        if not otp_reached:
            print("ERROR: OTP not reached", flush=True)
            await context.close()
            return

        print(f"OTP_SENT|{prefix}", flush=True)

        try:
            OTP_FILE.write_text("")
        except:
            pass

        # Phase 2: Poll for OTP
        deadline = time.time() + 90
        while time.time() < deadline:
            try:
                otp = OTP_FILE.read_text().strip()
            except:
                otp = ""

            if len(otp) == 6 and otp.isdigit():
                elapsed = time.time() - (deadline - 90)
                print(f"GOT_OTP|{otp}|{elapsed:.1f}s", flush=True)

                boxes = await page.query_selector_all("input.otp-input")
                if len(boxes) >= 4:
                    for i, digit in enumerate(otp[:len(boxes)]):
                        await boxes[i].click()
                        await page.keyboard.type(digit)
                        await asyncio.sleep(0.02)
                    print("OTP typed", flush=True)

                    # Wait for trust-browser or token
                    for i in range(15):
                        await asyncio.sleep(1)
                        if got_token:
                            print("TOKEN_RECEIVED, waiting for SPA to process...", flush=True)
                            # Wait for SPA to process token and establish SSO
                            await asyncio.sleep(5)
                            # Check SSO
                            try:
                                resp = await page.request.post(
                                    "https://txn01.hkjc.com/BetslipIB/services/SSOService.svc/DoCheckSSOSignInStatusTR",
                                    headers={"Content-Type": "application/json"},
                                    data='{"knownWebID":"","knownSSOGUID":"","isNew":true}')
                                sso = await resp.text()
                                if "SSO_SIGN_IN" in sso and "NOT_SIGN_IN" not in sso:
                                    print("SUCCESS|LOGIN_OK|SSO_VERIFIED", flush=True)
                                else:
                                    print(f"TOKEN_OK_BUT_SSO_FAILED|{sso[:200]}", flush=True)
                            except Exception as e:
                                print(f"TOKEN_OK_BUT_SSO_ERROR|{e}", flush=True)
                            await context.close()
                            return
                        if trust_stage:
                            break

                    if got_token:
                        print("SUCCESS|LOGIN_OK", flush=True)
                        await context.close()
                        return

                    # Phase 3: Handle trust-browser dialog
                    if trust_stage:
                        print("TRUST_BROWSER: handling dialog...", flush=True)
                        await asyncio.sleep(3)

                        # Click "Trust this browser" using Playwright click (real mouse event)
                        try:
                            trust_btn = await page.query_selector('#notTrustButton')
                            if trust_btn:
                                await trust_btn.click(force=True)
                                print("  Clicked #notTrustButton via Playwright", flush=True)
                            else:
                                print("  #notTrustButton not found", flush=True)
                        except Exception as e:
                            print(f"  Click #notTrustButton error: {e}", flush=True)
                        await asyncio.sleep(1)

                        # Click Next using Playwright — find the element and click it properly
                        next_clicked = False
                        try:
                            # Try known selectors first
                            for selector in ['.trustbrowser-btn-group', '#popup-123 .trustbrowser-btn-group',
                                             '[class*="trustbrowser-btn"]', '[class*="next"]',
                                             '#popup-trustbrowser-container [class*="btn"]']:
                                try:
                                    btn = await page.query_selector(selector)
                                    if btn:
                                        text = await btn.inner_text()
                                        print(f"  Trying selector {selector}: text='{text.strip()}'", flush=True)
                                        await btn.click(force=True)
                                        next_clicked = True
                                        print(f"  Clicked {selector}", flush=True)
                                        break
                                except:
                                    pass

                            # Fallback: find element with text "Next" via Playwright
                            if not next_clicked:
                                next_el = await page.query_selector('text="Next"')
                                if next_el:
                                    await next_el.click(force=True)
                                    next_clicked = True
                                    print("  Clicked text='Next' element via Playwright", flush=True)
                        except Exception as e:
                            print(f"  Next click error: {e}", flush=True)

                        if not next_clicked:
                            print("  No Next found, trying Enter...", flush=True)
                            await page.keyboard.press("Enter")

                        # Wait for ForgeRock response
                        await asyncio.sleep(5)

                        # Check if token arrived
                        for i in range(10):
                            await asyncio.sleep(1)
                            if got_token:
                                break

                        if got_token:
                            print("TOKEN_RECEIVED, waiting for SPA to process...", flush=True)
                            # Wait for SPA to process token and establish SSO
                            await asyncio.sleep(5)
                            # Check SSO
                            try:
                                resp = await page.request.post(
                                    "https://txn01.hkjc.com/BetslipIB/services/SSOService.svc/DoCheckSSOSignInStatusTR",
                                    headers={"Content-Type": "application/json"},
                                    data='{"knownWebID":"","knownSSOGUID":"","isNew":true}')
                                sso = await resp.text()
                                if "SSO_SIGN_IN" in sso and "NOT_SIGN_IN" not in sso:
                                    print("SUCCESS|LOGIN_OK|SSO_VERIFIED", flush=True)
                                else:
                                    print(f"TOKEN_OK_BUT_SSO_FAILED|{sso[:200]}", flush=True)
                            except Exception as e:
                                print(f"TOKEN_OK_BUT_SSO_ERROR|{e}", flush=True)
                            await context.close()
                            return

                        # Still no token — try API submission as last resort
                        print("  No token, trying API...", flush=True)

                        # Get current authId from ForgeRock
                        auth_id = await page.evaluate("""() => {
                            return window.__fr_auth_id || '';
                        }""")

                        # Try API with full callbacks
                        api_result = await page.evaluate("""
                            async () => {
                                const headers = {
                                    'Content-Type': 'application/json',
                                    'Accept-API-Version': 'resource=2.0, protocol=1.0',
                                    'X-Requested-With': 'XMLHttpRequest'
                                };
                                // First, re-init to get current authId
                                const r1 = await fetch(
                                    'https://auth.ark.hkjc.com/am/json/realms/root/realms/customer/authenticate?authIndexType=service&authIndexValue=hkjcHSLogin-Web',
                                    { method: 'POST', headers, body: '{}', credentials: 'include' }
                                );
                                const d1 = await r1.json();
                                return { stage: d1.stage, authId: (d1.authId||'').slice(0, 30) };
                            }
                        """)
                        print(f"  Re-init: {json.dumps(api_result)}", flush=True)

                        # Check SSO
                        await asyncio.sleep(2)
                        try:
                            resp = await page.request.post(
                                "https://txn01.hkjc.com/BetslipIB/services/SSOService.svc/DoCheckSSOSignInStatusTR",
                                headers={"Content-Type": "application/json"},
                                data='{"knownWebID":"","knownSSOGUID":"","isNew":true}')
                            sso = await resp.text()
                            if "SSO_SIGN_IN" in sso and "NOT_SIGN_IN" not in sso:
                                print("SUCCESS|LOGIN_OK|sso_verified", flush=True)
                            else:
                                print(f"FAILED|got_token={got_token}|sso={sso[:200]}", flush=True)
                        except Exception as e:
                            print(f"FAILED|{e}", flush=True)
                        await context.close()
                        return
                else:
                    print(f"FAILED|no_boxes", flush=True)
                    await context.close()
                    return

            await asyncio.sleep(0.3)

        print("TIMEOUT", flush=True)
        await context.close()

if __name__ == "__main__":
    asyncio.run(main())
