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
    "RESPONSE_TIMEOUT_SECONDS": 15,  # Reduced from 30
    "RETRY_DELAY": 500,  # Reduced from 1000
    "RATE_LIMIT_REQUESTS": 20,  # Increased from 10
    "RATE_LIMIT_WINDOW": 60,
    "MAX_RETRIES": 1,  # Reduced from 2
    "BROWSERLESS_TIMEOUT": 30000,  # Reduced from 60000
    "CAPTCHA_TIMEOUT": 120,
    "ULTRA_FAST_MODE": True,  # New ultra-fast mode
    "LOG_REQUEST_BODIES": False,  # Disabled for speed
    "TYPING_DELAY": 5,  # Ultra fast typing
    "WAIT_MULTIPLIER": 0.3,  # Reduce all waits by 70%
    "PARALLEL_FILL": True  # Fill form fields in parallel
}

app = Flask(__name__)
request_timestamps = deque(maxlen=CONFIG["RATE_LIMIT_REQUESTS"])

# Custom exception for token found
class CaptchaTokenFound(Exception):
    """Raised when captcha token is found"""
    pass

# 2Captcha solver class (optimized)
class TwoCaptchaSolver:
    def __init__(self, api_key):
        self.api_key = api_key
        self.base_url = "http://2captcha.com"
        
    async def solve_hcaptcha(self, sitekey, page_url):
        """Solve HCaptcha using 2captcha service"""
        if not self.api_key:
            return None
            
        print(f"[CAPTCHA] Submitting to 2captcha...")
        
        try:
            async with httpx.AsyncClient(timeout=10) as client:
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
                    return None
                
                captcha_id = result.get('request')
                print(f"[CAPTCHA] Task ID: {captcha_id}")
                
                start_time = time.time()
                check_interval = 3  # Reduced from 5
                while time.time() - start_time < CONFIG["CAPTCHA_TIMEOUT"]:
                    await asyncio.sleep(check_interval)
                    
                    check_url = f"{self.base_url}/res.php?key={self.api_key}&action=get&id={captcha_id}&json=1"
                    response = await client.get(check_url)
                    result = response.json()
                    
                    if result.get('status') == 1:
                        token = result.get('request')
                        print(f"[CAPTCHA] ✓ Solved in {int(time.time() - start_time)}s")
                        return token
                    elif result.get('request') != 'CAPCHA_NOT_READY':
                        return None
                
                return None
                
        except Exception as e:
            return None

# Optimized captcha detection and handling
class CaptchaHandler:
    @staticmethod
    async def detect_hcaptcha(page):
        """Fast HCaptcha detection"""
        try:
            # Check all selectors in parallel
            result = await page.evaluate("""
                () => {
                    return !!(
                        document.querySelector('iframe[src*="hcaptcha.com"]') ||
                        document.querySelector('div[data-hcaptcha-widget-id]') ||
                        document.querySelector('.h-captcha')
                    );
                }
            """)
            return result
        except:
            return False
    
    @staticmethod
    async def get_hcaptcha_sitekey(page):
        """Fast sitekey extraction"""
        try:
            sitekey = await page.evaluate("""
                () => {
                    const element = document.querySelector('[data-sitekey]');
                    if (element) return element.getAttribute('data-sitekey');
                    
                    const iframe = document.querySelector('iframe[src*="hcaptcha.com"]');
                    if (iframe) {
                        const src = iframe.getAttribute('src');
                        const match = src.match(/sitekey=([a-zA-Z0-9-]+)/);
                        if (match) return match[1];
                    }
                    
                    return null;
                }
            """)
            return sitekey
        except:
            return None
    
    @staticmethod
    async def inject_captcha_token(page, token):
        """Fast token injection"""
        try:
            await page.evaluate(f"""
                () => {{
                    const fields = [
                        '[name="h-captcha-response"]',
                        '[name="g-recaptcha-response"]'
                    ];
                    fields.forEach(selector => {{
                        const field = document.querySelector(selector);
                        if (field) {{
                            field.value = '{token}';
                            field.innerHTML = '{token}';
                        }}
                    }});
                    
                    if (window.hcaptcha?.execute) window.hcaptcha.execute();
                    if (window.hcaptchaCallback) window.hcaptchaCallback('{token}');
                    if (window.onHcaptchaCallback) window.onHcaptchaCallback('{token}');
                    
                    document.dispatchEvent(new CustomEvent('hcaptcha-verified', {{
                        detail: {{ response: '{token}' }}
                    }}));
                }}
            """)
            return True
        except:
            return False

