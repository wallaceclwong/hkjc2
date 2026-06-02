"""Debug login: captures ALL ForgeRock responses to diagnose OTP rejection.
Usage: python scripts/_login_debug.py
Write OTP to C:\tmp\hkjc_otp.txt when prompted.
"""
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

        # Check already authenticated
        try:
            resp = await page.request.post(
                "https://txn01.hkjc.com/BetslipIB/services/SSOService.svc/DoCheckSSOSignInStatusTR",
                headers={"Content-Type": "application/json"},
                data='{"knownWebID":"","knownSSOGUID":"","isNew":true}')
            sso = await resp.text()
            if "SSO_SIGN_IN" in sso and "NOT_SIGN_IN" not in sso:
                print("ALREADY_AUTHENTICATED", flush=True)
                await context.close()
                return
            print(f"INITIAL_SSO: {sso[:200]}", flush=True)
        except Exception as e:
            print(f"INITIAL_SSO_ERROR: {e}", flush=True)

        # Capture ALL ForgeRock responses
        fr_log = []

        async def on_response(response):
            if "auth.ark.hkjc.com" in response.url and "authenticate" in response.url:
                try:
                    body = await response.text()
                    data = json.loads(body)
                    entry = {
                        "url": response.url[-80:],
                        "status": response.status,
                        "stage": data.get("stage", "?"),
                        "authId": (data.get("authId", "") or "")[:40],
                        "tokenId": bool(data.get("tokenId")),
                        "code": data.get("code"),
                        "message": (data.get("message", "") or "")[:200],
                        "callbacks": [],
                    }
                    for cb in data.get("callbacks", []):
                        cb_info = {"type": cb.get("type", "?")}
                        for o in cb.get("output", []):
                            v = str(o.get("value", ""))
                            if len(v) < 300:
                                cb_info.setdefault("outputs", []).append(v)
                            else:
                                cb_info.setdefault("outputs", []).append(v[:200] + "...")
                        for inp in cb.get("input", []):
                            cb_info.setdefault("inputs", []).append({
                                "name": inp.get("name", ""),
                                "value": str(inp.get("value", ""))[:100]
                            })
                        entry["callbacks"].append(cb_info)
                    fr_log.append(entry)
                    # Print summary
                    cbs = [c["type"] for c in entry["callbacks"]]
                    code = f" code={entry['code']}" if entry['code'] else ""
                    print(f"FR: stage={entry['stage']} token={entry['tokenId']} authId={entry['authId'][:15]}... cb={cbs}{code}", flush=True)
                except:
                    pass

        page.on("response", on_response)

        # Submit credentials
        print("=== PHASE 1: Submitting credentials ===", flush=True)
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
        otp_reached = False
        for i in range(30):
            await asyncio.sleep(1)
            for entry in fr_log:
                if entry["stage"] == "otp":
                    otp_reached = True
                    break
            if otp_reached:
                break

        if not otp_reached:
            print("ERROR: OTP stage not reached after 30s", flush=True)
            print(f"FR log ({len(fr_log)} entries):", flush=True)
            for e in fr_log:
                print(f"  {json.dumps(e, indent=2)}", flush=True)
            await context.close()
            return

        # Find prefix from FR log
        prefix = "?"
        for entry in fr_log:
            if entry["stage"] == "otp":
                for cb in entry["callbacks"]:
                    for o in cb.get("outputs", []):
                        if "prefixCode" in o:
                            try:
                                prefix = json.loads(o).get("prefixCode", "?")
                            except:
                                pass

        print(f"=== OTP SENT! Prefix: {prefix} ===", flush=True)
        print(f"Write code to {OTP_FILE}", flush=True)

        # Clear old OTP
        try:
            OTP_FILE.write_text("")
        except:
            pass

        # Fast-poll for OTP
        deadline = time.time() + 90
        while time.time() < deadline:
            try:
                otp = OTP_FILE.read_text().strip()
            except:
                otp = ""

            if len(otp) == 6 and otp.isdigit():
                elapsed = time.time() - (deadline - 90)
                print(f"=== GOT OTP: {otp} at {elapsed:.1f}s ===", flush=True)

                # Type OTP via real keyboard
                boxes = await page.query_selector_all("input.otp-input")
                print(f"OTP boxes found: {len(boxes)}", flush=True)

                if len(boxes) >= 4:
                    for i, digit in enumerate(otp[:len(boxes)]):
                        await boxes[i].click()
                        await page.keyboard.type(digit)
                        await asyncio.sleep(0.02)
                    print(f"Typed {min(len(otp), len(boxes))} digits", flush=True)
                    await asyncio.sleep(0.5)
                    await page.keyboard.press("Enter")

                    # Wait for ForgeRock response
                    await asyncio.sleep(5)

                    # Print all FR responses since OTP submission
                    print(f"\n=== FORGEROCK FLOW ({len(fr_log)} steps) ===", flush=True)
                    for i, entry in enumerate(fr_log):
                        print(f"\n--- Step {i+1}: {entry['stage']} ---", flush=True)
                        print(f"  code: {entry['code']}", flush=True)
                        print(f"  message: {entry['message']}", flush=True)
                        print(f"  tokenId: {entry['tokenId']}", flush=True)
                        print(f"  authId: {entry['authId']}", flush=True)
                        for j, cb in enumerate(entry["callbacks"]):
                            print(f"  callback[{j}]: {cb['type']}", flush=True)
                            for o in cb.get("outputs", []):
                                print(f"    output: {o[:200]}", flush=True)
                            for inp in cb.get("inputs", []):
                                print(f"    input: {inp}", flush=True)

                    # Check SSO
                    print("\n=== SSO CHECK ===", flush=True)
                    try:
                        resp = await page.request.post(
                            "https://txn01.hkjc.com/BetslipIB/services/SSOService.svc/DoCheckSSOSignInStatusTR",
                            headers={"Content-Type": "application/json"},
                            data='{"knownWebID":"","knownSSOGUID":"","isNew":true}')
                        sso = await resp.text()
                        print(f"SSO: {sso}", flush=True)
                        if "SSO_SIGN_IN" in sso and "NOT_SIGN_IN" not in sso:
                            print("\n*** LOGIN SUCCESS! ***", flush=True)
                        else:
                            print("\n*** LOGIN FAILED ***", flush=True)
                    except Exception as e:
                        print(f"SSO error: {e}", flush=True)

                    await context.close()
                    return
                else:
                    print(f"ERROR: Only {len(boxes)} OTP boxes", flush=True)
                    await context.close()
                    return

            await asyncio.sleep(0.3)

        print("TIMEOUT: No OTP received", flush=True)
        await context.close()

if __name__ == "__main__":
    asyncio.run(main())
