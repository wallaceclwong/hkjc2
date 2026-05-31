"""Login: submit creds, poll .env for OTP, submit in SAME session."""
import asyncio, sys, os, json, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dotenv import load_dotenv
from playwright.async_api import async_playwright

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"


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
                except:
                    pass

        page.on("response", on_response)

        # Step 1: Load login and submit credentials
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
        for i in range(15):
            await asyncio.sleep(1)
            if otp_reached:
                break

        if not otp_reached:
            print("ERROR: OTP stage not reached", flush=True)
            await context.close()
            return

        print(f"\n*** OTP SENT! Prefix: {prefix} ***", flush=True)
        print(f"*** Update .env: HKJC_OTP=______ (within 90s) ***\n", flush=True)

        # Step 2: Poll .env for OTP change
        load_dotenv(ENV_PATH)
        last_otp = os.getenv("HKJC_OTP", "")
        print(f"Current .env OTP: {last_otp} (will wait for new value)", flush=True)

        deadline = time.time() + 95
        while time.time() < deadline:
            load_dotenv(ENV_PATH, override=True)
            current_otp = os.getenv("HKJC_OTP", "")

            if current_otp != last_otp and len(current_otp) == 6 and current_otp.isdigit():
                elapsed = int(time.time() - (deadline - 95))
                print(f"\n[{elapsed}s] Got OTP: {current_otp}", flush=True)

                # Check boxes still exist
                box_count = await page.evaluate("""() => document.querySelectorAll('input.otp-input').length""")
                submit_result = "filled"
                if box_count >= 4:
                    # Type digits using real Playwright keyboard (triggers React)
                    boxes = await page.query_selector_all("input.otp-input")
                    for i, digit in enumerate(current_otp[:len(boxes)]):
                        await boxes[i].click()
                        await page.keyboard.type(digit)
                        await asyncio.sleep(0.05)
                    print(f"  Typed {min(len(current_otp), len(boxes))} digits via keyboard", flush=True)
                    # Last digit should auto-submit; if not, press Enter
                    await asyncio.sleep(1)
                    await page.keyboard.press("Enter")
                    await asyncio.sleep(3)
                    # Check SSO after submission
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
                        print(f"  SSO check error: {e}", flush=True)

                if submit_result == "boxes_gone" and saved_auth_id:
                    print(f"  Trying API fallback with saved authId...", flush=True)
                    api_result = await page.evaluate(f"""
                        async () => {{
                            const r = await fetch('https://auth.ark.hkjc.com/am/json/realms/root/realms/customer/authenticate?authIndexType=service&authIndexValue=hkjcHSLogin-Web', {{
                                method: 'POST',
                                headers: {{'Content-Type': 'application/json', 'Accept-API-Version': 'resource=2.0, protocol=1.0', 'X-Requested-With': 'XMLHttpRequest'}},
                                body: JSON.stringify({{
                                    authId: '{saved_auth_id}',
                                    callbacks: [
                                        {{ type: 'TextInputCallback',
                                           output: [{{ name: 'prompt', value: 'sms_input' }}, {{ name: 'defaultText', value: '' }}],
                                           input: [{{ name: 'IDToken2', value: '{current_otp}' }}] }},
                                        {{ type: 'ConfirmationCallback',
                                           output: [{{ name: 'prompt', value: 'sms_confirm' }}, {{ name: 'messageType', value: 0 }},
                                                    {{ name: 'options', value: ['next', 'resend', 'resendVOICE'] }},
                                                    {{ name: 'optionType', value: -1 }}, {{ name: 'defaultOption', value: 0 }}],
                                           input: [{{ name: 'IDToken3', value: 0 }}] }}
                                    ]
                                }})
                            }});
                            const d = await r.json();
                            return {{ status: r.status, tokenId: d.tokenId ? 'yes' : 'no', stage: d.stage || '?' }};
                        }}
                    """)
                    print(f"  API: {json.dumps(api_result)}", flush=True)
                    if api_result.get("tokenId") == "yes":
                        print("\n*** LOGIN SUCCESS via API! ***", flush=True)
                        await context.close()
                        return
                    await page.evaluate(f"""
                        () => {{
                            const boxes = document.querySelectorAll('input.otp-input');
                            const digits = '{current_otp}';
                            for (let i = 0; i < Math.min(digits.length, boxes.length); i++) {{
                                const ns = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
                                ns.call(boxes[i], digits[i]);
                                boxes[i].dispatchEvent(new Event('input', {{ bubbles: true }}));
                                boxes[i].dispatchEvent(new Event('change', {{ bubbles: true }}));
                                boxes[i].dispatchEvent(new Event('keyup', {{ bubbles: true }}));
                            }}
                        }}
                    """)
                    await asyncio.sleep(1)

                    # Find OTP container and click submit within it
                    clicked = await page.evaluate("""() => {
                        // Find the OTP area by locating the otp-input boxes
                        const boxes = document.querySelectorAll('input.otp-input');
                        if (boxes.length === 0) return 'no boxes';

                        // Find ALL nearby elements
                        const lastBox = boxes[boxes.length-1];
                        const rect = lastBox.getBoundingClientRect();
                        // Get elements below/to the right of the OTP boxes
                        const nearby = document.elementsFromPoint(rect.right + 30, rect.top + rect.height/2);
                        const info = nearby.slice(0,10).map(el => ({
                            tag: el.tagName,
                            text: (el.innerText||'').trim().slice(0,30),
                            id: el.id,
                            cls: (el.className||'').toString().slice(0,40)
                        }));
                        // Try clicking each nearby element
                        for (const el of nearby) {
                            if (el.tagName === 'BUTTON' || el.getAttribute('role') === 'button' || el.onclick) {
                                el.click();
                                return 'clicked nearby: ' + (el.innerText||'').trim().slice(0,30) + ' ' + el.tagName;
                            }
                        }
                        // If no button found, try clicking first element
                        if (nearby.length > 0) {
                            nearby[0].click();
                            return 'clicked: ' + info[0].text + ' ' + info[0].tag;
                        }
                        return 'nearby: ' + JSON.stringify(info);
                    }""")
                    print(f"  Submit: {clicked}", flush=True)
                    await asyncio.sleep(4)

                    try:
                        resp = await page.request.post(
                            "https://txn01.hkjc.com/BetslipIB/services/SSOService.svc/DoCheckSSOSignInStatusTR",
                            headers={"Content-Type": "application/json"},
                            data='{"knownWebID":"","knownSSOGUID":"","isNew":true}'
                        )
                        sso = await resp.text()
                        if "SSO_SIGN_IN" in sso and "NOT_SIGN_IN" not in sso:
                            print("\n*** LOGIN SUCCESS! Cookies saved to browser_session_odds ***", flush=True)
                            await context.close()
                            return
                        else:
                            print(f"  Wrong OTP or expired — need fresh one.", flush=True)
                    except Exception as e:
                        print(f"  Error: {e}", flush=True)

                last_otp = current_otp

            await asyncio.sleep(2)

        print(f"\nTimeout after 95s.", flush=True)
        await context.close()


if __name__ == "__main__":
    asyncio.run(main())
