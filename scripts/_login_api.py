"""Login via ForgeRock API called from within browser context (page.evaluate).
This bypasses the SPA UI and submits callbacks directly to the API."""
import asyncio, sys, os, json, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dotenv import load_dotenv
from playwright.async_api import async_playwright

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(ENV_PATH)

AUTH_URL = "https://auth.ark.hkjc.com/am/json/realms/root/realms/customer/authenticate"
AUTH_PARAMS = "?authIndexType=service&authIndexValue=hkjcHSLogin-Web"


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
                print("Already authenticated!", flush=True)
                return
        except:
            pass

        # Get session cookies by visiting bet.hkjc.com
        print("Getting session...", flush=True)
        await page.goto("https://bet.hkjc.com/en/racing", wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(2)

        # Step 1: Init auth via page.evaluate (browser JS context)
        print("Starting ForgeRock auth...", flush=True)
        result = await page.evaluate(f"""
            async () => {{
                const headers = {{
                    'Content-Type': 'application/json',
                    'Accept-API-Version': 'resource=2.0, protocol=1.0',
                    'X-Requested-With': 'XMLHttpRequest',
                }};

                // Init auth
                const r1 = await fetch('{AUTH_URL}{AUTH_PARAMS}', {{ method: 'POST', headers, body: '{{}}', credentials: 'include' }});
                const d1 = await r1.json();
                if (!d1.authId) return {{ error: 'No authId', status: r1.status, body: JSON.stringify(d1).slice(0, 500) }};

                // Submit credentials
                const r2 = await fetch('{AUTH_URL}{AUTH_PARAMS}', {{
                    method: 'POST', headers, credentials: 'include',
                    body: JSON.stringify({{
                        authId: d1.authId,
                        callbacks: [
                            {{ type: 'NameCallback', output: [{{ name: 'prompt', value: 'User Name:' }}],
                               input: [{{ name: 'IDToken1', value: '{account}' }}] }},
                            {{ type: 'PasswordCallback', output: [{{ name: 'prompt', value: 'Password:' }}],
                               input: [{{ name: 'IDToken2', value: '{password}' }}] }}
                        ]
                    }})
                }});
                const d2 = await r2.json();
                return {{
                    status: r2.status,
                    stage: d2.stage || '?',
                    tokenId: d2.tokenId ? d2.tokenId.slice(0, 80) : null,
                    authId: (d2.authId || '').slice(0, 80),
                    callbacks: (d2.callbacks || []).map(c => c.type),
                    prefix: (() => {{
                        for (const cb of (d2.callbacks || [])) {{
                            for (const o of (cb.output || [])) {{
                                const v = (o.value || '').toString();
                                if (v.includes('prefixCode')) return JSON.parse(v).prefixCode;
                            }}
                        }}
                        return '';
                    }})()
                }};
            }}
        """)

        print(f"Result: {json.dumps(result, indent=2)}", flush=True)

        if result.get("tokenId"):
            print("\n*** LOGIN SUCCESS! ***", flush=True)
            return

        stage = result.get("stage", "")
        prefix = result.get("prefix", "")
        auth_id = result.get("authId", "")

        if stage == "otp" and prefix:
            print(f"\n*** OTP SENT! Prefix: {prefix} ***", flush=True)
            print(f"*** Update .env: HKJC_OTP=______ (within 90s) ***\n", flush=True)

            # Poll .env for OTP
            load_dotenv(ENV_PATH)
            last_otp = os.getenv("HKJC_OTP", "")
            deadline = time.time() + 90

            while time.time() < deadline:
                load_dotenv(ENV_PATH, override=True)
                otp = os.getenv("HKJC_OTP", "")
                if otp != last_otp and len(otp) == 6 and otp.isdigit():
                    elapsed = int(time.time() - (deadline - 90))
                    print(f"[{elapsed}s] Submitting OTP: {otp}", flush=True)

                    # Submit OTP via API
                    result2 = await page.evaluate(f"""
                        async () => {{
                            const headers = {{
                                'Content-Type': 'application/json',
                                'Accept-API-Version': 'resource=2.0, protocol=1.0',
                                'X-Requested-With': 'XMLHttpRequest',
                            }};
                            const r = await fetch('{AUTH_URL}{AUTH_PARAMS}', {{
                                method: 'POST', headers, credentials: 'include',
                                body: JSON.stringify({{
                                    authId: '{auth_id}',
                                    callbacks: [
                                        {{ type: 'TextInputCallback',
                                           output: [{{ name: 'prompt', value: 'sms_input' }}, {{ name: 'defaultText', value: '' }}],
                                           input: [{{ name: 'IDToken2', value: '{otp}' }}] }},
                                        {{ type: 'ConfirmationCallback',
                                           output: [{{ name: 'prompt', value: 'sms_confirm' }}, {{ name: 'messageType', value: 0 }},
                                                    {{ name: 'options', value: ['next', 'resend', 'resendVOICE'] }},
                                                    {{ name: 'optionType', value: -1 }}, {{ name: 'defaultOption', value: 0 }}],
                                           input: [{{ name: 'IDToken3', value: 0 }}] }}
                                    ]
                                }})
                            }});
                            const d = await r.json();
                            return {{
                                status: r.status,
                                tokenId: d.tokenId ? d.tokenId.slice(0, 80) : null,
                                stage: d.stage || '?',
                                error: (() => {{
                                    for (const cb of (d.callbacks || [])) {{
                                        for (const o of (cb.output || [])) {{
                                            const v = (o.value || '').toString();
                                            if (v.includes('messageId')) return JSON.parse(v).messageId;
                                        }}
                                    }}
                                    return '';
                                }})()
                            }};
                        }}
                    """)

                    print(f"  Result: {json.dumps(result2)}", flush=True)

                    if result2.get("tokenId"):
                        print("\n*** LOGIN SUCCESS! ***", flush=True)
                        return
                    elif result2.get("error"):
                        print(f"  Error code: {result2['error']}", flush=True)

                    last_otp = otp
                await asyncio.sleep(2)

            print("Timeout", flush=True)
        else:
            print(f"Unexpected stage: {stage}", flush=True)

        await context.close()


if __name__ == "__main__":
    asyncio.run(main())
