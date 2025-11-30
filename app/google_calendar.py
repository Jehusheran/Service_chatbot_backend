# app/google_calendar.py
"""
Google Calendar helpers (sync) with optional impersonation using a service account.

This module uses the google-api-python-client (sync) because the official client is blocking.
It is written so you can call it from synchronous Flask endpoints or wrap calls with
asyncio.to_thread() if you are using async endpoints (Quart/async Flask).

Environment variables:
 - GOOGLE_SERVICE_ACCOUNT_JSON_PATH  -> path to service account JSON
 - GOOGLE_IMPERSONATED_USER          -> optional user to impersonate (domain-wide delegation)
 - GOOGLE_CALENDAR_DEFAULT_ID        -> optional default calendar id (e.g. primary)

Usage (sync):
    from app.google_calendar import freebusy, create_event
    fb = freebusy(["primary"], "2025-12-01T00:00:00Z", "2025-12-01T23:59:59Z")
    ev = create_event("primary", {...})

Usage (async within async app):
    from asyncio import to_thread
    fb = await to_thread(freebusy, ["primary"], time_min, time_max)

Notes:
 - This module does not attempt to cache credentials; it creates a service object per call.
 - For high-throughput systems, reuse the service object or implement a small pool.
"""
from __future__ import annotations
import os
import json
import logging
from typing import List, Dict, Any, Optional

from dotenv import load_dotenv
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

load_dotenv()
logger = logging.getLogger("google_calendar")

SERVICE_ACCOUNT_PATH = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON_PATH")
IMPERSONATE = os.getenv("GOOGLE_IMPERSONATED_USER")  # optional
DEFAULT_CALENDAR_ID = os.getenv("GOOGLE_CALENDAR_DEFAULT_ID", "primary")
SCOPES = ["https://www.googleapis.com/auth/calendar"]

if not SERVICE_ACCOUNT_PATH:
    logger.warning("GOOGLE_SERVICE_ACCOUNT_JSON_PATH not set. Google Calendar functions will fail until set.")


def _get_credentials(impersonate: Optional[str] = None):
    """
    Load service account credentials and optionally impersonate a user.
    Raises RuntimeError if SERVICE_ACCOUNT_PATH is not set.
    """
    if not SERVICE_ACCOUNT_PATH:
        raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON_PATH is not configured")
    credentials = service_account.Credentials.from_service_account_file(SERVICE_ACCOUNT_PATH, scopes=SCOPES)
    subject = impersonate or IMPERSONATE
    if subject:
        credentials = credentials.with_subject(subject)
    return credentials


def _get_service(impersonate: Optional[str] = None):
    """
    Build a google calendar service object.
    Keep discovery cache disabled (cache_discovery=False) to avoid filesystem writes in some environments.
    """
    creds = _get_credentials(impersonate)
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


# -------------------------
# Freebusy (availability)
# -------------------------
def freebusy(calendar_ids: List[str], time_min: str, time_max: str, impersonate: Optional[str] = None) -> Dict[str, Any]:
    """
    Query free/busy for a list of calendar IDs between time_min and time_max (RFC3339 strings).
    Returns the raw API response.
    """
    svc = _get_service(impersonate)
    body = {
        "timeMin": time_min,
        "timeMax": time_max,
        "items": [{"id": cid} for cid in calendar_ids],
    }
    try:
        resp = svc.freebusy().query(body=body).execute()
        return resp
    except HttpError as e:
        logger.exception("Google freebusy HttpError: %s", e)
        raise


# -------------------------
# Create event
# -------------------------
def create_event(calendar_id: str, event_body: Dict[str, Any], impersonate: Optional[str] = None, send_updates: str = "all") -> Dict[str, Any]:
    """
    Create an event in calendar_id. event_body follows Google Calendar API event structure.
    send_updates: 'all'|'externalOnly'|'none'
    Returns the created event resource.
    """
    svc = _get_service(impersonate)
    try:
        event = svc.events().insert(calendarId=calendar_id, body=event_body, sendUpdates=send_updates).execute()
        return event
    except HttpError as e:
        logger.exception("Google create_event HttpError: %s", e)
        raise


