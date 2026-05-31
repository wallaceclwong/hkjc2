"""Use page.route() to change auth choice from namepass to SSOCookie."""
import asyncio, sys, os, json
from pathlib import Path
sys.path.insert(0, '/opt/hkjc2')
from dotenv import load_dotenv
from playwright.async_api import async_playwright

load_dotenv('/opt/hkjc2/.env')


async def main():
    account = os.getenv("HKJC_ACCOUNT", "")
    password = os.getenv("HKJC_PASSWORD", "")

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            './data/browser_session_odds', headless=True,
            viewport={'width': 1280, 'height': 800})
        page = context.pages[0] if context.pages else await context.new_page()
        await page.set_extra_http_headers({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36'
        })

        # Track full ForgeRock flow
        fr_responses = []
        choose_arkle = True
        submit_account = True

        async def on_response(response):
            if "auth.ark.hkjc.com" in response.url:
                try:
                    d = json.loads(await response.text())
                    stage = d.get("stage", "?")
                    token = bool(d.get("tokenId"))
                    cbs = [c["type"] for c in d.get("callbacks", [])]
                    fr_responses.append({"stage": stage, "token": token, "cb": cbs, "full": d})
                    if token:
                        print(f"TOKEN: {d.get('tokenId','')[:60]}", flush=True)
                except:
                    pass

        page.on("response", on_response)

        # Intercept ForgeRock requests
        async def handle_route(route):
            nonlocal choose_arkle, submit_account
            url = route.request.url
            if "auth.ark.hkjc.com" in url and "authenticate" in url:
                post_data = route.request.post_data
                if post_data:
                    try:
                        body = json.loads(post_data)
                        callbacks = body.get("callbacks", [])

                        # Step A: Change ChoiceCallback to ArkleLSToken (value 2)
                        if choose_arkle:
                            for cb in callbacks:
                                if cb.get("type") == "ChoiceCallback":
                                    cb["input"][0]["value"] = 2
                                    choose_arkle = False
                                    print("[ROUTE] Choice -> ArkleLSToken (2)", flush=True)
                                    await route.continue_(post_data=json.dumps(body))
                                    return

                        # Step B: Handle NameCallback on arklelstoken stage
                        if submit_account:
                            for cb in callbacks:
                                if cb.get("type") == "NameCallback":
                                    cb["input"][0]["value"] = account
                                    submit_account = False
                                    print(f"[ROUTE] ArkleLSToken NameCallback -> {account}", flush=True)
                                    await route.continue_(post_data=json.dumps(body))
                                    return

                    except Exception as e:
                        print(f"[ROUTE] Error: {e}", flush=True)

            await route.continue_()

        await page.route("**/*", handle_route)

        # Use the SPA login UI
        await page.goto("https://bet.hkjc.com/en/racing/login",
                        wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(3)

        print("Filling credentials via SPA...", flush=True)
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
        await asyncio.sleep(8)

        print(f"\nForgeRock flow:", flush=True)
        for r in fr_responses:
            print(f"  stage={r['stage']} token={r['token']} cb={r['cb']}", flush=True)
            # Show prompt values for debugging
            for cb in r.get("full", {}).get("callbacks", []):
                for o in cb.get("output", []):
                    v = str(o.get("value", ""))
                    if v and len(v) < 200:
                        print(f"    output: {v}", flush=True)

        # Check SSO
        try:
            resp = await page.request.post(
                "https://txn01.hkjc.com/BetslipIB/services/SSOService.svc/DoCheckSSOSignInStatusTR",
                headers={"Content-Type": "application/json"},
                data='{"knownWebID":"","knownSSOGUID":"","isNew":true}')
            sso = await resp.text()
            if "SSO_SIGN_IN" in sso and "NOT_SIGN_IN" not in sso:
                print("\n*** LOGIN SUCCESS! ***", flush=True)
            else:
                print(f"\nSSO: {sso[:200]}", flush=True)
        except Exception as e:
            print(f"SSO error: {e}", flush=True)

        await context.close()


if __name__ == "__main__":
    asyncio.run(main())
