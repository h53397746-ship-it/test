import os
import asyncio
import json
import random
import re
import time
import base64
from datetime import datetime
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout
from flask import Flask, request, jsonify
from dotenv import load_dotenv
from collections import deque

# Load environment variables
load_dotenv()

# Configuration
CONFIG = {
    "RUN_LOCAL": os.getenv('RUN_LOCAL', 'false').lower() == 'true',
    "BROWSERLESS_API_KEY": os.getenv('BROWSERLESS_API_KEY'),
    "RESPONSE_TIMEOUT_SECONDS": 60,
    "RETRY_DELAY": 2000,
    "RATE_LIMIT_REQUESTS": 5,
    "RATE_LIMIT_WINDOW": 60,
    "MAX_RETRIES": 3,
    "RETRY_BACKOFF": 5,
    "DEBUGGER_VERSION": "1.3",
    "DETAILED_LOGGING": True
}

app = Flask(__name__)

# Rate limiting storage
request_timestamps = deque(maxlen=CONFIG["RATE_LIMIT_REQUESTS"])

# === RATE LIMITING ===
def rate_limit_check():
    """Check if we're within rate limits"""
    now = time.time()
    while request_timestamps and request_timestamps[0] < now - CONFIG["RATE_LIMIT_WINDOW"]:
        request_timestamps.popleft()
    
    if len(request_timestamps) >= CONFIG["RATE_LIMIT_REQUESTS"]:
        oldest = request_timestamps[0]
        wait_time = CONFIG["RATE_LIMIT_WINDOW"] - (now - oldest)
        return False, wait_time
    
    request_timestamps.append(now)
    return True, 0

# === CARD UTILITIES ===
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
    if first_two in ['34', '37']:
        return 15
    return 16

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
        if char.lower() == 'x':
            result += random_digit()
        else:
            result += char
    
    while len(result) < card_length - 1:
        result += random_digit()
    
    result = result[:card_length - 1]
    return complete_luhn(result) or result + '0'

def process_card_with_placeholders(number, month, year, cvv):
    if 'x' in number.lower():
        processed_number = generate_card_from_pattern(number)
    else:
        processed_number = number
    
    if 'x' in month.lower():
        processed_month = str(random.randint(1, 12)).zfill(2)
    else:
        processed_month = month.zfill(2)
    
    current_year = datetime.now().year
    if 'x' in year.lower():
        processed_year = str(random.randint(current_year + 1, current_year + 6))
    elif len(year) == 2:
        processed_year = '20' + year
    else:
        processed_year = year
    
    cvv_length = get_cvv_length(processed_number)
    if 'x' in cvv.lower():
        processed_cvv = ''.join([random_digit() for _ in range(cvv_length)])
    else:
        processed_cvv = cvv
    
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
    
    number, month, year, cvv = parts
    return process_card_with_placeholders(number, month, year, cvv)

def generate_random_name():
    """Generate random name like JS extension"""
    first_names = ['Alex', 'Jordan', 'Taylor', 'Morgan', 'Casey', 'Riley', 'Avery', 'Quinn', 'Sage', 'Parker', 
                   'Sam', 'Jamie', 'Drew', 'Blake', 'Charlie', 'Skylar', 'Robin', 'Ashley', 'Leslie', 'Tracy']
    last_names = ['Smith', 'Johnson', 'Williams', 'Brown', 'Jones', 'Garcia', 'Miller', 'Davis', 'Rodriguez', 'Martinez',
                  'Anderson', 'Taylor', 'Thomas', 'Moore', 'Jackson', 'Martin', 'Lee', 'Thompson', 'White', 'Harris']
    return f"{random.choice(first_names)} {random.choice(last_names)}"