# -------------------------
# Update (patch) event
# -------------------------
def update_event(calendar_id: str, event_id: str, event_body: Dict[str, Any], impersonate: Optional[str] = None, send_updates: str = "all") -> Dict[str, Any]:
    """
    Patch/update an existing event. Returns the updated event resource.
    """
    svc = _get_service(impersonate)
    try:
        event = svc.events().patch(calendarId=calendar_id, eventId=event_id, body=event_body, sendUpdates=send_updates).execute()
        return event
    except HttpError as e:
        logger.exception("Google update_event HttpError: %s", e)
        raise


# -------------------------
# Delete event
# -------------------------
def delete_event(calendar_id: str, event_id: str, impersonate: Optional[str] = None, send_updates: str = "all") -> Dict[str, Any]:
    """
    Delete an event. Returns an empty response on success.
    """
    svc = _get_service(impersonate)
    try:
        resp = svc.events().delete(calendarId=calendar_id, eventId=event_id, sendUpdates=send_updates).execute()
        return resp or {}
    except HttpError as e:
        logger.exception("Google delete_event HttpError: %s", e)
        raise


# -------------------------
# List events (simple wrapper)
# -------------------------
def list_events(calendar_id: str, time_min: Optional[str] = None, time_max: Optional[str] = None, max_results: int = 50, q: Optional[str] = None, order_by: str = "startTime", single_events: bool = True, impersonate: Optional[str] = None) -> Dict[str, Any]:
    """
    List events from a calendar. time_min/time_max are RFC3339 strings.
    Returns API response with 'items' list.
    """
    svc = _get_service(impersonate)
    try:
        req = svc.events().list(
            calendarId=calendar_id,
            timeMin=time_min,
            timeMax=time_max,
            maxResults=max_results,
            q=q,
            orderBy=order_by,
            singleEvents=single_events,
        )
        resp = req.execute()
        return resp
    except HttpError as e:
        logger.exception("Google list_events HttpError: %s", e)
        raise


# -------------------------
# Convenience: compute available slots from freebusy response
# -------------------------
from datetime import datetime, timedelta


def compute_free_slots_from_freebusy(freebusy_resp: Dict[str, Any], date_str: str, duration_minutes: int = 30, work_start: int = 9, work_end: int = 17, timezone_str: str = "UTC") -> List[Dict[str, str]]:
    """
    Given a freebusy API response (as returned by freebusy()) and a date string 'YYYY-MM-DD',
    compute possible free slots of length duration_minutes within working hours (work_start..work_end).
    Returns list of {"start": RFC3339, "end": RFC3339}
    """
    # Build day start/end in UTC or using timezone_str if you later integrate tz handling.
    date = datetime.fromisoformat(date_str)
    start_dt = datetime(date.year, date.month, date.day, work_start)
    end_dt = datetime(date.year, date.month, date.day, work_end)

    # Aggregate busy intervals from all calendars
    busy_intervals: List[tuple] = []
    calendars = freebusy_resp.get("calendars", {}) or {}
    for cal_id, cal_data in calendars.items():
        for b in cal_data.get("busy", []):
            try:
                bstart = datetime.fromisoformat(b["start"].replace("Z", "+00:00")) if b.get("start") else None
                bend = datetime.fromisoformat(b["end"].replace("Z", "+00:00")) if b.get("end") else None
                if bstart and bend:
                    busy_intervals.append((bstart, bend))
            except Exception:
                # skip unparsable
                logger.debug("Skipping unparsable busy interval: %s", b)

    # Merge overlapping busy intervals
    busy_intervals.sort(key=lambda x: x[0])
    merged: List[tuple] = []
    for interval in busy_intervals:
        if not merged:
            merged.append(interval)
            continue
        last = merged[-1]
        if interval[0] <= last[1]:
            # overlap -> merge
            merged[-1] = (last[0], max(last[1], interval[1]))
        else:
            merged.append(interval)

    # Walk through the day to find free slots
    slots: List[Dict[str, str]] = []
    current = start_dt
    for bstart, bend in merged:
        if bstart > current:
            t = current
            while t + timedelta(minutes=duration_minutes) <= bstart:
                slots.append({
                    "start": t.isoformat() + "Z",
                    "end": (t + timedelta(minutes=duration_minutes)).isoformat() + "Z"
                })
                t = t + timedelta(minutes=duration_minutes)
        current = max(current, bend)
    # after last busy
    t = current
    while t + timedelta(minutes=duration_minutes) <= end_dt:
        slots.append({
            "start": t.isoformat() + "Z",
            "end": (t + timedelta(minutes=duration_minutes)).isoformat() + "Z"
        })
        t = t + timedelta(minutes=duration_minutes)
    return slots
