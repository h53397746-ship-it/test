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
    first_four = bin_str[:4] if len(bin_str) >= 4 else ""
    
    if first_two in ['34', '37']:
        return 15
    if first_two == '36' or first_two == '38':
        return 14
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

# === HCAPTCHA HANDLER ===
async def handle_hcaptcha(page):
    """Attempt to handle hCaptcha by clicking checkbox and randomly selecting images if needed"""
    try:
        print("[HCAPTCHA] Looking for hCaptcha iframe...")
        
        # Wait a bit for hCaptcha to load
        await page.wait_for_timeout(2000)
        
        # Find hCaptcha checkbox iframe
        hcaptcha_checkbox_frame = None
        for frame in page.frames:
            if 'hcaptcha.com/captcha' in frame.url and 'checkbox' in frame.url:
                hcaptcha_checkbox_frame = frame
                print(f"[HCAPTCHA] Found checkbox frame: {frame.url[:50]}...")
                break
        
        if hcaptcha_checkbox_frame:
            # Click the checkbox
            try:
                checkbox = await hcaptcha_checkbox_frame.query_selector('#checkbox')
                if checkbox:
                    await checkbox.click()
                    print("[HCAPTCHA] Clicked checkbox")
                    
                    # Wait to see if it auto-solves
                    await page.wait_for_timeout(3000)
                    
                    # Check if challenge frame appeared
                    challenge_frame = None
                    for frame in page.frames:
                        if 'hcaptcha.com/captcha' in frame.url and 'challenge' in frame.url:
                            challenge_frame = frame
                            print("[HCAPTCHA] Challenge frame appeared - attempting to solve...")
                            break
                    
                    if challenge_frame:
                        # Look for image grid
                        await page.wait_for_timeout(1000)
                        
                        # Try to find and click random images
                        try:
                            # Get all clickable image elements
                            images = await challenge_frame.query_selector_all('.task-image')
                            if images:
                                print(f"[HCAPTCHA] Found {len(images)} images in challenge")
                                
                                # Randomly select 3-4 images
                                num_to_select = random.randint(3, min(4, len(images)))
                                selected_indices = random.sample(range(len(images)), num_to_select)
                                
                                for idx in selected_indices:
                                    await images[idx].click()
                                    print(f"[HCAPTCHA] Clicked image {idx + 1}")
                                    await page.wait_for_timeout(random.randint(500, 1000))
                                
                                # Look for submit/continue button
                                submit_button = await challenge_frame.query_selector('.button-submit')
                                if not submit_button:
                                    submit_button = await challenge_frame.query_selector('.button')
                                if not submit_button:
                                    submit_button = await challenge_frame.query_selector('[role="button"]')
                                
                                if submit_button:
                                    await submit_button.click()
                                    print("[HCAPTCHA] Clicked submit button")
                                    await page.wait_for_timeout(2000)
                                    
                                    # Check if we need to solve another challenge
                                    new_images = await challenge_frame.query_selector_all('.task-image')
                                    if new_images and len(new_images) > 0:
                                        print("[HCAPTCHA] New challenge appeared, solving again...")
                                        # Repeat the process once more
                                        num_to_select = random.randint(2, min(3, len(new_images)))
                                        selected_indices = random.sample(range(len(new_images)), num_to_select)
                                        
                                        for idx in selected_indices:
                                            await new_images[idx].click()
                                            await page.wait_for_timeout(random.randint(500, 1000))
                                        
                                        submit_button = await challenge_frame.query_selector('.button-submit')
                                        if submit_button:
                                            await submit_button.click()
                                            print("[HCAPTCHA] Submitted second challenge")
                                
                        except Exception as e:
                            print(f"[HCAPTCHA] Error solving challenge: {e}")
                    else:
                        print("[HCAPTCHA] No challenge - possibly auto-solved!")
                        return True
                        
            except Exception as e:
                print(f"[HCAPTCHA] Error clicking checkbox: {e}")
        else:
            print("[HCAPTCHA] No hCaptcha checkbox found")
            
        return False
        
    except Exception as e:
        print(f"[HCAPTCHA] Error handling hCaptcha: {e}")
        return False

