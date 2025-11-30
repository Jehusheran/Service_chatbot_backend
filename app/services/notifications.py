# app/services/notifications.py
"""
Notification helpers for service-chatbot.

Channels supported (best-effort):
 - in-app notifications persisted in DB (recommended)
 - email (via app.email)
 - sms (via app.twilio_client)
 - slack webhook (simple POST)
 - websocket push (placeholder: you should hook this into your actual websocket server)
 - fcm push (placeholder for Firebase Cloud Messaging)

Pattern:
 - Each notification is fire-and-forget (won't block request handling)
 - Channel functions catch exceptions and return dict status
 - High-level helpers assemble payloads and fan-out to configured channels

Environment vars (optional):
 - SLACK_NOTIFICATION_WEBHOOK_URL
 - FCM_SERVER_KEY
 - NOTIFY_DEFAULT_FROM_EMAIL
"""
from __future__ import annotations
import os
import asyncio
import json
import logging
from typing import Optional, Dict, Any, List

from dotenv import load_dotenv

from .. import crud
from ..db import get_session
from .. import email_services as email_svc
from ..twilio_client import send_sms_async
from ..utils import generate_id, utcnow

load_dotenv()
logger = logging.getLogger("notifications")
logger.setLevel(logging.INFO)

SLACK_WEBHOOK = os.getenv("SLACK_NOTIFICATION_WEBHOOK_URL")
FCM_SERVER_KEY = os.getenv("FCM_SERVER_KEY")
NOTIFY_FROM_EMAIL = os.getenv("NOTIFY_DEFAULT_FROM_EMAIL")

# -------------------------
# Low-level channel adapters
# -------------------------

async def _send_email_async(subject: str, to_email: str, html: str, plain: Optional[str] = None, from_email: Optional[str] = None) -> Dict[str, Any]:
    try:
        res = await email_svc.send_email(subject, to_email, html, plain, from_email or NOTIFY_FROM_EMAIL)
        return {"ok": True, "provider": res.get("provider") if isinstance(res, dict) else "unknown", "detail": res}
    except Exception as e:
        logger.exception("Email send failed: %s", e)
        return {"ok": False, "error": str(e)}

async def _send_sms_async(to: str, body: str) -> Dict[str, Any]:
    try:
        res = await send_sms_async(to, body)
        return {"ok": True, "detail": res}
    except Exception as e:
        logger.exception("SMS send failed: %s", e)
        return {"ok": False, "error": str(e)}

async def _send_slack_async(text: str, title: Optional[str] = None) -> Dict[str, Any]:
    if not SLACK_WEBHOOK:
        return {"ok": False, "error": "slack_webhook_not_configured"}
    payload = {"text": f"*{title}*\n{text}" if title else text}
    try:
        # use asyncio.to_thread for requests.post blocking call
        import requests
        def _post():
            r = requests.post(SLACK_WEBHOOK, json=payload, timeout=8)
            r.raise_for_status()
            try:
                return r.json()
            except Exception:
                return {"status_code": r.status_code}
        result = await asyncio.to_thread(_post)
        return {"ok": True, "detail": result}
    except Exception as e:
        logger.exception("Slack notification failed: %s", e)
        return {"ok": False, "error": str(e)}

