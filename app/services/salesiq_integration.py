# app/services/salesiq_integration.py
"""
SalesIQ webhook integration service.

This module provides a Quart Blueprint that accepts incoming webhooks from
Zoho SalesIQ (Zobot) and converts them into your application's internal
message records (customer/agent/bot). It is intentionally flexible — SalesIQ
webhook payloads can vary, so the code attempts to map common fields and
falls back to reasonable defaults.

Behavior:
 - POST /v1/salesiq/webhook
     Accepts JSON payloads from SalesIQ. Common shapes supported:
       - { "contact": {"phone":"+..","email":"..","name":".."}, "message": {"content":"..."}, "visitorId": "...", "agentId":"..." }
       - { "visitor": {...}, "text": "...", "source": "bot|visitor|agent" }
 - The handler will:
     - Extract customer identifier (phone -> email -> visitorId)
     - Map agentId if present
     - Save the incoming message to DB via crud.save_message
     - If the message came from a visitor and no agent was assigned, optionally generate a bot reply (via llm.generate_bot_suggestions)
     - Reply with 200 and a small JSON acknowledging reception

Notes:
 - This Blueprint is consistent with the rest of the project which uses
   Quart-style blueprints for async endpoints.
 - Adapt the payload parsing logic to the exact SalesIQ contract you receive.
"""
from __future__ import annotations
import asyncio
from typing import Any, Dict, Optional
from quart import Blueprint, request, jsonify, current_app

from ..db import get_session
from .. import crud
from ..services import llm
from ..utils import generate_id

bp = Blueprint("salesiq_integration", __name__)


def _extract_field(d: Dict[str, Any], *keys):
    """Return first found key in dict, or None."""
    for k in keys:
        if not d:
            continue
        if isinstance(d, dict) and k in d:
            return d[k]
    return None


async def _maybe_generate_bot_reply(session, customer_id: str, agent_id: Optional[str], message_text: str) -> Optional[Dict[str, Any]]:
    """If the incoming message is from a visitor with no agent, optionally generate a bot reply and persist it."""
    try:
        # Only generate bot replies when there is no agent assigned
        if agent_id:
            return None
        suggestions = await llm.generate_bot_suggestions(message_text, n=1)
        if suggestions:
            bot_text = suggestions[0]
            bot_saved = await crud.save_message(
                session=session,
                customer_id=customer_id,
                agent_id=None,
                sender="bot",
                message=bot_text,
                meta={"source": "salesiq_auto_reply"},
            )
            return {
                "bot_message_id": bot_saved.message_id,
                "text": bot_text,
            }
    except Exception as e:
        current_app.logger.exception("Failed generating bot reply for %s: %s", customer_id, e)
    return None


@bp.route("/v1/salesiq/webhook", methods=["POST"])
async def salesiq_webhook():
    """Primary webhook receiver for SalesIQ / Zobot events.

    Expected JSON payloads vary; we try to support multiple shapes.
    """
    try:
        payload = await request.get_json()
    except Exception:
        return jsonify({"error": "invalid_json"}), 400

    # Common SalesIQ fields may include: visitorId / visitor / contact / message / text / from / agentId / user
    # We'll attempt to extract the most useful bits with fallbacks.
    # 1) Identify visitor / customer
    visitor = _extract_field(payload, "visitor", "contact", "visitorInfo", "user") or {}
    visitor_id = _extract_field(payload, "visitorId", "visitor_id", "visitorid") or visitor.get("id") or payload.get("visitorId")

    # Try phone, then email, then fallback to visitor_id
    phone = _extract_field(visitor, "phone", "mobile", "contact_number") or _extract_field(payload, "phone")
    email = _extract_field(visitor, "email", "contact_email") or _extract_field(payload, "email")
    name = _extract_field(visitor, "name", "displayName") or _extract_field(payload, "name")

    # Derive a stable customer_id for DB use. Prefer phone -> email -> visitorId
    if phone:
        customer_id = str(phone)
    elif email:
        customer_id = str(email).lower()
    elif visitor_id:
        customer_id = f"visitor-{visitor_id}"
    else:
        # last resort: generate a temporary id (not ideal for long term)
        customer_id = f"anon-{generate_id('visitor')}"

    # 2) Identify agent (if any)
    agent_id = _extract_field(payload, "agentId", "agent_id", "agent")
    if isinstance(agent_id, dict):
        # sometimes agent is an object
        agent_id = agent_id.get("id") or agent_id.get("agentId") or agent_id.get("name")

    # 3) Who sent this message? visitor | agent | bot
    sender_hint = None
    # Try common keys
    if "from" in payload:
        sender_hint = payload.get("from")
    elif "source" in payload:
        sender_hint = payload.get("source")
    elif payload.get("direction"):
        sender_hint = payload.get("direction")

    # Normalise to our sender enums: 'customer' | 'agent' | 'bot' | 'system'
    sender = "customer"
    if isinstance(sender_hint, str):
        s = sender_hint.lower()
        if "agent" in s or "operator" in s or "staff" in s:
            sender = "agent"
        elif "bot" in s or "zobot" in s or "system" in s:
            sender = "bot"
        elif "visitor" in s or "user" in s or "customer" in s:
            sender = "customer"

    # Some payloads include a top-level message or text
    text = _extract_field(payload, "message", "text", "msg", "content")
    if isinstance(text, dict):
        # message object -> try to get .content or .text
        text = text.get("content") or text.get("text") or text.get("body")

    if text is None:
        return jsonify({"error": "no_message_text_found", "received_keys": list(payload.keys())}), 400

    # 4) Persist incoming message
    async for session in get_session():
        try:
            saved = await crud.save_message(
                session=session,
                customer_id=customer_id,
                agent_id=agent_id,
                sender=("agent" if sender == "agent" else ("bot" if sender == "bot" else "customer")),
                message=str(text),
                meta={
                    "raw_payload": payload,
                    "salesiq_visitor": visitor,
                },
            )
        except Exception as e:
            current_app.logger.exception("Failed saving SalesIQ webhook message: %s", e)
            return jsonify({"error": "db_error", "details": str(e)}), 500

        response_payload: Dict[str, Any] = {"status": "saved", "message_id": saved.message_id, "customer_id": customer_id}

        # 5) If visitor sent message and no agent assigned, optionally create a bot reply
        if sender == "customer" and not agent_id:
            bot_out = await _maybe_generate_bot_reply(session, customer_id, agent_id, str(text))
            if bot_out:
                response_payload["bot_reply"] = bot_out

        # 6) Optionally notify your agent routing system here (websocket, push, SalesIQ API callback)...
        # For hackathon/demo, we only save into DB and return acknowledgement. You can extend this
        # to forward the message to an internal agent dashboard via a websocket or push notification.

        return jsonify(response_payload)


# Small health endpoint for external verification
@bp.route("/v1/salesiq/health", methods=["GET"])
async def salesiq_health():
    return jsonify({"ok": True, "service": "salesiq_integration"})
