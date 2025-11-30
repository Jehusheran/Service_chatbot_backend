# app/email.py
"""
Email utilities for the Service Chatbot project.

Features:
 - Send emails via SendGrid (if SENDGRID_API_KEY is set and `sendgrid` package installed)
 - Fallback to SMTP using standard library (SMTP_TLS_HOST, SMTP_TLS_PORT, SMTP_USER, SMTP_PASS)
 - Async wrappers that run the blocking send in a thread (`asyncio.to_thread`)
 - Helpers for booking confirmation and generic templated emails

Environment variables:
 - SENDGRID_API_KEY           (optional) — if present and sendgrid package available, used first
 - SMTP_HOST                  (required if SendGrid absent)
 - SMTP_PORT                  (default 587)
 - SMTP_USER
 - SMTP_PASS
 - EMAIL_FROM                 default from address if not provided
 - ADMIN_EMAIL                fallback admin email
"""
from __future__ import annotations

import os
import json
import logging
import asyncio
from typing import Optional, Dict

from email.message import EmailMessage
import smtplib
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger("email")
logger.setLevel(logging.INFO)

SENDGRID_API_KEY = os.getenv("SENDGRID_API_KEY")
SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")
EMAIL_FROM = os.getenv("EMAIL_FROM", f"no-reply@{os.getenv('DOMAIN','example.com')}")
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", EMAIL_FROM)


# Try to import sendgrid client lazily if available
_sendgrid_client = None
if SENDGRID_API_KEY:
    try:
        from sendgrid import SendGridAPIClient  # type: ignore
        from sendgrid.helpers.mail import Mail  # type: ignore
        _sendgrid_client = SendGridAPIClient(SENDGRID_API_KEY)
    except Exception:
        logger.info("SendGrid package not available or failed to init — will use SMTP fallback.")


# -------------------------
# Low level senders
# -------------------------
def _send_via_sendgrid_sync(subject: str, to_email: str, html: str, plain: Optional[str] = None, from_email: Optional[str] = None) -> dict:
    """
    Synchronous SendGrid send. Returns a dict with status info.
    """
    if _sendgrid_client is None:
        raise RuntimeError("SendGrid client not configured or package not installed")

    from_email = from_email or EMAIL_FROM
    content_plain = plain or ""
    mail = Mail(from_email=from_email, to_emails=to_email, subject=subject, html_content=html, plain_text_content=content_plain)
    try:
        resp = _sendgrid_client.send(mail)
        logger.info("SendGrid sent mail to %s status=%s", to_email, resp.status_code)
        return {"ok": True, "provider": "sendgrid", "status_code": resp.status_code}
    except Exception as e:
        logger.exception("SendGrid send failed: %s", e)
        return {"ok": False, "error": str(e), "provider": "sendgrid"}


def _send_via_smtp_sync(subject: str, to_email: str, html: str, plain: Optional[str] = None, from_email: Optional[str] = None) -> dict:
    """
    Synchronous SMTP send using smtplib (STARTTLS). Returns dict with status.
    """
    if not SMTP_HOST:
        raise RuntimeError("SMTP_HOST not configured")

    from_email = from_email or EMAIL_FROM
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = from_email
    msg["To"] = to_email
    if plain:
        msg.set_content(plain)
        msg.add_alternative(html, subtype="html")
    else:
        msg.set_content(html, subtype="html")

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as server:
            server.ehlo()
            if SMTP_PORT in (587, 25):
                server.starttls()
                server.ehlo()
            if SMTP_USER and SMTP_PASS:
                server.login(SMTP_USER, SMTP_PASS)
            server.send_message(msg)
        logger.info("SMTP email sent to %s via %s:%s", to_email, SMTP_HOST, SMTP_PORT)
        return {"ok": True, "provider": "smtp"}
    except Exception as e:
        logger.exception("SMTP send failed: %s", e)
        return {"ok": False, "error": str(e), "provider": "smtp"}


# -------------------------
# Public send wrapper
# -------------------------
def send_email_sync(subject: str, to_email: str, html: str, plain: Optional[str] = None, from_email: Optional[str] = None) -> dict:
    """
    Choose SendGrid if available, otherwise SMTP.
    """
    # prefer SendGrid if configured and client available
    if _sendgrid_client is not None:
        try:
            res = _send_via_sendgrid_sync(subject, to_email, html, plain, from_email)
            if res.get("ok"):
                return res
            # else fall through to SMTP fallback
            logger.info("SendGrid failed, trying SMTP fallback")
        except Exception as e:
            logger.exception("SendGrid attempt raised, falling back to SMTP: %s", e)

    # fallback to SMTP
    return _send_via_smtp_sync(subject, to_email, html, plain, from_email)


