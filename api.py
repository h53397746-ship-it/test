import os
import asyncio
import json
import random
import re
import time
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
    "RETRY_DELAY": 7000,
    "RATE_LIMIT_REQUESTS": 5,
    "RATE_LIMIT_WINDOW": 60,
    "MAX_RETRIES": 3,
    "RETRY_BACKOFF": 5
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

# === COMPREHENSIVE AUTOMATION FUNCTION ===
async def run_stripe_automation(url, cc_string, email=None):
    card = process_card_input(cc_string)
    if not card:
        return {"error": "Invalid card format. Use: number|month|year|cvv"}
    
    if not email:
        email = f"test{random.randint(1000,9999)}@example.com"
    
    print(f"\n{'='*60}")
    print(f"[INFO] Starting automation...")
    print(f"[CARD] {card['number']} | {card['month']}/{card['year']} | CVV: {card['cvv']}")
    print(f"[EMAIL] {email}")
    print('='*60)
    
    stripe_result = {
        "status": "pending",
        "raw_responses": [],
        "payment_confirmed": False,
        "token_created": False,
        "payment_intent_created": False,
        "hcaptcha_passes": []
    }
    
    async with async_playwright() as p:
        browser = None
        context = None
        page = None
        
        try:
            # Connect to browser
            if CONFIG["RUN_LOCAL"]:
                print("[BROWSER] Launching local browser...")
                browser = await p.chromium.launch(
                    headless=False,
                    slow_mo=150,
                    args=[
                        '--disable-blink-features=AutomationControlled',
                        '--disable-features=IsolateOrigins,site-per-process',
                        '--disable-web-security',
                        '--window-size=1920,1080',
                        '--disable-dev-shm-usage',
                        '--no-sandbox'
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
            
            # Enhanced response capture
            async def capture_response(response):
                try:
                    url_lower = response.url.lower()
                    
                    # Important endpoints
                    important_endpoints = [
                        '/v1/payment_intents',
                        '/v1/tokens',
                        '/v1/payment_methods',
                        '/v1/confirm',
                        '/v1/charges',
                        '/v1/customers',
                        'hcaptcha.com/getcaptcha',
                        '/v1/checkout/sessions',
                        '/v1/three_d_secure'
                    ]
                    
                    if any(endpoint in url_lower for endpoint in important_endpoints):
                        print(f"\n[CAPTURED] {response.url[:100]}...")
                        print(f"[STATUS] {response.status}")
                        
                        try:
                            body = await response.body()
                            text = body.decode('utf-8', errors='ignore')
                            
                            try:
                                data = json.loads(text)
                                
                                # Store raw response
                                stripe_result["raw_responses"].append({
                                    "url": response.url,
                                    "status": response.status,
                                    "data": data,
                                    "timestamp": datetime.now().isoformat()
                                })
                                
                                # Print relevant parts
                                if 'error' in data:
                                    print(f"[ERROR IN RESPONSE] {data['error']}")
                                    stripe_result["error"] = data['error'].get('message', 'Unknown error')
                                    stripe_result["decline_code"] = data['error'].get('decline_code')
                                
                                # Check for token creation
                                if '/v1/tokens' in url_lower and response.status == 200:
                                    stripe_result["token_created"] = True
                                    stripe_result["token_id"] = data.get('id')
                                    print(f"[TOKEN CREATED] {data.get('id')}")
                                
                                # Check for payment intent
                                if 'payment_intent' in url_lower and response.status == 200:
                                    stripe_result["payment_intent_created"] = True
                                    if data.get('status') in ['succeeded', 'requires_capture', 'processing']:
                                        stripe_result["payment_confirmed"] = True
                                        stripe_result["success"] = True
                                        stripe_result["message"] = f"Payment {data.get('status')}"
                                        print(f"[PAYMENT CONFIRMED] Status: {data.get('status')}")
                                
                                # Check for checkout session
                                if 'checkout/sessions' in url_lower and response.status == 200:
                                    if data.get('payment_status') == 'paid':
                                        stripe_result["payment_confirmed"] = True
                                        stripe_result["success"] = True
                                        print("[PAYMENT CONFIRMED] Checkout session paid")
                                
                                # Track hCaptcha passes
                                if 'hcaptcha.com/getcaptcha' in url_lower and data.get('pass'):
                                    stripe_result["hcaptcha_passes"].append(data.get('generated_pass_UUID'))
                                    print(f"[HCAPTCHA] Pass received")
                                    
                            except json.JSONDecodeError:
                                pass
                                    
                        except Exception as e:
                            print(f"[ERROR READING] {e}")
                            
                except Exception as e:
                    print(f"[CAPTURE ERROR] {e}")
            
            # Capture network requests for debugging
            async def capture_request(request):
                if 'stripe.com/v1' in request.url.lower():
                    print(f"[REQUEST] {request.method} {request.url[:100]}...")
                    if request.method == "POST":
                        try:
                            post_data = request.post_data
                            if post_data:
                                print(f"[POST DATA] {post_data[:200]}...")
                        except:
                            pass
            
            page.on("response", capture_response)
            page.on("request", capture_request)
            
            # Navigate to page
            print(f"\n[NAVIGATE] Loading: {url}")
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            
            # Wait for initial load
            await page.wait_for_timeout(3000)
            
            # Take initial screenshot
            if CONFIG["RUN_LOCAL"]:
                await page.screenshot(path="initial.png")
                print("[SCREENSHOT] Initial page captured")
            
            # APPROACH 1: Fill email first
            print("\n[APPROACH 1] Filling email...")
            email_filled = False
            
            # Try multiple email selectors
            email_selectors = [
                'input[type="email"]',
                'input[name="email"]',
                '#email',
                'input[placeholder*="email" i]',
                'input[id*="email" i]'
            ]
            
            for selector in email_selectors:
                try:
                    elements = await page.query_selector_all(selector)
                    for element in elements:
                        if await element.is_visible():
                            await element.click()
                            await element.fill("")
                            await element.type(email, delay=50)
                            await page.keyboard.press('Tab')
                            email_filled = True
                            print(f"[FILLED] Email using {selector}")
                            break
                    if email_filled:
                        break
                except:
                    continue
            
            # Wait for Stripe Elements to load
            await page.wait_for_timeout(3000)
            
            # APPROACH 2: Fill card details in Stripe iframes
            print("\n[APPROACH 2] Looking for Stripe iframes...")
            
            filled_status = {
                "card": False,
                "expiry": False,
                "cvc": False,
                "postal": False
            }
            
            # Get all frames
            frames = page.frames
            print(f"[FRAMES] Found {len(frames)} frames")
            
            # Identify Stripe frames by URL
            stripe_frames = []
            for frame in frames:
                if any(x in frame.url.lower() for x in ['stripe', 'checkout.stripe', 'js.stripe']):
                    stripe_frames.append(frame)
                    print(f"[STRIPE FRAME] {frame.url[:80]}...")
            
            # Try to fill in each Stripe frame
            for frame in stripe_frames:
                try:
                    # Card number
                    if not filled_status["card"]:
                        card_inputs = await frame.query_selector_all('input')
                        for inp in card_inputs:
                            try:
                                placeholder = await inp.get_attribute('placeholder')
                                name = await inp.get_attribute('name')
                                data_elements = await inp.get_attribute('data-elements-stable-field-name')
                                
                                if any(x for x in ['cardnumber', 'card number', '1234'] if x in (placeholder or '').lower()) or \
                                   name in ['cardnumber', 'cardNumber'] or \
                                   data_elements == 'cardNumber':
                                    await inp.click()
                                    for digit in card['number']:
                                        await inp.type(digit, delay=random.randint(50, 100))
                                    filled_status["card"] = True
                                    print("[FILLED] Card number in iframe")
                                    await page.keyboard.press('Tab')
                                    break
                            except:
                                continue
                    
                    # Expiry
                    if not filled_status["expiry"]:
                        exp_inputs = await frame.query_selector_all('input')
                        for inp in exp_inputs:
                            try:
                                placeholder = await inp.get_attribute('placeholder')
                                name = await inp.get_attribute('name')
                                
                                if any(x for x in ['mm', 'expiry', 'exp'] if x in (placeholder or '').lower()) or \
                                   name in ['exp-date', 'cardExpiry', 'cc-exp']:
                                    await inp.click()
                                    exp_string = f"{card['month']}/{card['year'][-2:]}"
                                    for char in exp_string:
                                        await inp.type(char, delay=random.randint(50, 100))
                                    filled_status["expiry"] = True
                                    print("[FILLED] Expiry in iframe")
                                    await page.keyboard.press('Tab')
                                    break
                            except:
                                continue
                    
                    # CVC
                    if not filled_status["cvc"]:
                        cvc_inputs = await frame.query_selector_all('input')
                        for inp in cvc_inputs:
                            try:
                                placeholder = await inp.get_attribute('placeholder')
                                name = await inp.get_attribute('name')
                                
                                if any(x for x in ['cvc', 'cvv', 'security'] if x in (placeholder or '').lower()) or \
                                   name in ['cvc', 'cardCvc', 'cc-csc']:
                                    await inp.click()
                                    for digit in card['cvv']:
                                        await inp.type(digit, delay=random.randint(50, 100))
                                    filled_status["cvc"] = True
                                    print("[FILLED] CVC in iframe")
                                    await page.keyboard.press('Tab')
                                    break
                            except:
                                continue
                    
                    # Postal code
                    if not filled_status["postal"]:
                        postal_inputs = await frame.query_selector_all('input')
                        for inp in postal_inputs:
                            try:
                                placeholder = await inp.get_attribute('placeholder')
                                name = await inp.get_attribute('name')
                                
                                if any(x for x in ['zip', 'postal'] if x in (placeholder or '').lower()) or \
                                   name in ['postalCode', 'postal', 'zip']:
                                    await inp.click()
                                    await inp.fill('10001')
                                    filled_status["postal"] = True
                                    print("[FILLED] Postal code in iframe")
                                    break
                            except:
                                continue
                                
                except Exception as e:
                    print(f"[FRAME ERROR] {e}")
                    continue
            
            # APPROACH 3: Use JavaScript injection as fallback
            if not all(filled_status.values()):
                print("\n[APPROACH 3] Using JavaScript injection...")
                
                for frame in page.frames:
                    try:
                        # Try to fill using JavaScript
                        if not filled_status["card"]:
                            await frame.evaluate(f'''
                                const inputs = document.querySelectorAll('input');
                                inputs.forEach(input => {{
                                    if (input.placeholder && input.placeholder.toLowerCase().includes('card number')) {{
                                        input.value = '{card["number"]}';
                                        input.dispatchEvent(new Event('input', {{ bubbles: true }}));
                                        input.dispatchEvent(new Event('change', {{ bubbles: true }}));
                                    }}
                                }});
                            ''')
                        
                        if not filled_status["expiry"]:
                            await frame.evaluate(f'''
                                const inputs = document.querySelectorAll('input');
                                inputs.forEach(input => {{
                                    if (input.placeholder && input.placeholder.toLowerCase().includes('mm')) {{
                                        input.value = '{card["month"]}/{card["year"][-2:]}';
                                        input.dispatchEvent(new Event('input', {{ bubbles: true }}));
                                        input.dispatchEvent(new Event('change', {{ bubbles: true }}));
                                    }}
                                }});
                            ''')
                        
                        if not filled_status["cvc"]:
                            await frame.evaluate(f'''
                                const inputs = document.querySelectorAll('input');
                                inputs.forEach(input => {{
                                    if (input.placeholder && (input.placeholder.toLowerCase().includes('cvc') || input.placeholder.toLowerCase().includes('cvv'))) {{
                                        input.value = '{card["cvv"]}';
                                        input.dispatchEvent(new Event('input', {{ bubbles: true }}));
                                        input.dispatchEvent(new Event('change', {{ bubbles: true }}));
                                    }}
                                }});
                            ''')
                        
                    except:
                        continue
            
            print(f"\n[FILL STATUS] {filled_status}")
            
            # Wait before hCaptcha
            await page.wait_for_timeout(2000)
            
            # APPROACH 4: Handle hCaptcha
            print("\n[APPROACH 4] Handling hCaptcha...")
            hcaptcha_solved = False
            
            # Look for hCaptcha checkbox
            for frame in page.frames:
                if 'hcaptcha.com' in frame.url and 'checkbox' in frame.url:
                    print("[HCAPTCHA] Found checkbox iframe")
                    try:
                        # Click checkbox
                        checkbox = await frame.query_selector('#checkbox')
                        if checkbox:
                            await checkbox.click()
                            print("[HCAPTCHA] Clicked checkbox")
                            
                            # Wait to see if auto-solved
                            await page.wait_for_timeout(5000)
                            
                            # Check for challenge
                            challenge_found = False
                            for cf in page.frames:
                                if 'hcaptcha.com' in cf.url and 'challenge' in cf.url:
                                    challenge_found = True
                                    print("[HCAPTCHA] Challenge appeared, selecting random images...")
                                    
                                    # Wait for images to load
                                    await page.wait_for_timeout(2000)
                                    
                                    # Find and click random images
                                    task_images = await cf.query_selector_all('.task-image, .task, [role="button"]')
                                    if task_images:
                                        print(f"[HCAPTCHA] Found {len(task_images)} images")
                                        # Select random 3-4 images
                                        num_select = random.randint(3, min(4, len(task_images)))
                                        indices = random.sample(range(len(task_images)), num_select)
                                        
                                        for idx in indices:
                                            await task_images[idx].click()
                                            print(f"[HCAPTCHA] Selected image {idx + 1}")
                                            await page.wait_for_timeout(random.randint(500, 1000))
                                        
                                        # Click submit
                                        submit_btns = await cf.query_selector_all('.button-submit, button')
                                        for btn in submit_btns:
                                            btn_text = await btn.text_content()
                                            if 'verify' in (btn_text or '').lower() or 'submit' in (btn_text or '').lower():
                                                await btn.click()
                                                print("[HCAPTCHA] Clicked verify")
                                                await page.wait_for_timeout(3000)
                                                break
                                    break
                            
                            if not challenge_found:
                                print("[HCAPTCHA] Auto-solved!")
                                hcaptcha_solved = True
                                
                    except Exception as e:
                        print(f"[HCAPTCHA ERROR] {e}")
                    break
            
            # Wait after hCaptcha
            await page.wait_for_timeout(2000)
            
            # APPROACH 5: Multiple submit strategies
            print("\n[APPROACH 5] Attempting to submit payment...")
            submit_attempted = False
            
            # Strategy 1: Click visible submit button
            submit_selectors = [
                'button[type="submit"]:visible',
                'button:has-text("Pay"):visible',
                'button:has-text("Submit"):visible',
                'button:has-text("Complete"):visible',
                '.SubmitButton:visible',
                'button[data-testid*="submit"]:visible'
            ]
            
            for selector in submit_selectors:
                try:
                    btn = page.locator(selector).first
                    if await btn.count() > 0:
                        await btn.scroll_into_view_if_needed()
                        await btn.click()
                        submit_attempted = True
                        print(f"[SUBMIT] Clicked button: {selector}")
                        break
                except:
                    continue
            
            # Strategy 2: Submit form directly
            if not submit_attempted:
                try:
                    await page.evaluate('''
                        const forms = document.querySelectorAll('form');
                        forms.forEach(form => {
                            if (form.querySelector('input[type="submit"]') || form.querySelector('button[type="submit"]')) {
                                form.submit();
                            }
                        });
                    ''')
                    submit_attempted = True
                    print("[SUBMIT] Triggered form submit via JavaScript")
                except:
                    pass
            
            # Strategy 3: Press Enter key
            if not submit_attempted:
                try:
                    await page.keyboard.press('Enter')
                    submit_attempted = True
                    print("[SUBMIT] Pressed Enter key")
                except:
                    pass
            
            if submit_attempted:
                # Wait for payment processing
                print("\n[PROCESSING] Waiting for payment to process...")
                await page.wait_for_timeout(10000)
                
                # Check if we need to handle hCaptcha again
                for frame in page.frames:
                    if 'hcaptcha.com' in frame.url and 'checkbox' in frame.url:
                        try:
                            checkbox = await frame.query_selector('#checkbox')
                            if checkbox and await checkbox.is_visible():
                                await checkbox.click()
                                print("[HCAPTCHA] Clicked checkbox after submit")
                                await page.wait_for_timeout(5000)
                        except:
                            pass
                        break
                
                # Try submit again if needed
                for selector in submit_selectors[:3]:
                    try:
                        btn = page.locator(selector).first
                        if await btn.count() > 0 and await btn.is_visible():
                            await btn.click()
                            print(f"[SUBMIT RETRY] Clicked {selector}")
                            break
                    except:
                        continue
            
            # Wait for final response
            print("\n[WAITING] Monitoring for payment confirmation...")
            start_time = time.time()
            
            while time.time() - start_time < CONFIG["RESPONSE_TIMEOUT_SECONDS"]:
                # Check for actual payment processing
                if stripe_result.get("token_created") or stripe_result.get("payment_intent_created") or stripe_result.get("payment_confirmed"):
                    print("[SUCCESS] Payment processing detected!")
                    break
                
                # Check URL for success indicators
                current_url = page.url
                if any(x in current_url.lower() for x in ['success', 'thank', 'confirm', 'complete']):
                    print(f"[URL] Success page detected: {current_url}")
                    stripe_result["success_page"] = True
                
                # Check for 3DS
                if '3d' in current_url.lower() or 'authenticate' in current_url.lower():
                    print("[3DS] Three-D Secure detected")
                    stripe_result["requires_3ds"] = True
                    break
                
                # Check for error messages
                try:
                    error_element = await page.query_selector('[role="alert"], .StripeError, .error-message, .decline-message')
                    if error_element and await error_element.is_visible():
                        error_text = await error_element.text_content()
                        if error_text and len(error_text) > 5:
                            print(f"[ERROR] {error_text}")
                            stripe_result["page_error"] = error_text
                            break
                except:
                    pass
                
                await asyncio.sleep(0.5)
            
            # Take final screenshot
            if CONFIG["RUN_LOCAL"]:
                await page.screenshot(path="final.png", full_page=True)
                print("[SCREENSHOT] Final page captured")
            
            # Prepare comprehensive response
            if stripe_result.get("payment_confirmed") or stripe_result.get("token_created"):
                return {
                    "success": True,
                    "message": stripe_result.get("message", "Payment processed"),
                    "token_id": stripe_result.get("token_id"),
                    "payment_intent": stripe_result.get("payment_intent_created"),
                    "hcaptcha_passes": len(stripe_result.get("hcaptcha_passes", [])),
                    "raw_responses": stripe_result["raw_responses"]
                }
            elif stripe_result.get("error") or stripe_result.get("page_error"):
                return {
                    "success": False,
                    "error": stripe_result.get("error") or stripe_result.get("page_error"),
                    "decline_code": stripe_result.get("decline_code"),
                    "raw_responses": stripe_result["raw_responses"]
                }
            elif stripe_result.get("requires_3ds"):
                return {
                    "success": False,
                    "requires_3ds": True,
                    "message": "3D Secure authentication required",
                    "raw_responses": stripe_result["raw_responses"]
                }
            else:
                # No clear result
                return {
                    "success": False,
                    "message": "Payment not processed - check raw responses",
                    "details": {
                        "email_filled": email_filled,
                        "card_filled": filled_status,
                        "hcaptcha_attempted": hcaptcha_solved,
                        "submit_attempted": submit_attempted,
                        "hcaptcha_passes": len(stripe_result.get("hcaptcha_passes", [])),
                        "responses_captured": len(stripe_result.get("raw_responses", []))
                    },
                    "raw_responses": stripe_result["raw_responses"]
                }
                
        except Exception as e:
            print(f"\n[EXCEPTION] {str(e)}")
            return {
                "error": "Automation failed",
                "details": str(e),
                "raw_responses": stripe_result.get("raw_responses", [])
            }
            
        finally:
            # Clean up
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
            print("[CLEANUP] Browser session closed")

# === API ENDPOINT ===
@app.route('/hrkXstripe', methods=['GET'])
def stripe_endpoint():
    can_proceed, wait_time = rate_limit_check()
    if not can_proceed:
        return jsonify({
            "error": "Rate limit exceeded",
            "retry_after": f"{wait_time:.1f} seconds"
        }), 429
    
    url = request.args.get('url')
    cc = request.args.get('cc')
    email = request.args.get('email')
    
    print(f"\n{'='*60}")
    print(f"NEW REQUEST: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"URL: {url}")
    print(f"CC: {cc}")
    print(f"Email: {email}")
    print('='*60)
    
    if not url or not cc:
        return jsonify({"error": "Missing required parameters: url and cc"}), 400
    
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    try:
        result = loop.run_until_complete(run_stripe_automation(url, cc, email))
        
        print("\n" + "="*60)
        print("FINAL RESULT:")
        print(json.dumps(result, indent=2))
        print("="*60 + "\n")
        
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
    return jsonify({
        "status": "online",
        "rate_limit": {
            "requests_made": len(request_timestamps),
            "requests_remaining": CONFIG["RATE_LIMIT_REQUESTS"] - len(request_timestamps)
        },
        "timestamp": datetime.now().isoformat()
    })

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5001))
    print(f"Starting server on port {port}...")
    app.run(host='0.0.0.0', port=port, debug=True)
