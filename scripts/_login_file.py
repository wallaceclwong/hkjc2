"""Hybrid login: JS form fill + ForgeRock route monitoring + file-based OTP."""
import asyncio, sys, os, json, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dotenv import load_dotenv
from playwright.async_api import async_playwright

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
OTP_FILE = Path("C:/Users/ASUS/hkjc_otp.txt")
STATE_FILE = Path(__file__).resolve().parent.parent / "data" / "session_state.json"


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

        # Check already auth'd
        try:
            resp = await page.request.post(
                "https://txn01.hkjc.com/BetslipIB/services/SSOService.svc/DoCheckSSOSignInStatusTR",
                headers={"Content-Type": "application/json"},
                data='{"knownWebID":"","knownSSOGUID":"","isNew":true}')
            sso = json.loads(await resp.text())
            items = {i["Key"]: i["Value"] for i in sso.get("DoCheckSSOSignInStatusTRResult", [])}
            if items.get("sso_sign_in_level", "0") != "0" and items.get("sso_guid"):
                print("ALREADY_AUTHENTICATED", flush=True)
                await context.close()
                return
        except:
            pass

        # ForgeRock state tracking (like _login_vm.py)
        got_token = False
        trust_stage = False
        otp_waiting = False
        prefix = "?"

        async def handle_route(route):
            nonlocal got_token, trust_stage, otp_waiting, prefix
            url = route.request.url

            if "auth.ark.hkjc.com" in url and "authenticate" in url:
                response = await route.fetch()
                try:
                    data = json.loads(await response.text())
                except:
                    await route.fulfill(response=response)
                    return

                stage = data.get("stage", "?")
                has_tok = bool(data.get("tokenId"))

                if has_tok:
                    got_token = True
                    print(f"FR: stage={stage} TOKEN!", flush=True)

                if stage == "otp":
                    otp_waiting = True
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
                    print("FR: trust-browser-confirm stage", flush=True)
                elif stage == "errorinfo":
                    for cb in data.get("callbacks", []):
                        for o in cb.get("output", []):
                            v = str(o.get("value", ""))
                            if "messageId" in v:
                                print(f"FR_ERROR: {v}", flush=True)

                print(f"FR: stage={stage} token={has_tok}", flush=True)
                await route.fulfill(response=response)
            else:
                await route.continue_()

        await page.route("**/*", handle_route)

        print("Loading login page...", flush=True)
        await page.goto("https://bet.hkjc.com/en/racing/login",
                        wait_until="networkidle", timeout=30000)
        await asyncio.sleep(2)

        # Fill credentials via JS
        print("Filling credentials...", flush=True)
        await page.evaluate(f"""
            () => {{
                var data = [['#login-account-input', '{account}'], ['#login-password-input', '{password}']];
                for (var i = 0; i < data.length; i++) {{
                    var input = document.querySelector(data[i][0]);
                    var ns = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
                    ns.call(input, data[i][1]);
                    input.dispatchEvent(new Event('input', {{ bubbles: true }}));
                    input.dispatchEvent(new Event('change', {{ bubbles: true }}));
                }}
            }}
        """)
        await asyncio.sleep(0.3)

        print("Clicking Login...", flush=True)
        await page.click("div.signIn")

        # Wait for token or OTP
        for i in range(30):
            await asyncio.sleep(1)
            if got_token or otp_waiting:
                break

        # --- Direct token (no OTP needed) ---
        if got_token:
            print("FAST_PATH: No OTP needed!", flush=True)
            for i in range(25):
                await asyncio.sleep(1)
                try:
                    resp = await page.request.post(
                        "https://txn01.hkjc.com/BetslipIB/services/SSOService.svc/DoCheckSSOSignInStatusTR",
                        headers={"Content-Type": "application/json"},
                        data='{"knownWebID":"","knownSSOGUID":"","isNew":true}')
                    sso = json.loads(await resp.text())
                    items = {i["Key"]: i["Value"] for i in sso.get("DoCheckSSOSignInStatusTRResult", [])}
                    if items.get("sso_sign_in_level", "0") != "0" and items.get("sso_guid"):
                        print(f"SUCCESS|level={items['sso_sign_in_level']}|guid={items['sso_guid'][:20]}", flush=True)
                        await context.close()
                        return
                except:
                    pass
            print("WARN: Token but SSO not established", flush=True)
            await context.close()
            return

        # --- OTP flow ---
        # Clear file and wait for code
        OTP_FILE.write_text("")
        print(f"\nOTP triggered (prefix={prefix})! Waiting for code in {OTP_FILE}...", flush=True)

        attempt = 0
        while otp_waiting and not got_token and attempt < 3:
            attempt += 1

            deadline = time.time() + 180
            otp = ""
            last_read = ""
            while time.time() < deadline:
                try:
                    content = OTP_FILE.read_text().strip()
                except:
                    content = ""
                if len(content) == 6 and content.isdigit() and content != last_read:
                    otp = content
                    break
                last_read = content
                await asyncio.sleep(0.5)

            if not otp:
                print("TIMEOUT: No OTP in file", flush=True)
                break

            print(f"Got OTP: {otp}", flush=True)

            # Fill OTP via JS
            await page.evaluate(f"""
                () => {{
                    const boxes = document.querySelectorAll('input.otp-input');
                    const digits = '{otp}';
                    for (let i = 0; i < Math.min(digits.length, boxes.length); i++) {{
                        const ns = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
                        ns.call(boxes[i], digits[i]);
                        boxes[i].dispatchEvent(new Event('input', {{ bubbles: true }}));
                        boxes[i].dispatchEvent(new Event('change', {{ bubbles: true }}));
                    }}
                }}
            """)
            await asyncio.sleep(0.5)

            # Press Enter on last box
            try:
                last_box = await page.query_selector("input.otp-input:last-of-type")
                if last_box:
                    await last_box.press("Enter")
            except:
                pass
            print("OTP submitted", flush=True)

            # Wait for outcome
            otp_waiting = False  # reset - will be set again by route handler if wrong OTP
            for i in range(25):
                await asyncio.sleep(1)
                if got_token or trust_stage or otp_waiting:
                    break

            # Handle trust-browser
            if trust_stage and not got_token:
                print("TRUST_BROWSER: handling...", flush=True)
                await asyncio.sleep(2)
                # Click "Trust this browser" — #notTrustButton = trust (HKJC IDs are backwards)
                try:
                    btn = await page.query_selector("#notTrustButton")
                    if btn:
                        await btn.click(force=True)
                        print("  Clicked: Trust this browser", flush=True)
                except:
                    pass
                await asyncio.sleep(1)
                # Click Next button (div#next or .trustbrowser-btn-group)
                try:
                    next_div = await page.query_selector("#next")
                    if next_div:
                        await next_div.click(force=True)
                        print("  Clicked: Next", flush=True)
                    else:
                        btn_group = await page.query_selector(".trustbrowser-btn-group")
                        if btn_group:
                            await btn_group.click(force=True)
                            print("  Clicked: btn-group", flush=True)
                except:
                    pass
                trust_stage = False
                for i in range(25):
                    await asyncio.sleep(1)
                    if got_token:
                        break

        # --- Final SSO check ---
        if got_token:
            print("TOKEN_OK, checking SSO...", flush=True)
            for i in range(30):
                await asyncio.sleep(1)
                try:
                    resp = await page.request.post(
                        "https://txn01.hkjc.com/BetslipIB/services/SSOService.svc/DoCheckSSOSignInStatusTR",
                        headers={"Content-Type": "application/json"},
                        data='{"knownWebID":"","knownSSOGUID":"","isNew":true}')
                    sso = json.loads(await resp.text())
                    items = {i["Key"]: i["Value"] for i in sso.get("DoCheckSSOSignInStatusTRResult", [])}
                    level = items.get("sso_sign_in_level", "0")
                    guid = items.get("sso_guid", "")
                    if i % 5 == 0:
                        print(f"  SSO: level={level} guid={'yes' if guid else 'no'}", flush=True)
                    if level != "0" and guid:
                        print(f"SUCCESS|level={level}|guid={guid[:20]}", flush=True)
                        print("Cookies saved to data/browser_session_odds", flush=True)

                        # Navigate to SPA so localStorage is populated before capturing state
                        try:
                            await page.goto("https://bet.hkjc.com/en/racing", timeout=30000)
                            await asyncio.sleep(3)
                        except:
                            pass

                        # Save storage state for pipeline reuse
                        state = await context.storage_state()
                        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
                        STATE_FILE.write_text(json.dumps(state, indent=2))
                        cookie_count = len(state.get("cookies", []))
                        origin_count = len(state.get("origins", []))
                        print(f"State saved: {cookie_count} cookies, {origin_count} origins -> {STATE_FILE}", flush=True)

                        await asyncio.sleep(1)
                        await context.close()
                        return
                except:
                    pass
            print("WARN: SSO not confirmed", flush=True)
        else:
            print("FAILED: No token", flush=True)

        await context.close()


if __name__ == "__main__":
    asyncio.run(main())
