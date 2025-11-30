# app/twilio_client.py
"""
Twilio helper utilities (sync + async wrappers).

This module supports two modes:
1. Twilio Verify Service (if TWILIO_VERIFY_SERVICE_SID is set) -- preferred.
2. Fallback: send a plain SMS containing a numeric OTP using Twilio Messages API.

It exposes both synchronous functions (send_otp, check_otp, send_sms) and
async wrappers (send_otp_async, check_otp_async, send_sms_async) which use
asyncio.to_thread to avoid blocking an async event loop.

Environment variables:
 - TWILIO_ACCOUNT_SID
 - TWILIO_AUTH_TOKEN
 - TWILIO_VERIFY_SERVICE_SID (optional; if present, use Verify API)
 - TWILIO_FROM_NUMBER (optional; phone number used to send SMS if Verify not used)
"""
from __future__ import annotations
import os
import secrets
import logging
import asyncio
from typing import Optional
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger("twilio_client")

TW_ACCOUNT = os.getenv("TWILIO_ACCOUNT_SID")
TW_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TW_VERIFY_SID = os.getenv("TWILIO_VERIFY_SERVICE_SID")
TW_FROM_NUMBER = os.getenv("TWILIO_FROM_NUMBER")  # e.g. "+12345556789"

# Lazy import Twilio client only when needed to avoid hard dependency on import time
_client = None


def _ensure_client():
    global _client
    if _client is not None:
        return _client
    if not TW_ACCOUNT or not TW_TOKEN:
        logger.warning("Twilio credentials not configured (TWILIO_ACCOUNT_SID/TWILIO_AUTH_TOKEN).")
        return None
    try:
        from twilio.rest import Client  # local import
    except Exception:
        logger.exception("Twilio package not installed.")
        return None
    _client = Client(TW_ACCOUNT, TW_TOKEN)
    return _client


# -------------------------
# Utilities
# -------------------------
def _generate_numeric_code(length: int = 6) -> str:
    """Generate a secure numeric OTP of given length (default 6)."""
    alphabet = "0123456789"
    return "".join(secrets.choice(alphabet) for _ in range(length))


# -------------------------
# Sync API
# -------------------------
def send_otp(phone: str, code_length: int = 6) -> dict:
    """
    Send an OTP to the given phone number.

    If TWILIO_VERIFY_SERVICE_SID is set, this uses the Verify API and returns
    {"status": "pending", "sid": "<verify-sid>"}.

    Otherwise, it sends a plain SMS from TWILIO_FROM_NUMBER with a generated code,
    and returns {"status":"sent", "code": "<the-code>"}.

    Note: when falling back to SMS, the generated code is returned so the caller
    can persist it (e.g. using app.crud.create_otp).
    """
    client = _ensure_client()
    if not client:
        # No Twilio client configured
        code = _generate_numeric_code(code_length)
        logger.warning("Twilio client unavailable — fallback generated code but did not send SMS.")
        return {"status": "no_client", "code": code}

    if TW_VERIFY_SID:
        try:
            ver = client.verify.services(TW_VERIFY_SID).verifications.create(to=phone, channel="sms")
            return {"status": getattr(ver, "status", "pending"), "sid": getattr(ver, "sid", None)}
        except Exception as e:
            logger.exception("Twilio Verify failed: %s", e)
            # fallback to SMS below
    # Fallback: send SMS with numeric code
    code = _generate_numeric_code(code_length)
    if not TW_FROM_NUMBER:
        logger.error("TWILIO_FROM_NUMBER is not set; cannot send SMS. Returning code to caller.")
        return {"status": "no_from_number", "code": code}
    try:
        msg = client.messages.create(body=f"Your verification code is: {code}", from_=TW_FROM_NUMBER, to=phone)
        return {"status": getattr(msg, "status", "sent"), "sid": getattr(msg, "sid", None), "code": code}
    except Exception as e:
        logger.exception("Twilio Messages API failed: %s", e)
        return {"status": "failed", "error": str(e)}


def check_otp(phone: str, code: str) -> bool:
    """
    Verify a code. If using Verify API, call verification_checks.create; otherwise,
    the caller must check the code against stored OTP records (this function cannot
    validate fallback codes since it doesn't store them).

    Returns True if verified, False otherwise.
    """
    client = _ensure_client()
    if not client:
        logger.warning("Twilio client not configured; cannot verify using Verify API.")
        return False

    if TW_VERIFY_SID:
        try:
            chk = client.verify.services(TW_VERIFY_SID).verification_checks.create(to=phone, code=code)
            return getattr(chk, "status", "") == "approved"
        except Exception as e:
            logger.exception("Twilio Verify check failed: %s", e)
            return False

    # If no Verify service configured, we cannot validate here — caller should verify via DB
    logger.warning("No TWILIO_VERIFY_SERVICE_SID configured: check_otp cannot validate SMS fallback codes.")
    return False


def send_sms(to: str, body: str) -> dict:
    """
    Send a plain SMS (synchronous).
    Returns Twilio message dict-like info or {"status":"no_client"} if client missing.
    """
    client = _ensure_client()
    if not client:
        logger.warning("Twilio client not configured; cannot send SMS.")
        return {"status": "no_client"}
    if not TW_FROM_NUMBER:
        logger.error("TWILIO_FROM_NUMBER not configured; cannot send SMS.")
        return {"status": "no_from_number"}
    try:
        msg = client.messages.create(body=body, from_=TW_FROM_NUMBER, to=to)
        return {"status": getattr(msg, "status", "sent"), "sid": getattr(msg, "sid", None)}
    except Exception as e:
        logger.exception("Twilio send_sms failed: %s", e)
        return {"status": "failed", "error": str(e)}


# -------------------------
# Async wrappers
# -------------------------
async def send_otp_async(phone: str, code_length: int = 6) -> dict:
    return await asyncio.to_thread(send_otp, phone, code_length)


async def check_otp_async(phone: str, code: str) -> bool:
    return await asyncio.to_thread(check_otp, phone, code)


async def send_sms_async(to: str, body: str) -> dict:
    return await asyncio.to_thread(send_sms, to, body)


# -------------------------
# Small demo utility
# -------------------------
def demo_send_and_return_code(phone: str) -> str:
    """
    Convenience for local demos: tries to send via Twilio but if not configured returns the generated code.
    Use only in development/testing.
    """
    res = send_otp(phone)
    if res.get("status") in ("sent", "pending", "approved", "no_client", "no_from_number"):
        return res.get("code")  # may be None when using Verify API
    return ""
