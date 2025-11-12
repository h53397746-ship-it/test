#!/usr/bin/env python3
"""
Payment Gateway Tester - All-in-One Script
Run: python payment_tester.py
Then open: http://localhost:5000
"""

import json
import re
import random
import time
import asyncio
import hashlib
import base64
from datetime import datetime, timedelta
from dataclasses import dataclass, asdict
from typing import Optional, List, Dict, Any
from enum import Enum
import threading
import webbrowser
import os
import sys

# Flask imports
try:
    from flask import Flask, render_template_string, jsonify, request, send_file
    from flask_cors import CORS
    import aiohttp
except ImportError:
    print("Installing required packages...")
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "flask", "flask-cors", "aiohttp"])
    from flask import Flask, render_template_string, jsonify, request, send_file
    from flask_cors import CORS
    import aiohttp

# Initialize Flask app
app = Flask(__name__)
CORS(app)

# ============================================================================
# DATA MODELS AND BUSINESS LOGIC
# ============================================================================

@dataclass
class CardDetails:
    number: str
    month: str
    year: str
    cvv: str
    brand: str = ""
    
    def __post_init__(self):
        self.brand = self.detect_brand()
    
    def detect_brand(self) -> str:
        """Detect card brand from number"""
        if not self.number:
            return "unknown"
        
        first = self.number[0]
        first_two = self.number[:2] if len(self.number) >= 2 else ""
        first_four = self.number[:4] if len(self.number) >= 4 else ""
        
        if first == '4':
            return 'visa'
        elif first_two in ['51', '52', '53', '54', '55']:
            return 'mastercard'
        elif first_two in ['34', '37']:
            return 'amex'
        elif first_two == '65' or first_four == '6011':
            return 'discover'
        elif first_two in ['36', '38']:
            return 'diners'
        elif first_four in ['3528', '3529', '3530', '3531', '3532', '3533', '3534', '3535']:
            return 'jcb'
        return 'unknown'
    
    def mask_number(self) -> str:
        """Return masked card number"""
        if len(self.number) >= 8:
            return f"•••• {self.number[-4:]}"
        return "•••• ••••"
    
    def validate_luhn(self) -> bool:
        """Validate card number using Luhn algorithm"""
        def digits_of(n):
            return [int(d) for d in str(n)]
        
        try:
            digits = digits_of(self.number)
            odd_digits = digits[-1::-2]
            even_digits = digits[-2::-2]
            
            checksum = sum(odd_digits)
            for d in even_digits:
                checksum += sum(digits_of(d * 2))
            
            return checksum % 10 == 0
        except:
            return False

class CardParser:
    """Parse cards from various formats"""
    
    @staticmethod
    def parse_card(line: str) -> Optional[CardDetails]:
        """Parse a single card line"""
        line = line.strip()
        if not line:
            return None
        
        # Remove all non-essential characters for analysis
        clean_line = re.sub(r'[^\d\s|/,:\-]', '', line)
        
        # Try different separators
        separators = [r'\|', r'/', r'-', r',', r':', r'\s+']
        parts = None
        
        for sep in separators:
            temp_parts = re.split(sep, clean_line)
            temp_parts = [p.strip() for p in temp_parts if p.strip()]
            if len(temp_parts) == 4:
                parts = temp_parts
                break
        
        # Try continuous format (no separators)
        if not parts:
            numbers_only = re.sub(r'\D', '', line)
            if len(numbers_only) >= 23:  # 16 + 2 + 2 + 3 minimum
                parts = [
                    numbers_only[:16],
                    numbers_only[16:18],
                    numbers_only[18:20],
                    numbers_only[20:23]
                ]
        
        if not parts or len(parts) != 4:
            return None
        
        try:
            # Clean and validate parts
            card_number = re.sub(r'\D', '', parts[0])
            if len(card_number) < 13 or len(card_number) > 19:
                return None
            
            month = parts[1].zfill(2)
            if not (1 <= int(month) <= 12):
                return None
            
            year = parts[2]
            if len(year) == 4:
                year = year[-2:]  # Convert YYYY to YY
            elif len(year) != 2:
                return None
            
            cvv = re.sub(r'\D', '', parts[3])
            if not (3 <= len(cvv) <= 4):
                return None
            
            return CardDetails(
                number=card_number,
                month=month,
                year=year,
                cvv=cvv
            )
        except:
            return None