# Optimized response analyzer
class UniversalResponseAnalyzer:
    def __init__(self, shared_state):
        self.shared_state = shared_state
    
    def analyze_response(self, url, status, headers, body_text, result_dict):
        """Fast response analysis for hCaptcha token only"""
        
        if 'hcaptcha.com' not in url.lower():
            return
            
        try:
            data = json.loads(body_text)
            
            if 'generated_pass_UUID' in data:
                self.shared_state.captcha_token = data['generated_pass_UUID']
                self.shared_state.captcha_token_found = True
                
                # Store the hCaptcha response
                captcha_response = {
                    "type": "hcaptcha_token",
                    "url": url,
                    "timestamp": datetime.now().isoformat(),
                    "generated_pass_UUID": data['generated_pass_UUID'],
                    "full_response": data
                }
                result_dict["hcaptcha_calls"].append(captcha_response)
                
                print(f"\n{'#'*80}")
                print(f"[TOKEN FOUND] {self.shared_state.captcha_token}")
                print(f"[TIME] {result_dict.get('elapsed_time', 0):.2f}s")
                print(f"{'#'*80}\n")
                
                raise CaptchaTokenFound(self.shared_state.captcha_token)
        except json.JSONDecodeError:
            pass
        except CaptchaTokenFound:
            raise

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

# Card utilities (unchanged but optimized)
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
    first_names = ['Alex', 'Jordan', 'Taylor', 'Morgan', 'Casey']
    last_names = ['Smith', 'Johnson', 'Williams', 'Brown', 'Jones']
    return f"{random.choice(first_names)} {random.choice(last_names)}"

# Optimized wait functions
async def safe_wait(page, ms):
    """Ultra-fast wait with multiplier"""
    if CONFIG["ULTRA_FAST_MODE"]:
        ms = int(ms * CONFIG["WAIT_MULTIPLIER"])
    try:
        if ms > 0:
            await page.wait_for_timeout(ms)
        return True
    except:
        return False

async def wait_for_network_idle(page, timeout=2000):
    """Fast network idle check"""
    if CONFIG["ULTRA_FAST_MODE"]:
        timeout = int(timeout * CONFIG["WAIT_MULTIPLIER"])
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

# Optimized form filling function
async def fill_form_parallel(page, frames, email, card, random_name):
    """Fill form fields in parallel for speed"""
    
    # JavaScript-based fast fill
    fill_script = f"""
    async () => {{
        const fillField = (selector, value) => {{
            const elements = document.querySelectorAll(selector);
            for (const el of elements) {{
                if (el && (el.offsetWidth > 0 || el.offsetHeight > 0)) {{
                    el.focus();
                    el.value = value;
                    el.dispatchEvent(new Event('input', {{ bubbles: true }}));
                    el.dispatchEvent(new Event('change', {{ bubbles: true }}));
                    return true;
                }}
            }}
            return false;
        }};
        
        // Fill email
        fillField('input[type="email"], input[name="email"], #email', '{email}');
        
        // Fill name
        fillField('input[name*="name"]:not([name*="email"]), #cardholder-name', '{random_name}');
        
        // Try to fill card fields in main document
        fillField('input[placeholder*="1234"], input[name*="card"], #cardNumber', '{card["number"]}');
        fillField('input[placeholder*="mm"], input[placeholder*="exp"], #cardExpiry', '{card["month"]}/{card["year"][-2:]}');
        fillField('input[placeholder*="cvc"], input[placeholder*="cvv"], #cardCvc', '{card["cvv"]}');
    }}
    """
    
    try:
        await page.evaluate(fill_script)
    except:
        pass
    
    # Fast iframe filling
    for frame in frames[:3]:  # Only check first 3 frames for speed
        try:
            await frame.evaluate(f"""
                () => {{
                    const fields = {{
                        'card': ['{card["number"]}', 'input[placeholder*="1234"], input[data-elements-stable-field-name="cardNumber"]'],
                        'exp': ['{card["month"]}/{card["year"][-2:]}', 'input[placeholder*="mm"], input[data-elements-stable-field-name="cardExpiry"]'],
                        'cvc': ['{card["cvv"]}', 'input[placeholder*="cvc"], input[data-elements-stable-field-name="cardCvc"]']
                    }};
                    
                    Object.values(fields).forEach(([value, selector]) => {{
                        const el = document.querySelector(selector);
                        if (el) {{
                            el.focus();
                            el.value = value;
                            el.dispatchEvent(new Event('input', {{ bubbles: true }}));
                            el.dispatchEvent(new Event('change', {{ bubbles: true }}));
                        }}
                    }});
                }}
            """)
        except:
            pass

