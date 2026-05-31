"""Try SSOCookie auth method to bypass OTP."""
import asyncio, sys, os, json
from pathlib import Path
sys.path.insert(0, '/opt/hkjc2')
from dotenv import load_dotenv
from playwright.async_api import async_playwright

load_dotenv('/opt/hkjc2/.env')

AUTH_URL = 'https://auth.ark.hkjc.com/am/json/realms/root/realms/customer/authenticate'
AUTH_PARAMS = '?authIndexType=service&authIndexValue=hkjcHSLogin-Web'


async def main():
    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            './data/browser_session_odds', headless=True,
            viewport={'width': 1280, 'height': 800})
        page = context.pages[0] if context.pages else await context.new_page()
        await page.set_extra_http_headers({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36'
        })

        # Check if already authenticated
        try:
            resp = await page.request.post(
                'https://txn01.hkjc.com/BetslipIB/services/SSOService.svc/DoCheckSSOSignInStatusTR',
                headers={'Content-Type': 'application/json'},
                data='{"knownWebID":"","knownSSOGUID":"","isNew":true}')
            if 'SSO_SIGN_IN' in await resp.text() and 'NOT_SIGN_IN' not in await resp.text():
                print('Already authenticated!', flush=True)
                return
        except:
            pass

        await page.goto('https://bet.hkjc.com/en/racing', wait_until='domcontentloaded', timeout=30000)
        await asyncio.sleep(2)

        account = os.getenv("HKJC_ACCOUNT", "")
        password = os.getenv("HKJC_PASSWORD", "")

        # Try SSOCookie auth via browser fetch
        print('Trying SSOCookie auth...', flush=True)

        result = await page.evaluate(f"""
            async () => {{
                const h = {{'Content-Type': 'application/json',
                           'Accept-API-Version': 'resource=2.0, protocol=1.0',
                           'X-Requested-With': 'XMLHttpRequest'}};

                // Step 1: Init auth
                const r1 = await fetch('{AUTH_URL}{AUTH_PARAMS}',
                    {{method:'POST', headers:h, body:'{{}}', credentials:'include'}});
                const d1 = await r1.json();
                console.log('Step 1:', JSON.stringify({{stage: d1.stage, cb: (d1.callbacks||[]).map(c=>c.type)}}));

                if (!d1.authId) return {{error: 'No authId in step 1'}};

                // Step 2: Choose SSOCookie (choice 0) instead of namepass (choice 1)
                const r2 = await fetch('{AUTH_URL}{AUTH_PARAMS}', {{
                    method:'POST', headers:h, credentials:'include',
                    body: JSON.stringify({{
                        authId: d1.authId,
                        callbacks: [
                            {{ type: 'ChoiceCallback',
                               output: [
                                   {{ name: 'prompt', value: 'Please input your choice' }},
                                   {{ name: 'choices', value: ['SSOCookie', 'namepass', 'ArkleLSToken'] }},
                                   {{ name: 'defaultChoice', value: 1 }}
                               ],
                               input: [{{ name: 'IDToken1', value: 0 }}] }}
                        ]
                    }})
                }});
                const d2 = await r2.json();
                console.log('Step 2:', JSON.stringify({{stage: d2.stage, token: !!d2.tokenId, cb: (d2.callbacks||[]).map(c=>c.type)}}));

                // If SSOCookie didn't work, try namepass with credentials
                let result = {{step1: d1.stage, step2: d2.stage || '?', token: !!d2.tokenId}};

                if (!d2.tokenId && d2.authId) {{
                    // Try namepass
                    const r3 = await fetch('{AUTH_URL}{AUTH_PARAMS}', {{
                        method:'POST', headers:h, credentials:'include',
                        body: JSON.stringify({{
                            authId: d2.authId,
                            callbacks: [
                                {{ type: 'NameCallback',
                                   output: [{{name:'prompt',value:'User Name:'}}],
                                   input: [{{name:'IDToken1',value:'{account}'}}] }},
                                {{ type: 'PasswordCallback',
                                   output: [{{name:'prompt',value:'Password:'}}],
                                   input: [{{name:'IDToken2',value:'{password}'}}] }}
                            ]
                        }})
                    }});
                    const d3 = await r3.json();
                    console.log('Step 3:', JSON.stringify({{stage: d3.stage, token: !!d3.tokenId}}));
                    result.step3 = d3.stage || '?';
                    result.token3 = !!d3.tokenId;
                    if (d3.tokenId) result.tokenId = d3.tokenId.slice(0, 50);
                }}

                return result;
            }}
        """)

        print(f'Result: {json.dumps(result, indent=2)}', flush=True)

        if result.get('token') or result.get('token3'):
            print('\n*** LOGIN SUCCESS via SSOCookie path! ***', flush=True)
        else:
            print(f'\nSSOCookie: {result.get("step2")} -> namepass: {result.get("step3","n/a")}', flush=True)

        await context.close()


if __name__ == "__main__":
    asyncio.run(main())
