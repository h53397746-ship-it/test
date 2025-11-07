import os
import asyncio
import json
import random
import re
import time
from datetime import datetime
from urllib.parse import quote
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout
from playwright._impl._errors import TargetClosedError
from flask import Flask, request, jsonify
from dotenv import load_dotenv
from collections import deque

load_dotenv()

# ============================================================================
# CONFIGURATION
# ============================================================================

CONFIG = {
    "RUN_LOCAL": os.getenv('RUN_LOCAL', 'false').lower() == 'true',
    "BROWSERSTACK_USERNAME": os.getenv('BROWSERSTACK_USERNAME'),
    "BROWSERSTACK_ACCESS_KEY": os.getenv('BROWSERSTACK_ACCESS_KEY'),
    "BROWSERSTACK_TIMEOUT": int(os.getenv('BROWSERSTACK_TIMEOUT', '60000')),
    "RESPONSE_TIMEOUT_SECONDS": 50,
    "RETRY_DELAY": 2000,
    "RATE_LIMIT_REQUESTS": 5,
    "RATE_LIMIT_WINDOW": 60,
    "MAX_RETRIES": 2,
}

# Validate BrowserStack credentials
if not CONFIG["RUN_LOCAL"]:
    if not CONFIG["BROWSERSTACK_USERNAME"] or not CONFIG["BROWSERSTACK_ACCESS_KEY"]:
        print("⚠️  WARNING: BROWSERSTACK_USERNAME and BROWSERSTACK_ACCESS_KEY not set!")
        print("Set RUN_LOCAL=true to use local browser instead")

app = Flask(__name__)
request_timestamps = deque(maxlen=CONFIG["RATE_LIMIT_REQUESTS"])

# ============================================================================
# RATE LIMITING
# ============================================================================

def rate_limit_check():
    """Check if request is within rate limit"""
    now = time.time()
    while request_timestamps and request_timestamps[0] < now - CONFIG["RATE_LIMIT_WINDOW"]:
        request_timestamps.popleft()
    
    if len(request_timestamps) >= CONFIG["RATE_LIMIT_REQUESTS"]:
        oldest = request_timestamps[0]
        wait_time = CONFIG["RATE_LIMIT_WINDOW"] - (now - oldest)
        return False, wait_time
    
    request_timestamps.append(now)
    return True, 0

# ============================================================================
# CARD UTILITIES
# ============================================================================

def luhn_algorithm(number_str):
    """Validate card number using Luhn algorithm"""
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
    """Complete card number with valid Luhn checksum"""
    for d in range(10):
        candidate = base + str(d)
        if luhn_algorithm(candidate):
            return candidate
    return None

def get_card_length(bin_str):
    """Determine card length based on BIN"""
    first_two = bin_str[:2] if len(bin_str) >= 2 else ""
    return 15 if first_two in ['34', '37'] else 16

def get_cvv_length(card_number):
    """Determine CVV length based on card number"""
    return 4 if len(card_number) == 15 else 3

def random_digit():
    """Generate random digit"""
    return str(random.randint(0, 9))

def generate_card_from_pattern(pattern):
    """Generate card number from pattern with x placeholders"""
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
    """Process card details with placeholder handling"""
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
    """Parse card input string (format: number|month|year|cvv)"""
    parts = cc_string.split('|')
    if len(parts) != 4:
        return None
    return process_card_with_placeholders(*parts)

def generate_random_name():
    """Generate random cardholder name"""
    first_names = ['Alex', 'Jordan', 'Taylor', 'Morgan', 'Casey', 'Riley', 'Avery', 'Quinn', 'Sage', 'Parker']
    last_names = ['Smith', 'Johnson', 'Williams', 'Brown', 'Jones', 'Garcia', 'Miller', 'Davis', 'Rodriguez', 'Martinez']
    return f"{random.choice(first_names)} {random.choice(last_names)}"

# ============================================================================
# STRIPE RESPONSE ANALYZER
# ============================================================================