async def run_stripe_automation(url, cc_string, email=None):
    # Start timing
    start_time = time.time()
    
    card = process_card_input(cc_string)
    if not card:
        return {"error": "Invalid card format", "execution_time": 0}
    
    email = email or f"test{random.randint(1000,9999)}@example.com"
    random_name = generate_random_name()
    
    # Initialize shared state
    shared_state = ExecutionControl()
    
    print(f"\n{'='*80}")
    print(f"[START] {datetime.now().strftime('%H:%M:%S')} - ULTRA FAST MODE")
    print(f"[CARD] {card['number'][:6]}****{card['number'][-4:]}")
    print(f"[URL] {url[:50]}...")
    print('='*80)
    
    stripe_result = {
        "status": "pending",
        "hcaptcha_calls": [],
        "success": False,
        "error": None,
        "captcha_solved": False,
        "network_requests": 0,
        "network_errors": 0,
        "elapsed_time": 0
    }
    
    analyzer = UniversalResponseAnalyzer(shared_state)
    captcha_handler = CaptchaHandler()
    captcha_solver = TwoCaptchaSolver(CONFIG.get("TWOCAPTCHA_API_KEY"))
    
    async with async_playwright() as p:
        browser = None
        context = None
        page = None
        
        try:
            # Ultra-fast browser setup
            browser_args = [
                '--no-sandbox',
                '--disable-setuid-sandbox',
                '--disable-dev-shm-usage',
                '--disable-blink-features=AutomationControlled',
                '--disable-web-security',
                '--disable-features=IsolateOrigins,site-per-process',
                '--disable-images',  # Don't load images
                '--disable-css',  # Minimal CSS
                '--disable-fonts',  # No custom fonts
                '--disable-popup-blocking',
                '--disable-content-security-policy'
            ]
            
            if CONFIG["RUN_LOCAL"]:
                browser = await p.chromium.launch(
                    headless=False, 
                    args=browser_args
                )
            else:
                browser_url = f"wss://production-sfo.browserless.io/chromium/playwright?token={CONFIG['BROWSERLESS_API_KEY']}&timeout={CONFIG['BROWSERLESS_TIMEOUT']}"
                try:
                    browser = await p.chromium.connect(browser_url, timeout=10000)
                except:
                    browser = await p.chromium.launch(headless=True, args=browser_args)
            
            # Minimal context
            context = await browser.new_context(
                viewport={'width': 1280, 'height': 720},
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0',
                locale='en-US',
                ignore_https_errors=True,
                bypass_csp=True,
                java_script_enabled=True
            )
            
            # Minimal stealth
            await context.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', { get: () => false });
            """)
            
            page = await context.new_page()
            
            # Ultra-fast network handlers
            async def capture_response(response):
                if shared_state.captcha_token_found:
                    return
                try:
                    url = response.url
                    if 'hcaptcha.com' not in url.lower():
                        return  # Skip non-hCaptcha responses
                        
                    status = response.status
                    headers = response.headers
                    
                    try:
                        body = await response.body()
                        text = body.decode('utf-8', errors='ignore') if body else ""
                        stripe_result["elapsed_time"] = time.time() - start_time
                        analyzer.analyze_response(url, status, headers, text, stripe_result)
                    except CaptchaTokenFound:
                        return
                except CaptchaTokenFound:
                    return
                except:
                    stripe_result["network_errors"] += 1
            
            # Only monitor responses
            page.on("response", capture_response)
            
            try:
                # Ultra-fast navigation
                print("[NAV] Loading page...")
                nav_start = time.time()
                await page.goto(url, wait_until="domcontentloaded", timeout=15000)
                print(f"[NAV] ✓ Loaded in {time.time() - nav_start:.1f}s")
                
                # Minimal wait
                await safe_wait(page, 500)
                
                # Quick captcha check
                has_captcha = await captcha_handler.detect_hcaptcha(page)
                if has_captcha and CONFIG.get("TWOCAPTCHA_API_KEY"):
                    sitekey = await captcha_handler.get_hcaptcha_sitekey(page)
                    if sitekey:
                        token = await captcha_solver.solve_hcaptcha(sitekey, page.url)
                        if token:
                            await captcha_handler.inject_captcha_token(page, token)
                            stripe_result["captcha_solved"] = True
                
                # Check if token was found
                if shared_state.captcha_token_found:
                    execution_time = time.time() - start_time
                    print(f"\n[SUCCESS] Token captured in {execution_time:.2f}s")
                    return {
                        "success": True,
                        "action": "CAPTCHA_TOKEN_CAPTURED",
                        "generated_pass_UUID": shared_state.captcha_token,
                        "hcaptcha_data": stripe_result['hcaptcha_calls'],
                        "execution_time_seconds": round(execution_time, 2),
                        "network_stats": {
                            "total_requests": stripe_result['network_requests'],
                            "errors": stripe_result['network_errors']
                        }
                    }
                
                # Get frames
                all_frames = []
                def collect_frames(frame):
                    all_frames.append(frame)
                    for child in frame.child_frames:
                        collect_frames(child)
                
                collect_frames(page.main_frame)
                stripe_frames = [f for f in all_frames if 'stripe' in f.url.lower() or 'checkout' in f.url.lower()]
                
                # Ultra-fast parallel form filling
                print("[FORM] Filling all fields...")
                fill_start = time.time()
                
                if CONFIG["PARALLEL_FILL"]:
                    await fill_form_parallel(page, stripe_frames, email, card, random_name)
                else:
                    # Traditional fast fill as fallback
                    # Email
                    await page.evaluate(f"""
                        () => {{
                            const el = document.querySelector('input[type="email"], input[name="email"], #email');
                            if (el) {{
                                el.value = '{email}';
                                el.dispatchEvent(new Event('input', {{ bubbles: true }}));
                            }}
                        }}
                    """)
                    
                    # Card fields in frames
                    for frame in stripe_frames[:2]:  # Only first 2 frames
                        try:
                            await frame.fill('input[placeholder*="1234"]', card['number'])
                            await frame.fill('input[placeholder*="mm"]', f"{card['month']}/{card['year'][-2:]}")
                            await frame.fill('input[placeholder*="cvc"]', card['cvv'])
                        except:
                            pass
                
                print(f"[FORM] ✓ Filled in {time.time() - fill_start:.1f}s")
                
                # Minimal wait for validation
                await safe_wait(page, 300)
                
                # Check if token was found
                if shared_state.captcha_token_found:
                    execution_time = time.time() - start_time
                    print(f"\n[SUCCESS] Token captured in {execution_time:.2f}s")
                    return {
                        "success": True,
                        "action": "CAPTCHA_TOKEN_CAPTURED",
                        "generated_pass_UUID": shared_state.captcha_token,
                        "hcaptcha_data": stripe_result['hcaptcha_calls'],
                        "execution_time_seconds": round(execution_time, 2),
                        "network_stats": {
                            "total_requests": stripe_result['network_requests'],
                            "errors": stripe_result['network_errors']
                        }
                    }
                
                # Ultra-fast submit
                print("[SUBMIT] Submitting...")
                submit_success = await page.evaluate("""
                    () => {
                        const buttons = [
                            'button[type="submit"]',
                            'button:has-text("pay")',
                            'button:has-text("submit")',
                            'button:has-text("complete")',
                            'button.btn-primary'
                        ];
                        
                        for (const selector of buttons) {
                            const btn = document.querySelector(selector);
                            if (btn && !btn.disabled) {
                                btn.click();
                                return true;
                            }
                        }
                        return false;
                    }
                """)
                
                if not submit_success:
                    await page.keyboard.press('Enter')
                
                print("[SUBMIT] ✓ Submitted")
                
                # Fast wait for processing
                await safe_wait(page, 3000)
                
                # Fast monitoring loop
                monitor_start = time.time()
                max_monitor = CONFIG["RESPONSE_TIMEOUT_SECONDS"]
                
                while time.time() - monitor_start < max_monitor and not shared_state.captcha_token_found:
                    await asyncio.sleep(0.2)  # Fast check interval
                    
                    # Keep alive check
                    if int(time.time() - monitor_start) % 3 == 0:
                        try:
                            await page.evaluate('1')
                        except:
                            break

            except CaptchaTokenFound:
                pass

            # Final result
            execution_time = time.time() - start_time
            
            if shared_state.captcha_token_found:
                print(f"\n[SUCCESS] Token captured in {execution_time:.2f}s")
                return {
                    "success": True,
                    "action": "CAPTCHA_TOKEN_CAPTURED",
                    "generated_pass_UUID": shared_state.captcha_token,
                    "hcaptcha_data": stripe_result['hcaptcha_calls'],
                    "execution_time_seconds": round(execution_time, 2),
                    "network_stats": {
                        "total_requests": stripe_result['network_requests'],
                        "errors": stripe_result['network_errors']
                    }
                }
            
            print(f"[RESULT] No token found after {execution_time:.2f}s")
            return {
                "success": False,
                "message": "No HCaptcha token found",
                "error": "No HCaptcha token detected in API responses",
                "captcha_solved": stripe_result.get("captcha_solved"),
                "execution_time_seconds": round(execution_time, 2),
                "network_stats": {
                    "total_requests": stripe_result['network_requests'],
                    "errors": stripe_result['network_errors']
                }
            }
                
        except Exception as e:
            execution_time = time.time() - start_time
            
            if shared_state.captcha_token_found:
                print(f"\n[SUCCESS] Token captured in {execution_time:.2f}s")
                return {
                    "success": True,
                    "action": "CAPTCHA_TOKEN_CAPTURED",
                    "generated_pass_UUID": shared_state.captcha_token,
                    "hcaptcha_data": stripe_result['hcaptcha_calls'],
                    "execution_time_seconds": round(execution_time, 2),
                    "network_stats": {
                        "total_requests": stripe_result['network_requests'],
                        "errors": stripe_result['network_errors']
                    }
                }
            
            print(f"[ERROR] {str(e)[:100]} (after {execution_time:.2f}s)")
            return {
                "success": False,
                "error": f"Automation failed: {str(e)}",
                "execution_time_seconds": round(execution_time, 2),
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
            except:
                pass

@app.route('/hrkXstripe', methods=['GET'])
def stripe_endpoint():
    endpoint_start = time.time()
    
    can_proceed, wait_time = rate_limit_check()
    if not can_proceed:
        return jsonify({
            "error": "Rate limit exceeded", 
            "retry_after": f"{wait_time:.1f}s",
            "execution_time_seconds": round(time.time() - endpoint_start, 2)
        }), 429
    
    url = request.args.get('url')
    cc = request.args.get('cc')
    email = request.args.get('email')
    
    print(f"\n{'='*80}")
    print(f"[REQUEST] {datetime.now().strftime('%H:%M:%S')}")
    print(f"[URL] {url[:50] if url else 'None'}...")
    print('='*80)
    
    if not url or not cc:
        return jsonify({
            "error": "Missing parameters: url and cc",
            "execution_time_seconds": round(time.time() - endpoint_start, 2)
        }), 400
    
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    try:
        result = loop.run_until_complete(run_stripe_automation(url, cc, email))
        
        # Add total endpoint time
        result["total_api_time_seconds"] = round(time.time() - endpoint_start, 2)
        
        if result.get('action') == 'CAPTCHA_TOKEN_CAPTURED':
            print(f"\n[✓] Token: {result['generated_pass_UUID']}")
            print(f"[⏱] Execution: {result['execution_time_seconds']}s")
            print(f"[⏱] Total API: {result['total_api_time_seconds']}s")
        
        return jsonify(result), 200 if result.get('success') else 400
        
    except Exception as e:
        return jsonify({
            "error": str(e),
            "execution_time_seconds": round(time.time() - endpoint_start, 2)
        }), 500
    finally:
        loop.close()

@app.route('/status', methods=['GET'])
def status_endpoint():
    return jsonify({
        "status": "online",
        "version": "6.0-ultra-fast",
        "features": {
            "hcaptcha_token_capture": True,
            "ultra_fast_mode": CONFIG["ULTRA_FAST_MODE"],
            "parallel_form_filling": CONFIG["PARALLEL_FILL"],
            "typing_delay_ms": CONFIG["TYPING_DELAY"],
            "wait_multiplier": CONFIG["WAIT_MULTIPLIER"],
            "response_timeout_seconds": CONFIG["RESPONSE_TIMEOUT_SECONDS"],
            "2captcha": bool(CONFIG.get("TWOCAPTCHA_API_KEY"))
        },
        "timestamp": datetime.now().isoformat()
    })

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5001))
    print("="*80)
    print("[SERVER] Stripe Automation v6.0 - ULTRA FAST")
    print(f"[PORT] {port}")
    print(f"[MODE] Ultra Fast - {CONFIG['WAIT_MULTIPLIER']*100:.0f}% speed")
    print(f"[TYPING] {CONFIG['TYPING_DELAY']}ms delay")
    print(f"[TIMEOUT] {CONFIG['RESPONSE_TIMEOUT_SECONDS']}s max wait")
    print(f"[PARALLEL] {'Enabled' if CONFIG['PARALLEL_FILL'] else 'Disabled'}")
    print("="*80)
    app.run(host='0.0.0.0', port=port, debug=False)
