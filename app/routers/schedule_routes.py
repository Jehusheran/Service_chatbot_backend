# app/routers/schedule_routes.py
"""
Async scheduling routes (Quart Blueprint)

Endpoints:
 - POST /availability      : { date: "YYYY-MM-DD", duration: 30, calendarIds: ["primary"], work_start?, work_end? } -> slots
 - POST /book              : { customer: {...}, calendar_id, start, end, service_id, agent_id?, idempotency_key? } -> booking info
 - POST /reschedule        : { booking_ref, new_start, new_end } -> updated booking
 - POST /cancel            : { booking_ref } -> cancellation confirmation
 - GET  /bookings/<customer_id>  : list bookings for customer
 - GET  /booking/<booking_ref>   : get booking details
"""
from __future__ import annotations
import asyncio
from typing import Optional, List, Dict, Any
from datetime import datetime

from quart import Blueprint, request, jsonify, current_app
from marshmallow import ValidationError

from ..db import get_session
from .. import crud
from ..services import google_calendar
from .. import email_services as email_svc
from ..schemas import BookingSchema

bp = Blueprint("schedule_routes", __name__)


# Helper to run sync google_calendar functions without blocking
async def _freebusy_async(calendar_ids: List[str], time_min: str, time_max: str) -> Dict[str, Any]:
    return await asyncio.to_thread(google_calendar.freebusy, calendar_ids, time_min, time_max)


async def _create_event_async(calendar_id: str, event_body: Dict[str, Any]) -> Dict[str, Any]:
    return await asyncio.to_thread(google_calendar.create_event, calendar_id, event_body)


async def _update_event_async(calendar_id: str, event_id: str, event_body: Dict[str, Any]) -> Dict[str, Any]:
    return await asyncio.to_thread(google_calendar.update_event, calendar_id, event_id, event_body)


async def _delete_event_async(calendar_id: str, event_id: str) -> Dict[str, Any]:
    return await asyncio.to_thread(google_calendar.delete_event, calendar_id, event_id)


# -----------------------------------------------------------------------
# Availability
# -----------------------------------------------------------------------
@bp.route("/availability", methods=["POST"])
async def availability():
    """
    Request body:
    { "date": "YYYY-MM-DD", "duration": 30, "calendarIds": ["primary"], "work_start":9, "work_end":17 }
    """
    try:
        payload = await request.get_json()
    except Exception:
        return jsonify({"error": "invalid_json"}), 400

    date = payload.get("date")
    duration = int(payload.get("duration", 30))
    calendar_ids = payload.get("calendarIds", ["primary"])
    work_start = int(payload.get("work_start", 9))
    work_end = int(payload.get("work_end", 17))

    if not date:
        return jsonify({"error": "missing date"}), 400

    # Prepare RFC3339 bounds (UTC). The google helper expects RFC3339 strings.
    # For a day, use Z timezone to indicate UTC day bounds.
    time_min = f"{date}T00:00:00Z"
    time_max = f"{date}T23:59:59Z"

    try:
        fb = await _freebusy_async(calendar_ids, time_min, time_max)
        slots = google_calendar.compute_free_slots_from_freebusy(fb, date, duration_minutes=duration, work_start=work_start, work_end=work_end)
        return jsonify({"date": date, "slots": slots, "calendarIds": calendar_ids})
    except Exception as e:
        current_app.logger.exception("Freebusy error: %s", e)
        return jsonify({"error": "freebusy_failed", "details": str(e)}), 500