# === RESPONSE ANALYZER WITH DETAILED LOGGING ===
class StripeResponseAnalyzer:
    """Analyzes Stripe API responses with detailed logging"""
    
    @staticmethod
    def is_stripe_endpoint(url):
        """Check if URL is a Stripe endpoint we care about"""
        stripe_domains = ['stripe.com', 'stripe.network']
        return any(domain in url.lower() for domain in stripe_domains)
    
    @staticmethod
    def analyze_response(url, status, body_text, result_dict):
        """
        Analyze response with detailed logging
        Modifies result_dict in place
        """
        print("\n" + "="*80)
        print(f"[RESPONSE LOG] {datetime.now().strftime('%H:%M:%S.%f')[:-3]}")
        print(f"[URL] {url}")
        print(f"[STATUS] {status}")
        
        # Try to parse JSON
        try:
            data = json.loads(body_text)
            
            if CONFIG["DETAILED_LOGGING"]:
                print("[RAW RESPONSE]")
                print(json.dumps(data, indent=2)[:2000])
                
        except json.JSONDecodeError:
            print(f"[RAW TEXT] {body_text[:500]}")
            print("="*80)
            return
        
        print("="*80)
        
        # Store raw response with full details
        result_dict["raw_responses"].append({
            "url": url,
            "status": status,
            "data": data,
            "timestamp": datetime.now().isoformat(),
            "raw_text": body_text if CONFIG["DETAILED_LOGGING"] else None
        })
        
        # Check for success_url
        if data.get('success_url'):
            result_dict["success_url"] = data['success_url']
            print(f"[SUCCESS URL FOUND] {data['success_url']}")
        
        # Check for payment success
        is_payment_success = (
            data.get('status', '').lower() in ['succeeded', 'success', 'requires_capture', 'processing'] or
            data.get('payment_intent', {}).get('status', '').lower() in ['succeeded', 'success', 'requires_capture', 'processing'] or
            data.get('payment_status') == 'paid' or
            data.get('outcome', {}).get('type') == 'authorized'
        )
        
        if is_payment_success:
            result_dict["payment_confirmed"] = True
            result_dict["success"] = True
            result_dict["message"] = f"Payment {data.get('status', 'succeeded')}"
            result_dict["payment_intent_id"] = data.get('id') or data.get('payment_intent', {}).get('id')
            print(f"[✓ PAYMENT CONFIRMED] Status: {data.get('status')}")
            return
        
        # Check for token creation
        if '/v1/tokens' in url.lower() and status == 200 and data.get('id', '').startswith('tok_'):
            result_dict["token_created"] = True
            result_dict["token_id"] = data.get('id')
            print(f"[✓ TOKEN CREATED] {data.get('id')}")
        
        # Check for payment method creation
        if '/v1/payment_methods' in url.lower() and status == 200:
            result_dict["payment_method_created"] = True
            result_dict["payment_method_id"] = data.get('id')
            print(f"[✓ PAYMENT METHOD] {data.get('id')}")
        
        # Check for payment intent
        if 'payment_intent' in url.lower() and status == 200:
            result_dict["payment_intent_created"] = True
            result_dict["payment_intent_id"] = data.get('id')
            result_dict["client_secret"] = data.get('client_secret')
            print(f"[✓ PAYMENT INTENT] {data.get('id')}")
        
        # Check for errors
        if 'error' in data or data.get('payment_intent', {}).get('last_payment_error'):
            error = data.get('error') or data.get('payment_intent', {}).get('last_payment_error')
            decline_code = error.get('decline_code') or error.get('code') or "unknown"
            error_message = error.get('message') or "An error occurred during the transaction."
            
            result_dict["error"] = error_message
            result_dict["decline_code"] = decline_code
            result_dict["success"] = False
            print(f"[✗ ERROR] {decline_code}: {error_message}")
        
        # Check for 3DS
        if data.get('status') == 'requires_action' or data.get('payment_intent', {}).get('status') == 'requires_action':
            result_dict["requires_3ds"] = True
            print("[⚠ 3DS REQUIRED] Three-D Secure authentication needed")

