from flask import Flask, request, jsonify
from playwright.sync_api import sync_playwright
import json
import time
import base64
import logging
from typing import Dict, List, Any
import os

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

# Configuration
BROWSERLESS_API_KEY = os.getenv('BROWSERLESS_API_KEY', 'YOUR_BROWSERLESS_API_KEY')
BROWSERLESS_WS_URL = f"wss://chrome.browserless.io?token={BROWSERLESS_API_KEY}"

class HCaptchaExtractor:
    """Extract hCaptcha data using Playwright with browserless.io"""
    
    def __init__(self):
        self.requests_log = []
        self.responses_log = []
        self.generated_pass_uuid = None
        
    def log_request(self, route, request):
        """Log network request"""
        url = request.url
        
        if 'api.hcaptcha.com' in url or 'hcaptcha.com' in url:
            request_data = {
                'timestamp': time.time(),
                'url': url,
                'method': request.method,
                'headers': request.headers,
                'post_data': request.post_data if request.method == 'POST' else None,
                'resource_type': request.resource_type
            }
            
            self.requests_log.append(request_data)
            
            logging.info(f"📤 Request: {request.method} {url[:100]}")
        
        # Continue the request
        route.continue_()
    
    def log_response(self, response):
        """Log network response"""
        url = response.url
        
        if 'api.hcaptcha.com' in url or 'hcaptcha.com' in url:
            try:
                response_data = {
                    'timestamp': time.time(),
                    'url': url,
                    'status': response.status,
                    'status_text': response.status_text,
                    'headers': response.headers,
                }
                
                # Try to get response body
                try:
                    body = response.body()
                    response_data['body_size'] = len(body)
                    
                    # Try to parse as JSON
                    try:
                        body_text = body.decode('utf-8')
                        body_json = json.loads(body_text)
                        response_data['body'] = body_json
                        
                        # Check for generated_pass_UUID
                        if 'generated_pass_UUID' in body_json:
                            self.generated_pass_uuid = body_json['generated_pass_UUID']
                            logging.info(f"✅ Found generated_pass_UUID!")
                        
                        logging.info(f"📥 Response: {response.status} {url[:100]}")
                        
                    except json.JSONDecodeError:
                        response_data['body_text'] = body_text[:500]
                    except UnicodeDecodeError:
                        response_data['body_base64'] = base64.b64encode(body).decode()
                        
                except Exception as e:
                    response_data['body_error'] = str(e)
                
                self.responses_log.append(response_data)
                
            except Exception as e:
                logging.error(f"Error logging response: {e}")
    
    def extract_hcaptcha_data(self, site_key: str, host: str = "checkout.stripe.com", timeout: int = 30000):
        """
        Extract hCaptcha data using Playwright
        
        Args:
            site_key: hCaptcha site key
            host: Host domain to use
            timeout: Maximum wait time in milliseconds
            
        Returns:
            Dict with extracted data
        """
        
        logging.info(f"Starting extraction for site_key: {site_key}")
        
        with sync_playwright() as p:
            try:
                # Connect to browserless.io
                logging.info(f"Connecting to browserless.io...")
                browser = p.chromium.connect_over_cdp(BROWSERLESS_WS_URL)
                
                # Create new context with realistic settings
                context = browser.new_context(
                    viewport={'width': 1920, 'height': 1080},
                    user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                    locale='en-US',
                    timezone_id='America/New_York',
                )
                
                page = context.new_page()
                
                # Enable request/response interception
                page.route('**/*', self.log_request)
                page.on('response', self.log_response)
                
                # Create HTML page with hCaptcha
                html_content = f"""
                <!DOCTYPE html>
                <html lang="en">
                <head>
                    <meta charset="UTF-8">
                    <meta name="viewport" content="width=device-width, initial-scale=1.0">
                    <title>hCaptcha Test</title>
                    <script src="https://js.hcaptcha.com/1/api.js" async defer></script>
                    <style>
                        body {{
                            font-family: Arial, sans-serif;
                            padding: 50px;
                            background: #f5f5f5;
                        }}
                        .container {{
                            max-width: 600px;
                            margin: 0 auto;
                            background: white;
                            padding: 30px;
                            border-radius: 8px;
                            box-shadow: 0 2px 10px rgba(0,0,0,0.1);
                        }}
                        h1 {{ color: #333; }}
                        #status {{
                            padding: 15px;
                            margin: 20px 0;
                            background: #e3f2fd;
                            border-left: 4px solid #2196f3;
                            border-radius: 4px;
                        }}
                    </style>
                </head>
                <body>
                    <div class="container">
                        <h1>hCaptcha Loading Test</h1>
                        <div id="status">Initializing hCaptcha...</div>
                        <div class="h-captcha" data-sitekey="{site_key}"></div>
                    </div>
                    
                    <script>
                        console.log('Page loaded, waiting for hCaptcha...');
                        
                        window.onload = function() {{
                            document.getElementById('status').innerText = 'hCaptcha script loaded, initializing...';
                            
                            setTimeout(function() {{
                                if (typeof hcaptcha !== 'undefined') {{
                                    document.getElementById('status').innerText = '✓ hCaptcha API initialized';
                                    console.log('hCaptcha ready');
                                }} else {{
                                    document.getElementById('status').innerText = '⚠ hCaptcha not loaded';
                                }}
                            }}, 3000);
                        }};
                        
                        // Log any network errors
                        window.addEventListener('error', function(e) {{
                            console.error('Error:', e);
                        }});
                    </script>
                </body>
                </html>
                """
                
                # Navigate to data URL
                logging.info("Loading hCaptcha page...")
                page.goto(f"data:text/html,{html_content}")
                
                # Wait for hCaptcha to load and make API calls
                logging.info("Waiting for hCaptcha initialization...")
                try:
                    # Wait for hCaptcha iframe to load
                    page.wait_for_selector('iframe[src*="hcaptcha"]', timeout=10000)
                    logging.info("hCaptcha iframe detected")
                except:
                    logging.warning("hCaptcha iframe not detected, continuing anyway...")
                
                # Additional wait for API calls
                time.sleep(8)
                
                # Get all cookies
                cookies = context.cookies()
                
                # Get page info
                page_title = page.title()
                page_url = page.url
                
                # Close browser
                context.close()
                browser.close()
                
                logging.info(f"Extraction complete. Captured {len(self.requests_log)} requests, {len(self.responses_log)} responses")
                
                return {
                    'success': True,
                    'site_key': site_key,
                    'host': host,
                    'page_info': {
                        'title': page_title,
                        'url': page_url,
                    },
                    'network': {
                        'requests': self.requests_log,
                        'responses': self.responses_log,
                        'total_requests': len(self.requests_log),
                        'total_responses': len(self.responses_log)
                    },
                    'cookies': cookies,
                    'generated_pass_UUID': self.generated_pass_uuid,
                    'timestamp': time.time()
                }
                
            except Exception as e:
                logging.error(f"Error during extraction: {e}")
                import traceback
                return {
                    'success': False,
                    'error': str(e),
                    'traceback': traceback.format_exc(),
                    'partial_data': {
                        'requests': self.requests_log,
                        'responses': self.responses_log
                    }
                }


