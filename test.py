from flask import Flask, request, jsonify
from playwright.async_api import async_playwright
import json
import time
import base64
import logging
import os
import asyncio
from datetime import datetime
from dotenv import load_dotenv
from typing import Dict, List, Any

load_dotenv()

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

# Configuration
CONFIG = {
    "BROWSERLESS_API_KEY": os.getenv('BROWSERLESS_API_KEY'),
    "RUN_LOCAL": os.getenv('RUN_LOCAL', 'false').lower() == 'true',
    "BROWSERLESS_TIMEOUT": 60000,
}

class HCaptchaNetworkCapture:
    """Capture all hCaptcha network traffic including generated_pass_UUID"""
    
    def __init__(self):
        self.all_requests = []
        self.all_responses = []
        self.generated_pass_uuid = None
        self.hsw_challenge = None
        
    async def handle_request(self, request):
        """Capture outgoing requests"""
        url = request.url
        
        # Log all hCaptcha related requests
        if 'hcaptcha' in url.lower():
            request_data = {
                "type": "request",
                "timestamp": datetime.now().isoformat(),
                "url": url,
                "method": request.method,
                "headers": request.headers,
                "resource_type": request.resource_type
            }
            
            # Capture POST data if available
            if request.method == "POST":
                try:
                    post_data = request.post_data
                    if post_data:
                        request_data["body"] = post_data
                        # Try to parse as JSON or form data
                        try:
                            request_data["body_parsed"] = json.loads(post_data)
                        except:
                            # Try URL encoded
                            from urllib.parse import parse_qs
                            try:
                                request_data["body_parsed"] = parse_qs(post_data)
                            except:
                                pass
                except:
                    pass
            
            self.all_requests.append(request_data)
            
            if 'getcaptcha' in url:
                print(f"📤 [HCAPTCHA-REQUEST] {url[:80]}...")
    
    async def handle_response(self, response):
        """Capture incoming responses"""
        url = response.url
        
        # Log all hCaptcha related responses
        if 'hcaptcha' in url.lower():
            response_data = {
                "type": "response",
                "timestamp": datetime.now().isoformat(),
                "url": url,
                "status": response.status,
                "status_text": response.status_text,
                "headers": response.headers,
            }
            
            # Try to get response body
            try:
                body = await response.body()
                response_data["body_size"] = len(body)
                
                # Decode and parse
                try:
                    body_text = body.decode('utf-8')
                    
                    # Try JSON parse
                    try:
                        body_json = json.loads(body_text)
                        response_data["body"] = body_json
                        
                        # Check for generated_pass_UUID
                        if 'generated_pass_UUID' in body_json:
                            self.generated_pass_uuid = body_json['generated_pass_UUID']
                            print(f"✅ [FOUND] generated_pass_UUID: {self.generated_pass_uuid[:50]}...")
                            
                        # Check for HSW challenge
                        if 'c' in body_json and isinstance(body_json['c'], dict):
                            self.hsw_challenge = body_json['c']
                            print(f"🔨 [FOUND] HSW Challenge: {body_json['c'].get('type')}")
                            
                        # Log key fields
                        if 'pass' in body_json:
                            print(f"🎫 [PASS] {body_json['pass']}")
                        if 'expiration' in body_json:
                            print(f"⏰ [EXPIRATION] {body_json['expiration']}s")
                            
                    except json.JSONDecodeError:
                        response_data["body_text"] = body_text[:1000]
                        
                except UnicodeDecodeError:
                    # Binary data - base64 encode
                    response_data["body_base64"] = base64.b64encode(body).decode()
                    
            except Exception as e:
                response_data["body_error"] = str(e)
            
            self.all_responses.append(response_data)
            
            if 'getcaptcha' in url:
                print(f"📥 [HCAPTCHA-RESPONSE] {response.status} - {url[:80]}...")
    
    def get_summary(self):
        """Get summary of captured data"""
        return {
            "total_requests": len(self.all_requests),
            "total_responses": len(self.all_responses),
            "generated_pass_uuid_found": self.generated_pass_uuid is not None,
            "hsw_challenge_found": self.hsw_challenge is not None
        }