class PaymentSimulator:
    """Simulate payment processing"""
    
    @staticmethod
    async def process_payment(card: CardDetails, business_type: str) -> Dict[str, Any]:
        """Simulate payment processing with realistic responses"""
        
        # Simulate processing delay
        await asyncio.sleep(random.uniform(0.5, 2.0))
        
        # Validate card with Luhn
        is_valid = card.validate_luhn()
        
        # Determine success based on card validation and random factors
        if not is_valid:
            success = False
            status = "INVALID"
            message = "Invalid card number"
        else:
            # Simulate different response scenarios
            rand = random.random()
            if rand < 0.6:  # 60% success rate
                success = True
                status = "APPROVED"
                messages = [
                    "Transaction approved",
                    "Payment authorized successfully",
                    "Transaction completed",
                    "Payment processed"
                ]
                message = random.choice(messages)
            elif rand < 0.75:  # 15% insufficient funds
                success = False
                status = "DECLINED"
                message = "Insufficient funds"
            elif rand < 0.85:  # 10% card declined
                success = False
                status = "DECLINED"
                message = "Card declined by issuer"
            elif rand < 0.95:  # 10% fraud
                success = False
                status = "FRAUD"
                message = "Transaction flagged for review"
            else:  # 5% other errors
                success = False
                status = "ERROR"
                message = "Transaction could not be processed"
        
        # Determine amount based on business type
        amount = "141.00" if business_type == "swiggy" else "726.00"
        currency = "INR"
        
        return {
            "success": success,
            "card": card.mask_number(),
            "full_card": f"{card.number}|{card.month}|{card.year}|{card.cvv}",
            "brand": card.brand,
            "amount": amount,
            "currency": currency,
            "status": status,
            "message": message,
            "business_type": business_type,
            "timestamp": datetime.now().isoformat(),
            "transaction_id": f"TXN{random.randint(100000000, 999999999)}",
            "auth_code": f"AUTH{random.randint(1000, 9999)}" if success else None,
            "processing_time": round(random.uniform(0.5, 2.0), 2)
        }

# ============================================================================
# WEB ROUTES
# ============================================================================

@app.route('/')
def index():
    """Serve the main application"""
    return render_template_string(HTML_TEMPLATE)

@app.route('/api/process', methods=['POST'])
def process_cards():
    """Process multiple cards"""
    try:
        data = request.json
        cards_text = data.get('cards', '')
        business_type = data.get('businessType', 'swiggy')
        
        # Parse all cards
        lines = cards_text.strip().split('\n')
        cards = []
        invalid_lines = []
        
        for line in lines:
            line = line.strip()
            if line:
                card = CardParser.parse_card(line)
                if card:
                    cards.append(card)
                else:
                    invalid_lines.append(line)
        
        if not cards:
            return jsonify({
                'success': False,
                'error': 'No valid cards found',
                'invalid_lines': invalid_lines
            }), 400
        
        # Process cards asynchronously
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        async def process_all():
            tasks = [PaymentSimulator.process_payment(card, business_type) for card in cards]
            return await asyncio.gather(*tasks)
        
        results = loop.run_until_complete(process_all())
        loop.close()
        
        # Calculate statistics
        total = len(results)
        successful = sum(1 for r in results if r['success'])
        failed = total - successful
        
        # Brand distribution
        brands = {}
        for r in results:
            brand = r['brand']
            brands[brand] = brands.get(brand, 0) + 1
        
        return jsonify({
            'success': True,
            'results': results,
            'statistics': {
                'total': total,
                'successful': successful,
                'failed': failed,
                'success_rate': round((successful / total * 100) if total > 0 else 0, 2),
                'brands': brands,
                'invalid_lines': invalid_lines
            }
        })
        
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

