"""Final login: handles both trusted (no OTP) and untrusted (OTP) flows.
Usage: python scripts/_login_final.py [--otp-file C:/tmp/hkjc_otp.txt]
Keeps browser open until SSO session is fully established.
"""
import asyncio, sys, os, json, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dotenv import load_dotenv
from playwright.async_api import async_playwright

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
OTP_FILE = Path("/tmp/hkjc_otp.txt")

async def check_sso(page):
    """Check SSO status. Returns (is_authenticated, details)."""
    try:
        resp = await page.request.post(
            "https://txn01.hkjc.com/BetslipIB/services/SSOService.svc/DoCheckSSOSignInStatusTR",
            headers={"Content-Type": "application/json"},
            data='{"knownWebID":"","knownSSOGUID":"","isNew":true}')
        sso_text = await resp.text()
        sso = json.loads(sso_text)
        details = {}
        for item in sso.get("DoCheckSSOSignInStatusTRResult", []):
            details[item["Key"]] = item["Value"]
        level = details.get("sso_sign_in_level", "0")
        guid = details.get("sso_guid", "")
        is_auth = (level != "0" and guid) or ("SSO_SIGN_IN" in sso_text and "NOT_SIGN_IN" not in sso_text)
        return is_auth, details
    except Exception as e:
        return False, {"error": str(e)}

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

        # Pre-check
        is_auth, details = await check_sso(page)
        if is_auth:
            print(f"ALREADY_AUTHENTICATED|level={details.get('sso_sign_in_level')}|guid={details.get('sso_guid','')[:20]}", flush=True)
            await context.close()
            return

        # ForgeRock state
        otp_reached = False
        trust_stage = False
        got_token = False
        prefix = "?"
        fr_events = []

        async def on_response(response):
            nonlocal otp_reached, prefix, trust_stage, got_token
            if "auth.ark.hkjc.com" in response.url and "authenticate" in response.url:
                try:
                    data = json.loads(await response.text())
                    stage = data.get("stage", "")
                    has_token = bool(data.get("tokenId"))
                    fr_events.append({"stage": stage, "token": has_token})
                    if has_token:
                        got_token = True
                        print(f"FR_TOKEN|stage={stage}|token={str(data.get('tokenId',''))[:50]}", flush=True)
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

        # Submit credentials
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

        # Wait for OTP stage, token, or timeout
        for i in range(30):
            await asyncio.sleep(1)
            if got_token:
                break
            if otp_reached:
                break

        # === PATH A: Trusted browser — token received directly ===
        if got_token and not otp_reached:
            print("FAST_PATH|trusted_browser_no_otp", flush=True)
            # Wait for SPA to establish SSO session
            for i in range(20):
                await asyncio.sleep(1)
                is_auth, details = await check_sso(page)
                if is_auth:
                    print(f"SUCCESS|SSO_ESTABLISHED|level={details.get('sso_sign_in_level')}|guid={details.get('sso_guid','')[:20]}", flush=True)
                    # Extra wait to ensure cookies are fully persisted
                    await asyncio.sleep(3)
                    await context.close()
                    return
            # Check once more
            is_auth, details = await check_sso(page)
            print(f"RESULT|auth={is_auth}|level={details.get('sso_sign_in_level')}|guid={details.get('sso_guid','')[:20]}|events={fr_events}", flush=True)
            await context.close()
            return

        # === PATH B: OTP required ===
        if otp_reached and not got_token:
            print(f"OTP_SENT|{prefix}", flush=True)
            try:
                OTP_FILE.write_text("")
            except:
                pass

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

                        # Wait for trust-browser or token
                        for i in range(15):
                            await asyncio.sleep(1)
                            if got_token:
                                break
                            if trust_stage:
                                break

                        if trust_stage and not got_token:
                            print("TRUST_BROWSER: handling...", flush=True)
                            await asyncio.sleep(3)
                            try:
                                btn = await page.query_selector('#notTrustButton')
                                if btn:
                                    await btn.click(force=True)
                            except:
                                pass
                            await asyncio.sleep(1)
                            try:
                                next_btn = await page.query_selector('.trustbrowser-btn-group')
                                if next_btn:
                                    await next_btn.click(force=True)
                            except:
                                pass
                            for i in range(20):
                                await asyncio.sleep(1)
                                if got_token:
                                    break

                        if got_token:
                            print("TOKEN_OK|waiting_for_sso...", flush=True)
                            for i in range(20):
                                await asyncio.sleep(1)
                                is_auth, details = await check_sso(page)
                                if is_auth:
                                    print(f"SUCCESS|SSO_ESTABLISHED|level={details.get('sso_sign_in_level')}|guid={details.get('sso_guid','')[:20]}", flush=True)
                                    await asyncio.sleep(3)
                                    await context.close()
                                    return

                        is_auth, details = await check_sso(page)
                        print(f"RESULT|auth={is_auth}|level={details.get('sso_sign_in_level')}|guid={details.get('sso_guid','')[:20]}", flush=True)
                        await context.close()
                        return
                    else:
                        print("FAILED|no_boxes", flush=True)
                        await context.close()
                        return

                await asyncio.sleep(0.3)

            print("TIMEOUT|otp", flush=True)
            await context.close()
            return

        # === PATH C: Error ===
        print(f"ERROR|unexpected|got_token={got_token}|otp={otp_reached}|events={fr_events}", flush=True)
        await context.close()

if __name__ == "__main__":
    asyncio.run(main())