async def extract_hcaptcha_data(site_key: str, host: str = "checkout.stripe.com", timeout: int = 30):
    """
    Extract hCaptcha data including generated_pass_UUID using Playwright + browserless.io
    
    Args:
        site_key: hCaptcha site key
        host: Host domain
        timeout: Timeout in seconds
        
    Returns:
        Dict with all captured data
    """
    
    print(f"\n{'='*70}")
    print(f"🚀 Starting hCaptcha Extraction")
    print(f"{'='*70}")
    print(f"Site Key: {site_key}")
    print(f"Host: {host}")
    print(f"Mode: {'Local' if CONFIG['RUN_LOCAL'] else 'Browserless.io'}")
    print(f"{'='*70}\n")
    
    capture = HCaptchaNetworkCapture()
    
    async with async_playwright() as p:
        browser = None
        context = None
        page = None
        
        try:
            # Browser setup - same pattern as your working code
            browser_args = [
                '--no-sandbox',
                '--disable-setuid-usage',
                '--disable-dev-shm-usage',
                '--disable-blink-features=AutomationControlled',
                '--disable-web-security',
            ]
            
            if CONFIG["RUN_LOCAL"]:
                print("[BROWSER] Launching local browser...")
                browser = await p.chromium.launch(
                    headless=False,
                    args=browser_args
                )
            else:
                # UPDATED: Use production-sfo endpoint (not legacy chrome.browserless.io)
                print("[BROWSER] Connecting to browserless.io...")
                browser_url = f"wss://production-sfo.browserless.io/chromium/playwright?token={CONFIG['BROWSERLESS_API_KEY']}&timeout={CONFIG['BROWSERLESS_TIMEOUT']}"
                
                try:
                    browser = await p.chromium.connect(browser_url, timeout=30000)
                    print("[BROWSER] ✅ Connected to browserless.io")
                except Exception as e:
                    print(f"[BROWSER] ❌ Connection failed: {e}")
                    # Fallback to local
                    print("[BROWSER] Falling back to local browser...")
                    browser = await p.chromium.launch(headless=True, args=browser_args)
            
            # Create context with realistic settings
            context = await browser.new_context(
                viewport={'width': 1920, 'height': 1080},
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                locale='en-US',
                timezone_id='America/New_York',
                ignore_https_errors=True,
                java_script_enabled=True,
            )
            
            # Add stealth script
            await context.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', {
                    get: () => false,
                });
                
                window.chrome = {
                    runtime: {},
                    loadTimes: function() {},
                    csi: function() {},
                    app: {}
                };
            """)
            
            page = await context.new_page()
            
            # Attach network handlers
            page.on("request", capture.handle_request)
            page.on("response", capture.handle_response)
            
            # Log console messages
            page.on("console", lambda msg: print(f"[CONSOLE] {msg.text[:200]}") if msg.type == 'error' else None)
            
            # Create test page with hCaptcha
            html_content = f"""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>hCaptcha Data Extraction</title>
    <script src="https://js.hcaptcha.com/1/api.js" async defer></script>
    <style>
        body {{
            font-family: Arial, sans-serif;
            max-width: 600px;
            margin: 50px auto;
            padding: 20px;
            background: #f5f5f5;
        }}
        .container {{
            background: white;
            padding: 30px;
            border-radius: 8px;
            box-shadow: 0 2px 10px rgba(0,0,0,0.1);
        }}
        h1 {{
            color: #333;
            margin-bottom: 20px;
        }}
        #status {{
            padding: 15px;
            margin: 20px 0;
            background: #e3f2fd;
            border-left: 4px solid #2196f3;
            border-radius: 4px;
            font-family: monospace;
        }}
        .h-captcha {{
            margin: 20px 0;
        }}
    </style>
</head>
<body>
    <div class="container">
        <h1>🔐 hCaptcha Data Extraction</h1>
        <div id="status">Initializing hCaptcha...</div>
        <div class="h-captcha" data-sitekey="{site_key}"></div>
    </div>
    
    <script>
        console.log('Page loaded, initializing hCaptcha...');
        
        let checkCount = 0;
        const maxChecks = 20;
        
        function checkHCaptcha() {{
            checkCount++;
            const status = document.getElementById('status');
            
            if (typeof hcaptcha !== 'undefined') {{
                status.innerHTML = '✅ hCaptcha API loaded and initialized';
                console.log('hCaptcha ready');
            }} else if (checkCount < maxChecks) {{
                status.innerHTML = `⏳ Loading hCaptcha... (${{checkCount}}/${{maxChecks}})`;
                setTimeout(checkHCaptcha, 500);
            }} else {{
                status.innerHTML = '❌ hCaptcha failed to load';
            }}
        }}
        
        window.addEventListener('load', function() {{
            setTimeout(checkHCaptcha, 1000);
        }});
        
        // Log hCaptcha callbacks if fired
        window.hcaptchaOnLoad = function() {{
            console.log('hCaptcha onLoad callback fired');
        }};
        
        window.hcaptchaCallback = function(token) {{
            console.log('hCaptcha solved! Token:', token.substring(0, 20) + '...');
        }};
    </script>