# -----------------------------------------------------------------------
# Book
# -----------------------------------------------------------------------
@bp.route("/book", methods=["POST"])
async def book():
    """
    Body (JSON):
    {
      "customer": { "customer_id": "...", "name": "...", "email": "...", "phone": "..." },
      "calendar_id": "primary",
      "start": "2025-12-01T10:00:00Z",
      "end":   "2025-12-01T10:30:00Z",
      "service_id": "Consultation",
      "agent_id": "agent-abc" (optional),
      "idempotency_key": "client-provided-key" (optional)
    }
    """
    try:
        payload = await request.get_json()
    except Exception:
        return jsonify({"error": "invalid_json"}), 400

    # Validate basic fields
    customer = payload.get("customer")
    calendar_id = payload.get("calendar_id", "primary")
    start = payload.get("start")
    end = payload.get("end")
    service_id = payload.get("service_id")
    agent_id = payload.get("agent_id")
    idempotency_key = payload.get("idempotency_key")

    if not customer or not start or not end or not service_id:
        return jsonify({"error": "missing_fields", "required": ["customer","start","end","service_id"]}), 400

    # Validate times parseable
    try:
        start_dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
        end_dt = datetime.fromisoformat(end.replace("Z", "+00:00"))
        if end_dt <= start_dt:
            return jsonify({"error": "end_must_be_after_start"}), 400
    except Exception:
        return jsonify({"error": "invalid_datetime_format; use ISO format with timezone (RFC3339)"}), 400

    # Idempotency check: if idempotency_key provided, find existing booking
    async for session in get_session():
        if idempotency_key:
            existing = await crud.get_booking_by_ref(session, idempotency_key)
            # Note: here we treat idempotency_key as booking_ref if client used same; alternatively
            # you can have a dedicated idempotency_key column — we store it in booking.idempotency_key.
            if existing:
                return jsonify({"status": "ok", "booking": _serialize_booking(existing)})

        # Prepare event body for Google Calendar
        event_body = {
            "summary": f"{service_id} - {customer.get('name') or customer.get('customer_id') or customer.get('email')}",
            "description": payload.get("description", ""),
            "start": {"dateTime": start, "timeZone": "UTC"},
            "end": {"dateTime": end, "timeZone": "UTC"},
            "attendees": [{"email": customer.get("email")}] if customer.get("email") else [],
            "reminders": {"useDefault": True},
        }

        try:
            evt = await _create_event_async(calendar_id, event_body)
        except Exception as e:
            current_app.logger.exception("Google create_event failed: %s", e)
            return jsonify({"error": "calendar_create_failed", "details": str(e)}), 500

        # persist booking in DB
        try:
            booking = await crud.create_booking(
                session=session,
                booking_ref=payload.get("booking_ref") or ("BK-" + str(int(datetime.utcnow().timestamp()))),
                customer_id=customer.get("customer_id") or customer.get("phone") or customer.get("email"),
                agent_id=agent_id,
                calendar_id=calendar_id,
                event_id=evt.get("id"),
                service_id=service_id,
                start=start_dt,
                end=end_dt,
                status="confirmed",
                idempotency_key=idempotency_key,
                paid=bool(payload.get("paid", False)),
            )
        except Exception as e:
            current_app.logger.exception("DB create booking failed: %s", e)
            # Attempt to delete event to avoid ghost event
            try:
                await _delete_event_async(calendar_id, evt.get("id"))
            except Exception:
                current_app.logger.exception("Failed to rollback calendar event after DB failure")
            return jsonify({"error": "db_create_failed", "details": str(e)}), 500

        # Send confirmation email asynchronously (fire-and-forget)
        try:
            # best-effort: don't block response
            asyncio.create_task(email_svc.send_booking_confirmation_email(customer.get("email"), {
                "booking_ref": booking.booking_ref,
                "service_id": booking.service_id,
                "start": booking.start.isoformat(),
                "end": booking.end.isoformat(),
                "calendar_id": booking.calendar_id,
                "event_id": booking.event_id
            }, {"name": customer.get("name"), "customer_id": booking.customer_id, "email": customer.get("email")}))
        except Exception:
            current_app.logger.exception("Failed to enqueue confirmation email")

        return jsonify({"status": "ok", "booking": _serialize_booking(booking)})


# -----------------------------------------------------------------------
# Reschedule
# -----------------------------------------------------------------------
@bp.route("/reschedule", methods=["POST"])
async def reschedule():
    """
    Body:
      { "booking_ref": "...", "new_start": "...", "new_end": "..." }
    """
    try:
        payload = await request.get_json()
    except Exception:
        return jsonify({"error": "invalid_json"}), 400

    booking_ref = payload.get("booking_ref")
    new_start = payload.get("new_start")
    new_end = payload.get("new_end")

    if not booking_ref or not new_start or not new_end:
        return jsonify({"error": "booking_ref_new_start_new_end_required"}), 400

    try:
        new_start_dt = datetime.fromisoformat(new_start.replace("Z", "+00:00"))
        new_end_dt = datetime.fromisoformat(new_end.replace("Z", "+00:00"))
        if new_end_dt <= new_start_dt:
            return jsonify({"error": "end_must_be_after_start"}), 400
    except Exception:
        return jsonify({"error": "invalid_datetime_format"}), 400

    async for session in get_session():
        booking = await crud.get_booking_by_ref(session, booking_ref)
        if not booking:
            return jsonify({"error": "booking_not_found"}), 404

        # Update Google event
        try:
            event_body = {
                "start": {"dateTime": new_start, "timeZone": "UTC"},
                "end": {"dateTime": new_end, "timeZone": "UTC"},
            }
            await _update_event_async(booking.calendar_id, booking.event_id, event_body)
        except Exception as e:
            current_app.logger.exception("Google update_event failed: %s", e)
            return jsonify({"error": "calendar_update_failed", "details": str(e)}), 500

        # Update DB
        try:
            updated = await crud.reschedule_booking(session, booking_ref, new_start_dt, new_end_dt)
        except Exception as e:
            current_app.logger.exception("DB reschedule failed: %s", e)
            return jsonify({"error": "db_update_failed", "details": str(e)}), 500

        # send confirmation email
        try:
            asyncio.create_task(email_svc.send_booking_confirmation_email(
                booking.customer_id or "", {
                    "booking_ref": updated.booking_ref,
                    "service_id": updated.service_id,
                    "start": updated.start.isoformat(),
                    "end": updated.end.isoformat(),
                    "calendar_id": updated.calendar_id,
                    "event_id": updated.event_id
                },
                {"name": None, "customer_id": updated.customer_id, "email": booking.customer_id}
            ))
        except Exception:
            current_app.logger.exception("Failed to enqueue reschedule email")

        return jsonify({"status": "ok", "booking": _serialize_booking(updated)})


