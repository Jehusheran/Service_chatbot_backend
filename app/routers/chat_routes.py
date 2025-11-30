# app/routers/chat_routes.py
"""
Async chat routes (Quart Blueprint)

Endpoints:
 - POST /save                     : save one or many messages (body.messages list)
 - POST /incoming                 : single incoming message (customer/agent) -> may trigger bot reply
 - GET  /history/<customer_id>    : get history for customer (optional agent_id query param)
 - GET  /history/<customer_id>/<agent_id> : get history for that (customer,agent) pair
 - GET  /customers_for_agent/<agent_id>   : list unique customers this agent has interacted with
"""
from __future__ import annotations
from typing import Any, Dict, List, Optional
from datetime import datetime
import itertools

from quart import Blueprint, request, jsonify, current_app
from marshmallow import ValidationError

from ..db import get_session
from .. import crud
from ..services import llm
from ..schemas import MessageSchema
from ..utils import export_messages_to_text, to_iso

bp = Blueprint("chat_routes", __name__)


def _serialize_model(obj: Any) -> Dict[str, Any]:
    """
    Generic lightweight serializer for SQLModel instances used in responses.
    Avoids depending on pydantic; converts datetimes to ISO strings and JSON-serializable fields.
    """
    if not obj:
        return {}
    data = {}
    for k, v in vars(obj).items():
        if k.startswith("_"):
            continue
        # skip SQLAlchemy internal attr
        if k == "_sa_instance_state":
            continue
        val = v
        if hasattr(val, "isoformat"):
            try:
                val = val.isoformat()
            except Exception:
                val = str(val)
        data[k] = val
    return data


# -----------------------------------------------------------------------
# Save messages (bulk)
# Body: { customer_id, agent_id (nullable), messages: [{ sender, message, message_id?, meta?, created_at? }, ...] }
# -----------------------------------------------------------------------
@bp.route("/save", methods=["POST"])
async def save_messages():
    try:
        payload = await request.get_json()
    except Exception:
        return jsonify({"error": "invalid_json"}), 400

    customer_id = payload.get("customer_id")
    agent_id = payload.get("agent_id")  # can be None for bot-mode
    messages = payload.get("messages", [])

    if not customer_id or not isinstance(messages, list):
        return jsonify({"error": "customer_id_and_messages_required"}), 400

    saved = []
    # validate messages lightly with MessageSchema
    schema = MessageSchema()
    async for session in get_session():
        for m in messages:
            try:
                mdata = schema.load(m)
            except ValidationError as e:
                current_app.logger.debug("Message validation failed: %s", e.messages)
                return jsonify({"error": "message_validation_failed", "details": e.messages}), 400

            row = await crud.save_message(
                session=session,
                customer_id=customer_id,
                agent_id=agent_id,
                sender=mdata["sender"],
                message=mdata["message"],
                meta=mdata.get("meta", {}),
                message_id=mdata.get("message_id"),
                created_at=mdata.get("created_at"),
            )
            saved.append(_serialize_model(row))
    return jsonify({"status": "ok", "saved": saved})


# -----------------------------------------------------------------------
# Incoming single message (webhook-like)
# Body: { customer_id, agent_id (nullable), sender, message, meta? }
# If sender == 'customer' and agent_id is None -> bot mode; generate bot reply and save it.
# -----------------------------------------------------------------------
@bp.route("/incoming", methods=["POST"])
async def incoming():
    try:
        payload = await request.get_json()
    except Exception:
        return jsonify({"error": "invalid_json"}), 400

    customer_id = payload.get("customer_id")
    agent_id = payload.get("agent_id")
    sender = payload.get("sender")
    message = payload.get("message")
    meta = payload.get("meta", {})

    if not customer_id or not sender or message is None:
        return jsonify({"error": "customer_id_sender_message_required"}), 400

    async for session in get_session():
        saved = await crud.save_message(session, customer_id=customer_id, agent_id=agent_id, sender=sender, message=message, meta=meta)
        response: Dict[str, Any] = {"status": "ok", "saved": _serialize_model(saved)}

        # Bot auto-reply behavior (simple): when customer messages without agent, bot suggests reply
        if sender == "customer" and (agent_id is None or agent_id == "" or agent_id == "bot"):
            try:
                suggestions = await llm.generate_bot_suggestions(message, n=1)
                if suggestions:
                    bot_text = suggestions[0]
                    bot_saved = await crud.save_message(session, customer_id=customer_id, agent_id=None, sender="bot", message=bot_text, meta={"bot_generated": True})
                    response["bot_reply"] = _serialize_model(bot_saved)
            except Exception as e:
                current_app.logger.exception("LLM bot reply failed: %s", e)
        return jsonify(response)


# -----------------------------------------------------------------------
# Get history for (customer, agent) or for customer only (all agents)
# Query params:
#   start (ISO), end (ISO), limit, offset
# -----------------------------------------------------------------------
@bp.route("/history/<string:customer_id>", methods=["GET"])
@bp.route("/history/<string:customer_id>/<string:agent_id>", methods=["GET"])
async def get_history(customer_id: str, agent_id: Optional[str] = None):
    params = request.args
    start = params.get("start")
    end = params.get("end")
    limit = params.get("limit")
    offset = params.get("offset")

    # convert to datetime if provided
    start_dt = None
    end_dt = None
    try:
        if start:
            start_dt = datetime.fromisoformat(start)
        if end:
            end_dt = datetime.fromisoformat(end)
    except Exception:
        return jsonify({"error": "invalid_date_format; use ISO"}), 400

    try:
        lmt = int(limit) if limit else None
        off = int(offset) if offset else None
    except Exception:
        return jsonify({"error": "invalid_limit_offset"}), 400

    async for session in get_session():
        msgs = await crud.get_messages(session, customer_id=customer_id, agent_id=agent_id, start=start_dt, end=end_dt, limit=lmt, offset=off)
        serialized = [_serialize_model(m) for m in msgs]
        return jsonify({"customer_id": customer_id, "agent_id": agent_id, "count": len(serialized), "messages": serialized})


# -----------------------------------------------------------------------
# List unique customers for an agent
# -----------------------------------------------------------------------
@bp.route("/customers_for_agent/<string:agent_id>", methods=["GET"])
async def customers_for_agent(agent_id: str):
    async for session in get_session():
        try:
            customers = await crud.list_customers_for_agent(session, agent_id)
            return jsonify({"agent_id": agent_id, "customers": customers})
        except Exception as e:
            current_app.logger.exception("Failed listing customers for agent %s: %s", agent_id, e)
            return jsonify({"error": "internal_error"}), 500