async def send_email(subject: str, to_email: str, html: str, plain: Optional[str] = None, from_email: Optional[str] = None) -> dict:
    """
    Async wrapper for send_email_sync.
    """
    return await asyncio.to_thread(send_email_sync, subject, to_email, html, plain, from_email)


# -------------------------
# Templates / helpers
# -------------------------
def render_booking_confirmation_html(booking: Dict, customer: Dict) -> str:
    """
    Simple HTML template for booking confirmation.
    booking: dict with keys booking_ref, service_id, start, end, calendar_id, event_id
    customer: dict with keys name, email, phone, customer_id
    """
    start = booking.get("start")
    end = booking.get("end")
    service = booking.get("service_id", "Service")
    booking_ref = booking.get("booking_ref", "")
    html = f"""
    <html>
      <body>
        <h2>Booking Confirmed — {service}</h2>
        <p>Hi {customer.get('name') or customer.get('customer_id')},</p>
        <p>Your booking <strong>{booking_ref}</strong> is confirmed.</p>
        <ul>
          <li>Service: {service}</li>
          <li>When: {start} to {end}</li>
          <li>Calendar: {booking.get('calendar_id')}</li>
          <li>Event ID: {booking.get('event_id')}</li>
        </ul>
        <p>If you need to reschedule or cancel, reply to this email or use the support portal.</p>
        <p>Thanks,<br/>Support Team</p>
      </body>
    </html>
    """
    return html


def render_booking_confirmation_plain(booking: Dict, customer: Dict) -> str:
    return (
        f"Booking Confirmed — {booking.get('service_id', 'Service')}\n\n"
        f"Hi {customer.get('name') or customer.get('customer_id')},\n\n"
        f"Your booking {booking.get('booking_ref')} is confirmed.\n"
        f"When: {booking.get('start')} to {booking.get('end')}\n"
        f"Calendar: {booking.get('calendar_id')}\n"
        f"Event ID: {booking.get('event_id')}\n\n"
        "If you need to reschedule or cancel, reply to this email or use the support portal.\n\n"
        "Thanks,\nSupport Team\n"
    )


# -------------------------
# High-level helpers
# -------------------------
async def send_booking_confirmation_email(customer_email: str, booking: Dict, customer: Dict, subject: Optional[str] = None) -> dict:
    """
    Send booking confirmation to the customer and BCC admin.
    """
    if not customer_email:
        logger.warning("No customer email provided for booking confirmation: %s", booking.get("booking_ref"))
        return {"ok": False, "error": "no_email"}

    subject = subject or f"Booking Confirmed — {booking.get('service_id', 'Service')} ({booking.get('booking_ref')})"
    html = render_booking_confirmation_html(booking, customer)
    plain = render_booking_confirmation_plain(booking, customer)

    res = await send_email(subject, customer_email, html, plain, from_email=EMAIL_FROM)

    # Optionally send copy to admin
    try:
        if ADMIN_EMAIL and ADMIN_EMAIL != customer_email:
            admin_subject = f"[COPY] {subject}"
            await send_email(admin_subject, ADMIN_EMAIL, html, plain, from_email=EMAIL_FROM)
    except Exception:
        logger.exception("Failed to send admin copy for booking %s", booking.get("booking_ref"))

    return res


async def send_generic_email(to_email: str, subject: str, body_html: str, body_plain: Optional[str] = None, from_email: Optional[str] = None) -> dict:
    return await send_email(subject, to_email, body_html, body_plain, from_email)


# -------------------------
# Convenience demo function
# -------------------------
async def demo_send_test():
    """
    Demo/test helper used in development to verify email sending.
    """
    test_to = os.getenv("DEMO_TEST_EMAIL") or ADMIN_EMAIL
    booking = {"booking_ref": "BK-DEMO-123", "service_id": "Demo Service", "start": "2025-12-01T10:00:00Z", "end": "2025-12-01T10:30:00Z", "calendar_id": "primary", "event_id": "evt-demo"}
    customer = {"name": "Demo User", "customer_id": "customer-demo", "email": test_to}
    return await send_booking_confirmation_email(test_to, booking, customer)