# === ENHANCED HCAPTCHA HANDLER ===
async def handle_hcaptcha_advanced(page):
    """Advanced hCaptcha handling"""
    print("\n[HCAPTCHA] Checking for hCaptcha presence...")
    
    # Check if hCaptcha exists on page
    try:
        hcaptcha_exists = await page.evaluate('''
            () => {
                return document.querySelector('iframe[src*="hcaptcha.com"]') !== null ||
                       document.querySelector('[data-hcaptcha-widget-id]') !== null ||
                       document.querySelector('.h-captcha') !== null;
            }
        ''')
    except:
        return True
    
    if not hcaptcha_exists:
        print("[HCAPTCHA] No hCaptcha found on page")
        return True
    
    print("[HCAPTCHA] hCaptcha detected, attempting to solve...")
    
    async def simulate_click(frame, element):
        """Simulate realistic mouse clicks"""
        try:
            box = await element.bounding_box()
            if box:
                x = box['x'] + box['width'] / 2
                y = box['y'] + box['height'] / 2
                
                await page.mouse.move(x, y)
                await asyncio.sleep(random.uniform(0.1, 0.3))
                await page.mouse.down()
                await asyncio.sleep(random.uniform(0.05, 0.15))
                await page.mouse.up()
                return True
        except Exception as e:
            print(f"[HCAPTCHA] Click error: {e}")
        return False
    
    # Find and click checkbox
    widget_frame = None
    for frame in page.frames:
        if 'hcaptcha.com' in frame.url and 'checkbox' in frame.url:
            widget_frame = frame
            print("[HCAPTCHA] Found checkbox iframe")
            break
    
    if widget_frame:
        try:
            check_div = await widget_frame.query_selector('div.check')
            if check_div:
                display = await check_div.get_attribute('style')
                if 'display: block' in (display or ''):
                    print("[HCAPTCHA] Already solved!")
                    return True
            
            checkbox = await widget_frame.query_selector('#checkbox')
            if checkbox:
                await simulate_click(widget_frame, checkbox)
                print("[HCAPTCHA] Clicked checkbox")
                await asyncio.sleep(3)
                
                check_div = await widget_frame.query_selector('div.check')
                if check_div:
                    display = await check_div.get_attribute('style')
                    if 'display: block' in (display or ''):
                        print("[HCAPTCHA] Auto-solved!")
                        return True
        except Exception as e:
            print(f"[HCAPTCHA] Widget error: {e}")
    
    await asyncio.sleep(2)
    
    challenge_frame = None
    for frame in page.frames:
        if 'hcaptcha.com' in frame.url and 'challenge' in frame.url:
            challenge_frame = frame
            print("[HCAPTCHA] Challenge frame found")
            break
    
    if challenge_frame:
        try:
            await asyncio.sleep(2)
            
            submit_button = await challenge_frame.query_selector('.button-submit')
            if submit_button:
                await simulate_click(challenge_frame, submit_button)
                print("[HCAPTCHA] Submitted challenge")
                await asyncio.sleep(3)
                return True
                
        except Exception as e:
            print(f"[HCAPTCHA] Challenge error: {e}")
    
    return False