class StripeResponseAnalyzer:
    """Analyze Stripe API responses for payment status"""
    
    @staticmethod
    def is_stripe_endpoint(url):
        """Check if URL is a Stripe endpoint"""
        return any(domain in url.lower() for domain in ['stripe.com', 'stripe.network'])
    
    @staticmethod
    def is_payment_critical_endpoint(url):
        """Check for payment-critical endpoints"""
        critical = [
            '/v1/payment_intents',
            '/v1/payment_pages',
            '/v1/tokens',
            '/v1/payment_methods',
            '/confirm',
            '/v1/charges',
            '/v1/setup_intents'
        ]
        return any(endpoint in url.lower() for endpoint in critical)
    
    @staticmethod
    def analyze_response(url, status, body_text, result_dict):
        """Analyze API response and update result dictionary"""
        try:
            data = json.loads(body_text)
        except:
            return
        
        # Store ALL Stripe responses
        result_dict["raw_responses"].append({
            "url": url,
            "status": status,
            "data": data,
            "timestamp": datetime.now().isoformat()
        })
        
        # Log payment-critical endpoints
        if StripeResponseAnalyzer.is_payment_critical_endpoint(url):
            print(f"[PAYMENT API] {url[:60]}... [{status}]")
            print(f"[DATA] {json.dumps(data)[:300]}...")
        
        # Check for success_url
        if data.get('success_url'):
            result_dict["success_url"] = data['success_url']
            print(f"[SUCCESS URL] {data['success_url']}")
        
        # Check for payment confirmation - EXPANDED CHECKS
        is_payment_success = (
            data.get('status') in ['succeeded', 'success', 'requires_capture', 'processing', 'complete'] or
            data.get('payment_intent', {}).get('status') in ['succeeded', 'success', 'requires_capture', 'processing'] or
            data.get('payment_status') in ['paid', 'complete'] or
            data.get('outcome', {}).get('type') == 'authorized' or
            (data.get('status') == 'complete' and 'payment_intent' in data) or
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
        
        # Check for token
        if status == 200 and data.get('id', '').startswith('tok_'):
            result_dict["token_created"] = True
            result_dict["token_id"] = data.get('id')
            print(f"[TOKEN] {data.get('id')}")
        
        # Check for payment method
        if status == 200 and data.get('id', '').startswith('pm_'):
            result_dict["payment_method_created"] = True
            result_dict["payment_method_id"] = data.get('id')
            print(f"[PAYMENT METHOD] {data.get('id')}")
        
        # Check for payment intent (but not confirmed yet)
        if status == 200 and data.get('id', '').startswith('pi_'):
            result_dict["payment_intent_created"] = True
            result_dict["payment_intent_id"] = data.get('id')
            result_dict["client_secret"] = data.get('client_secret')
            print(f"[PAYMENT INTENT] {data.get('id')} - Status: {data.get('status')}")
        
        # Check for errors
        error = data.get('error') or data.get('payment_intent', {}).get('last_payment_error')
        if error:
            decline_code = error.get('decline_code') or error.get('code') or "unknown"
            error_message = error.get('message') or "Transaction error"
            result_dict["error"] = error_message
            result_dict["decline_code"] = decline_code
            result_dict["success"] = False
            print(f"[ERROR] {decline_code}: {error_message}")
        
        # Check for 3DS
        if data.get('status') == 'requires_action' or data.get('next_action'):
            result_dict["requires_3ds"] = True
            print("[3DS] Required")

# ============================================================================
# BROWSERSTACK CONNECTION
# ============================================================================

async def get_browserstack_browser(playwright):
    """
    Connect to BrowserStack using official CDP connection
    """
    print("[BROWSERSTACK] Initializing connection...")
    
    # Get Playwright version
    playwright_version = "1.40.0"  # Fallback version
    try:
        import playwright
        playwright_version = playwright.__version__
    except:
        pass
    
    # BrowserStack capabilities
    capabilities = {
        # Browser configuration
        'browser': 'chrome',
        'browser_version': 'latest',
        'os': 'Windows',
        'os_version': '10',
        
        # Session info
        'name': f'Stripe-{datetime.now().strftime("%H%M%S")}',
        'build': f'stripe-automation-{datetime.now().strftime("%Y%m%d")}',
        'project': 'Stripe Payment Automation',
        
        # BrowserStack credentials
        'browserstack.username': CONFIG['BROWSERSTACK_USERNAME'],
        'browserstack.accessKey': CONFIG['BROWSERSTACK_ACCESS_KEY'],
        
        # BrowserStack options
        'browserstack.local': 'false',
        'browserstack.networkLogs': 'true',
        'browserstack.console': 'verbose',
        'browserstack.debug': 'true',
        'browserstack.video': 'false',  # Disable video to save resources
        'browserstack.seleniumLogs': 'false',
        'browserstack.idleTimeout': '300',
        
        # Playwright version
        'client.playwrightVersion': playwright_version,
    }
    
    # Create CDP URL with encoded capabilities
    caps_json = json.dumps(capabilities)
    caps_encoded = quote(caps_json)
    cdp_url = f"wss://cdp.browserstack.com/playwright?caps={caps_encoded}"
    
    print(f"[BROWSERSTACK] Build: {capabilities['build']}")
    print(f"[BROWSERSTACK] Session: {capabilities['name']}")
    print(f"[BROWSERSTACK] Connecting...")
    
    try:
        browser = await playwright.chromium.connect(
            cdp_url,
            timeout=30000  # 30 second connection timeout
        )
        print("[BROWSERSTACK] ✓ Connected successfully")
        return browser
    except Exception as e:
        print(f"[BROWSERSTACK] ✗ Connection failed: {str(e)}")
        raise

# ============================================================================
# HELPER FUNCTIONS
# ============================================================================

async def safe_wait(page, ms):
    """Safely wait without throwing errors"""
    try:
        await page.wait_for_timeout(ms)
        return True
    except:
        return False

# ============================================================================
# MAIN AUTOMATION FUNCTION
# ============================================================================

async def run_stripe_automation(url, cc_string, email=None):
    """
    Main automation function to test Stripe payment
    """
    card = process_card_input(cc_string)
    if not card:
        return {"error": "Invalid card format. Expected: number|month|year|cvv"}
    
    email = email or f"test{random.randint(1000,9999)}@example.com"
    random_name = generate_random_name()
    
    print(f"\n{'='*80}")
    print(f"[START] {datetime.now().strftime('%H:%M:%S')}")
    print(f"[CARD] {card['number']} | {card['month']}/{card['year']} | {card['cvv']}")
    print(f"[EMAIL] {email}")
    print(f"[NAME] {random_name}")
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
    
    async with async_playwright() as p:
        browser = None
        context = None
        page = None
        payment_submitted = False
        
        try:
            # ============================================
            # BROWSER CONNECTION (LOCAL OR BROWSERSTACK)
            # ============================================
            
            if CONFIG["RUN_LOCAL"]:
                print("[BROWSER] Launching local Chromium...")
                browser = await p.chromium.launch(
                    headless=False,
                    slow_mo=100
                )
                print("[BROWSER] ✓ Local browser ready")
            else:
                browser = await get_browserstack_browser(p)
            
            # Create browser context
            context = await browser.new_context(
                viewport={'width': 1920, 'height': 1080},
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                locale='en-US',
                timezone_id='America/New_York'
            )
            
            page = await context.new_page()
            
            # ============================================
            # RESPONSE CAPTURE
            # ============================================
            
            async def capture_response(response):
                """Capture and analyze Stripe API responses"""
                try:
                    if not analyzer.is_stripe_endpoint(response.url):
                        return
                    
                    content_type = response.headers.get('content-type', '')
                    if 'application/json' in content_type:
                        try:
                            body = await response.body()
                            text = body.decode('utf-8', errors='ignore')
                            analyzer.analyze_response(response.url, response.status, text, stripe_result)
                        except Exception as e:
                            print(f"[WARN] Failed to parse response: {e}")
                except Exception as e:
                    print(f"[WARN] Response capture error: {e}")
            
            page.on("response", capture_response)
            
            # ============================================
            # NAVIGATION
            # ============================================
            
            print(f"[NAV] Loading {url[:60]}...")
            await page.goto(url, wait_until="domcontentloaded", timeout=40000)
            print("[NAV] ✓ Page loaded")
            
            await safe_wait(page, 3000)
            
            # ============================================
            # FILL EMAIL
            # ============================================
            
            print("[FILL] Email field...")
            email_filled = False
            try:
                for selector in ['input[type="email"]', 'input[name="email"]', '#email', '[placeholder*="email" i]']:
                    try:
                        elements = await page.query_selector_all(selector)
                        for element in elements:
                            if await element.is_visible():
                                await element.click()
                                await element.fill(email)
                                email_filled = True
                                print(f"[EMAIL] ✓ {email}")
                                break
                        if email_filled:
                            break
                    except:
                        continue
            except Exception as e:
                print(f"[EMAIL] ⚠ Could not fill: {e}")
            
            await safe_wait(page, 1000)
            
            # ============================================
            # FILL CARD DETAILS IN STRIPE IFRAMES
            # ============================================
            
            print("[FILL] Card details in Stripe iframes...")
            filled_status = {"card": False, "expiry": False, "cvc": False}
            
            frames = page.frames
            stripe_frames = [f for f in frames if 'stripe' in f.url.lower()]
            print(f"[FRAMES] Found {len(stripe_frames)} Stripe frames")
            
            for frame in stripe_frames:
                try:
                    # Card number
                    if not filled_status["card"]:
                        for inp in await frame.query_selector_all('input'):
                            try:
                                ph = (await inp.get_attribute('placeholder') or '').lower()
                                name = (await inp.get_attribute('name') or '').lower()
                                if 'card number' in ph or '1234' in ph or 'cardnumber' in name:
                                    await inp.click()
                                    await inp.fill('')  # Clear first
                                    for digit in card['number']:
                                        await inp.type(digit, delay=random.randint(50, 100))
                                    filled_status["card"] = True
                                    print(f"[CARD] ✓ {card['number']}")
                                    break
                            except:
                                continue
                    
                    # Expiry
                    if not filled_status["expiry"]:
                        for inp in await frame.query_selector_all('input'):
                            try:
                                ph = (await inp.get_attribute('placeholder') or '').lower()
                                name = (await inp.get_attribute('name') or '').lower()
                                if 'mm' in ph or 'expir' in ph or 'exp' in name:
                                    await inp.click()
                                    await inp.fill('')
                                    exp_string = f"{card['month']}{card['year'][-2:]}"
                                    for char in exp_string:
                                        await inp.type(char, delay=random.randint(50, 100))
                                    filled_status["expiry"] = True
                                    print(f"[EXPIRY] ✓ {card['month']}/{card['year'][-2:]}")
                                    break
                            except:
                                continue
                    
                    # CVC
                    if not filled_status["cvc"]:
                        for inp in await frame.query_selector_all('input'):
                            try:
                                ph = (await inp.get_attribute('placeholder') or '').lower()
                                name = (await inp.get_attribute('name') or '').lower()
                                if 'cvc' in ph or 'cvv' in ph or 'security' in ph or 'cvc' in name:
                                    await inp.click()
                                    await inp.fill('')
                                    for digit in card['cvv']:
                                        await inp.type(digit, delay=random.randint(50, 100))
                                    filled_status["cvc"] = True
                                    print(f"[CVC] ✓ {card['cvv']}")
                                    break
                            except:
                                continue
                except Exception as e:
                    print(f"[WARN] Frame processing error: {e}")
                    continue
            
            print(f"[FILL STATUS] Card: {filled_status['card']}, Expiry: {filled_status['expiry']}, CVC: {filled_status['cvc']}")
            
            # ============================================
            # SUBMIT PAYMENT
            # ============================================
            
            await safe_wait(page, 2000)
            
            print("[SUBMIT] Looking for submit button...")
            try:
                submit_selectors = [
                    'button[type="submit"]',
                    'button.SubmitButton',
                    'button:has-text("Pay")',
                    'button:has-text("Submit")',
                    'button:has-text("Donate")',
                    '[role="button"][type="submit"]'
                ]
                
                for selector in submit_selectors:
                    try:
                        btn = page.locator(selector).first
                        if await btn.count() > 0 and await btn.is_visible():
                            await btn.click()
                            payment_submitted = True
                            print("[SUBMIT] ✓ Button clicked")
                            break
                    except:
                        continue
                
                if not payment_submitted:
                    await page.keyboard.press('Enter')
                    payment_submitted = True
                    print("[SUBMIT] ✓ Enter pressed")
            except Exception as e:
                print(f"[SUBMIT] ⚠ Failed: {e}")
            
            # ============================================
            # WAIT FOR PAYMENT PROCESSING
            # ============================================
            
            print("[WAIT] Processing payment (10s initial wait)...")
            await safe_wait(page, 10000)
            
            # ============================================
            # MONITOR FOR CONFIRMATION
            # ============================================
            
            print("[MONITOR] Waiting for payment confirmation...")
            start_time = time.time()
            max_wait = CONFIG["RESPONSE_TIMEOUT_SECONDS"]
            last_response_count = 0
            
            while time.time() - start_time < max_wait:
                elapsed = time.time() - start_time
                
                # Check for new responses
                current_responses = len(stripe_result['raw_responses'])
                if current_responses > last_response_count:
                    print(f"[MONITOR] {current_responses} Stripe responses captured")
                    last_response_count = current_responses
                
                # Check for payment confirmation
                if stripe_result.get("payment_confirmed"):
                    print("[✓✓✓ SUCCESS] Payment confirmed!")
                    break
                
                # Check for error
                if stripe_result.get("error"):
                    print(f"[ERROR] {stripe_result['error']}")
                    break
                
                # Check for 3DS
                if stripe_result.get("requires_3ds"):
                    print("[3DS] Authentication required - stopping")
                    break
                
                # Check URL for success redirect
                try:
                    current_url = page.url
                    success_url = stripe_result.get("success_url")
                    
                    if success_url and current_url.startswith(success_url):
                        print(f"[✓ SUCCESS] Redirected to: {current_url[:80]}")
                        stripe_result["payment_confirmed"] = True
                        stripe_result["success"] = True
                        stripe_result["message"] = "Payment successful (redirect)"
                        break
                    
                    if any(keyword in current_url.lower() for keyword in ['success', 'thank-you', 'complete', 'confirmed']):
                        print(f"[SUCCESS PAGE] {current_url[:80]}")
                        stripe_result["payment_confirmed"] = True
                        stripe_result["success"] = True
                        stripe_result["message"] = "Payment successful (success page)"
                        break
                except:
                    pass
                
                # Heartbeat check
                if int(elapsed) % 5 == 0 and int(elapsed) > 0:
                    try:
                        await page.evaluate('1')
                    except:
                        print("[WARNING] Browser connection lost")
                        break
                
                await asyncio.sleep(0.5)
            
            print(f"\n[SUMMARY] Captured {len(stripe_result['raw_responses'])} Stripe API responses")
            
            # ============================================
            # BUILD FINAL RESPONSE
            # ============================================
            
            if stripe_result.get("payment_confirmed"):
                return {
                    "success": True,
                    "message": stripe_result.get("message", "Payment successful"),
                    "card": f"{card['number']}|{card['month']}|{card['year']}|{card['cvv']}",
                    "email": email,
                    "payment_intent_id": stripe_result.get("payment_intent_id"),
                    "token_id": stripe_result.get("token_id"),
                    "payment_method_id": stripe_result.get("payment_method_id"),
                    "raw_responses": stripe_result["raw_responses"]
                }
            elif stripe_result.get("requires_3ds"):
                return {
                    "success": False,
                    "requires_3ds": True,
                    "message": "3D Secure authentication required",
                    "card": f"{card['number']}|{card['month']}|{card['year']}|{card['cvv']}",
                    "email": email,
                    "raw_responses": stripe_result["raw_responses"]
                }
            elif stripe_result.get("error"):
                return {
                    "success": False,
                    "error": stripe_result["error"],
                    "decline_code": stripe_result.get("decline_code"),
                    "card": f"{card['number']}|{card['month']}|{card['year']}|{card['cvv']}",
                    "email": email,
                    "raw_responses": stripe_result["raw_responses"]
                }
            else:
                return {
                    "success": False,
                    "message": "Payment status unclear - check raw responses",
                    "card": f"{card['number']}|{card['month']}|{card['year']}|{card['cvv']}",
                    "email": email,
                    "details": {
                        "filled": filled_status,
                        "payment_submitted": payment_submitted,
                        "responses_captured": len(stripe_result["raw_responses"])
                    },
                    "raw_responses": stripe_result["raw_responses"]
                }
                
        except Exception as e:
            error_msg = str(e)
            print(f"[ERROR] Automation failed: {error_msg}")
            return {
                "error": f"Automation failed: {error_msg}",
                "card": f"{card['number']}|{card['month']}|{card['year']}|{card['cvv']}",
                "email": email,
                "raw_responses": stripe_result.get("raw_responses", [])
            }
            
        finally:
            # ============================================
            # CLEANUP
            # ============================================
            print("[CLEANUP] Closing browser resources...")
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
                    print("[CLEANUP] ✓ Browser closed")
                except:
                    pass

# ============================================================================
# FLASK ENDPOINTS
# ============================================================================

@app.route('/hrkXstripe', methods=['GET'])
def stripe_endpoint():
    """
    Main endpoint for Stripe automation
    
    Query params:
        - url: Stripe payment page URL (required)
        - cc: Card details in format number|month|year|cvv (required)
        - email: Email address (optional)
    
    Example:
        /hrkXstripe?url=https://donate.example.com&cc=4242424242424242|12|2025|123&email=test@example.com
    """
    # Rate limiting
    can_proceed, wait_time = rate_limit_check()
    if not can_proceed:
        return jsonify({
            "error": "Rate limit exceeded",
            "retry_after": f"{wait_time:.1f}s"
        }), 429
    
    # Get parameters
    url = request.args.get('url')
    cc = request.args.get('cc')
    email = request.args.get('email')
    
    print(f"\n{'='*80}")
    print(f"[REQUEST] {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"[URL] {url}")
    print(f"[CC] {cc}")
    print(f"[EMAIL] {email or 'auto-generated'}")
    print('='*80)
    
    # Validation
    if not url or not cc:
        return jsonify({
            "error": "Missing required parameters",
            "required": {
                "url": "Stripe payment page URL",
                "cc": "Card details (format: number|month|year|cvv)"
            },
            "optional": {
                "email": "Email address"
            }
        }), 400
    
    # Run automation
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    try:
        result = loop.run_until_complete(run_stripe_automation(url, cc, email))
        
        success = result.get('success', False)
        status_code = 200 if success else 400
        
        print(f"[RESULT] {'✓ SUCCESS' if success else '✗ FAILED'}")
        
        return jsonify(result), status_code
        
    except Exception as e:
        print(f"[SERVER ERROR] {e}")
        return jsonify({
            "error": f"Server error: {str(e)}"
        }), 500
        
    finally:
        try:
            loop.close()
        except:
            pass

@app.route('/status', methods=['GET'])
def status_endpoint():
    """Health check endpoint"""
    return jsonify({
        "status": "online",
        "version": "3.0-browserstack",
        "timestamp": datetime.now().isoformat(),
        "config": {
            "run_local": CONFIG["RUN_LOCAL"],
            "browserstack_configured": bool(CONFIG["BROWSERSTACK_USERNAME"] and CONFIG["BROWSERSTACK_ACCESS_KEY"]),
            "rate_limit": f"{CONFIG['RATE_LIMIT_REQUESTS']} requests per {CONFIG['RATE_LIMIT_WINDOW']}s"
        }
    })

@app.route('/test', methods=['GET'])
def test_endpoint():
    """
    Test BrowserStack connection
    """
    if CONFIG["RUN_LOCAL"]:
        return jsonify({
            "error": "Test endpoint only works with BrowserStack. Set RUN_LOCAL=false"
        }), 400
    
    async def test_connection():
        async with async_playwright() as p:
            try:
                browser = await get_browserstack_browser(p)
                page = await browser.new_page()
                await page.goto('https://www.google.com', timeout=15000)
                title = await page.title()
                await browser.close()
                return {
                    "success": True,
                    "message": "BrowserStack connection successful",
                    "test_page_title": title
                }
            except Exception as e:
                return {
                    "success": False,
                    "error": str(e)
                }
    
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    try:
        result = loop.run_until_complete(test_connection())
        status_code = 200 if result.get('success') else 500
        return jsonify(result), status_code
    finally:
        loop.close()

@app.route('/', methods=['GET'])
def index():
    """API documentation"""
    return jsonify({
        "name": "Stripe Payment Automation API",
        "version": "3.0-browserstack",
        "endpoints": {
            "/hrkXstripe": {
                "method": "GET",
                "description": "Automate Stripe payment testing",
                "params": {
                    "url": "Payment page URL (required)",
                    "cc": "Card format: number|month|year|cvv (required)",
                    "email": "Email address (optional)"
                },
                "example": "/hrkXstripe?url=https://donate.example.com&cc=4242424242424242|12|2025|123"
            },
            "/status": {
                "method": "GET",
                "description": "Health check and configuration status"
            },
            "/test": {
                "method": "GET",
                "description": "Test BrowserStack connection"
            }
        },
        "card_format": {
            "example": "4242424242424242|12|2025|123",
            "placeholders": "Use 'x' for random: 424242xxxxxx4242|xx|xxxx|xxx"
        }
    })

# ============================================================================
# MAIN
# ============================================================================

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5001))
    
    print("="*80)
    print("🚀 Stripe Payment Automation API v3.0 - BrowserStack Edition")
    print("="*80)
    print(f"[PORT] {port}")
    print(f"[MODE] {'LOCAL BROWSER' if CONFIG['RUN_LOCAL'] else 'BROWSERSTACK'}")
    
    if not CONFIG["RUN_LOCAL"]:
        if CONFIG["BROWSERSTACK_USERNAME"] and CONFIG["BROWSERSTACK_ACCESS_KEY"]:
            print(f"[BROWSERSTACK] ✓ Credentials configured")
            print(f"[USERNAME] {CONFIG['BROWSERSTACK_USERNAME']}")
        else:
            print("[WARNING] ⚠ BrowserStack credentials not set!")
            print("[WARNING] Set BROWSERSTACK_USERNAME and BROWSERSTACK_ACCESS_KEY")
    
    print("="*80)
    print("\n📖 Endpoints:")
    print(f"  - GET  http://localhost:{port}/")
    print(f"  - GET  http://localhost:{port}/status")
    print(f"  - GET  http://localhost:{port}/test")
    print(f"  - GET  http://localhost:{port}/hrkXstripe?url=...&cc=...")
    print("\n" + "="*80 + "\n")
    
    app.run(host='0.0.0.0', port=port, debug=False)
