#!/usr/bin/env python3
"""
HRK NumInfo API - Simple Flask Version
Perfect for Render Deployment
"""

import os
import re
import json
import psycopg2
from psycopg2.extras import RealDictCursor
from flask import Flask, request, jsonify

# ---------------- CONFIG ----------------
POSTGRES_URI = os.getenv("POSTGRES_URI")
USERS_TABLE = os.getenv("USERS_TABLE", "users_data")
MONGO_URI = os.getenv("MONGO_URI")

# Optional Mongo
mongo_client = None
mongo_db = None

try:
    if MONGO_URI:
        from pymongo import MongoClient
        mongo_client = MongoClient(MONGO_URI)
        mongo_db = mongo_client.get_database()
except Exception:
    mongo_client = None
    mongo_db = None


# ---------------- FLASK APP ----------------
app = Flask(__name__)


# ---------------- UTILITIES ----------------
def normalize_mobile(raw):
    """Convert into 10 digit Indian mobile number."""
    if not raw:
        return None
    
    digits = re.sub(r"\D", "", raw)

    if len(digits) > 10 and digits.startswith("91"):
        digits = digits[2:]

    if len(digits) == 10 and digits[0] in "6789":
        return digits

    return None


def is_blacklisted(mobile):
    """Check blacklist in Mongo if enabled."""
    if mongo_db is None:
        return False
    try:
        doc = mongo_db.blacklist.find_one({"mobile": mobile, "is_active": True})
        return bool(doc)
    except Exception:
        return False


def search_postgres(mobile):
    """Query Postgres table."""
    conn = psycopg2.connect(POSTGRES_URI, cursor_factory=RealDictCursor)
    cur = conn.cursor()

    cur.execute(f"SELECT * FROM {USERS_TABLE} WHERE mobile = %s", (mobile,))
    rows = cur.fetchall()

    cur.close()
    conn.close()

    cleaned = []
    for r in rows:
        cleaned.append({k: v for k, v in r.items() if v not in (None, "", "None")})

    return cleaned


# ---------------- ROUTES ----------------

@app.route("/")
def home():
    return jsonify({
        "status": "ok",
        "message": "HRK NumInfo Flask API working.",
        "usage": [
            "/hrk/numinfo?num=9876543210",
            "/hrk/numinfo/num=9876543210"
        ]
    })


@app.route("/hrk/numinfo")
def query_api():
    """Query parameter usage."""
    number = request.args.get("num")
    return process_number(number)


@app.route("/hrk/numinfo/num=<number>")
def path_api(number):
    """Path parameter usage."""
    return process_number(number)


def process_number(raw_number):
    """Main processing function."""
    normalized = normalize_mobile(raw_number)

    if not normalized:
        return jsonify({
            "query": raw_number,
            "normalized": None,
            "found": False,
            "blocked": False,
            "message": "Invalid mobile number. Use 10-digit Indian mobile.",
            "results": []
        }), 400

    # Check blacklist
    blocked = is_blacklisted(normalized)
    if blocked:
        return jsonify({
            "query": raw_number,
            "normalized": normalized,
            "found": False,
            "blocked": True,
            "message": "Number is blacklisted.",
            "results": []
        })

    # Search Postgres
    try:
        results = search_postgres(normalized)
    except Exception as e:
        return jsonify({
            "error": "Database error",
            "details": str(e)
        }), 500

    return jsonify({
        "query": raw_number,
        "normalized": normalized,
        "found": len(results) > 0,
        "blocked": False,
        "count": len(results),
        "results": results,
        "message": None if results else "No data found"
    })


# ---------------- RUN (LOCAL) ----------------
if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    app.run(host="0.0.0.0", port=port)
