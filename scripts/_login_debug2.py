"""Debug: dump trust-browser dialog content to find the Yes button."""
import asyncio, sys, os, json, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dotenv import load_dotenv
from playwright.async_api import async_playwright

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
OTP_FILE = Path("C:/tmp/hkjc_otp.txt")

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

        # Check auth
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

        otp_reached = False
        trust_stage = False
        got_token = False
        saved_auth_id = ""
        prefix = "?"

        async def on_response(response):
            nonlocal otp_reached, saved_auth_id, prefix, trust_stage, got_token
            if "auth.ark.hkjc.com" in response.url and "authenticate" in response.url:
                try:
                    data = json.loads(await response.text())
                    stage = data.get("stage", "")
                    if data.get("tokenId"):
                        got_token = True
                        print(f"GOT TOKEN! {str(data.get('tokenId',''))[:60]}", flush=True)
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
                    elif stage == "trust-browser-confirm":
                        trust_stage = True
                        saved_auth_id = data.get("authId", "")
                    print(f"FR: stage={stage} token={bool(data.get('tokenId'))} authId={(data.get('authId','') or '')[:20]}", flush=True)
                except:
                    pass

        page.on("response", on_response)

        # Submit credentials
        print("=== Submitting credentials ===", flush=True)
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
            print("ERROR: no OTP", flush=True)
            await context.close()
            return

        print(f"OTP_SENT|{prefix}", flush=True)

        try:
            OTP_FILE.write_text("")
        except:
            pass

        # Poll for OTP
        deadline = time.time() + 90
        while time.time() < deadline:
            try:
                otp = OTP_FILE.read_text().strip()
            except:
                otp = ""

            if len(otp) == 6 and otp.isdigit():
                print(f"GOT OTP: {otp}", flush=True)

                boxes = await page.query_selector_all("input.otp-input")
                if len(boxes) >= 4:
                    for i, digit in enumerate(otp[:len(boxes)]):
                        await boxes[i].click()
                        await page.keyboard.type(digit)
                        await asyncio.sleep(0.02)
                    print(f"Typed {min(len(otp), len(boxes))} digits, waiting...", flush=True)

                    # Wait for trust-browser or token
                    for i in range(15):
                        await asyncio.sleep(1)
                        if got_token:
                            print("SUCCESS!", flush=True)
                            await context.close()
                            return
                        if trust_stage:
                            break

                    if got_token:
                        print("SUCCESS!", flush=True)
                        await context.close()
                        return

                    if trust_stage:
                        print(f"\n=== TRUST BROWSER STAGE ===", flush=True)

                        # Wait a bit for the dialog to fully render
                        await asyncio.sleep(3)

                        # DUMP ALL DIALOGS
                        dialog_dump = await page.evaluate("""() => {
                            const dialogs = document.querySelectorAll('[role="dialog"], .modal, .dialog, [class*="modal"], [class*="dialog"], [class*="popup"], [class*="overlay"]');
                            const result = [];
                            dialogs.forEach((d, idx) => {
                                const style = window.getComputedStyle(d);
                                result.push({
                                    idx: idx,
                                    tag: d.tagName,
                                    id: d.id,
                                    className: (d.className || '').toString().slice(0, 100),
                                    visible: d.offsetParent !== null,
                                    display: style.display,
                                    visibility: style.visibility,
                                    opacity: style.opacity,
                                    zIndex: style.zIndex,
                                    innerHTML: d.innerHTML.slice(0, 2000),
                                    innerText: (d.innerText || '').slice(0, 1000),
                                });
                            });
                            return result;
                        }""")

                        for d in dialog_dump:
                            print(f"\n--- Dialog {d['idx']}: {d['tag']}#{d['id']} class={d['className'][:80]}", flush=True)
                            print(f"  visible={d['visible']} display={d['display']} visibility={d['visibility']} opacity={d['opacity']} zIndex={d['zIndex']}", flush=True)
                            print(f"  text: {d['innerText'][:500]}", flush=True)
                            print(f"  html: {d['innerHTML'][:1000]}", flush=True)

                        # Also search the ENTIRE page for "Yes" or "Trust" or "200004"
                        trust_text = await page.evaluate("""() => {
                            const body = document.body.innerText;
                            const idx = body.indexOf('Trust');
                            if (idx >= 0) return body.slice(Math.max(0, idx-50), idx+200);
                            const idx2 = body.indexOf('200004');
                            if (idx2 >= 0) return body.slice(Math.max(0, idx2-50), idx2+200);
                            const idx3 = body.indexOf('Yes');
                            if (idx3 >= 0) return body.slice(Math.max(0, idx3-50), idx3+200);
                            return 'NOT FOUND';
                        }""")
                        print(f"\nTrust/Yes search: {trust_text}", flush=True)

                        # Search ALL elements for "Yes" button more broadly
                        all_yes = await page.evaluate("""() => {
                            const result = [];
                            const all = document.querySelectorAll('*');
                            for (const el of all) {
                                const t = (el.innerText || el.textContent || '').trim();
                                if (t === 'Yes' || t === 'YES' || t === '是') {
                                    const style = window.getComputedStyle(el);
                                    result.push({
                                        tag: el.tagName,
                                        id: el.id,
                                        class: (el.className||'').toString().slice(0, 80),
                                        visible: el.offsetParent !== null,
                                        display: style.display,
                                        opacity: style.opacity,
                                        zIndex: style.zIndex,
                                        parent: el.parentElement?.tagName || 'none',
                                        html: el.outerHTML.slice(0, 300),
                                    });
                                }
                                if (result.length >= 10) break;
                            }
                            return result;
                        }""")
                        print(f"\nAll 'Yes' elements: {json.dumps(all_yes, indent=2)}", flush=True)

                        await context.close()
                        return
                else:
                    print(f"No OTP boxes: {len(boxes)}", flush=True)
                    await context.close()
                    return

            await asyncio.sleep(0.3)

        print("TIMEOUT", flush=True)
        await context.close()

if __name__ == "__main__":
    asyncio.run(main())