def format_response_data(data: Dict) -> Dict:
    """Format response data for better readability"""
    
    formatted = {
        'success': data.get('success', False),
        'timestamp': data.get('timestamp'),
        'site_key': data.get('site_key'),
        'summary': {}
    }
    
    # Add summary
    if data.get('success'):
        formatted['summary'] = {
            'total_requests': data.get('network', {}).get('total_requests', 0),
            'total_responses': data.get('network', {}).get('total_responses', 0),
            'generated_pass_UUID_found': data.get('generated_pass_UUID') is not None,
            'cookies_captured': len(data.get('cookies', []))
        }
    
    # Add generated_pass_UUID prominently
    if data.get('generated_pass_UUID'):
        formatted['generated_pass_UUID'] = data['generated_pass_UUID']
        
        # Decode JWT
        try:
            pass_uuid = data['generated_pass_UUID']
            if pass_uuid.startswith('P1_'):
                jwt_token = pass_uuid[3:]
                parts = jwt_token.split('.')
                
                if len(parts) >= 2:
                    # Decode payload
                    payload_b64 = parts[1] + '=' * (4 - len(parts[1]) % 4)
                    payload_decoded = base64.urlsafe_b64decode(payload_b64)
                    payload_json = json.loads(payload_decoded.decode())
                    
                    formatted['pass_uuid_decoded'] = {
                        'prefix': 'P1',
                        'algorithm': 'HS256',
                        'payload': payload_json
                    }
        except Exception as e:
            formatted['pass_uuid_decode_error'] = str(e)
    
    # Add full data
    formatted['full_data'] = data
    
    return formatted


