import os
import asyncio
import json
import random
import re
import time
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
    "RESPONSE_TIMEOUT_SECONDS": 60,  # Increased timeout
    "RETRY_DELAY": 2000,
    "RATE_LIMIT_REQUESTS": 5,
    "RATE_LIMIT_WINDOW": 60,
    "MAX_RETRIES": 2,
    "BROWSERLESS_TIMEOUT": 90000  # Increased timeout
}

app = Flask(__name__)
request_timestamps = deque(maxlen=CONFIG["RATE_LIMIT_REQUESTS"])

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

# Enhanced Response Analyzer
class StripeResponseAnalyzer:
    @staticmethod
    def is_stripe_endpoint(url):
        return any(domain in url.lower() for domain in ['stripe.com', 'stripe.network', 'checkout.stripe.com'])
    
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
            '/v1/checkout/sessions',
            '/v1/sources'
        ]
        return any(endpoint in url.lower() for endpoint in critical)
    
    @staticmethod
    def analyze_response(url, status, body_text, result_dict):
        try:
            data = json.loads(body_text) if body_text else {}
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
            if data:
                print(f"[DATA] {json.dumps(data)[:300]}...")
        
        # Check for success_url
        if data.get('success_url'):
            result_dict["success_url"] = data['success_url']
            print(f"[SUCCESS URL] {data['success_url']}")
        
        # EXPANDED payment confirmation checks
        is_payment_success = False
        
        # Direct status checks
        if data.get('status') in ['succeeded', 'success', 'requires_capture', 'processing', 'complete', 'paid']:
            is_payment_success = True
            
        # Payment intent status
        pi = data.get('payment_intent', {})
        if isinstance(pi, dict) and pi.get('status') in ['succeeded', 'success', 'requires_capture', 'processing']:
            is_payment_success = True
            
        # Charge success
        if data.get('object') == 'charge' and data.get('paid') == True:
            is_payment_success = True
            
        # Checkout session
        if data.get('object') == 'checkout.session' and data.get('payment_status') in ['paid', 'no_payment_required']:
            is_payment_success = True
            
        # Setup intent
        if data.get('object') == 'setup_intent' and data.get('status') == 'succeeded':
            is_payment_success = True
            
        # Source success
        if data.get('object') == 'source' and data.get('status') == 'chargeable':
            result_dict["source_created"] = True
            result_dict["source_id"] = data.get('id')
            print(f"[SOURCE] {data.get('id')} - chargeable")
        
        if is_payment_success:
            result_dict["payment_confirmed"] = True
            result_dict["success"] = True
            result_dict["message"] = f"Payment {data.get('status', 'succeeded')}"
            result_dict["payment_intent_id"] = (
                data.get('id') or 
                data.get('payment_intent', {}).get('id') if isinstance(data.get('payment_intent'), dict) else data.get('payment_intent')
            )
            print(f"[✓✓✓ PAYMENT CONFIRMED] Status: {data.get('status')} | Object: {data.get('object')}")
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
        
        # Check for payment intent (not confirmed yet)
        if status == 200 and data.get('id', '').startswith('pi_'):
            result_dict["payment_intent_created"] = True
            result_dict["payment_intent_id"] = data.get('id')
            result_dict["client_secret"] = data.get('client_secret')
            print(f"[PAYMENT INTENT] {data.get('id')} - Status: {data.get('status')}")
        
        # Check for errors
        error = data.get('error') or (data.get('payment_intent', {}).get('last_payment_error') if isinstance(data.get('payment_intent'), dict) else None)
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

async def safe_wait(page, ms):
    try:
        await page.wait_for_timeout(ms)
        return True
    except:
        return False

