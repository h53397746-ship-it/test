#!/usr/bin/env python3
"""
HRK NumInfo API - single-file FastAPI app, ready for Render.

Endpoints:
- GET /hrk/numinfo/num={number}      (path param)
- GET /hrk/numinfo?num={number}     (query param)

Environment variables:
- POSTGRES_URI (required) e.g. postgres://user:pass@host:5432/db
- USERS_TABLE (optional, default 'users_data')
- MONGO_URI (optional) -> if provided we check 'blacklist' collection for blocked numbers
- POOL_MIN (optional, default 1)
- POOL_MAX (optional, default 10)
- PORT (optional, used by Render/uvicorn externally)

Run locally:
pip install fastapi uvicorn psycopg2-binary pymongo
POSTGRES_URI="postgres://user:pass@localhost:5432/db" uvicorn api_numinfo:app --host 0.0.0.0 --port 8000
"""

import os
import re
import logging
from typing import List, Dict, Any, Optional
from datetime import datetime

from fastapi import FastAPI, HTTPException, Path, Query
from pydantic import BaseModel
import psycopg2
from psycopg2.extras import RealDictCursor
from psycopg2.pool import SimpleConnectionPool

# Optional Mongo
try:
    from pymongo import MongoClient
except Exception:
    MongoClient = None

# ---------------- Config & Logging ----------------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("hrk_numinfo_api")

POSTGRES_URI = os.getenv("POSTGRES_URI")
if not POSTGRES_URI:
    logger.error("POSTGRES_URI environment variable is not set. Exiting.")
    raise RuntimeError("POSTGRES_URI environment variable is required")

USERS_TABLE = os.getenv("USERS_TABLE", "users_data")
MONGO_URI = os.getenv("MONGO_URI", None)

POOL_MIN = int(os.getenv("POOL_MIN", "1"))
POOL_MAX = int(os.getenv("POOL_MAX", "10"))

# ---------------- FastAPI app ----------------
app = FastAPI(title="HRK NumInfo API", version="1.1")

# Global variables to be set on startup
pg_pool: Optional[SimpleConnectionPool] = None
mongo_client = None
mongo_db = None

# ---------------- Utilities ----------------
def normalize_mobile(raw: str) -> Optional[str]:
    """
    Normalize to a 10-digit Indian mobile string (e.g., '9876543210').
    Accepts inputs like '9876543210', '+919876543210', '919876543210', or strings with separators.
    Returns None if invalid.
    """
    if not raw or not isinstance(raw, str):
        return None
    digits = re.sub(r"\D", "", raw)
    if len(digits) > 10 and digits.startswith("91"):
        digits = digits[2:]
    if len(digits) == 10 and digits[0] in "6789":
        return digits
    return None

def get_pg_conn():
    """
    Checkout a connection from the psycopg2 SimpleConnectionPool.
    Caller must put it back using conn.close() (connection pool returns connection on close).
    """
    global pg_pool
    if pg_pool is None:
        raise RuntimeError("Postgres pool not initialized")
    try:
        conn = pg_pool.getconn()
        return conn
    except Exception as e:
        logger.exception("Failed to get Postgres connection from pool: %s", e)
        raise

def release_pg_conn(conn):
    """Return connection to pool (close actually returns it)."""
    global pg_pool
    try:
        # rollback any open transaction and then put back
        conn.rollback()
    except Exception:
        pass
    try:
        pg_pool.putconn(conn)
    except Exception:
        try:
            conn.close()
        except Exception:
            pass

def check_blacklist(mobile: str) -> bool:
    """
    If Mongo configured, check the 'blacklist' collection for an active entry for the mobile.
    Returns True if blocked.
    """
    global mongo_db
    if not MONGO_URI or mongo_db is None:
        return False
    try:
        # assume mobile stored as string "9876543210"
        doc = mongo_db.blacklist.find_one({"mobile": mobile, "is_active": True})
        return bool(doc)
    except Exception as e:
        logger.exception("Mongo blacklist check failed: %s", e)
        # In case of error, do NOT block (fail-open)
        return False

# ---------------- Pydantic Models ----------------
class NumInfoResponse(BaseModel):
    query: str
    normalized: Optional[str]
    found: bool
    count: int
    results: List[Dict[str, Any]]
    blocked: bool = False
    message: Optional[str] = None
    queried_at: str