# === MAIN AUTOMATION FUNCTION ===
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
        "hcaptcha_handled": False
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
                    slow_mo=150,  # Slower for more human-like behavior
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
                    print("[BROWSER] Connected to Browserless successfully")
                except Exception as e:
                    if "429" in str(e):
                        print("[ERROR] Browserless rate limit hit. Using local browser...")
                        browser = await p.chromium.launch(headless=True)
                    else:
                        raise e
            
            # Create context
            context = await browser.new_context(
                viewport={'width': 1920, 'height': 1080},
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36',
                locale='en-US',
                timezone_id='America/New_York',
                extra_http_headers={
                    'Accept-Language': 'en-US,en;q=0.9',
                    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8'
                }
            )
            
            page = await context.new_page()
            
            # Enhanced response capture
            async def capture_response(response):
                try:
                    url_lower = response.url.lower()
                    
                    # Important endpoints to capture
                    important_endpoints = [
                        '/v1/payment_intents',
                        '/v1/tokens',
                        '/v1/payment_methods',
                        '/v1/confirm',
                        '/v1/customers',
                        '/v1/charges',
                        '/v1/3ds2/',
                        '/v1/elements/sessions',
                        'hcaptcha.com'
                    ]
                    
                    if any(endpoint in url_lower for endpoint in important_endpoints):
                        print(f"\n[CAPTURED] {response.url[:100]}...")
                        print(f"[STATUS] {response.status}")
                        
                        try:
                            body = await response.body()
                            text = body.decode('utf-8', errors='ignore')
                            
                            try:
                                data = json.loads(text)
                                print(f"[RAW RESPONSE] {json.dumps(data, indent=2)[:1000]}...")
                                
                                stripe_result["raw_responses"].append({
                                    "url": response.url,
                                    "status": response.status,
                                    "data": data,
                                    "timestamp": datetime.now().isoformat()
                                })
                                
                                # Check for payment confirmation
                                if 'payment_intent' in url_lower or 'confirm' in url_lower:
                                    if response.status == 200:
                                        stripe_result["payment_confirmed"] = True
                                        
                                        if data.get('status') in ['succeeded', 'requires_capture']:
                                            stripe_result["success"] = True
                                            stripe_result["message"] = "Payment successful"
                                        elif data.get('status') == 'requires_action':
                                            stripe_result["requires_3ds"] = True
                                            stripe_result["message"] = "3D Secure required"
                                        elif data.get('error'):
                                            stripe_result["error"] = data.get('error', {}).get('message', 'Unknown error')
                                            stripe_result["decline_code"] = data.get('error', {}).get('decline_code')
                                
                                # Check for token creation
                                if '/v1/tokens' in url_lower and response.status == 200:
                                    stripe_result["token_created"] = True
                                    stripe_result["token_id"] = data.get('id')
                                    print(f"[TOKEN] Created: {data.get('id')}")
                                    
                            except json.JSONDecodeError:
                                if len(text) < 1000:
                                    print(f"[RAW TEXT] {text}")
                                    
                        except Exception as e:
                            print(f"[ERROR READING] {e}")
                            
                except Exception as e:
                    print(f"[CAPTURE ERROR] {e}")
            
            page.on("response", capture_response)
            
            # Navigate to page
            print(f"\n[NAVIGATE] Loading: {url}")
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            
            # Wait for page to stabilize
            await page.wait_for_timeout(3000)
            
            # Fill email in main page first
            print("\n[FORM] Starting to fill email...")
            email_filled = False
            email_selectors = [
                'input[type="email"]',
                'input[name="email"]',
                'input[id*="email" i]',
                'input[placeholder*="email" i]'
            ]
            
            for selector in email_selectors:
                try:
                    email_field = await page.query_selector(selector)
                    if email_field and await email_field.is_visible():
                        await email_field.click()
                        await email_field.fill("")  # Clear first
                        await email_field.type(email, delay=50)
                        print(f"[FILLED] Email field")
                        email_filled = True
                        
                        # Press Tab to trigger any validation
                        await page.keyboard.press('Tab')
                        await page.wait_for_timeout(1000)
                        break
                except:
                    continue
            
            # Wait for Stripe Elements to load
            print("[WAITING] Waiting for Stripe Elements to load...")
            await page.wait_for_timeout(3000)
            
            # Get all frames
            frames = page.frames
            print(f"[FRAMES] Found {len(frames)} frames on page")
            
            # Look for Stripe frames
            stripe_frames = []
            for frame in frames:
                frame_url = frame.url
                if 'stripe' in frame_url.lower() or 'checkout.stripe' in frame_url.lower():
                    stripe_frames.append(frame)
                    print(f"[STRIPE FRAME] {frame_url[:80]}...")
            
            # Fill card details
            card_filled = False
            expiry_filled = False
            cvc_filled = False
            postal_filled = False
            
            # Try each Stripe frame
            for frame in stripe_frames:
                print(f"\n[FRAME] Filling card details in Stripe frame...")
                
                # Card number
                if not card_filled:
                    card_selectors = [
                        'input[name="cardnumber"]',
                        'input[name="cardNumber"]',
                        'input[placeholder*="1234" i]',
                        'input[placeholder*="Card number" i]',
                        'input[data-elements-stable-field-name="cardNumber"]',
                        '#Field-numberInput'
                    ]
                    
                    for selector in card_selectors:
                        try:
                            element = await frame.query_selector(selector)
                            if element:
                                await element.click()
                                await page.wait_for_timeout(100)
                                
                                # Type card number with human-like delays
                                for digit in card['number']:
                                    await element.type(digit, delay=random.randint(50, 100))
                                
                                print(f"[FILLED] Card number")
                                card_filled = True
                                await page.keyboard.press('Tab')
                                await page.wait_for_timeout(500)
                                break
                        except:
                            continue
                
                # Expiry
                if not expiry_filled:
                    expiry_selectors = [
                        'input[name="exp-date"]',
                        'input[name="cardExpiry"]',
                        'input[placeholder*="MM / YY" i]',
                        'input[placeholder*="MM/YY" i]',
                        'input[data-elements-stable-field-name="cardExpiry"]',
                        '#Field-expiryInput'
                    ]
                    
                    for selector in expiry_selectors:
                        try:
                            element = await frame.query_selector(selector)
                            if element:
                                await element.click()
                                await page.wait_for_timeout(100)
                                
                                expiry_str = f"{card['month']}/{card['year'][-2:]}"
                                for char in expiry_str:
                                    await element.type(char, delay=random.randint(50, 100))
                                
                                print(f"[FILLED] Expiry date")
                                expiry_filled = True
                                await page.keyboard.press('Tab')
                                await page.wait_for_timeout(500)
                                break
                        except:
                            continue
                
                # CVC
                if not cvc_filled:
                    cvc_selectors = [
                        'input[name="cvc"]',
                        'input[name="cardCvc"]',
                        'input[placeholder*="CVC" i]',
                        'input[placeholder*="CVV" i]',
                        'input[data-elements-stable-field-name="cardCvc"]',
                        '#Field-cvcInput'
                    ]
                    
                    for selector in cvc_selectors:
                        try:
                            element = await frame.query_selector(selector)
                            if element:
                                await element.click()
                                await page.wait_for_timeout(100)
                                
                                for digit in card['cvv']:
                                    await element.type(digit, delay=random.randint(50, 100))
                                
                                print(f"[FILLED] CVV/CVC")
                                cvc_filled = True
                                await page.keyboard.press('Tab')
                                await page.wait_for_timeout(500)
                                break
                        except:
                            continue
                
                # Postal code
                if not postal_filled:
                    postal_selectors = [
                        'input[name="postalCode"]',
                        'input[name="postal"]',
                        'input[placeholder*="ZIP" i]',
                        'input[placeholder*="Postal" i]',
                        '#Field-postalCodeInput'
                    ]
                    
                    for selector in postal_selectors:
                        try:
                            element = await frame.query_selector(selector)
                            if element:
                                await element.click()
                                await element.fill('10001')
                                print(f"[FILLED] Postal code")
                                postal_filled = True
                                break
                        except:
                            continue
            
            print(f"\n[FORM STATUS] Email: {email_filled}, Card: {card_filled}, Expiry: {expiry_filled}, CVC: {cvc_filled}, Postal: {postal_filled}")
            
            # Wait before attempting to submit
            await page.wait_for_timeout(2000)
            
            # Handle hCaptcha before submitting
            print("\n[HCAPTCHA] Checking for hCaptcha...")
            hcaptcha_handled = await handle_hcaptcha(page)
            stripe_result["hcaptcha_handled"] = hcaptcha_handled
            
            # Wait a bit after hCaptcha handling
            await page.wait_for_timeout(2000)
            
            # Try to submit the form
            print("\n[SUBMIT] Looking for submit button...")
            submit_selectors = [
                'button[type="submit"]',
                'button:has-text("Pay")',
                'button:has-text("Subscribe")',
                'button:has-text("Complete")',
                'button:has-text("Confirm")',
                '.SubmitButton',
                'button.SubmitButton',
                'button[data-testid="hosted-payment-submit-button"]',
                'button.Button--primary'
            ]
            
            submit_clicked = False
            for selector in submit_selectors:
                try:
                    # Try in main page
                    buttons = await page.query_selector_all(selector)
                    for button in buttons:
                        if await button.is_visible():
                            # Scroll to button
                            await button.scroll_into_view_if_needed()
                            await page.wait_for_timeout(500)
                            
                            # Click the button
                            await button.click()
                            print(f"[CLICKED] Submit button: {selector}")
                            submit_clicked = True
                            break
                    
                    if submit_clicked:
                        break
                        
                except Exception as e:
                    continue
            
            if not submit_clicked:
                # Try with force click
                print("[RETRY] Trying force click on submit...")
                try:
                    submit_button = page.locator('button[type="submit"]').first
                    await submit_button.click(force=True)
                    submit_clicked = True
                    print("[CLICKED] Submit via force click")
                except:
                    pass
            
            # If still not clicked, try clicking any visible button
            if not submit_clicked:
                print("[RETRY] Clicking any visible button...")
                try:
                    all_buttons = await page.query_selector_all('button')
                    for button in all_buttons:
                        text = await button.text_content()
                        if text and any(word in text.lower() for word in ['pay', 'submit', 'complete', 'confirm']):
                            await button.click()
                            print(f"[CLICKED] Button with text: {text}")
                            submit_clicked = True
                            break
                except:
                    pass
            
            if submit_clicked:
                # Wait for processing
                print("\n[PROCESSING] Waiting for payment processing...")
                await page.wait_for_timeout(5000)
                
                # Check if hCaptcha appeared after submit
                if not hcaptcha_handled:
                    print("[HCAPTCHA] Checking again after submit...")
                    hcaptcha_handled = await handle_hcaptcha(page)
                    if hcaptcha_handled:
                        # Try to submit again after hCaptcha
                        await page.wait_for_timeout(2000)
                        for selector in submit_selectors:
                            try:
                                button = page.locator(selector).first
                                if await button.is_visible(timeout=1000):
                                    await button.click()
                                    print(f"[CLICKED] Submit again after hCaptcha")
                                    break
                            except:
                                continue
            
            # Wait for Stripe response
            print("\n[WAITING] Waiting for payment confirmation...")
            start_time = time.time()
            
            while time.time() - start_time < CONFIG["RESPONSE_TIMEOUT_SECONDS"]:
                # Check if we got a token or payment confirmation
                if stripe_result.get("token_created") or stripe_result.get("payment_confirmed"):
                    print("[SUCCESS] Payment token or confirmation received!")
                    break
                
                # Check for 3DS redirect
                current_url = page.url
                if any(term in current_url.lower() for term in ['3ds', 'three-d-secure', 'authenticate']):
                    print("[3DS] Three-D Secure authentication detected")
                    stripe_result["requires_3ds"] = True
                    break
                
                # Check for success/error messages on page
                try:
                    # Success indicators
                    success_texts = ['success', 'thank you', 'payment successful', 'confirmed', 'complete']
                    page_text = await page.content()
                    page_text_lower = page_text.lower()
                    
                    for success_text in success_texts:
                        if success_text in page_text_lower:
                            print(f"[SUCCESS] Found '{success_text}' in page content")
                            stripe_result["success"] = True
                            stripe_result["message"] = f"Payment appears successful (found: {success_text})"
                            break
                    
                    # Check for specific error messages
                    error_element = await page.query_selector('[role="alert"], .StripeError, .error-message')
                    if error_element:
                        error_text = await error_element.text_content()
                        print(f"[ERROR] Found error message: {error_text}")
                        stripe_result["error"] = error_text
                        break
                        
                except:
                    pass
                
                await asyncio.sleep(0.5)
            
            # Take screenshot for debugging
            if CONFIG["RUN_LOCAL"]:
                screenshot = await page.screenshot(full_page=True)
                print(f"[SCREENSHOT] Captured final state ({len(screenshot)} bytes)")
            
            # Prepare final response
            if stripe_result.get("success"):
                return {
                    "success": True,
                    "message": stripe_result.get("message", "Payment successful"),
                    "token_id": stripe_result.get("token_id"),
                    "hcaptcha_handled": stripe_result.get("hcaptcha_handled"),
                    "raw_responses": stripe_result["raw_responses"]
                }
            elif stripe_result.get("token_created"):
                return {
                    "success": True,
                    "message": "Token created successfully",
                    "token_id": stripe_result.get("token_id"),
                    "hcaptcha_handled": stripe_result.get("hcaptcha_handled"),
                    "raw_responses": stripe_result["raw_responses"]
                }
            elif stripe_result.get("error"):
                return {
                    "success": False,
                    "error": stripe_result.get("error"),
                    "decline_code": stripe_result.get("decline_code"),
                    "hcaptcha_handled": stripe_result.get("hcaptcha_handled"),
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
                # No clear result - check raw responses for clues
                has_token = any('tokens' in r.get('url', '') for r in stripe_result.get("raw_responses", []))
                has_intent = any('payment_intent' in r.get('url', '') for r in stripe_result.get("raw_responses", []))
                
                return {
                    "success": False,
                    "message": "Payment processing unclear",
                    "details": {
                        "submit_clicked": submit_clicked,
                        "hcaptcha_handled": stripe_result.get("hcaptcha_handled"),
                        "token_attempt": has_token,
                        "payment_intent_attempt": has_intent,
                        "filled_fields": {
                            "email": email_filled,
                            "card": card_filled,
                            "expiry": expiry_filled,
                            "cvc": cvc_filled,
                            "postal": postal_filled
                        }
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

# === API ENDPOINTS ===
@app.route('/hrkXstripe', methods=['GET'])
def stripe_endpoint():
    # Check rate limit
    can_proceed, wait_time = rate_limit_check()
    if not can_proceed:
        return jsonify({
            "error": "Rate limit exceeded",
            "retry_after": f"{wait_time:.1f} seconds",
            "message": f"Maximum {CONFIG['RATE_LIMIT_REQUESTS']} requests per {CONFIG['RATE_LIMIT_WINDOW']} seconds"
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
    
    # Run async function
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
            "requests_remaining": CONFIG["RATE_LIMIT_REQUESTS"] - len(request_timestamps),
            "window_seconds": CONFIG["RATE_LIMIT_WINDOW"]
        },
        "config": {
            "run_local": CONFIG["RUN_LOCAL"],
            "has_api_key": bool(CONFIG["BROWSERLESS_API_KEY"])
        },
        "timestamp": datetime.now().isoformat()
    })

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5001))
    print(f"Starting server on port {port}...")
    app.run(host='0.0.0.0', port=port, debug=True)