async def fill_stripe_iframe_field(frame, field_type, value):
    """Enhanced iframe field filling with better selectors"""
    selectors = {
        'card': [
            'input[placeholder*="Card number"]',
            'input[placeholder*="card number"]',
            'input[placeholder*="1234"]',
            'input[name*="cardnumber"]',
            'input[name*="cardNumber"]',
            'input[aria-label*="Card number"]',
            'input[data-elements-stable-field-name="cardNumber"]'
        ],
        'expiry': [
            'input[placeholder*="MM / YY"]',
            'input[placeholder*="MM/YY"]',
            'input[placeholder*="Expiry"]',
            'input[placeholder*="expiry"]',
            'input[name*="exp"]',
            'input[aria-label*="Expiry"]',
            'input[data-elements-stable-field-name="cardExpiry"]'
        ],
        'cvc': [
            'input[placeholder*="CVC"]',
            'input[placeholder*="CVV"]',
            'input[placeholder*="Security"]',
            'input[name*="cvc"]',
            'input[name*="cvv"]',
            'input[aria-label*="CVC"]',
            'input[aria-label*="Security"]',
            'input[data-elements-stable-field-name="cardCvc"]'
        ]
    }
    
    for selector in selectors.get(field_type, []):
        try:
            element = await frame.query_selector(selector)
            if element and await element.is_visible():
                await element.click()
                await frame.wait_for_timeout(100)
                
                # Clear field first
                await element.press('Control+a')
                await element.press('Backspace')
                
                # Type value with realistic delays
                for char in value:
                    await element.type(char, delay=random.randint(30, 80))
                
                print(f"[FILLED] {field_type}: {value[:4]}...")
                return True
        except Exception as e:
            continue
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
        "requires_3ds": False
    }
    
    analyzer = StripeResponseAnalyzer()
    
    async with async_playwright() as p:
        browser = None
        context = None
        page = None
        payment_submitted = False
        
        try:
            # Connect to browser with retry logic
            if CONFIG["RUN_LOCAL"]:
                browser = await p.chromium.launch(
                    headless=False, 
                    slow_mo=100,
                    args=['--disable-blink-features=AutomationControlled']
                )
                print("[BROWSER] Local mode")
            else:
                print("[BROWSER] Connecting to Browserless...")
                browser_url = f"wss://production-sfo.browserless.io/chromium/playwright?token={CONFIG['BROWSERLESS_API_KEY']}&timeout={CONFIG['BROWSERLESS_TIMEOUT']}"
                
                for attempt in range(3):
                    try:
                        browser = await p.chromium.connect(browser_url, timeout=30000)
                        print("[BROWSER] ✓ Connected to Browserless")
                        break
                    except Exception as e:
                        print(f"[BROWSER] Attempt {attempt + 1} failed: {e}")
                        if attempt == 2:
                            print("[BROWSER] Falling back to local headless")
                            browser = await p.chromium.launch(headless=True)
                        else:
                            await asyncio.sleep(2)
            
            context = await browser.new_context(
                viewport={'width': 1920, 'height': 1080},
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36',
                locale='en-US',
                timezone_id='America/New_York'
            )
            
            # Add stealth scripts
            await context.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
            """)
            
            page = await context.new_page()
            
            # Enhanced response capture
            async def capture_response(response):
                try:
                    if not analyzer.is_stripe_endpoint(response.url):
                        return
                    
                    content_type = response.headers.get('content-type', '')
                    if 'application/json' in content_type or 'text/plain' in content_type:
                        try:
                            body = await response.body()
                            text = body.decode('utf-8', errors='ignore')
                            if text.strip():
                                analyzer.analyze_response(response.url, response.status, text, stripe_result)
                        except Exception as e:
                            print(f"[RESPONSE ERROR] {e}")
                except Exception as e:
                    print(f"[CAPTURE ERROR] {e}")
            
            page.on("response", capture_response)
            
            # Navigate with retry
            print("[NAV] Loading page...")
            for attempt in range(3):
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=45000)
                    print("[NAV] ✓ Page loaded")
                    break
                except PlaywrightTimeout:
                    print(f"[NAV] Timeout attempt {attempt + 1}")
                    if attempt == 2:
                        return {"error": "Page load timeout after 3 attempts"}
                    await asyncio.sleep(2)
            
            # Wait for initial load
            await safe_wait(page, 3000)
            
            # Check for Stripe Elements
            stripe_present = await page.evaluate("""
                () => {
                    return typeof Stripe !== 'undefined' || 
                           document.querySelector('iframe[name*="stripe"]') !== null ||
                           document.querySelector('[class*="StripeElement"]') !== null;
                }
            """)
            print(f"[STRIPE] Present: {stripe_present}")
            
            # Fill email if present
            print("[FILL] Looking for email field...")
            email_filled = False
            for selector in [
                'input[type="email"]',
                'input[name="email"]',
                'input[id="email"]',
                'input[placeholder*="email" i]',
                'input[autocomplete="email"]'
            ]:
                try:
                    elements = await page.query_selector_all(selector)
                    for element in elements:
                        if await element.is_visible():
                            await element.click()
                            await element.fill("")
                            await element.type(email, delay=50)
                            email_filled = True
                            print(f"[EMAIL] ✓ Filled: {email}")
                            break
                    if email_filled:
                        break
                except:
                    continue
            
            # Small wait after email
            await safe_wait(page, 1000)
            
            # Fill card details
            print("[FILL] Processing card fields...")
            filled_status = {"card": False, "expiry": False, "cvc": False}
            
            # Wait for Stripe iframes to load
            await page.wait_for_selector('iframe', timeout=10000)
            await safe_wait(page, 2000)
            
            # Get all frames
            frames = page.frames
            stripe_frames = [f for f in frames if 'stripe' in f.url.lower()]
            print(f"[FRAMES] Found {len(stripe_frames)} Stripe frames")
            
            # Try each Stripe frame
            for frame in stripe_frames:
                try:
                    # Wait for frame to be ready
                    await frame.wait_for_selector('input', timeout=5000)
                    
                    # Fill card number
                    if not filled_status["card"]:
                        if await fill_stripe_iframe_field(frame, 'card', card['number']):
                            filled_status["card"] = True
                    
                    # Fill expiry
                    if not filled_status["expiry"]:
                        expiry_value = f"{card['month']}/{card['year'][-2:]}"
                        if await fill_stripe_iframe_field(frame, 'expiry', expiry_value):
                            filled_status["expiry"] = True
                    
                    # Fill CVC
                    if not filled_status["cvc"]:
                        if await fill_stripe_iframe_field(frame, 'cvc', card['cvv']):
                            filled_status["cvc"] = True
                            
                except Exception as e:
                    print(f"[FRAME ERROR] {e}")
                    continue
            
            print(f"[FILL STATUS] {filled_status}")
            
            # If not all fields filled, try combined card field
            if not all(filled_status.values()):
                print("[FILL] Trying combined card field...")
                for frame in stripe_frames:
                    try:
                        combined_selectors = [
                            'input[placeholder*="Card information"]',
                            'input[placeholder*="Card details"]',
                            'input[aria-label*="Card information"]'
                        ]
                        for selector in combined_selectors:
                            element = await frame.query_selector(selector)
                            if element and await element.is_visible():
                                await element.click()
                                card_string = f"{card['number']}{card['month']}{card['year'][-2:]}{card['cvv']}"
                                for char in card_string:
                                    await element.type(char, delay=50)
                                print("[FILL] ✓ Combined field filled")
                                filled_status = {"card": True, "expiry": True, "cvc": True}
                                break
                    except:
                        continue
            
            # Additional wait to ensure validation
            await safe_wait(page, 3000)
            
            # Submit payment
            print("[SUBMIT] Looking for submit button...")
            submit_selectors = [
                'button[type="submit"]:visible',
                'button:has-text("Pay"):visible',
                'button:has-text("Submit"):visible',
                'button:has-text("Complete"):visible',
                'button:has-text("Confirm"):visible',
                'button.SubmitButton:visible',
                '[class*="SubmitButton"]:visible',
                'button[class*="pay"]:visible'
            ]
            
            for selector in submit_selectors:
                try:
                    buttons = await page.query_selector_all(selector)
                    for button in buttons:
                        if await button.is_visible() and await button.is_enabled():
                            # Scroll to button
                            await button.scroll_into_view_if_needed()
                            await safe_wait(page, 500)
                            
                            # Click with retry
                            for click_attempt in range(3):
                                try:
                                    await button.click()
                                    payment_submitted = True
                                    print(f"[SUBMIT] ✓ Clicked: {selector}")
                                    break
                                except:
                                    await safe_wait(page, 1000)
                            
                            if payment_submitted:
                                break
                    if payment_submitted:
                        break
                except:
                    continue
            
            # If still not submitted, try Enter key
            if not payment_submitted:
                print("[SUBMIT] Trying Enter key...")
                await page.keyboard.press('Enter')
                payment_submitted = True
                print("[SUBMIT] ✓ Enter pressed")
            
            # Critical: Wait for payment processing
            print("[PROCESSING] Waiting for payment response...")
            await safe_wait(page, 5000)
            
            # Monitor for response
            print("[MONITOR] Checking for confirmation...")
            start_time = time.time()
            max_wait = CONFIG["RESPONSE_TIMEOUT_SECONDS"]
            last_log_time = time.time()
            
            while time.time() - start_time < max_wait:
                elapsed = time.time() - start_time
                
                # Log status every 5 seconds
                if time.time() - last_log_time > 5:
                    print(f"[MONITOR] {elapsed:.0f}s - Responses: {len(stripe_result['raw_responses'])}")
                    last_log_time = time.time()
                
                # Check for payment confirmation
                if stripe_result.get("payment_confirmed"):
                    print("[✓✓✓ SUCCESS] Payment confirmed!")
                    break
                
                # Check for definitive error
                if stripe_result.get("error") and stripe_result.get("decline_code"):
                    print(f"[DECLINED] {stripe_result['decline_code']}: {stripe_result['error']}")
                    break
                
                # Check for 3DS
                if stripe_result.get("requires_3ds"):
                    print("[3DS] Authentication required")
                    
                    # Check for 3DS iframe
                    try:
                        three_ds_frame = await page.wait_for_selector('iframe[name*="3ds"]', timeout=5000)
                        if three_ds_frame:
                            print("[3DS] Frame detected - manual intervention required")
                            break
                    except:
                        pass
                    break
                
                # Check current URL for success indicators
                try:
                    current_url = page.url
                    
                    # Check against success_url from API
                    if stripe_result.get("success_url") and current_url.startswith(stripe_result["success_url"]):
                        print(f"[✓ SUCCESS] Redirected to success URL")
                        stripe_result["payment_confirmed"] = True
                        stripe_result["success"] = True
                        break
                    
                    # Check for common success patterns
                    success_patterns = ['success', 'thank', 'complete', 'confirmed', 'receipt', 'order-confirmed']
                    if any(pattern in current_url.lower() for pattern in success_patterns):
                        print(f"[SUCCESS PAGE] {current_url[:80]}")
                        stripe_result["payment_confirmed"] = True
                        stripe_result["success"] = True
                        break
                    
                    # Check for error patterns
                    error_patterns = ['error', 'failed', 'declined', 'rejected']
                    if any(pattern in current_url.lower() for pattern in error_patterns):
                        print(f"[ERROR PAGE] {current_url[:80]}")
                        if not stripe_result.get("error"):
                            stripe_result["error"] = "Payment failed - error page detected"
                        break
                        
                except:
                    pass
                
                # Check page content for success messages
                try:
                    page_text = await page.evaluate('document.body.innerText')
                    success_texts = ['payment successful', 'thank you for your order', 'payment complete', 'order confirmed']
                    if any(text in page_text.lower() for text in success_texts):
                        print("[PAGE] Success message detected")
                        stripe_result["payment_confirmed"] = True
                        stripe_result["success"] = True
                        break
                except:
                    pass
                
                # Keep page alive
                if int(elapsed) % 10 == 0:
                    try:
                        await page.evaluate('1')
                    except:
                        print("[WARNING] Page disconnected")
                        break
                
                await asyncio.sleep(0.5)
            
            # Final summary
            print(f"\n{'='*40}")
            print(f"[SUMMARY] Total responses: {len(stripe_result['raw_responses'])}")
            print(f"[SUMMARY] Payment confirmed: {stripe_result.get('payment_confirmed', False)}")
            print(f"[SUMMARY] Token created: {stripe_result.get('token_created', False)}")
            print(f"[SUMMARY] Payment method: {stripe_result.get('payment_method_created', False)}")
            print(f"[SUMMARY] 3DS required: {stripe_result.get('requires_3ds', False)}")
            print(f"{'='*40}\n")
            
            # Build response
            if stripe_result.get("payment_confirmed"):
                return {
                    "success": True,
                    "message": stripe_result.get("message", "Payment successful"),
                    "card": f"{card['number']}|{card['month']}|{card['year']}|{card['cvv']}",
                    "payment_intent_id": stripe_result.get("payment_intent_id"),
                    "token_id": stripe_result.get("token_id"),
                    "payment_method_id": stripe_result.get("payment_method_id"),
                    "raw_responses": stripe_result["raw_responses"][-5:]  # Last 5 responses
                }
            elif stripe_result.get("requires_3ds"):
                return {
                    "success": False,
                    "requires_3ds": True,
                    "message": "3D Secure authentication required",
                    "card": f"{card['number']}|{card['month']}|{card['year']}|{card['cvv']}",
                    "payment_intent_id": stripe_result.get("payment_intent_id"),
                    "raw_responses": stripe_result["raw_responses"][-5:]
                }
            elif stripe_result.get("error"):
                return {
                    "success": False,
                    "error": stripe_result["error"],
                    "decline_code": stripe_result.get("decline_code", "unknown"),
                    "card": f"{card['number']}|{card['month']}|{card['year']}|{card['cvv']}",
                    "raw_responses": stripe_result["raw_responses"][-5:]
                }
            else:
                # No clear result
                return {
                    "success": False,
                    "message": "Payment status unclear - manual review needed",
                    "card": f"{card['number']}|{card['month']}|{card['year']}|{card['cvv']}",
                    "details": {
                        "filled": filled_status,
                        "submitted": payment_submitted,
                        "total_responses": len(stripe_result["raw_responses"]),
                        "token_created": stripe_result.get("token_created", False),
                        "payment_method_created": stripe_result.get("payment_method_created", False),
                        "payment_intent_created": stripe_result.get("payment_intent_created", False)
                    },
                    "raw_responses": stripe_result["raw_responses"][-10:]  # Last 10 for debugging
                }
                
        except Exception as e:
            import traceback
            error_details = traceback.format_exc()
            print(f"[ERROR] {str(e)}")
            print(f"[TRACEBACK] {error_details}")
            return {
                "error": f"Automation failed: {str(e)}",
                "details": error_details[:500],
                "raw_responses": stripe_result.get("raw_responses", [])
            }
            
        finally:
            # Cleanup
            try:
                if page:
                    await page.close()
                if context:
                    await context.close()
                if browser:
                    await browser.close()
                print("[CLEANUP] ✓ Complete")
            except:
                print("[CLEANUP] Error during cleanup")

@app.route('/hrkXstripe', methods=['GET', 'POST'])
def stripe_endpoint():
    # Support both GET and POST
    if request.method == 'POST':
        data = request.json
        url = data.get('url')
        cc = data.get('cc')
        email = data.get('email')
    else:
        url = request.args.get('url')
        cc = request.args.get('cc')
        email = request.args.get('email')
    
    # Rate limiting
    can_proceed, wait_time = rate_limit_check()
    if not can_proceed:
        return jsonify({
            "error": "Rate limit exceeded",
            "retry_after": f"{wait_time:.1f}s"
        }), 429
    
    print(f"\n{'='*80}")
    print(f"[REQUEST] {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"[URL] {url[:100] if url else 'None'}...")
    print(f"[CC] {cc if cc else 'None'}")
    print('='*80)
    
    # Validation
    if not url or not cc:
        return jsonify({
            "error": "Missing required parameters",
            "required": ["url", "cc"],
            "format": "cc should be: number|month|year|cvv"
        }), 400
    
    # Validate URL
    if not url.startswith(('http://', 'https://')):
        return jsonify({"error": "Invalid URL format"}), 400
    
    # Run automation
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    try:
        result = loop.run_until_complete(run_stripe_automation(url, cc, email))
        status_code = 200 if result.get('success') else (400 if result.get('error') else 202)
        print(f"[RESULT] {'✓ SUCCESS' if result.get('success') else '✗ FAILED'} [{status_code}]")
        return jsonify(result), status_code
    except Exception as e:
        print(f"[SERVER ERROR] {e}")
        return jsonify({"error": f"Server error: {str(e)}"}), 500
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
        "version": "3.0-enhanced",
        "timestamp": datetime.now().isoformat(),
        "features": [
            "Enhanced iframe detection",
            "Better error handling",
            "3DS detection",
            "Multiple submit button patterns",
            "Improved payment confirmation detection"
        ]
    })

@app.route('/', methods=['GET'])
def home():
    """Home endpoint with usage instructions"""
    return jsonify({
        "service": "Stripe Payment Automation",
        "version": "3.0",
        "endpoints": {
            "/hrkXstripe": {
                "methods": ["GET", "POST"],
                "params": {
                    "url": "Stripe checkout page URL (required)",
                    "cc": "Card in format: number|month|year|cvv (required)",
                    "email": "Email address (optional)"
                },
                "example": "/hrkXstripe?url=https://checkout.stripe.com/...&cc=4242424242424242|12|25|123"
            },
            "/status": "Health check"
        }
    })

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5001))
    debug_mode = os.getenv('DEBUG', 'false').lower() == 'true'
    
    print("="*80)
    print("[SERVER] Stripe Automation v3.0 - Enhanced")
    print(f"[PORT] {port}")
    print(f"[MODE] {'Local' if CONFIG['RUN_LOCAL'] else 'Browserless'}")
    print(f"[DEBUG] {debug_mode}")
    print("="*80)
    
    app.run(host='0.0.0.0', port=port, debug=debug_mode)