async def _send_fcm_async(token: str, title: str, body: str, data: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """
    Placeholder FCM sender. Implement with firebase-admin or HTTP v1 API in production.
    """
    if not FCM_SERVER_KEY:
        return {"ok": False, "error": "fcm_not_configured"}
    try:
        import requests
        headers = {
            "Authorization": f"key={FCM_SERVER_KEY}",
            "Content-Type": "application/json",
        }
        payload = {
            "to": token,
            "notification": {"title": title, "body": body},
            "data": data or {},
        }
        def _post():
            r = requests.post("https://fcm.googleapis.com/fcm/send", headers=headers, json=payload, timeout=8)
            r.raise_for_status()
            return r.json()
        result = await asyncio.to_thread(_post)
        return {"ok": True, "detail": result}
    except Exception as e:
        logger.exception("FCM send failed: %s", e)
        return {"ok": False, "error": str(e)}

# -------------------------
# Websocket placeholder
# -------------------------
async def _send_ws_push(agent_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Placeholder for pushing notifications over WebSocket to an agent session.

    Integrate this with your real websocket system (FastAPI websocket, Socket.IO, Redis pub/sub, etc.)
    Example pattern: `await websocket_manager.push_to_agent(agent_id, payload)`

    For now this function logs the payload and returns success.
    """
    try:
        # Replace this with actual push logic
        logger.info("WS push to agent %s — payload: %s", agent_id, payload)
        # Example: await websocket_manager.push_to_agent(agent_id, payload)
        return {"ok": True, "detail": "enqueued"}
    except Exception as e:
        logger.exception("WS push failed: %s", e)
        return {"ok": False, "error": str(e)}


# -------------------------
# In-app notification (persist in DB)
# -------------------------
async def create_in_app_notification(
    session,
    recipient_id: str,
    title: str,
    body: str,
    meta: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Persist an in-app notification. This example stores notifications as a system Message row
    with sender='system' and meta { notification: True } — adapt to a dedicated Notification model if you add one.
    """
    try:
        # Using crud.save_message as a minimal persistence; you can create a dedicated Notification model later.
        msg = await crud.save_message(
            session=session,
            customer_id=recipient_id,
            agent_id=None,
            sender="system",
            message=f"{title}\n\n{body}",
            meta={"notification": True, **(meta or {})},
        )
        return {"ok": True, "id": msg.message_id}
    except Exception as e:
        logger.exception("Failed to create in-app notification: %s", e)
        return {"ok": False, "error": str(e)}


# -------------------------
# High-level notification helpers (fan-out)
# -------------------------
async def notify_new_message(
    *,
    customer_id: str,
    agent_ids: Optional[List[str]] = None,
    message_text: str,
    message_id: Optional[str] = None,
    channels: Optional[List[str]] = None,
    extra: Optional[Dict[str, Any]] = None,
):
    """
    Notify agent(s) that a new message has arrived from a customer.
    channels: list like ['ws','email','sms','slack','inapp','fcm'] - if None, use defaults
    agent_ids: list of agent ids to notify (if None, you may broadcast to all or do nothing)
    """
    channels = channels or ["ws", "inapp", "slack"]
    extra = extra or {}

    # Build a human-friendly title/body
    title = "New customer message"
    body = f"Message: {message_text[:240]}"

    # Fire-and-forget: schedule tasks but don't await them here
    async def _notify_agent(agent_id: str):
        tasks = []
        payload = {"event": "new_message", "customer_id": customer_id, "agent_id": agent_id, "message_id": message_id, "message_text": message_text, "meta": extra}
        if "inapp" in channels:
            # create an in-app notification persisted in DB
            async for session in get_session():
                tasks.append(asyncio.create_task(create_in_app_notification(session, recipient_id=agent_id, title=title, body=body, meta=extra)))
                break  # get_session yields one session per async for
        if "ws" in channels:
            tasks.append(asyncio.create_task(_send_ws_push(agent_id, payload)))
        if "slack" in channels and SLACK_WEBHOOK:
            tasks.append(asyncio.create_task(_send_slack_async(body, title)))
        # SMS/email/fcm typically need agent contact info — lookup agent record
        async for session in get_session():
            agent = await crud.get_agent_by_id(session, agent_id)
            if "email" in channels and agent and agent.email:
                tasks.append(asyncio.create_task(_send_email_async(f"{title} — {customer_id}", agent.email, f"<p>{body}</p>")))
            if "sms" in channels and agent and getattr(agent, "phone", None):
                tasks.append(asyncio.create_task(_send_sms_async(agent.phone, body)))
            # If agent has fcm token in meta or DB, you can call _send_fcm_async here
            # e.g. if agent.meta.get('fcm_token')
            break

        # await all tasks and return results
        if tasks:
            res = await asyncio.gather(*tasks, return_exceptions=True)
            logger.debug("Notification fanout results for agent %s: %s", agent_id, res)
            return res
        return []

    if not agent_ids:
        # No specific agents: you might route to on-duty agents or a supervisor channel
        if "slack" in channels and SLACK_WEBHOOK:
            # notify general team channel
            asyncio.create_task(_send_slack_async(f"New message from {customer_id}: {message_text[:240]}", "New message (unassigned)"))
        return {"ok": True, "detail": "no_agent_specified"}

    # Launch notifications for all agent_ids concurrently
    for aid in agent_ids:
        asyncio.create_task(_notify_agent(aid))

    return {"ok": True, "detail": f"notifications_enqueued_for_{len(agent_ids)}_agents"}


async def notify_booking_event(
    *,
    customer_id: str,
    booking: Dict[str, Any],
    event_type: str = "booked",  # booked | rescheduled | cancelled
    notify_customer_email: bool = True,
    notify_agent_ids: Optional[List[str]] = None,
):
    """
    Notify relevant parties about a booking event.
    - Persist an in-app notification for the customer
    - Send confirmation email to customer (async)
    - Notify agents (fan-out)
    """
    title_map = {"booked": "Booking confirmed", "rescheduled": "Booking updated", "cancelled": "Booking cancelled"}
    title = title_map.get(event_type, "Booking update")
    body = f"{title}: {booking.get('booking_ref')} — {booking.get('service_id')} on {booking.get('start')}"

    # 1) persist in-app for customer
    async for session in get_session():
        await create_in_app_notification(session, recipient_id=customer_id, title=title, body=body, meta={"booking_ref": booking.get("booking_ref"), "event": event_type})
        break

    # 2) email customer
    if notify_customer_email and booking.get("customer_email"):
        asyncio.create_task(_send_email_async(f"{title} — {booking.get('service_id')}", booking.get("customer_email"), email_svc.render_booking_confirmation_html(booking, {"name": booking.get("customer_name"), "customer_id": customer_id}), email_svc.render_booking_confirmation_plain(booking, {"name": booking.get("customer_name"), "customer_id": customer_id})))

    # 3) notify assigned agents (fan-out)
    if notify_agent_ids:
        await notify_new_message(customer_id=customer_id, agent_ids=notify_agent_ids, message_text=body, channels=["inapp","ws","slack"], extra={"booking": booking, "event_type": event_type})

    return {"ok": True}


# -------------------------
# Helper: send system broadcast
# -------------------------
async def send_system_broadcast(title: str, message: str, channels: Optional[List[str]] = None):
    """
    Broadcast a system message to admin channels (Slack/email).
    """
    channels = channels or ["slack"]
    tasks = []
    if "slack" in channels and SLACK_WEBHOOK:
        tasks.append(asyncio.create_task(_send_slack_async(message, title)))
    if "email" in channels and NOTIFY_FROM_EMAIL:
        admin = os.getenv("ADMIN_EMAIL")
        if admin:
            tasks.append(asyncio.create_task(_send_email_async(title, admin, f"<p>{message}</p>")))
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    return {"ok": True}