# API Routes

@app.route('/hrk/captcha', methods=['GET'])
def get_hcaptcha():
    """
    Extract hCaptcha data including generated_pass_UUID
    
    Query Parameters:
        site_key (required): hCaptcha site key
        host (optional): Host domain (default: checkout.stripe.com)
        timeout (optional): Timeout in seconds (default: 30)
        pretty (optional): Pretty print JSON (true/false)
        raw (optional): Return raw data without formatting (true/false)
    
    Example:
        /hrk/captcha?site_key=ec637546-e9b8-447a-ab81-b5fb6d228ab8&pretty=true
    """
    
    # Get parameters
    site_key = request.args.get('site_key')
    host = request.args.get('host', 'checkout.stripe.com')
    timeout = int(request.args.get('timeout', 30))
    pretty = request.args.get('pretty', 'false').lower() == 'true'
    raw = request.args.get('raw', 'false').lower() == 'true'
    
    # Validate required parameters
    if not site_key:
        return jsonify({
            'success': False,
            'error': 'Missing required parameter: site_key',
            'usage': {
                'endpoint': '/hrk/captcha',
                'required': ['site_key'],
                'optional': ['host', 'timeout', 'pretty', 'raw'],
                'example': '/hrk/captcha?site_key=ec637546-e9b8-447a-ab81-b5fb6d228ab8&pretty=true'
            }
        }), 400
    
    # Check API key
    if BROWSERLESS_API_KEY == 'YOUR_BROWSERLESS_API_KEY':
        return jsonify({
            'success': False,
            'error': 'Browserless.io API key not configured',
            'setup': {
                'step_1': 'Get API key from https://www.browserless.io/',
                'step_2': 'Set environment variable: export BROWSERLESS_API_KEY=your_key',
                'step_3': 'Or pass as query parameter: ?api_key=your_key',
                'note': 'Free tier available with 6 hours/month'
            }
        }), 400
    
    logging.info(f"=== New Request ===")
    logging.info(f"Site Key: {site_key}")
    logging.info(f"Host: {host}")
    logging.info(f"Timeout: {timeout}s")
    
    try:
        # Extract hCaptcha data
        extractor = HCaptchaExtractor()
        result = extractor.extract_hcaptcha_data(site_key, host, timeout * 1000)
        
        # Format response
        if raw:
            response_data = result
        else:
            response_data = format_response_data(result)
        
        # Return response
        if pretty:
            return app.response_class(
                response=json.dumps(response_data, indent=2, ensure_ascii=False),
                status=200,
                mimetype='application/json'
            )
        else:
            return jsonify(response_data)
        
    except Exception as e:
        logging.error(f"Error: {e}")
        import traceback
        return jsonify({
            'success': False,
            'error': str(e),
            'traceback': traceback.format_exc()
        }), 500


@app.route('/hrk/health', methods=['GET'])
def health_check():
    """Health check endpoint"""
    
    browserless_configured = BROWSERLESS_API_KEY != 'YOUR_BROWSERLESS_API_KEY'
    
    return jsonify({
        'status': 'healthy',
        'service': 'hCaptcha Extraction API',
        'version': '2.0 (Playwright)',
        'browserless': {
            'configured': browserless_configured,
            'endpoint': BROWSERLESS_WS_URL.replace(BROWSERLESS_API_KEY, '***') if browserless_configured else 'Not configured'
        },
        'timestamp': time.time()
    })


