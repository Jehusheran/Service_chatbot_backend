# app/schemas.py
"""
Marshmallow schemas used across the project.

Designed to be compatible across marshmallow versions by avoiding `missing=...`
in field constructors and instead setting defaults in a `@pre_load` hook.
"""
from __future__ import annotations
from datetime import datetime
import re
from typing import Any, Dict, Optional

from marshmallow import Schema, fields, ValidationError, pre_load, post_load


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PHONE_RE = re.compile(r"^\+?[0-9]{7,15}$")


def _parse_iso_datetime(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        # accept trailing Z
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        # fallback naive parse
        try:
            return datetime.fromisoformat(value)
        except Exception:
            return None


class MessageSchema(Schema):
    """
    Message payload for saving messages.
    Accepts:
      {
        "sender": "customer" | "agent" | "bot" | "system",
        "message": "text",
        "message_id": "optional",
        "meta": {...},
        "created_at": "ISO string (optional)"
      }
    """
    sender = fields.Str(required=True)
    message = fields.Str(required=True)
    message_id = fields.Str(required=False, allow_none=True)
    meta = fields.Raw(required=False, allow_none=True)
    created_at = fields.Str(required=False, allow_none=True)

    @pre_load
    def ensure_defaults(self, data: Dict[str, Any], **kwargs) -> Dict[str, Any]:
        # Provide default meta as empty dict if absent or null
        if "meta" not in data or data.get("meta") is None:
            data["meta"] = {}
        # Generate a basic message_id if not provided
        if not data.get("message_id"):
            # short pseudo-unique id
            data["message_id"] = f"msg_{int(datetime.utcnow().timestamp() * 1000)}"
        # Normalize created_at to ISO string if provided as datetime
        ca = data.get("created_at")
        if ca and hasattr(ca, "isoformat"):
            data["created_at"] = ca.isoformat()
        return data

    @post_load
    def convert_types(self, data: Dict[str, Any], **kwargs) -> Dict[str, Any]:
        # convert created_at to datetime if provided
        if data.get("created_at"):
            parsed = _parse_iso_datetime(data["created_at"])
            if parsed:
                data["created_at"] = parsed
            else:
                # if cannot parse, remove it so DB defaults can apply
                data.pop("created_at", None)
        return data


class AgentCreateSchema(Schema):
    email = fields.Str(required=True)
    password = fields.Str(required=True)
    name = fields.Str(required=False, allow_none=True)

    @pre_load
    def strip_email(self, data: Dict[str, Any], **kwargs) -> Dict[str, Any]:
        if "email" in data and isinstance(data["email"], str):
            data["email"] = data["email"].strip().lower()
        return data

    @post_load
    def validate_email(self, data: Dict[str, Any], **kwargs) -> Dict[str, Any]:
        email = data.get("email")
        if not email or not EMAIL_RE.match(email):
            raise ValidationError("Invalid email address", field_name="email")
        if not data.get("password"):
            raise ValidationError("Password required", field_name="password")
        return data


class OTPRequestSchema(Schema):
    phone = fields.Str(required=True)

    @post_load
    def validate_phone(self, data: Dict[str, Any], **kwargs) -> Dict[str, Any]:
        phone = data.get("phone")
        if not phone or not PHONE_RE.match(phone):
            raise ValidationError("Invalid phone number format", field_name="phone")
        return data


class OTPVerifySchema(Schema):
    phone = fields.Str(required=True)
    code = fields.Str(required=True)
    name = fields.Str(required=False, allow_none=True)
    email = fields.Str(required=False, allow_none=True)

    @post_load
    def validate(self, data: Dict[str, Any], **kwargs) -> Dict[str, Any]:
        phone = data.get("phone")
        if not phone or not PHONE_RE.match(phone):
            raise ValidationError("Invalid phone number format", field_name="phone")
        # If email provided, validate it loosely
        em = data.get("email")
        if em and not EMAIL_RE.match(em):
            raise ValidationError("Invalid email address", field_name="email")
        return data


class BookingSchema(Schema):
    customer = fields.Dict(required=True)
    calendar_id = fields.Str(required=True)
    start = fields.Str(required=True)
    end = fields.Str(required=True)
    service_id = fields.Str(required=True)
    agent_id = fields.Str(required=False, allow_none=True)
    idempotency_key = fields.Str(required=False, allow_none=True)
    description = fields.Str(required=False, allow_none=True)
    paid = fields.Boolean(required=False, missing=False)  # safe default for marshmallow v3; if older marshal ignores

    @pre_load
    def ensure_customer_fields(self, data: Dict[str, Any], **kwargs) -> Dict[str, Any]:
        if "customer" not in data or not isinstance(data["customer"], dict):
            raise ValidationError("customer must be an object with contact info", field_name="customer")
        return data

    @post_load
    def normalize_times(self, data: Dict[str, Any], **kwargs) -> Dict[str, Any]:
        # Validate ISO datetimes
        start = data.get("start")
        end = data.get("end")
        if not _parse_iso_datetime(start):
            raise ValidationError("start must be ISO datetime string", field_name="start")
        if not _parse_iso_datetime(end):
            raise ValidationError("end must be ISO datetime string", field_name="end")
        return data