# -----------------------------------------------------------------------
# Cancel
# -----------------------------------------------------------------------
@bp.route("/cancel", methods=["POST"])
async def cancel():
    """
    Body: { "booking_ref": "..." }
    """
    try:
        payload = await request.get_json()
    except Exception:
        return jsonify({"error": "invalid_json"}), 400

    booking_ref = payload.get("booking_ref")
    if not booking_ref:
        return jsonify({"error": "booking_ref_required"}), 400

    async for session in get_session():
        booking = await crud.get_booking_by_ref(session, booking_ref)
        if not booking:
            return jsonify({"error": "booking_not_found"}), 404

        # Delete or cancel on Google Calendar (we will attempt delete)
        try:
            await _delete_event_async(booking.calendar_id, booking.event_id)
        except Exception:
            current_app.logger.exception("Google delete_event failed; attempting to mark cancelled in DB")

        # Update DB status
        try:
            cancelled = await crud.update_booking_status(session, booking_ref, "cancelled")
        except Exception as e:
            current_app.logger.exception("DB update failed: %s", e)
            return jsonify({"error": "db_update_failed", "details": str(e)}), 500

        # notify customer
        try:
            asyncio.create_task(email_svc.send_generic_email(
                to_email=booking.customer_id or "",
                subject=f"Booking Cancelled — {booking.booking_ref}",
                body_html=f"<p>Your booking {booking.booking_ref} has been cancelled.</p>",
                body_plain=f"Your booking {booking.booking_ref} has been cancelled."
            ))
        except Exception:
            current_app.logger.exception("Failed to enqueue cancellation email")

        return jsonify({"status": "ok", "booking": _serialize_booking(cancelled)})


# -----------------------------------------------------------------------
# List bookings for a customer
# -----------------------------------------------------------------------
@bp.route("/bookings/<string:customer_id>", methods=["GET"])
async def list_bookings(customer_id: str):
    upcoming_only = request.args.get("upcoming_only", "true").lower() != "false"
    async for session in get_session():
        try:
            bookings = await crud.list_bookings_for_customer(session, customer_id, upcoming_only=upcoming_only)
            results = [_serialize_booking(b) for b in bookings]
            return jsonify({"customer_id": customer_id, "count": len(results), "bookings": results})
        except Exception as e:
            current_app.logger.exception("Failed listing bookings for customer %s: %s", customer_id, e)
            return jsonify({"error": "internal_error"}), 500


# -----------------------------------------------------------------------
# Get booking by ref
# -----------------------------------------------------------------------
@bp.route("/booking/<string:booking_ref>", methods=["GET"])
async def get_booking(booking_ref: str):
    async for session in get_session():
        b = await crud.get_booking_by_ref(session, booking_ref)
        if not b:
            return jsonify({"error": "not_found"}), 404
        return jsonify({"booking": _serialize_booking(b)})


# -----------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------
def _serialize_booking(b) -> Dict[str, Any]:
    if not b:
        return {}
    return {
        "id": b.id,
        "booking_ref": b.booking_ref,
        "idempotency_key": b.idempotency_key,
        "customer_id": b.customer_id,
        "agent_id": b.agent_id,
        "calendar_id": b.calendar_id,
        "event_id": b.event_id,
        "service_id": b.service_id,
        "start": b.start.isoformat() if hasattr(b.start, "isoformat") else str(b.start),
        "end": b.end.isoformat() if hasattr(b.end, "isoformat") else str(b.end),
        "status": b.status,
        "paid": bool(b.paid),
        "created_at": b.created_at.isoformat() if hasattr(b.created_at, "isoformat") else str(b.created_at),
        "updated_at": b.updated_at.isoformat() if getattr(b, "updated_at", None) and hasattr(b.updated_at, "isoformat") else (b.updated_at and str(b.updated_at)),
    }