@app.route('/', methods=['GET'])
def index():
    """API documentation"""
    
    return jsonify({
        'service': 'hCaptcha Extraction API',
        'version': '2.0',
        'engine': 'Playwright + browserless.io',
        'endpoints': {
            'GET /': {
                'description': 'API documentation (this page)'
            },
            'GET /hrk/captcha': {
                'description': 'Extract hCaptcha data including generated_pass_UUID',
                'parameters': {
                    'site_key': {
                        'required': True,
                        'type': 'string',
                        'description': 'hCaptcha site key'
                    },
                    'host': {
                        'required': False,
                        'type': 'string',
                        'default': 'checkout.stripe.com',
                        'description': 'Host domain'
                    },
                    'timeout': {
                        'required': False,
                        'type': 'integer',
                        'default': 30,
                        'description': 'Timeout in seconds'
                    },
                    'pretty': {
                        'required': False,
                        'type': 'boolean',
                        'default': False,
                        'description': 'Pretty print JSON response'
                    },
                    'raw': {
                        'required': False,
                        'type': 'boolean',
                        'default': False,
                        'description': 'Return raw data without formatting'
                    }
                },
                'example': '/hrk/captcha?site_key=ec637546-e9b8-447a-ab81-b5fb6d228ab8&pretty=true'
            },
            'GET /hrk/health': {
                'description': 'Health check and service status'
            }
        },
        'setup': {
            '1': {
                'title': 'Install dependencies',
                'command': 'pip install -r requirements.txt'
            },
            '2': {
                'title': 'Install Playwright browsers',
                'command': 'playwright install chromium'
            },
            '3': {
                'title': 'Get browserless.io API key',
                'url': 'https://www.browserless.io/',
                'note': 'Free tier: 6 hours/month, no credit card required'
            },
            '4': {
                'title': 'Set API key',
                'command': 'export BROWSERLESS_API_KEY=your_key_here'
            },
            '5': {
                'title': 'Run server',
                'command': 'python app.py'
            }
        },
        'features': [
            'Uses real Chrome browser via browserless.io',
            'Captures all network requests and responses',
            'Extracts generated_pass_UUID from hCaptcha',
            'Decodes JWT tokens automatically',
            'Returns all raw HTTP data',
            'Supports custom domains and timeouts'
        ],
        'response_structure': {
            'success': 'Boolean indicating if extraction succeeded',
            'generated_pass_UUID': 'The P1_xxx JWT token from hCaptcha',
            'pass_uuid_decoded': 'Decoded JWT payload',
            'summary': 'Summary of captured data',
            'full_data': {
                'network': {
                    'requests': 'All HTTP requests to hCaptcha',
                    'responses': 'All HTTP responses with bodies'
                },
                'cookies': 'All cookies set during session',
                'page_info': 'Information about loaded page'
            }
        }
    })


if __name__ == '__main__':
    print("\n" + "="*70)
    print("🚀 hCaptcha Extraction API Server (Playwright)")
    print("="*70)
    
    print("\n📋 Configuration:")
    if BROWSERLESS_API_KEY == 'YOUR_BROWSERLESS_API_KEY':
        print("   ❌ Browserless API Key: NOT SET")
        print("   → Get your key from: https://www.browserless.io/")
        print("   → Then run: export BROWSERLESS_API_KEY=your_key")
    else:
        print(f"   ✅ Browserless API Key: {BROWSERLESS_API_KEY[:10]}***")
    
    print("\n🌐 Endpoints:")
    print("   GET /                - API documentation")
    print("   GET /hrk/captcha     - Extract hCaptcha data")
    print("   GET /hrk/health      - Health check")
    
    print("\n📖 Example Usage:")
    print("   curl 'http://localhost:5000/hrk/captcha?site_key=ec637546-e9b8-447a-ab81-b5fb6d228ab8&pretty=true'")
    
    print("\n🔧 Requirements:")
    print("   • Flask")
    print("   • Playwright")
    print("   • browserless.io account (free tier available)")
    
    print("\n" + "="*70 + "\n")
    
    app.run(debug=True, host='0.0.0.0', port=5000)