# ---------------- Startup / Shutdown ----------------
@app.on_event("startup")
def on_startup():
    global pg_pool, mongo_client, mongo_db
    logger.info("Starting HRK NumInfo API - initializing Postgres pool")
    try:
        pg_pool = SimpleConnectionPool(POOL_MIN, POOL_MAX, dsn=POSTGRES_URI, cursor_factory=RealDictCursor)
        # quick sanity check
        conn = pg_pool.getconn()
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            _ = cur.fetchone()
        pg_pool.putconn(conn)
    except Exception as e:
        logger.exception("Failed to create or test Postgres connection pool: %s", e)
        raise

    if MONGO_URI:
        if MongoClient is None:
            logger.warning("pymongo not installed; ignoring MONGO_URI")
        else:
            try:
                logger.info("Connecting to Mongo (for blacklist checks)")
                mongo_client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
                mongo_db = mongo_client.get_database()  # default DB from URI
                # quick ping
                mongo_client.admin.command("ping")
            except Exception as e:
                logger.exception("Failed to connect to MongoDB: %s", e)
                # leave mongo_db None to avoid blocking functionality
                mongo_db = None

@app.on_event("shutdown")
def on_shutdown():
    global pg_pool, mongo_client
    logger.info("Shutting down HRK NumInfo API - closing DB connections")
    try:
        if pg_pool is not None:
            pg_pool.closeall()
    except Exception:
        pass
    try:
        if mongo_client:
            mongo_client.close()
    except Exception:
        pass

# ---------------- Core search function ----------------
def search_postgres_by_mobile(mobile: str) -> List[Dict[str, Any]]:
    """
    Query the USERS_TABLE for rows where mobile = %s.
    Returns list of cleaned dict results (no null/empty fields).
    """
    conn = None
    try:
        conn = get_pg_conn()
        with conn.cursor() as cur:
            # Parameterized query to avoid SQL injection
            sql = f"SELECT * FROM {USERS_TABLE} WHERE mobile = %s"
            cur.execute(sql, (mobile,))
            rows = cur.fetchall()  # RealDictCursor -> list of dicts
            results = []
            for r in rows:
                cleaned = {k: v for k, v in r.items() if v is not None and v != ""}
                results.append(cleaned)
            return results
    except Exception as e:
        logger.exception("Postgres search error for %s: %s", mobile, e)
        raise
    finally:
        if conn:
            release_pg_conn(conn)

# ---------------- Endpoints ----------------
@app.get("/", tags=["health"])
def root():
    return {"status": "ok", "message": "HRK NumInfo API. Use /hrk/numinfo/num={number} or /hrk/numinfo?num={number}"}

@app.get("/hrk/numinfo/num={number}", response_model=NumInfoResponse)
def numinfo_path(number: str = Path(..., description="Phone number (path param). Accepts +91, 91 or 10-digit")):
    """
    Path-based endpoint. Example:
    GET /hrk/numinfo/num=+919876543210
    """
    return _handle_numinfo_request(number)

@app.get("/hrk/numinfo", response_model=NumInfoResponse)
def numinfo_query(num: str = Query(..., description="Phone number (query param). Accepts +91, 91 or 10-digit")):
    """
    Query-based endpoint. Example:
    GET /hrk/numinfo?num=9876543210
    """
    return _handle_numinfo_request(num)

def _handle_numinfo_request(raw_number: str) -> NumInfoResponse:
    queried_at = datetime.utcnow().isoformat() + "Z"
    normalized = normalize_mobile(raw_number)
    if not normalized:
        raise HTTPException(status_code=400, detail="Invalid phone number. Provide a valid 10-digit Indian mobile (or +91 prefixed).")

    # Optionally check blacklist first
    blocked = False
    try:
        blocked = check_blacklist(normalized)
    except Exception:
        # Already logged; fail-open (do not block) on errors
        blocked = False

    # If blocked, optionally return early with blocked: true and no results
    if blocked:
        return NumInfoResponse(
            query=raw_number,
            normalized=normalized,
            found=False,
            count=0,
            results=[],
            blocked=True,
            message="Number is blacklisted and blocked from search.",
            queried_at=queried_at
        )

    # Search Postgres for the normalized number
    try:
        results = search_postgres_by_mobile(normalized)
    except Exception as e:
        logger.exception("Database error when searching for %s", normalized)
        raise HTTPException(status_code=500, detail="Internal database error.")

    return NumInfoResponse(
        query=raw_number,
        normalized=normalized,
        found=len(results) > 0,
        count=len(results),
        results=results,
        blocked=False,
        message=None if results else "No data found for this number in Postgres.",
        queried_at=queried_at
    )
