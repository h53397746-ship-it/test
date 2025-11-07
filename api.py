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

# Load environment variables
load_dotenv()

# Configuration
CONFIG = {
    "RUN_LOCAL": os.getenv('RUN_LOCAL', 'false').lower() == 'true',
    "BROWSERLESS_API_KEY": os.getenv('BROWSERLESS_API_KEY'),
    "RESPONSE_TIMEOUT_SECONDS": 45,  # Reduced for Browserless
    "RETRY_DELAY": 2000,
    "RATE_LIMIT_REQUESTS": 5,
    "RATE_LIMIT_WINDOW": 60,
    "MAX_RETRIES": 2,  # Reduced for Browserless
    "RETRY_BACKOFF": 5,
    "DEBUGGER_VERSION": "1.3",
    "DETAILED_LOGGING": True,
    "BROWSERLESS_TIMEOUT": 50000  # Browserless session timeout
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
        """Analyze response with detailed logging"""
        print(f"\n[STRIPE API] {url[:80]}... [{status}]")
        
        try:
            data = json.loads(body_text)
            
            if CONFIG["DETAILED_LOGGING"] and len(body_text) < 5000:
                print(f"[RESPONSE] {json.dumps(data, indent=2)[:1000]}")
                
        except json.JSONDecodeError:
            return
        
        # Store raw response
        result_dict["raw_responses"].append({
            "url": url,
            "status": status,
            "data": data,
            "timestamp": datetime.now().isoformat()
        })
        
        # Check for success_url
        if data.get('success_url'):
            result_dict["success_url"] = data['success_url']
            print(f"[✓ SUCCESS URL] {data['success_url']}")
        
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
            print(f"[✓ PAYMENT CONFIRMED] {data.get('status')}")
            return
        
        # Check for token creation
        if '/v1/tokens' in url.lower() and status == 200 and data.get('id', '').startswith('tok_'):
            result_dict["token_created"] = True
            result_dict["token_id"] = data.get('id')
            print(f"[✓ TOKEN] {data.get('id')}")
        
        # Check for payment method
        if '/v1/payment_methods' in url.lower() and status == 200:
            result_dict["payment_method_created"] = True
            result_dict["payment_method_id"] = data.get('id')
            print(f"[✓ PAYMENT METHOD] {data.get('id')}")
        
        # Check for payment intent
        if 'payment_intent' in url.lower() and status == 200:
            result_dict["payment_intent_created"] = True
            result_dict["payment_intent_id"] = data.get('id')
            print(f"[✓ PAYMENT INTENT] {data.get('id')}")
        
        # Check for errors
        if 'error' in data or data.get('payment_intent', {}).get('last_payment_error'):
            error = data.get('error') or data.get('payment_intent', {}).get('last_payment_error')
            decline_code = error.get('decline_code') or error.get('code') or "unknown"
            error_message = error.get('message') or "Transaction error"
            
            result_dict["error"] = error_message
            result_dict["decline_code"] = decline_code
            result_dict["success"] = False
            print(f"[✗ ERROR] {decline_code}: {error_message}")
        
        # Check for 3DS
        if data.get('status') == 'requires_action' or data.get('payment_intent', {}).get('status') == 'requires_action':
            result_dict["requires_3ds"] = True
            print("[⚠ 3DS] Required")

# === ENHANCED HCAPTCHA HANDLER ===
async def handle_hcaptcha_advanced(page):
    """Advanced hCaptcha handling"""
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
        return True
    
    print("[HCAPTCHA] Detected")
    
    async def simulate_click(frame, element):
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
        except:
            pass
        return False
    
    widget_frame = None
    for frame in page.frames:
        if 'hcaptcha.com' in frame.url and 'checkbox' in frame.url:
            widget_frame = frame
            break
    
    if widget_frame:
        try:
            checkbox = await widget_frame.query_selector('#checkbox')
            if checkbox:
                await simulate_click(widget_frame, checkbox)
                await asyncio.sleep(3)
                print("[HCAPTCHA] Clicked checkbox")
                return True
        except:
            pass
    
    return False

# === SAFE WAIT HELPER ===
async def safe_wait(page, ms):
    """Wait with TargetClosedError protection"""
    try:
        await page.wait_for_timeout(ms)
        return True
    except TargetClosedError:
        print("[WARNING] Browser closed during wait")
        return False
    except Exception as e:
        print(f"[WARNING] Wait error: {e}")
        return False

# === MAIN AUTOMATION FUNCTION ===
async def run_stripe_automation(url, cc_string, email=None):
    card = process_card_input(cc_string)
    if not card:
        return {"error": "Invalid card format. Use: number|month|year|cvv"}
    
    if not email:
        email = f"test{random.randint(1000,9999)}@example.com"
    
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
        cdp = None
        browser_closed = False
        
        try:
            # Connect to browser
            if CONFIG["RUN_LOCAL"]:
                print("[BROWSER] Local mode")
                browser = await p.chromium.launch(
                    headless=False,
                    slow_mo=100,
                    args=['--disable-blink-features=AutomationControlled']
                )
            else:
                print("[BROWSER] Connecting to Browserless...")
                browser_url = f"wss://production-sfo.browserless.io/chromium/playwright?token={CONFIG['BROWSERLESS_API_KEY']}&timeout={CONFIG['BROWSERLESS_TIMEOUT']}"
                try:
                    browser = await p.chromium.connect(browser_url, timeout=30000)
                    print("[BROWSER] ✓ Connected")
                except Exception as e:
                    print(f"[BROWSER] Browserless failed, using local: {e}")
                    browser = await p.chromium.launch(headless=True)
            
            # Create context
            context = await browser.new_context(
                viewport={'width': 1920, 'height': 1080},
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36'
            )
            
            page = await context.new_page()
            print("[PAGE] ✓ Created")
            
            # Response capture (NO CDP for Browserless)
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
            
            # Navigate with error handling
            print(f"[NAV] Loading...")
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=40000)
                print("[NAV] ✓ Loaded")
            except TargetClosedError:
                return {"error": "Browser closed by Browserless (session timeout)"}
            except Exception as e:
                return {"error": f"Navigation failed: {str(e)}"}
            
            # Wait for load
            if not await safe_wait(page, 3000):
                return {"error": "Browser closed after navigation"}
            
            # === FILL EMAIL ===
            print("[FILL] Email...")
            email_filled = False
            
            try:
                for selector in ['input[type="email"]', 'input[name="email"]', '#email']:
                    try:
                        elements = await page.query_selector_all(selector)
                        for element in elements:
                            if await element.is_visible():
                                await element.click()
                                await element.fill(email)
                                email_filled = True
                                print(f"[FILL] ✓ Email: {email}")
                                break
                        if email_filled:
                            break
                    except:
                        continue
            except TargetClosedError:
                return {"error": "Browser closed during form fill"}
            
            if not await safe_wait(page, 1000):
                return {"error": "Browser closed"}
            
            # === FILL CARD IN STRIPE IFRAMES ===
            print("[FILL] Card fields...")
            
            filled_status = {"card": False, "expiry": False, "cvc": False}
            
            try:
                frames = page.frames
                stripe_frames = [f for f in frames if 'stripe' in f.url.lower()]
                print(f"[FRAMES] Found {len(stripe_frames)} Stripe frames")
                
                for frame in stripe_frames:
                    try:
                        # Card number
                        if not filled_status["card"]:
                            card_inputs = await frame.query_selector_all('input')
                            for inp in card_inputs:
                                try:
                                    placeholder = (await inp.get_attribute('placeholder') or '').lower()
                                    if 'card number' in placeholder or '1234' in placeholder:
                                        await inp.click()
                                        for digit in card['number']:
                                            await inp.type(digit, delay=random.randint(50, 100))
                                        filled_status["card"] = True
                                        print(f"[FILL] ✓ Card: {card['number']}")
                                        break
                                except:
                                    continue
                        
                        # Expiry
                        if not filled_status["expiry"]:
                            exp_inputs = await frame.query_selector_all('input')
                            for inp in exp_inputs:
                                try:
                                    placeholder = (await inp.get_attribute('placeholder') or '').lower()
                                    if 'mm' in placeholder or 'expir' in placeholder:
                                        await inp.click()
                                        exp_string = f"{card['month']}{card['year'][-2:]}"
                                        for char in exp_string:
                                            await inp.type(char, delay=random.randint(50, 100))
                                        filled_status["expiry"] = True
                                        print(f"[FILL] ✓ Expiry: {card['month']}/{card['year'][-2:]}")
                                        break
                                except:
                                    continue
                        
                        # CVC
                        if not filled_status["cvc"]:
                            cvc_inputs = await frame.query_selector_all('input')
                            for inp in cvc_inputs:
                                try:
                                    placeholder = (await inp.get_attribute('placeholder') or '').lower()
                                    if 'cvc' in placeholder or 'cvv' in placeholder:
                                        await inp.click()
                                        for digit in card['cvv']:
                                            await inp.type(digit, delay=random.randint(50, 100))
                                        filled_status["cvc"] = True
                                        print(f"[FILL] ✓ CVC: {card['cvv']}")
                                        break
                                except:
                                    continue
                    except:
                        continue
            except TargetClosedError:
                return {"error": "Browser closed during card fill"}
            
            print(f"[STATUS] {filled_status}")
            
            if not await safe_wait(page, 2000):
                return {"error": "Browser closed before submit"}
            
            # === HANDLE HCAPTCHA ===
            try:
                await handle_hcaptcha_advanced(page)
            except:
                pass
            
            if not await safe_wait(page, 1000):
                return {"error": "Browser closed after captcha"}
            
            # === SUBMIT ===
            print("[SUBMIT] Attempting...")
            submit_attempted = False
            
            try:
                for selector in ['button[type="submit"]:visible', 'button.SubmitButton:visible']:
                    try:
                        btn = page.locator(selector).first
                        if await btn.count() > 0 and await btn.is_visible():
                            await btn.click()
                            submit_attempted = True
                            print("[SUBMIT] ✓ Clicked")
                            break
                    except:
                        continue
                
                if not submit_attempted:
                    await page.keyboard.press('Enter')
                    submit_attempted = True
                    print("[SUBMIT] ✓ Enter pressed")
            except TargetClosedError:
                return {"error": "Browser closed during submit"}
            
            # === WAIT FOR RESPONSE ===
            print("[WAIT] Monitoring for response...")
            
            start_time = time.time()
            max_wait = CONFIG["RESPONSE_TIMEOUT_SECONDS"]
            
            while time.time() - start_time < max_wait:
                # Check if page still alive
                try:
                    await page.evaluate('1')
                except:
                    print("[WARNING] Browser connection lost")
                    browser_closed = True
                    break
                
                if stripe_result.get("payment_confirmed"):
                    print("[✓ SUCCESS] Payment confirmed!")
                    break
                
                if stripe_result.get("error"):
                    print(f"[✗ ERROR] {stripe_result['error']}")
                    break
                
                if stripe_result.get("requires_3ds"):
                    print("[⚠ 3DS] Required")
                    break
                
                try:
                    current_url = page.url
                    if any(x in current_url.lower() for x in ['success', 'thank', 'complete']):
                        stripe_result["payment_confirmed"] = True
                        stripe_result["success"] = True
                        print(f"[✓ SUCCESS] Redirect detected")
                        break
                except:
                    pass
                
                await asyncio.sleep(0.5)
            
            print(f"\n[SUMMARY] Captured {len(stripe_result['raw_responses'])} Stripe responses")
            
            # === RETURN RESULT ===
            if stripe_result.get("payment_confirmed"):
                return {
                    "success": True,
                    "message": stripe_result.get("message", "Payment successful"),
                    "card": f"{card['number']}|{card['month']}|{card['year']}|{card['cvv']}",
                    "token_id": stripe_result.get("token_id"),
                    "payment_intent_id": stripe_result.get("payment_intent_id"),
                    "raw_responses": stripe_result["raw_responses"]
                }
            elif stripe_result.get("requires_3ds"):
                return {
                    "success": False,
                    "requires_3ds": True,
                    "message": "3D Secure required",
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
                        "submit_attempted": submit_attempted,
                        "browser_closed": browser_closed,
                        "responses_captured": len(stripe_result["raw_responses"])
                    },
                    "raw_responses": stripe_result["raw_responses"]
                }
                
        except TargetClosedError:
            return {
                "error": "Browserless session closed (timeout or limit reached)",
                "raw_responses": stripe_result.get("raw_responses", [])
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
            # Clean up
            if cdp:
                try:
                    await cdp.detach()
                except:
                    pass
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
            print("[CLEANUP] ✓ Done")

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
    
    print(f"\n{'='*80}")
    print(f"[REQUEST] {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"[URL] {url[:100]}...")
    print(f"[CC] {cc}")
    print('='*80)
    
    if not url or not cc:
        return jsonify({"error": "Missing required parameters: url and cc"}), 400
    
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    try:
        result = loop.run_until_complete(run_stripe_automation(url, cc, email))
        
        print(f"\n[RESULT] {'SUCCESS' if result.get('success') else 'FAILED'}")
        
        if result.get('success'):
            return jsonify(result), 200
        else:
            return jsonify(result), 400
        
    except Exception as e:
        print(f"[SERVER ERROR] {str(e)}")
        return jsonify({"error": "Server error", "details": str(e)}), 500
    finally:
        loop.close()

@app.route('/status', methods=['GET'])
def status_endpoint():
    """Health check"""
    return jsonify({
        "status": "online",
        "version": "2.3-browserless-optimized",
        "timestamp": datetime.now().isoformat()
    })

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5001))
    print("="*80)
    print(f"[SERVER] Stripe Automation v2.3")
    print(f"[PORT] {port}")
    print(f"[MODE] Browserless Optimized")
    print("="*80)
    app.run(host='0.0.0.0', port=port, debug=False)