</body>
</html>
"""
            
            # Navigate to data URL
            print("[PAGE] Loading hCaptcha page...")
            await page.goto(f"data:text/html,{html_content}", wait_until="networkidle", timeout=40000)
            print("[PAGE] ✅ Page loaded")
            
            # Wait for hCaptcha to initialize and make API calls
            print("[WAIT] Waiting for hCaptcha to initialize...")
            
            # Check for hCaptcha iframe
            try:
                await page.wait_for_selector('iframe[src*="hcaptcha"]', timeout=10000)
                print("[IFRAME] ✅ hCaptcha iframe detected")
            except:
                print("[IFRAME] ⚠️  No iframe detected yet, continuing...")
            
            # Wait for network activity
            await asyncio.sleep(8)  # Give time for all API calls
            
            # Additional wait for late-loading resources
            try:
                await page.wait_for_load_state('networkidle', timeout=5000)
            except:
                pass
            
            # Get all frames for debugging
            frames = page.frames
            print(f"[FRAMES] Total frames: {len(frames)}")
            for idx, frame in enumerate(frames):
                try:
                    if frame.url and 'hcaptcha' in frame.url:
                        print(f"  Frame {idx}: {frame.url[:80]}...")
                except:
                    pass
            
            # Get cookies
            cookies = await context.cookies()
            hcaptcha_cookies = [c for c in cookies if 'hcaptcha' in c.get('domain', '').lower()]
            
            print(f"\n[SUMMARY]")
            print(f"  Requests captured: {len(capture.all_requests)}")
            print(f"  Responses captured: {len(capture.all_responses)}")
            print(f"  hCaptcha cookies: {len(hcaptcha_cookies)}")
            print(f"  generated_pass_UUID: {'✅ FOUND' if capture.generated_pass_uuid else '❌ NOT FOUND'}")
            
            # Decode JWT if found
            decoded_pass_uuid = None
            if capture.generated_pass_uuid:
                try:
                    decoded_pass_uuid = decode_jwt_token(capture.generated_pass_uuid)
                except Exception as e:
                    print(f"[JWT] Decode error: {e}")
            
            # Build result
            result = {
                "success": True,
                "timestamp": datetime.now().isoformat(),
                "site_key": site_key,
                "host": host,
                "summary": capture.get_summary(),
                "generated_pass_uuid": {
                    "token": capture.generated_pass_uuid,
                    "decoded": decoded_pass_uuid
                } if capture.generated_pass_uuid else None,
                "hsw_challenge": capture.hsw_challenge,
                "network": {
                    "requests": capture.all_requests,
                    "responses": capture.all_responses
                },
                "cookies": hcaptcha_cookies,
                "page_info": {
                    "url": page.url,
                    "title": await page.title(),
                    "frames": len(frames)
                }
            }
            
            return result
            
        except Exception as e:
            print(f"[ERROR] {e}")
            import traceback
            return {
                "success": False,
                "error": str(e),
                "traceback": traceback.format_exc(),
                "partial_data": {
                    "requests": capture.all_requests,
                    "responses": capture.all_responses,
                    "summary": capture.get_summary()
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
                print("[CLEANUP] ✅ Browser closed")
            except Exception as e:
                print(f"[CLEANUP] Error: {e}")


def decode_jwt_token(token: str) -> Dict:
    """Decode JWT token (generated_pass_UUID)"""
    try:
        # Remove P1_ prefix if present
        if token.startswith('P1_'):
            jwt_token = token[3:]
            prefix = 'P1'
        else:
            jwt_token = token
            prefix = None
        
        parts = jwt_token.split('.')
        if len(parts) < 2:
            return {"error": "Invalid JWT format"}
        
        # Decode header
        header_b64 = parts[0] + '=' * (4 - len(parts[0]) % 4)
        header_decoded = base64.urlsafe_b64decode(header_b64)
        header_json = json.loads(header_decoded.decode())
        
        # Decode payload
        payload_b64 = parts[1] + '=' * (4 - len(parts[1]) % 4)
        payload_decoded = base64.urlsafe_b64decode(payload_b64)
        payload_json = json.loads(payload_decoded.decode())
        
        # Format expiration if present
        if 'exp' in payload_json:
            try:
                exp_dt = datetime.fromtimestamp(payload_json['exp'])
                payload_json['exp_formatted'] = exp_dt.isoformat()
            except:
                pass
        
        result = {
            "prefix": prefix,
            "header": header_json,
            "payload": payload_json
        }
        
        if len(parts) >= 3:
            result["signature"] = parts[2][:50] + "..." if len(parts[2]) > 50 else parts[2]
        
        return result
        
    except Exception as e:
        return {"error": str(e)}


# Flask Routes

@app.route('/hrk/captcha', methods=['GET'])
def hcaptcha_endpoint():
    """
    Extract hCaptcha data including generated_pass_UUID
    
    Query Parameters:
        site_key (required): hCaptcha site key
        host (optional): Host domain (default: checkout.stripe.com)
        timeout (optional): Timeout in seconds (default: 30)
        pretty (optional): Pretty print JSON (true/false)
    
    Example:
        /hrk/captcha?site_key=ec637546-e9b8-447a-ab81-b5fb6d228ab8&pretty=true
    """
    
    site_key = request.args.get('site_key')
    host = request.args.get('host', 'checkout.stripe.com')
    timeout = int(request.args.get('timeout', 30))
    pretty = request.args.get('pretty', 'false').lower() == 'true'
    
    if not site_key:
        return jsonify({
            "success": False,
            "error": "Missing required parameter: site_key",
            "usage": {
                "endpoint": "/hrk/captcha",
                "required": ["site_key"],
                "optional": ["host", "timeout", "pretty"],
                "example": "/hrk/captcha?site_key=ec637546-e9b8-447a-ab81-b5fb6d228ab8&pretty=true"
            }
        }), 400
    
    if not CONFIG["BROWSERLESS_API_KEY"] and not CONFIG["RUN_LOCAL"]:
        return jsonify({
            "success": False,
            "error": "Browserless.io API key not configured",
            "setup": {
                "get_key": "https://www.browserless.io/",
                "set_env": "BROWSERLESS_API_KEY=your_key",
                "or_local": "RUN_LOCAL=true"
            }
        }), 400
    
    # Run async function
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    try:
        result = loop.run_until_complete(extract_hcaptcha_data(site_key, host, timeout))
        
        if pretty:
            return app.response_class(
                response=json.dumps(result, indent=2, ensure_ascii=False),
                status=200,
                mimetype='application/json'
            )
        else:
            return jsonify(result)
            
    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500
    finally:
        loop.close()


@app.route('/hrk/health', methods=['GET'])
def health_check():
    """Health check endpoint"""
    return jsonify({
        "status": "healthy",
        "service": "hCaptcha Extraction API",
        "version": "1.0",
        "browserless": {
            "configured": bool(CONFIG["BROWSERLESS_API_KEY"]),
            "endpoint": "wss://production-sfo.browserless.io"
        },
        "local_mode": CONFIG["RUN_LOCAL"],
        "timestamp": datetime.now().isoformat()
    })


@app.route('/', methods=['GET'])
def index():
    """API documentation"""
    return jsonify({
        "service": "hCaptcha Extraction API",
        "version": "1.0",
        "endpoints": {
            "/": "API documentation",
            "/hrk/captcha": "Extract hCaptcha data",
            "/hrk/health": "Health check"
        },
        "usage": {
            "endpoint": "/hrk/captcha",
            "method": "GET",
            "parameters": {
                "site_key": "hCaptcha site key (required)",
                "host": "Host domain (optional, default: checkout.stripe.com)",
                "timeout": "Timeout in seconds (optional, default: 30)",
                "pretty": "Pretty print JSON (optional, true/false)"
            },
            "example": "/hrk/captcha?site_key=ec637546-e9b8-447a-ab81-b5fb6d228ab8&pretty=true"
        },
        "response_fields": {
            "success": "Boolean indicating success",
            "generated_pass_uuid": {
                "token": "The P1_xxx JWT token",
                "decoded": "Decoded JWT payload"
            },
            "hsw_challenge": "The HSW challenge data",
            "network": {
                "requests": "All HTTP requests",
                "responses": "All HTTP responses with bodies"
            },
            "summary": "Summary statistics"
        }
    })


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    
    print("\n" + "="*70)
    print("🚀 hCaptcha Extraction API")
    print("="*70)
    print(f"Port: {port}")
    print(f"Browserless: {'✅ Configured' if CONFIG['BROWSERLESS_API_KEY'] else '❌ Not configured'}")
    print(f"Mode: {'Local' if CONFIG['RUN_LOCAL'] else 'Browserless.io'}")
    print(f"Endpoint: wss://production-sfo.browserless.io")
    print("="*70)
    print("\n📖 Usage:")
    print(f"  http://localhost:{port}/hrk/captcha?site_key=YOUR_SITE_KEY&pretty=true")
    print("\n" + "="*70 + "\n")
    
    app.run(host='0.0.0.0', port=port, debug=False)