# === MAIN AUTOMATION FUNCTION ===
async def run_stripe_automation(url, cc_string, email=None):
    card = process_card_input(cc_string)
    if not card:
        return {"error": "Invalid card format. Use: number|month|year|cvv"}
    
    if not email:
        email = f"test{random.randint(1000,9999)}@example.com"
    
    random_name = generate_random_name()
    billing_details = {
        "name": random_name,
        "address_line1": "123 Main Street",
        "address_line2": "OK",
        "city": "Macao",
        "country": "MO",
        "state": "Macau",
        "postal_code": "999078"
    }
    
    print("\n" + "="*80)
    print(f"[AUTOMATION START] {datetime.now().strftime('%H:%M:%S')}")
    print(f"[TARGET URL] {url}")
    print(f"[CARD] {card['number']} | {card['month']}/{card['year']} | CVV: {card['cvv']}")
    print(f"[EMAIL] {email}")
    print(f"[NAME] {random_name}")
    print("="*80)
    
    stripe_result = {
        "status": "pending",
        "raw_responses": [],
        "payment_confirmed": False,
        "token_created": False,
        "payment_method_created": False,
        "payment_intent_created": False,
        "success_url": None,
        "requires_3ds": False,
        "all_requests": []
    }
    
    analyzer = StripeResponseAnalyzer()
    
    async with async_playwright() as p:
        # Initialize ALL variables before try block
        browser = None
        context = None
        page = None
        cdp = None  # FIX: Initialize cdp here
        
        try:
            # Connect to browser
            if CONFIG["RUN_LOCAL"]:
                print("[BROWSER] Launching local browser...")
                browser = await p.chromium.launch(
                    headless=False,
                    slow_mo=100,
                    args=[
                        '--disable-blink-features=AutomationControlled',
                        '--disable-features=IsolateOrigins,site-per-process',
                        '--disable-web-security',
                        '--window-size=1920,1080'
                    ]
                )
            else:
                print("[BROWSER] Connecting to Browserless...")
                browser_url = f"wss://production-sfo.browserless.io/chromium/playwright?token={CONFIG['BROWSERLESS_API_KEY']}"
                try:
                    browser = await p.chromium.connect(browser_url, timeout=30000)
                    print("[BROWSER] Connected to Browserless")
                except Exception as e:
                    if "429" in str(e):
                        print("[ERROR] Rate limit hit, using local browser...")
                        browser = await p.chromium.launch(headless=True)
                    else:
                        raise e
            
            # Create context
            context = await browser.new_context(
                viewport={'width': 1920, 'height': 1080},
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36',
                locale='en-US',
                timezone_id='America/New_York'
            )
            
            page = await context.new_page()
            
            # Try to enable CDP session (optional, won't crash if fails)
            try:
                cdp = await page.context.new_cdp_session(page)
                await cdp.send('Network.enable')
                await cdp.send('Fetch.enable', {
                    'patterns': [
                        {'urlPattern': '*stripe.com*'},
                        {'urlPattern': '*stripe.network*'}
                    ]
                })
                print("[CDP] Network monitoring enabled")
            except Exception as e:
                print(f"[CDP] Warning: Could not enable CDP: {e}")
                print("[CDP] Continuing without CDP monitoring...")
                cdp = None  # Ensure it stays None if failed
            
            # Enhanced response capture
            async def capture_response(response):
                try:
                    url_lower = response.url.lower()
                    
                    # Log ALL network activity
                    if CONFIG["DETAILED_LOGGING"]:
                        stripe_result["all_requests"].append({
                            "type": "response",
                            "url": response.url,
                            "status": response.status,
                            "timestamp": datetime.now().isoformat()
                        })
                    
                    # Only process Stripe endpoints
                    if not analyzer.is_stripe_endpoint(response.url):
                        return
                    
                    print(f"\n[CAPTURED RESPONSE] {response.url[:100]}...")
                    print(f"[STATUS CODE] {response.status}")
                    
                    content_type = response.headers.get('content-type', '')
                    
                    if 'application/json' in content_type or 'text/' in content_type:
                        try:
                            body = await response.body()
                            text = body.decode('utf-8', errors='ignore')
                            
                            if CONFIG["DETAILED_LOGGING"]:
                                print(f"[RAW BODY LENGTH] {len(text)} bytes")
                            
                            analyzer.analyze_response(
                                response.url,
                                response.status,
                                text,
                                stripe_result
                            )
                            
                        except Exception as e:
                            print(f"[ERROR] Could not read body: {e}")
                            
                except Exception as e:
                    print(f"[ERROR] Response capture failed: {e}")
            
            # Enhanced request capture
            async def capture_request(request):
                url_lower = request.url.lower()
                
                # Log ALL requests
                if CONFIG["DETAILED_LOGGING"]:
                    stripe_result["all_requests"].append({
                        "type": "request",
                        "method": request.method,
                        "url": request.url,
                        "timestamp": datetime.now().isoformat()
                    })
                
                # Detailed logging for Stripe requests
                if 'stripe.com' in url_lower or 'stripe.network' in url_lower:
                    print(f"\n[REQUEST] {request.method} {request.url[:100]}...")
                    
                    try:
                        if request.method == "POST":
                            post_data = request.post_data
                            if post_data:
                                print(f"[POST DATA PREVIEW] {post_data[:200]}...")
                                
                                if 'card[number]' in post_data or 'card%5Bnumber%5D' in post_data:
                                    print("[DETECTED] Card submission in request")
                                
                                if 'payment_method' in post_data:
                                    print("[DETECTED] Payment method in request")
                    except Exception as e:
                        print(f"[ERROR] Could not read request data: {e}")
            
            # Attach event handlers
            page.on("response", capture_response)
            page.on("request", capture_request)
            
            # Navigate to page
            print(f"\n[NAVIGATION] Loading: {url}")
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            print("[NAVIGATION] Page loaded")
            
            # Wait for initial load
            await page.wait_for_timeout(3000)
            
            # Take initial screenshot
            if CONFIG["RUN_LOCAL"]:
                await page.screenshot(path="01_initial.png")
                print("[SCREENSHOT] Initial page captured")
            
            # === FILL EMAIL ===
            print("\n[FORM] Starting form filling...")
            email_filled = False
            
            email_selectors = [
                'input[type="email"]',
                'input[name="email"]',
                '#email',
                'input[placeholder*="email" i]',
                'input[autocomplete="email"]',
                'input[data-testid="email"]'
            ]
            
            for selector in email_selectors:
                try:
                    elements = await page.query_selector_all(selector)
                    for element in elements:
                        if await element.is_visible():
                            await element.click()
                            await element.fill("")
                            await element.type(email, delay=random.randint(50, 100))
                            await page.keyboard.press('Tab')
                            email_filled = True
                            print(f"[FILLED] Email: {email}")
                            break
                    if email_filled:
                        break
                except:
                    continue
            
            # === FILL BILLING NAME ===
            print("[FORM] Filling billing name...")
            name_selectors = [
                'input[name="billingName"]',
                'input[name="name"]',
                'input[autocomplete*="name"]',
                'input[placeholder*="name" i]'
            ]
            
            for selector in name_selectors:
                try:
                    elements = await page.query_selector_all(selector)
                    for element in elements:
                        if await element.is_visible():
                            await element.click()
                            await element.fill(billing_details["name"])
                            print(f"[FILLED] Name: {billing_details['name']}")
                            break
                except:
                    continue
            
            # === FILL ADDRESS FIELDS ===
            print("[FORM] Filling address fields...")
            address_fields = {
                'input[name="billingAddressLine1"], input[autocomplete*="address-line1"]': billing_details["address_line1"],
                'input[name="billingLocality"], input[autocomplete*="address-level2"]': billing_details["city"],
                'input[name="billingPostalCode"], input[autocomplete*="postal-code"]': billing_details["postal_code"]
            }
            
            for selector, value in address_fields.items():
                try:
                    element = await page.query_selector(selector)
                    if element and await element.is_visible():
                        await element.click()
                        await element.fill(value)
                        print(f"[FILLED] {value}")
                except:
                    continue
            
            # Wait for Stripe Elements
            await page.wait_for_timeout(2000)
            
            # === FILL CARD DETAILS IN STRIPE IFRAMES ===
            print("\n[STRIPE] Looking for Stripe card fields...")
            
            filled_status = {
                "card": False,
                "expiry": False,
                "cvc": False
            }
            
            # Get all frames
            frames = page.frames
            stripe_frames = [f for f in frames if any(x in f.url.lower() for x in ['stripe', 'js.stripe'])]
            
            print(f"[STRIPE] Found {len(stripe_frames)} Stripe frames")
            
            if CONFIG["DETAILED_LOGGING"]:
                for frame in stripe_frames:
                    print(f"  Frame URL: {frame.url[:100]}")
            
            # Fill each Stripe iframe
            for frame in stripe_frames:
                try:
                    # Card number
                    if not filled_status["card"]:
                        card_inputs = await frame.query_selector_all('input')
                        for inp in card_inputs:
                            try:
                                placeholder = await inp.get_attribute('placeholder') or ''
                                name = await inp.get_attribute('name') or ''
                                aria_label = await inp.get_attribute('aria-label') or ''
                                
                                is_card_field = (
                                    'card number' in placeholder.lower() or
                                    '1234' in placeholder.lower() or
                                    'cardnumber' in name.lower() or
                                    'card number' in aria_label.lower()
                                )
                                
                                if is_card_field:
                                    await inp.click()
                                    for digit in card['number']:
                                        await inp.type(digit, delay=random.randint(50, 120))
                                    filled_status["card"] = True
                                    print(f"[FILLED] Card number: {card['number']}")
                                    await page.keyboard.press('Tab')
                                    break
                            except:
                                continue
                    
                    # Expiry
                    if not filled_status["expiry"]:
                        exp_inputs = await frame.query_selector_all('input')
                        for inp in exp_inputs:
                            try:
                                placeholder = await inp.get_attribute('placeholder') or ''
                                name = await inp.get_attribute('name') or ''
                                
                                is_expiry_field = (
                                    'mm' in placeholder.lower() or
                                    'expir' in placeholder.lower() or
                                    'expir' in name.lower()
                                )
                                
                                if is_expiry_field:
                                    await inp.click()
                                    exp_string = f"{card['month']}{card['year'][-2:]}"
                                    for char in exp_string:
                                        await inp.type(char, delay=random.randint(50, 120))
                                    filled_status["expiry"] = True
                                    print(f"[FILLED] Expiry: {card['month']}/{card['year'][-2:]}")
                                    await page.keyboard.press('Tab')
                                    break
                            except:
                                continue
                    
                    # CVC
                    if not filled_status["cvc"]:
                        cvc_inputs = await frame.query_selector_all('input')
                        for inp in cvc_inputs:
                            try:
                                placeholder = await inp.get_attribute('placeholder') or ''
                                name = await inp.get_attribute('name') or ''
                                
                                is_cvc_field = (
                                    'cvc' in placeholder.lower() or
                                    'cvv' in placeholder.lower() or
                                    'security' in placeholder.lower() or
                                    'cvc' in name.lower()
                                )
                                
                                if is_cvc_field:
                                    await inp.click()
                                    for digit in card['cvv']:
                                        await inp.type(digit, delay=random.randint(50, 120))
                                    filled_status["cvc"] = True
                                    print(f"[FILLED] CVC: {card['cvv']}")
                                    break
                            except:
                                continue
                        
                except Exception as e:
                    print(f"[ERROR] Frame filling error: {e}")
                    continue
            
            print(f"\n[STATUS] Form fill status: {filled_status}")
            
            if CONFIG["RUN_LOCAL"]:
                await page.screenshot(path="02_filled.png")
                print("[SCREENSHOT] Form filled")
            
            # Wait before handling hCaptcha
            await page.wait_for_timeout(2000)
            
            # === HANDLE HCAPTCHA ===
            hcaptcha_solved = await handle_hcaptcha_advanced(page)
            
            if CONFIG["RUN_LOCAL"]:
                await page.screenshot(path="03_after_captcha.png")
            
            # Wait after hCaptcha
            await page.wait_for_timeout(2000)
            
            # === SUBMIT PAYMENT ===
            print("\n[SUBMIT] Attempting payment submission...")
            submit_attempted = False
            
            submit_selectors = [
                'button.SubmitButton:visible',
                'button[type="submit"]:visible',
                'button:has-text("Pay"):visible',
                'button:has-text("Submit"):visible',
                'button:has-text("Complete"):visible',
                'input[type="submit"]:visible'
            ]
            
            for selector in submit_selectors:
                try:
                    btn = page.locator(selector).first
                    if await btn.count() > 0 and await btn.is_visible():
                        await btn.scroll_into_view_if_needed()
                        await btn.click()
                        submit_attempted = True
                        print(f"[SUBMIT] Clicked: {selector}")
                        break
                except:
                    continue
            
            if not submit_attempted:
                try:
                    await page.evaluate('''
                        const forms = document.querySelectorAll('form');
                        for (const form of forms) {
                            const submitBtn = form.querySelector('button[type="submit"], input[type="submit"]');
                            if (submitBtn) {
                                submitBtn.click();
                                break;
                            }
                        }
                    ''')
                    submit_attempted = True
                    print("[SUBMIT] Via JavaScript")
                except:
                    pass
            
            if not submit_attempted:
                await page.keyboard.press('Enter')
                submit_attempted = True
                print("[SUBMIT] Pressed Enter")
            
            if CONFIG["RUN_LOCAL"]:
                await page.screenshot(path="04_submitted.png")
            
            # === WAIT FOR RESPONSE ===
            print("\n[MONITORING] Waiting for payment response...")
            print("[MONITORING] Capturing all Stripe API activity...")
            
            start_time = time.time()
            retry_count = 0
            max_wait = CONFIG["RESPONSE_TIMEOUT_SECONDS"]
            
            while time.time() - start_time < max_wait:
                elapsed = time.time() - start_time
                
                # Status update every 5 seconds
                if int(elapsed) % 5 == 0 and int(elapsed) > 0:
                    print(f"[MONITORING] {int(elapsed)}s elapsed, captured {len(stripe_result['raw_responses'])} responses")
                
                # Check for payment confirmation
                if stripe_result.get("payment_confirmed"):
                    print("\n[✓ SUCCESS] Payment confirmed via API!")
                    break
                
                # Check for token
                if stripe_result.get("token_created"):
                    print("[INFO] Token created, waiting for payment intent...")
                
                # Check for error and retry
                if stripe_result.get("error") and not stripe_result.get("payment_confirmed"):
                    print(f"\n[✗ ERROR] {stripe_result['error']}")
                    
                    if retry_count < CONFIG["MAX_RETRIES"]:
                        retry_count += 1
                        print(f"[RETRY] Attempt {retry_count}/{CONFIG['MAX_RETRIES']}...")
                        
                        await asyncio.sleep(CONFIG["RETRY_DELAY"] / 1000)
                        
                        await handle_hcaptcha_advanced(page)
                        await asyncio.sleep(1)
                        
                        for selector in submit_selectors[:3]:
                            try:
                                btn = page.locator(selector).first
                                if await btn.count() > 0 and await btn.is_visible():
                                    await btn.click()
                                    print(f"[RETRY] Resubmitted")
                                    break
                            except:
                                continue
                        
                        stripe_result["error"] = None
                    else:
                        print("[RETRY] Max retries reached")
                        break
                
                # Check for 3DS
                if stripe_result.get("requires_3ds"):
                    print("[⚠ 3DS] Three-D Secure required")
                    break
                
                # Check URL for success
                current_url = page.url
                if stripe_result.get("success_url") and current_url.startswith(stripe_result["success_url"]):
                    print(f"[✓ SUCCESS] Redirected to success URL: {current_url}")
                    stripe_result["payment_confirmed"] = True
                    stripe_result["success"] = True
                    break
                
                if any(x in current_url.lower() for x in ['success', 'thank', 'confirm', 'complete']):
                    print(f"[INFO] Success page detected: {current_url}")
                    stripe_result["success_page"] = True
                
                await asyncio.sleep(0.5)
            
            # Final screenshot
            if CONFIG["RUN_LOCAL"]:
                await page.screenshot(path="05_final.png", full_page=True)
                print("[SCREENSHOT] Final page captured")
            
            # === PREPARE FINAL RESPONSE ===
            print("\n" + "="*80)
            print("[SUMMARY] Processing complete")
            print(f"[CAPTURED] {len(stripe_result['raw_responses'])} Stripe API responses")
            print(f"[TOTAL REQUESTS] {len(stripe_result['all_requests'])} total network requests")
            print("="*80)
            
            if stripe_result.get("payment_confirmed"):
                return {
                    "success": True,
                    "message": stripe_result.get("message", "Payment processed successfully"),
                    "card": f"{card['number']}|{card['month']}|{card['year']}|{card['cvv']}",
                    "token_id": stripe_result.get("token_id"),
                    "payment_intent_id": stripe_result.get("payment_intent_id"),
                    "payment_method_id": stripe_result.get("payment_method_id"),
                    "success_url": stripe_result.get("success_url"),
                    "raw_responses": stripe_result["raw_responses"],
                    "network_summary": {
                        "total_requests": len(stripe_result["all_requests"]),
                        "stripe_responses": len(stripe_result["raw_responses"])
                    }
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
                    "message": "Payment not confirmed - check raw responses",
                    "card": f"{card['number']}|{card['month']}|{card['year']}|{card['cvv']}",
                    "details": {
                        "email_filled": email_filled,
                        "card_filled": filled_status,
                        "hcaptcha_solved": hcaptcha_solved,
                        "submit_attempted": submit_attempted,
                        "responses_captured": len(stripe_result["raw_responses"]),
                        "total_network_requests": len(stripe_result["all_requests"])
                    },
                    "raw_responses": stripe_result["raw_responses"],
                    "all_requests": stripe_result["all_requests"][:20]
                }
                
        except Exception as e:
            print(f"\n[EXCEPTION] {str(e)}")
            import traceback
            traceback.print_exc()
            return {
                "error": "Automation failed",
                "details": str(e),
                "traceback": traceback.format_exc(),
                "raw_responses": stripe_result.get("raw_responses", [])
            }
            
        finally:
            # Clean up - cdp is guaranteed to be defined
            if cdp:
                try:
                    await cdp.detach()
                    print("[CLEANUP] CDP session detached")
                except:
                    pass
            if page:
                try:
                    await page.close()
                    print("[CLEANUP] Page closed")
                except:
                    pass
            if context:
                try:
                    await context.close()
                    print("[CLEANUP] Context closed")
                except:
                    pass
            if browser:
                try:
                    await browser.close()
                    print("[CLEANUP] Browser closed")
                except:
                    pass
            print("[CLEANUP] Session cleanup complete")

