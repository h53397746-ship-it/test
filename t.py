#!/usr/bin/env python3
"""
API endpoint that searches Postgres for a mobile number and returns JSON.

Endpoint:
    GET /hrk/numinfo/num={number}

Config:
    - Set environment variable POSTGRES_URI or edit POSTGRES_URI below.
    - The Postgres table expected is `users_data` with columns like:
      mobile, name, fname, address, alt, circle, aadhar, email (adjust query if yours differs).

Run:
    pip install fastapi uvicorn psycopg2-binary
    POSTGRES_URI="postgres://user:pass@localhost:5432/dbname" uvicorn api_numinfo:app --host 0.0.0.0 --port 8000

Example:
    curl "http://localhost:8000/hrk/numinfo/num=+919876543210"
"""

import os
import re
from typing import List, Dict, Any, Optional

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel
import psycopg2
from psycopg2.extras import RealDictCursor

# ----------------- Configuration -----------------
POSTGRES_URI = os.getenv("POSTGRES_URI", "postgres://user:pass@localhost:5432/dbname")
# Name of the table where mobile data is stored
USERS_TABLE = os.getenv("USERS_TABLE", "users_data")

# ----------------- App & DB -----------------
app = FastAPI(title="HRK NumInfo API",
              description="Search Postgres 'users_data' for a phone number and return JSON-formatted results.",
              version="1.0")

def get_pg_conn():
    """
    Create and return a new Postgres connection.
    Using a new connection per request keeps this simple and safe for small deployments.
    For production, use a connection pool (e.g. psycopg2.pool or asyncpg + pool).
    """
    try:
        conn = psycopg2.connect(POSTGRES_URI, cursor_factory=RealDictCursor)
        return conn
    except Exception as e:
        raise

# ----------------- Utilities -----------------
def normalize_mobile(raw: str) -> Optional[str]:
    """
    Normalize phone input to 10-digit Indian mobile, or return None if invalid.
    Accepts:
      - 9876543210
      - +919876543210
      - 919876543210
      - any string with digits (non-digits removed)
    """
    if not raw or not isinstance(raw, str):
        return None
    digits = re.sub(r"\D", "", raw)
    # strip leading country code 91 if present and length > 10
    if len(digits) > 10 and digits.startswith("91"):
        digits = digits[2:]
    if len(digits) == 10 and digits[0] in "6789":
        return digits
    return None

# ----------------- Response models -----------------
class ResultItem(BaseModel):
    mobile: Optional[str] = None
    # include optional fields present in your users_data table
    name: Optional[str] = None
    fname: Optional[str] = None
    address: Optional[str] = None
    alt: Optional[str] = None
    circle: Optional[str] = None
    aadhar: Optional[str] = None
    email: Optional[str] = None
    # any extra fields will appear as well (left as-is in dict)

class NumInfoResponse(BaseModel):
    query: str
    normalized: Optional[str]
    found: bool
    count: int
    results: List[Dict[str, Any]]
    message: Optional[str] = None

# ----------------- Main endpoint -----------------
@app.get("/hrk/numinfo/num={number}", response_model=NumInfoResponse)
def numinfo(number: str = Query(..., description="Phone number to search (10-digit or +91 prefixed)")):
    """
    Search the users_data table for the provided phone number and return JSON.
    """
    normalized = normalize_mobile(number)
    if not normalized:
        raise HTTPException(status_code=400, detail="Invalid phone number format. Provide a 10-digit Indian mobile (or +91 prefixed).")

    # Query Postgres
    try:
        conn = get_pg_conn()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to connect to Postgres: {str(e)}")

    try:
        with conn.cursor() as cur:
            # Adjust selected columns to match your schema. Selecting all columns is fine if you prefer.
            sql = f"""
                SELECT *
                FROM {USERS_TABLE}
                WHERE mobile = %s
            """
            cur.execute(sql, (normalized,))
            rows = cur.fetchall()
    except Exception as e:
        conn.close()
        raise HTTPException(status_code=500, detail=f"Database query error: {str(e)}")
    finally:
        # ensure connection closed
        try:
            conn.close()
        except Exception:
            pass

    # Build response
    results = []
    for r in rows:
        # psycopg2 RealDictCursor gives dict-like rows. Keep values as-is, but convert any non-JSONable if needed.
        # Remove null/empty fields for cleaner output (optional)
        cleaned = {k: v for k, v in r.items() if v is not None and v != ""}
        results.append(cleaned)

    resp = NumInfoResponse(
        query=number,
        normalized=normalized,
        found=len(results) > 0,
        count=len(results),
        results=results,
        message=None if results else "No data found for this number in Postgres."
    )
    return resp

# ----------------- Health & simple root -----------------
@app.get("/", tags=["health"])
def root():
    return {"status": "ok", "message": "HRK NumInfo API. Use /hrk/numinfo/num={number}"}