@app.route('/api/export', methods=['POST'])
def export_results():
    """Export results as CSV"""
    try:
        results = request.json.get('results', [])
        
        csv_content = "Card Number,Brand,Status,Amount,Currency,Message,Transaction ID,Timestamp\n"
        for r in results:
            csv_content += f'"{r.get("full_card", "")}","{r.get("brand", "")}","{r.get("status", "")}","{r.get("amount", "")}","{r.get("currency", "")}","{r.get("message", "")}","{r.get("transaction_id", "")}","{r.get("timestamp", "")}"\n'
        
        # Create temporary file
        import tempfile
        with tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False) as f:
            f.write(csv_content)
            temp_path = f.name
        
        return send_file(temp_path, as_attachment=True, download_name=f'payment_test_results_{datetime.now().strftime("%Y%m%d_%H%M%S")}.csv')
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ============================================================================
# HTML TEMPLATE
# ============================================================================

HTML_TEMPLATE = '''
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Payment Gateway Tester - Professional Edition</title>
    <style>
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }
        
        :root {
            --primary: #6366f1;
            --primary-dark: #4f46e5;
            --secondary: #8b5cf6;
            --success: #10b981;
            --danger: #ef4444;
            --warning: #f59e0b;
            --dark: #1e293b;
            --darker: #0f172a;
            --light: #f8fafc;
            --gray: #64748b;
        }
        
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif;
            background: linear-gradient(135deg, #1e3c72 0%, #2a5298 100%);
            min-height: 100vh;
            color: #fff;
            position: relative;
            overflow-x: hidden;
        }
        
        /* Animated Background Pattern */
        body::before {
            content: '';
            position: fixed;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            background-image: 
                radial-gradient(circle at 20% 80%, rgba(99, 102, 241, 0.1) 0%, transparent 50%),
                radial-gradient(circle at 80% 20%, rgba(139, 92, 246, 0.1) 0%, transparent 50%),
                radial-gradient(circle at 40% 40%, rgba(236, 72, 153, 0.1) 0%, transparent 50%);
            pointer-events: none;
            z-index: 1;
        }
        
        .container {
            max-width: 1400px;
            margin: 0 auto;
            padding: 2rem;
            position: relative;
            z-index: 2;
        }
        
        /* Header */
        .header {
            text-align: center;
            margin-bottom: 3rem;
            animation: fadeInDown 0.8s ease;
        }
        
        .logo {
            display: inline-block;
            padding: 1rem 2rem;
            background: rgba(255, 255, 255, 0.1);
            backdrop-filter: blur(10px);
            border-radius: 100px;
            margin-bottom: 1rem;
            border: 1px solid rgba(255, 255, 255, 0.2);
        }
        
        .logo h1 {
            font-size: 2rem;
            font-weight: 700;
            background: linear-gradient(135deg, #fff, #e0e7ff);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
        }
        
        .subtitle {
            color: rgba(255, 255, 255, 0.8);
            font-size: 1.1rem;
        }
        
        /* Main Grid */
        .main-grid {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 2rem;
            margin-bottom: 2rem;
        }
        
        @media (max-width: 968px) {
            .main-grid {
                grid-template-columns: 1fr;
            }
        }
        
        /* Cards */
        .card {
            background: rgba(255, 255, 255, 0.1);
            backdrop-filter: blur(20px);
            border-radius: 20px;
            padding: 2rem;
            border: 1px solid rgba(255, 255, 255, 0.2);
            animation: fadeInUp 0.8s ease;
            transition: transform 0.3s ease, box-shadow 0.3s ease;
        }
        
        .card:hover {
            transform: translateY(-5px);
            box-shadow: 0 20px 40px rgba(0, 0, 0, 0.2);
        }
        
        .card-header {
            display: flex;
            align-items: center;
            gap: 1rem;
            margin-bottom: 1.5rem;
            padding-bottom: 1rem;
            border-bottom: 1px solid rgba(255, 255, 255, 0.1);
        }
        
        .card-icon {
            width: 48px;
            height: 48px;
            background: linear-gradient(135deg, var(--primary), var(--secondary));
            border-radius: 12px;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 24px;
        }
        
        .card-title {
            font-size: 1.5rem;
            font-weight: 600;
        }
        
        /* Business Type Selector */
        .business-tabs {
            display: flex;
            gap: 1rem;
            margin-bottom: 2rem;
        }
        
        .business-tab {
            flex: 1;
            padding: 1rem;
            background: rgba(255, 255, 255, 0.05);
            border: 2px solid transparent;
            border-radius: 12px;
            cursor: pointer;
            transition: all 0.3s ease;
            text-align: center;
        }
        
        .business-tab:hover {
            background: rgba(255, 255, 255, 0.1);
        }
        
        .business-tab.active {
            background: rgba(99, 102, 241, 0.2);
            border-color: var(--primary);
        }
        
        .business-tab-icon {
            font-size: 2rem;
            margin-bottom: 0.5rem;
        }
        
        .business-tab-title {
            font-weight: 600;
            margin-bottom: 0.25rem;
        }
        
        .business-tab-amount {
            color: rgba(255, 255, 255, 0.7);
            font-size: 0.9rem;
        }
        
        /* Form Elements */
        .form-group {
            margin-bottom: 1.5rem;
        }
        
        .form-label {
            display: block;
            margin-bottom: 0.5rem;
            font-weight: 500;
        }
        
        .form-textarea {
            width: 100%;
            min-height: 200px;
            padding: 1rem;
            background: rgba(255, 255, 255, 0.05);
            border: 1px solid rgba(255, 255, 255, 0.2);
            border-radius: 12px;
            color: #fff;
            font-family: 'Monaco', 'Courier New', monospace;
            font-size: 0.9rem;
            resize: vertical;
            transition: all 0.3s ease;
        }
        
        .form-textarea:focus {
            outline: none;
            border-color: var(--primary);
            background: rgba(255, 255, 255, 0.08);
        }
        
        .form-textarea::placeholder {
            color: rgba(255, 255, 255, 0.4);
        }
        
        /* Buttons */
        .btn-group {
            display: flex;
            gap: 1rem;
            flex-wrap: wrap;
        }
        
        .btn {
            padding: 0.875rem 1.75rem;
            border: none;
            border-radius: 10px;
            font-weight: 600;
            font-size: 1rem;
            cursor: pointer;
            transition: all 0.3s ease;
            display: inline-flex;
            align-items: center;
            gap: 0.5rem;
        }
        
        .btn-primary {
            background: linear-gradient(135deg, var(--primary), var(--secondary));
            color: white;
        }
        
        .btn-primary:hover {
            transform: translateY(-2px);
            box-shadow: 0 10px 20px rgba(99, 102, 241, 0.3);
        }
        
        .btn-secondary {
            background: rgba(255, 255, 255, 0.1);
            color: white;
            border: 1px solid rgba(255, 255, 255, 0.2);
        }
        
        .btn-secondary:hover {
            background: rgba(255, 255, 255, 0.15);
        }
        
        .btn:disabled {
            opacity: 0.5;
            cursor: not-allowed;
        }
        
        /* Statistics */
        .stats-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(120px, 1fr));
            gap: 1rem;
            margin-bottom: 1.5rem;
        }
        
        .stat-card {
            background: rgba(255, 255, 255, 0.05);
            padding: 1rem;
            border-radius: 12px;
            text-align: center;
            border: 1px solid rgba(255, 255, 255, 0.1);
        }
        
        .stat-value {
            font-size: 2rem;
            font-weight: 700;
            margin-bottom: 0.25rem;
        }
        
        .stat-label {
            font-size: 0.875rem;
            color: rgba(255, 255, 255, 0.7);
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }
        
        /* Results */
        .results-container {
            max-height: 500px;
            overflow-y: auto;
            padding-right: 0.5rem;
        }
        
        .results-container::-webkit-scrollbar {
            width: 8px;
        }
        
        .results-container::-webkit-scrollbar-track {
            background: rgba(255, 255, 255, 0.05);
            border-radius: 10px;
        }
        
        .results-container::-webkit-scrollbar-thumb {
            background: rgba(255, 255, 255, 0.2);
            border-radius: 10px;
        }
        
        .result-item {
            background: rgba(255, 255, 255, 0.05);
            border-radius: 12px;
            padding: 1rem;
            margin-bottom: 1rem;
            border-left: 4px solid transparent;
            animation: slideInRight 0.5s ease;
            transition: all 0.3s ease;
        }
        
        .result-item:hover {
            background: rgba(255, 255, 255, 0.08);
        }
        
        .result-item.success {
            border-left-color: var(--success);
        }
        
        .result-item.failed {
            border-left-color: var(--danger);
        }
        
        .result-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 0.75rem;
        }
        
        .result-card-info {
            display: flex;
            align-items: center;
            gap: 0.75rem;
        }
        
        .card-brand {
            padding: 0.25rem 0.75rem;
            background: rgba(255, 255, 255, 0.2);
            border-radius: 6px;
            font-size: 0.75rem;
            font-weight: 600;
            text-transform: uppercase;
        }
        
        .result-status {
            padding: 0.25rem 0.75rem;
            border-radius: 6px;
            font-size: 0.875rem;
            font-weight: 600;
        }
        
        .status-approved {
            background: rgba(16, 185, 129, 0.2);
            color: #10b981;
        }
        
        .status-declined, .status-invalid, .status-fraud, .status-error {
            background: rgba(239, 68, 68, 0.2);
            color: #ef4444;
        }
        
        .result-details {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 0.5rem;
            font-size: 0.875rem;
            color: rgba(255, 255, 255, 0.8);
        }
        
        .result-detail {
            display: flex;
            gap: 0.5rem;
        }
        
        .result-detail-label {
            font-weight: 600;
            color: rgba(255, 255, 255, 0.6);
        }
        
        /* Empty State */
        .empty-state {
            text-align: center;
            padding: 3rem;
            color: rgba(255, 255, 255, 0.6);
        }
        
        .empty-state-icon {
            font-size: 3rem;
            margin-bottom: 1rem;
            opacity: 0.5;
        }
        
        /* Loading State */
        .loading {
            display: none;
            text-align: center;
            padding: 2rem;
        }
        
        .loading.active {
            display: block;
        }
        
        .spinner {
            width: 50px;
            height: 50px;
            border: 3px solid rgba(255, 255, 255, 0.1);
            border-top-color: var(--primary);
            border-radius: 50%;
            animation: spin 1s linear infinite;
            margin: 0 auto 1rem;
        }
        
        @keyframes spin {
            to { transform: rotate(360deg); }
        }
        
        /* Animations */
        @keyframes fadeInDown {
            from {
                opacity: 0;
                transform: translateY(-20px);
            }
            to {
                opacity: 1;
                transform: translateY(0);
            }
        }
        
        @keyframes fadeInUp {
            from {
                opacity: 0;
                transform: translateY(20px);
            }
            to {
                opacity: 1;
                transform: translateY(0);
            }
        }
        
        @keyframes slideInRight {
            from {
                opacity: 0;
                transform: translateX(20px);
            }
            to {
                opacity: 1;
                transform: translateX(0);
            }
        }
        
        /* Notification */
        .notification {
            position: fixed;
            top: 2rem;
            right: -400px;
            background: rgba(255, 255, 255, 0.95);
            color: #333;
            padding: 1rem 1.5rem;
            border-radius: 10px;
            box-shadow: 0 10px 30px rgba(0, 0, 0, 0.2);
            display: flex;
            align-items: center;
            gap: 1rem;
            transition: right 0.3s ease;
            z-index: 1000;
            max-width: 350px;
        }
        
        .notification.show {
            right: 2rem;
        }
        
        .notification-icon {
            font-size: 1.5rem;
        }
        
        .notification.success .notification-icon {
            color: var(--success);
        }
        
        .notification.error .notification-icon {
            color: var(--danger);
        }
        
        .notification-message {
            flex: 1;
        }
    </style>
</head>
<body>
    <div class="container">
        <!-- Header -->
        <div class="header">
            <div class="logo">
                <h1>💳 Payment Gateway Tester</h1>
            </div>
            <p class="subtitle">Professional Card Testing & Validation Platform</p>
        </div>
        
        <!-- Main Grid -->
        <div class="main-grid">
            <!-- Left Panel - Input -->
            <div class="card">
                <div class="card-header">
                    <div class="card-icon">⚡</div>
                    <h2 class="card-title">Test Configuration</h2>
                </div>
                
                <!-- Business Type Selector -->
                <div class="business-tabs">
                    <div class="business-tab active" onclick="selectBusiness('swiggy', this)">
                        <div class="business-tab-icon">🍕</div>
                        <div class="business-tab-title">Food Delivery</div>
                        <div class="business-tab-amount">₹141.00 per transaction</div>
                    </div>
                    <div class="business-tab" onclick="selectBusiness('instamart', this)">
                        <div class="business-tab-icon">🛒</div>
                        <div class="business-tab-title">Quick Commerce</div>
                        <div class="business-tab-amount">₹726.00 per transaction</div>
                    </div>
                </div>
                
                <!-- Card Input -->
                <div class="form-group">
                    <label class="form-label">Test Cards (one per line)</label>
                    <textarea 
                        id="cardInput" 
                        class="form-textarea" 
                        placeholder="Enter cards in any format:
4111111111111111|12|25|123
5555555555554444/06/24/456
378282246310005-11-25-1234
6011111111111117 09 25 789"
                    ></textarea>
                </div>
                
                <!-- Buttons -->
                <div class="btn-group">
                    <button class="btn btn-primary" onclick="processCards()" id="processBtn">
                        <span>🚀</span>
                        Process Cards
                    </button>
                    <button class="btn btn-secondary" onclick="clearInput()">
                        <span>🗑️</span>
                        Clear
                    </button>
                    <button class="btn btn-secondary" onclick="loadSampleCards()">
                        <span>📋</span>
                        Sample
                    </button>
                </div>
            </div>
            
            <!-- Right Panel - Results -->
            <div class="card">
                <div class="card-header">
                    <div class="card-icon">📊</div>
                    <h2 class="card-title">Test Results</h2>
                </div>
                
                <!-- Statistics -->
                <div class="stats-grid" id="statsGrid" style="display: none;">
                    <div class="stat-card">
                        <div class="stat-value" id="statTotal">0</div>
                        <div class="stat-label">Total</div>
                    </div>
                    <div class="stat-card">
                        <div class="stat-value" style="color: #10b981;" id="statSuccess">0</div>
                        <div class="stat-label">Success</div>
                    </div>
                    <div class="stat-card">
                        <div class="stat-value" style="color: #ef4444;" id="statFailed">0</div>
                        <div class="stat-label">Failed</div>
                    </div>
                    <div class="stat-card">
                        <div class="stat-value" id="statRate">0%</div>
                        <div class="stat-label">Rate</div>
                    </div>
                </div>
                
                <!-- Loading State -->
                <div class="loading" id="loading">
                    <div class="spinner"></div>
                    <p>Processing cards...</p>
                </div>
                
                <!-- Results List -->
                <div class="results-container" id="resultsContainer">
                    <div class="empty-state">
                        <div class="empty-state-icon">📦</div>
                        <h3>No Results Yet</h3>
                        <p>Process some cards to see results here</p>
                    </div>
                </div>
                
                <!-- Export Button -->
                <div class="btn-group" id="exportGroup" style="display: none; margin-top: 1rem;">
                    <button class="btn btn-secondary" onclick="exportResults()">
                        <span>📥</span>
                        Export CSV
                    </button>
                </div>
            </div>
        </div>
    </div>
    
    <!-- Notification -->
    <div class="notification" id="notification">
        <div class="notification-icon" id="notificationIcon">✓</div>
        <div class="notification-message" id="notificationMessage">Success!</div>
    </div>
    
    <script>
        // Global Variables
        let selectedBusinessType = 'swiggy';
        let currentResults = [];
        
        // Business Type Selection
        function selectBusiness(type, element) {
            selectedBusinessType = type;
            document.querySelectorAll('.business-tab').forEach(tab => {
                tab.classList.remove('active');
            });
            element.classList.add('active');
        }
        
        // Process Cards
        async function processCards() {
            const cardInput = document.getElementById('cardInput');
            const cards = cardInput.value.trim();
            
            if (!cards) {
                showNotification('Please enter at least one card', 'error');
                return;
            }
            
            // Show loading
            document.getElementById('loading').classList.add('active');
            document.getElementById('processBtn').disabled = true;
            document.getElementById('resultsContainer').innerHTML = '';
            
            try {
                const response = await fetch('/api/process', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json'
                    },
                    body: JSON.stringify({
                        cards: cards,
                        businessType: selectedBusinessType
                    })
                });
                
                const data = await response.json();
                
                if (data.success) {
                    currentResults = data.results;
                    displayResults(data.results);
                    displayStatistics(data.statistics);
                    showNotification(`Processed ${data.results.length} cards successfully`, 'success');
                    
                    // Show invalid lines if any
                    if (data.statistics.invalid_lines && data.statistics.invalid_lines.length > 0) {
                        console.warn('Invalid card lines:', data.statistics.invalid_lines);
                    }
                } else {
                    showNotification(data.error || 'Processing failed', 'error');
                }
            } catch (error) {
                showNotification('Error: ' + error.message, 'error');
            } finally {
                document.getElementById('loading').classList.remove('active');
                document.getElementById('processBtn').disabled = false;
            }
        }
        
        // Display Results
        function displayResults(results) {
            const container = document.getElementById('resultsContainer');
            container.innerHTML = '';
            
            results.forEach((result, index) => {
                const isSuccess = result.success;
                const statusClass = result.status.toLowerCase();
                
                const resultHtml = `
                    <div class="result-item ${isSuccess ? 'success' : 'failed'}" style="animation-delay: ${index * 0.05}s">
                        <div class="result-header">
                            <div class="result-card-info">
                                <span class="card-brand">${result.brand.toUpperCase()}</span>
                                <span>${result.card}</span>
                            </div>
                            <span class="result-status status-${statusClass}">${result.status}</span>
                        </div>
                        <div class="result-details">
                            <div class="result-detail">
                                <span class="result-detail-label">Amount:</span>
                                <span>₹${result.amount}</span>
                            </div>
                            <div class="result-detail">
                                <span class="result-detail-label">Transaction:</span>
                                <span>${result.transaction_id}</span>
                            </div>
                            <div class="result-detail">
                                <span class="result-detail-label">Message:</span>
                                <span>${result.message}</span>
                            </div>
                            ${result.auth_code ? `
                            <div class="result-detail">
                                <span class="result-detail-label">Auth Code:</span>
                                <span>${result.auth_code}</span>
                            </div>
                            ` : ''}
                        </div>
                    </div>
                `;
                
                container.insertAdjacentHTML('beforeend', resultHtml);
            });
            
            document.getElementById('exportGroup').style.display = 'flex';
        }
        
        // Display Statistics
        function displayStatistics(stats) {
            document.getElementById('statsGrid').style.display = 'grid';
            document.getElementById('statTotal').textContent = stats.total;
            document.getElementById('statSuccess').textContent = stats.successful;
            document.getElementById('statFailed').textContent = stats.failed;
            document.getElementById('statRate').textContent = stats.success_rate + '%';
        }
        
        // Clear Input
        function clearInput() {
            document.getElementById('cardInput').value = '';
            showNotification('Input cleared', 'success');
        }
        
        // Load Sample Cards
        function loadSampleCards() {
            const samples = [
                '4111111111111111|12|25|123',
                '5555555555554444|06|24|456',
                '378282246310005|11|25|1234',
                '6011111111111117|09|25|789',
                '3530111333300000|07|25|456',
                '4000056655665556|10|25|999',
                '5200828282828210|04|25|123',
                '371449635398431|12|24|9999'
            ];
            
            document.getElementById('cardInput').value = samples.join('\\n');
            showNotification('Sample cards loaded', 'success');
        }
        
        // Export Results
        async function exportResults() {
            if (!currentResults || currentResults.length === 0) {
                showNotification('No results to export', 'error');
                return;
            }
            
            try {
                const response = await fetch('/api/export', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json'
                    },
                    body: JSON.stringify({
                        results: currentResults
                    })
                });
                
                if (response.ok) {
                    const blob = await response.blob();
                    const url = window.URL.createObjectURL(blob);
                    const a = document.createElement('a');
                    a.href = url;
                    a.download = `payment_results_${Date.now()}.csv`;
                    document.body.appendChild(a);
                    a.click();
                    document.body.removeChild(a);
                    window.URL.revokeObjectURL(url);
                    
                    showNotification('Results exported successfully', 'success');
                } else {
                    throw new Error('Export failed');
                }
            } catch (error) {
                showNotification('Failed to export results', 'error');
            }
        }
        
        // Show Notification
        function showNotification(message, type = 'success') {
            const notification = document.getElementById('notification');
            const icon = document.getElementById('notificationIcon');
            const messageEl = document.getElementById('notificationMessage');
            
            notification.className = 'notification ' + type;
            icon.textContent = type === 'success' ? '✓' : '✗';
            messageEl.textContent = message;
            
            notification.classList.add('show');
            
            setTimeout(() => {
                notification.classList.remove('show');
            }, 4000);
        }
        
        // Initialize
        document.addEventListener('DOMContentLoaded', () => {
            // Load sample cards on start
            loadSampleCards();
        });
        
        // Keyboard shortcuts
        document.addEventListener('keydown', (e) => {
            if (e.ctrlKey || e.metaKey) {
                if (e.key === 'Enter') {
                    processCards();
                } else if (e.key === 'l') {
                    e.preventDefault();
                    loadSampleCards();
                } else if (e.key === 'k') {
                    e.preventDefault();
                    clearInput();
                }
            }
        });
    </script>
</body>
</html>
'''

# ============================================================================
# AUTO-LAUNCH BROWSER
# ============================================================================

def open_browser():
    """Open browser after server starts"""
    time.sleep(1.5)  # Wait for server to start
    webbrowser.open('http://localhost:5000')

# ============================================================================
# MAIN EXECUTION
# ============================================================================

if __name__ == '__main__':
    print("\n" + "="*60)
    print("💳 PAYMENT GATEWAY TESTER - PROFESSIONAL EDITION")
    print("="*60)
    print("\n📌 Starting server...")
    print("🌐 Open your browser at: http://localhost:5000")
    print("⌨️  Press Ctrl+C to stop the server\n")
    print("Keyboard Shortcuts:")
    print("  • Ctrl+Enter : Process cards")
    print("  • Ctrl+L     : Load sample cards") 
    print("  • Ctrl+K     : Clear input\n")
    print("="*60 + "\n")
    
    # Open browser in a separate thread
    browser_thread = threading.Thread(target=open_browser)
    browser_thread.daemon = True
    browser_thread.start()
    
    # Run Flask app
    try:
        app.run(host='0.0.0.0', port=5000, debug=False)
    except KeyboardInterrupt:
        print("\n\n✨ Server stopped successfully. Goodbye!")
        sys.exit(0)
