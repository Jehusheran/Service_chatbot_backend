# app/routers/auth_routes.py
"""
Authentication routes (Quart Blueprint)

Routes:
 - POST /create_agent         -> create an agent (email + password)
 - POST /login                -> agent login (email + password) -> returns JWT
 - POST /otp/send             -> send OTP to phone (customer)
 - POST /otp/verify           -> verify OTP; creates/updates customer and returns JWT
 - GET  /me                   -> return subject from Authorization: Bearer <token>
"""
from __future__ import annotations
import uuid
from datetime import timedelta

from quart import Blueprint, request, jsonify, current_app
from marshmallow import ValidationError

from .. import auth as auth_tools
from .. import crud
from ..schemas import AgentCreateSchema, OTPRequestSchema, OTPVerifySchema
from ..db import get_session
from ..twilio_client import send_otp_async, check_otp_async
from ..utils import generate_otp

bp = Blueprint("auth_routes", __name__)

# ---------------------------
# Helper: read Authorization header
# ---------------------------
def _extract_bearer_token(headers) -> str | None:
    auth = headers.get("Authorization") or headers.get("authorization")
    if not auth:
        return None
    parts = auth.split()
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1]
    return None


# ---------------------------
# Create agent
# ---------------------------
@bp.route("/create_agent", methods=["POST"])
async def create_agent():
    """
    Create a new agent:
    Body: { email, password, name? }
    Returns: { agent_id, email }
    """
    try:
        payload = await request.get_json()
    except Exception:
        return jsonify({"error": "invalid json"}), 400

    try:
        data = AgentCreateSchema().load(payload)
    except ValidationError as e:
        return jsonify({"error": "validation", "details": e.messages}), 400

    email = data["email"]
    password = data["password"]
    name = data.get("name")

    async for session in get_session():
        existing = await crud.get_agent_by_email(session, email)
        if existing:
            return jsonify({"error": "agent_exists", "email": email}), 409

        agent_id = "agent-" + uuid.uuid4().hex[:8]
        hashed = auth_tools.hash_password(password)
        agent = await crud.create_agent(session, agent_id=agent_id, email=email, password_hash=hashed, name=name)
        return jsonify({"agent_id": agent.agent_id, "email": agent.email}), 201


# ---------------------------
# Agent login
# ---------------------------
@bp.route("/login", methods=["POST"])
async def login_agent():
    """
    Agent login with email + password:
    Body: { email, password }
    Returns: { access_token, agent_id }
    """
    try:
        payload = await request.get_json()
    except Exception:
        return jsonify({"error": "invalid json"}), 400

    email = (payload or {}).get("email")
    password = (payload or {}).get("password")
    if not email or not password:
        return jsonify({"error": "email_and_password_required"}), 400

    async for session in get_session():
        agent = await crud.get_agent_by_email(session, email)
        if not agent:
            return jsonify({"error": "invalid_credentials"}), 401
        if not auth_tools.verify_password(password, agent.password_hash or ""):
            return jsonify({"error": "invalid_credentials"}), 401

        token = auth_tools.create_access_token(agent.agent_id)
        return jsonify({"access_token": token, "agent_id": agent.agent_id})


# ---------------------------
# Send OTP (customer)
# ---------------------------
@bp.route("/otp/send", methods=["POST"])
async def otp_send():
    """
    Send OTP to a phone number.
    Body: { phone }
    Returns: { status, maybe code (dev fallback) }
    """
    try:
        payload = await request.get_json()
    except Exception:
        return jsonify({"error": "invalid json"}), 400

    try:
        data = OTPRequestSchema().load(payload)
    except ValidationError as e:
        return jsonify({"error": "validation", "details": e.messages}), 400

    phone = data["phone"]

    # Generate code and persist via crud; send via Twilio (async)
    code = generate_otp(6)

    async for session in get_session():
        otp_row = await crud.create_otp(session, phone=phone, code=code, valid_for_seconds=300)

    # Send via twilio (async). twilio client may return the code in fallback mode,
    # so include it in development responses (but avoid returning code in prod).
    try:
        twres = await send_otp_async(phone, code_length=6)
    except Exception as e:
        current_app.logger.exception("Twilio send failed: %s", e)
        twres = {"status": "failed", "error": str(e)}

    # In development, include the code when Twilio not configured
    if twres.get("status") in ("no_client", "no_from_number", "no_client"):
        return jsonify({"status": "dev", "code": code})

    return jsonify({"status": twres.get("status", "sent"), "twilio": twres})


# ---------------------------
# Verify OTP (customer)
# ---------------------------
@bp.route("/otp/verify", methods=["POST"])
async def otp_verify():
    """
    Verify OTP for phone. If valid, create or update Customer record and return a JWT for that customer.
    Body: { phone, code, name? , email? }
    """
    try:
        payload = await request.get_json()
    except Exception:
        return jsonify({"error": "invalid json"}), 400

    try:
        data = OTPVerifySchema().load(payload)
    except ValidationError as e:
        return jsonify({"error": "validation", "details": e.messages}), 400

    phone = data["phone"]
    code = data["code"]
    # optional metadata
    name = (payload or {}).get("name")
    email = (payload or {}).get("email")

    # If Twilio Verify is present, prefer verifying via Twilio; otherwise verify via DB
    twilio_verified = None
    try:
        twilio_verified = await check_otp_async(phone, code)
    except Exception:
        current_app.logger.exception("Twilio verify exception (falling back to DB): %s", phone)

    async for session in get_session():
        verified = False
        if twilio_verified is True:
            verified = True
        else:
            # fallback to DB-stored OTP
            verified = await crud.verify_otp_code(session, phone, code)

        if not verified:
            return jsonify({"error": "invalid_or_expired_code"}), 400

        # Create or update customer
        # customer_id can be phone-based or generated id; we'll use phone as customer_id for simplicity
        customer_id = phone
        customer = await crud.create_or_update_customer(session, customer_id=customer_id, name=name, email=email, phone=phone)

        # create JWT token for customer
        token = auth_tools.create_access_token(subject=customer.customer_id)
        return jsonify({"access_token": token, "customer_id": customer.customer_id})


# ---------------------------
# Who am I (convenience)
# ---------------------------
@bp.route("/me", methods=["GET"])
async def whoami():
    """
    Return the subject from Authorization header (Bearer token). This is a convenience
    endpoint useful for frontend to check logged-in identity.
    """
    token = _extract_bearer_token(request.headers)
    if not token:
        return jsonify({"error": "missing_token"}), 401
    subject = auth_tools.decode_access_token(token)
    if not subject:
        return jsonify({"error": "invalid_token"}), 401
    return jsonify({"sub": subject})