# === API ENDPOINTS ===
@app.route('/hrkXstripe', methods=['GET'])
def stripe_endpoint():
    """Main Stripe automation endpoint"""
    can_proceed, wait_time = rate_limit_check()
    if not can_proceed:
        return jsonify({
            "error": "Rate limit exceeded",
            "retry_after": f"{wait_time:.1f} seconds"
        }), 429
    
    url = request.args.get('url')
    cc = request.args.get('cc')
    email = request.args.get('email')
    
    print("\n" + "="*80)
    print(f"[NEW REQUEST] {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"[URL] {url}")
    print(f"[CC] {cc}")
    print(f"[EMAIL] {email or 'auto-generated'}")
    print("="*80)
    
    if not url or not cc:
        return jsonify({"error": "Missing required parameters: url and cc"}), 400
    
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    try:
        result = loop.run_until_complete(run_stripe_automation(url, cc, email))
        
        print("\n" + "="*80)
        print("[FINAL RESULT]")
        print(json.dumps(result, indent=2)[:1000])
        print("="*80 + "\n")
        
        if result.get('success'):
            return jsonify(result), 200
        else:
            return jsonify(result), 400
        
    except Exception as e:
        print(f"[SERVER ERROR] {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({
            "error": "Server error",
            "details": str(e),
            "traceback": traceback.format_exc()
        }), 500
    finally:
        loop.close()

@app.route('/status', methods=['GET'])
def status_endpoint():
    """Health check and status endpoint"""
    return jsonify({
        "status": "online",
        "version": "2.1-enhanced",
        "features": {
            "detailed_logging": CONFIG["DETAILED_LOGGING"],
            "cdp_monitoring": "optional",
            "hcaptcha_support": True,
            "auto_retry": True,
            "network_capture": True
        },
        "rate_limit": {
            "requests_made": len(request_timestamps),
            "requests_remaining": CONFIG["RATE_LIMIT_REQUESTS"] - len(request_timestamps)
        },
        "timestamp": datetime.now().isoformat()
    })

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5001))
    print("="*80)
    print(f"[SERVER] Starting Enhanced Stripe Automation Server")
    print(f"[VERSION] 2.1 with detailed logging and CDP error handling")
    print(f"[PORT] {port}")
    print(f"[FEATURES] Optional CDP monitoring, detailed request/response logging")
    print("="*80)
    app.run(host='0.0.0.0', port=port, debug=True)
