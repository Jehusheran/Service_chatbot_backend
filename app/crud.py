# app/crud.py
"""
Asynchronous CRUD helpers for the service-chatbot project.

All functions accept an `AsyncSession` from `app.db.AsyncSessionLocal` (or dependency)
and perform simple create/read/update operations. They return SQLModel instances
or plain Python dicts/lists where appropriate.

This file is framework-agnostic (works for Quart/async Flask) and avoids any
web-specific behavior. Add more functions as you need.
"""
from __future__ import annotations
from typing import Optional, List, Dict, Any, Tuple
from uuid import uuid4
from datetime import datetime, timezone

from sqlalchemy import select, update, delete, func
from sqlalchemy.ext.asyncio import AsyncSession

from . import models


# -----------------------
# Agents
# -----------------------
async def create_agent(
    session: AsyncSession,
    agent_id: str,
    email: str,
    password_hash: str,
    name: Optional[str] = None,
) -> models.Agent:
    a = models.Agent(agent_id=agent_id, email=email.lower(), password_hash=password_hash, name=name)
    session.add(a)
    await session.commit()
    await session.refresh(a)
    return a


async def get_agent_by_email(session: AsyncSession, email: str) -> Optional[models.Agent]:
    q = select(models.Agent).where(models.Agent.email == email.lower())
    r = await session.execute(q)
    return r.scalars().first()


async def get_agent_by_id(session: AsyncSession, agent_id: str) -> Optional[models.Agent]:
    q = select(models.Agent).where(models.Agent.agent_id == agent_id)
    r = await session.execute(q)
    return r.scalars().first()


# -----------------------
# Customers
# -----------------------
async def create_or_update_customer(
    session: AsyncSession,
    customer_id: str,
    name: Optional[str] = None,
    email: Optional[str] = None,
    phone: Optional[str] = None,
) -> models.Customer:
    """
    Upsert simple customer record.
    """
    existing = await get_customer_by_id(session, customer_id)
    if existing:
        if name is not None:
            existing.name = name
        if email is not None:
            existing.email = email
        if phone is not None:
            existing.phone = phone
        session.add(existing)
        await session.commit()
        await session.refresh(existing)
        return existing
    c = models.Customer(customer_id=customer_id, name=name, email=email, phone=phone)
    session.add(c)
    await session.commit()
    await session.refresh(c)
    return c


async def get_customer_by_id(session: AsyncSession, customer_id: str) -> Optional[models.Customer]:
    q = select(models.Customer).where(models.Customer.customer_id == customer_id)
    r = await session.execute(q)
    return r.scalars().first()


# -----------------------
# Messages
# -----------------------
async def save_message(
    session: AsyncSession,
    customer_id: str,
    agent_id: Optional[str],
    sender: str,
    message: str,
    meta: Optional[Dict[str, Any]] = None,
    message_id: Optional[str] = None,
    created_at: Optional[datetime] = None,
) -> models.Message:
    """
    Save a message and return the created Message model.
    """
    if message_id is None:
        message_id = str(uuid4())
    if created_at is None:
        created_at = datetime.now(timezone.utc)
    m = models.Message(
        message_id=message_id,
        customer_id=customer_id,
        agent_id=agent_id,
        sender=sender,
        message=message,
        meta=meta or {},
        created_at=created_at,
    )
    session.add(m)
    await session.commit()
    await session.refresh(m)
    return m


async def get_messages(
    session: AsyncSession,
    customer_id: str,
    agent_id: Optional[str] = None,
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
    limit: Optional[int] = None,
    offset: Optional[int] = None,
) -> List[models.Message]:
    q = select(models.Message).where(models.Message.customer_id == customer_id)
    if agent_id is not None:
        q = q.where(models.Message.agent_id == agent_id)
    if start:
        q = q.where(models.Message.created_at >= start)
    if end:
        q = q.where(models.Message.created_at <= end)
    q = q.order_by(models.Message.created_at)
    if limit:
        q = q.limit(limit)
    if offset:
        q = q.offset(offset)
    r = await session.execute(q)
    return r.scalars().all()


async def get_last_message(session: AsyncSession, customer_id: str, agent_id: Optional[str] = None) -> Optional[models.Message]:
    q = select(models.Message).where(models.Message.customer_id == customer_id)
    if agent_id is not None:
        q = q.where(models.Message.agent_id == agent_id)
    q = q.order_by(models.Message.created_at.desc()).limit(1)
    r = await session.execute(q)
    return r.scalars().first()


# -----------------------
# Conversations
# -----------------------
async def open_or_get_conversation(
    session: AsyncSession,
    customer_id: str,
    agent_id: Optional[str] = None,
    create_if_missing: bool = True,
) -> models.Conversation:
    q = select(models.Conversation).where(models.Conversation.customer_id == customer_id)
    if agent_id is not None:
        q = q.where(models.Conversation.agent_id == agent_id)
    r = await session.execute(q)
    conv = r.scalars().first()
    if conv:
        return conv
    if not create_if_missing:
        return None
    conv = models.Conversation(customer_id=customer_id, agent_id=agent_id, mode="agent" if agent_id else "bot", bot_assist=True)
    session.add(conv)
    await session.commit()
    await session.refresh(conv)
    return conv


async def set_conversation_mode(
    session: AsyncSession,
    conv_id: int,
    mode: Optional[str] = None,
    bot_assist: Optional[bool] = None,
    agent_online: Optional[bool] = None,
) -> Optional[models.Conversation]:
    conv = await get_conversation_by_id(session, conv_id)
    if not conv:
        return None
    if mode is not None:
        conv.mode = mode
    if bot_assist is not None:
        conv.bot_assist = bot_assist
    if agent_online is not None:
        conv.agent_online = agent_online
    session.add(conv)
    await session.commit()
    await session.refresh(conv)
    return conv


async def get_conversation_by_id(session: AsyncSession, conv_id: int) -> Optional[models.Conversation]:
    q = select(models.Conversation).where(models.Conversation.id == conv_id)
    r = await session.execute(q)
    return r.scalars().first()


# -----------------------
# Bookings
# -----------------------
async def create_booking(
    session: AsyncSession,
    booking_ref: Optional[str],
    customer_id: Optional[str],
    agent_id: Optional[str],
    calendar_id: str,
    event_id: str,
    service_id: str,
    start: datetime,
    end: datetime,
    status: str = "confirmed",
    idempotency_key: Optional[str] = None,
    paid: bool = False,
) -> models.Booking:
    if booking_ref is None:
        booking_ref = "BK-" + uuid4().hex[:12]
    b = models.Booking(
        booking_ref=booking_ref,
        idempotency_key=idempotency_key,
        customer_id=customer_id,
        agent_id=agent_id,
        calendar_id=calendar_id,
        event_id=event_id,
        service_id=service_id,
        start=start,
        end=end,
        status=status,
        paid=paid,
    )
    session.add(b)
    await session.commit()
    await session.refresh(b)
    return b


async def get_booking_by_ref(session: AsyncSession, booking_ref: str) -> Optional[models.Booking]:
    q = select(models.Booking).where(models.Booking.booking_ref == booking_ref)
    r = await session.execute(q)
    return r.scalars().first()


async def list_bookings_for_customer(session: AsyncSession, customer_id: str, upcoming_only: bool = True) -> List[models.Booking]:
    q = select(models.Booking).where(models.Booking.customer_id == customer_id)
    if upcoming_only:
        q = q.where(models.Booking.end >= datetime.now(timezone.utc))
    q = q.order_by(models.Booking.start)
    r = await session.execute(q)
    return r.scalars().all()


async def update_booking_status(session: AsyncSession, booking_ref: str, status: str) -> Optional[models.Booking]:
    b = await get_booking_by_ref(session, booking_ref)
    if not b:
        return None
    b.status = status
    b.updated_at = datetime.now(timezone.utc)
    session.add(b)
    await session.commit()
    await session.refresh(b)
    return b


async def reschedule_booking(
    session: AsyncSession, booking_ref: str, new_start: datetime, new_end: datetime
) -> Optional[models.Booking]:
    b = await get_booking_by_ref(session, booking_ref)
    if not b:
        return None
    b.start = new_start
    b.end = new_end
    b.status = "rescheduled"
    b.updated_at = datetime.now(timezone.utc)
    session.add(b)
    await session.commit()
    await session.refresh(b)
    return b


async def cancel_booking(session: AsyncSession, booking_ref: str) -> Optional[models.Booking]:
    return await update_booking_status(session, booking_ref, "cancelled")


# -----------------------
# OTP
# -----------------------
async def create_otp(session: AsyncSession, phone: str, code: str, valid_for_seconds: int = 300) -> models.OTP:
    valid_until = datetime.now(timezone.utc) + "timedelta"(seconds=valid_for_seconds)
    otp = models.OTP(phone=phone, code=code, valid_until=valid_until, used=False)
    session.add(otp)
    await session.commit()
    await session.refresh(otp)
    return otp


async def verify_otp_code(session: AsyncSession, phone: str, code: str) -> bool:
    q = select(models.OTP).where(models.OTP.phone == phone, models.OTP.code == code, models.OTP.used == False)
    r = await session.execute(q)
    otp = r.scalars().first()
    if not otp:
        return False
    if otp.valid_until < datetime.now(timezone.utc):
        return False
    # mark used
    otp.used = True
    session.add(otp)
    await session.commit()
    return True


# -----------------------
# Summaries caching
# -----------------------
async def save_summary(
    session: AsyncSession,
    customer_id: str,
    agent_id: Optional[str],
    range_start: Optional[datetime],
    range_end: Optional[datetime],
    sentences: List[str],
    topics: List[str],
    sentiment: Optional[str],
    message_count: int,
    model_meta: Optional[Dict[str, Any]] = None,
    cache_key: Optional[str] = None,
    source_hash: Optional[str] = None,
) -> models.Summary:
    if cache_key is None:
        cache_key = "sum-" + uuid4().hex[:12]
    s = models.Summary(
        customer_id=customer_id,
        agent_id=agent_id,
        range_start=range_start,
        range_end=range_end,
        sentences=sentences,
        topics=topics,
        sentiment=sentiment,
        message_count=message_count,
        model_meta=model_meta or {},
        generated_at=datetime.now(timezone.utc),
        cache_key=cache_key,
        source_hash=source_hash,
    )
    session.add(s)
    await session.commit()
    await session.refresh(s)
    return s


async def get_summary_by_cache_key(session: AsyncSession, cache_key: str) -> Optional[models.Summary]:
    q = select(models.Summary).where(models.Summary.cache_key == cache_key)
    r = await session.execute(q)
    return r.scalars().first()


async def list_summaries_for_customer(session: AsyncSession, customer_id: str) -> List[models.Summary]:
    q = select(models.Summary).where(models.Summary.customer_id == customer_id).order_by(models.Summary.generated_at.desc())
    r = await session.execute(q)
    return r.scalars().all()


# -----------------------
# Utility
# -----------------------
async def list_customers_for_agent(session: AsyncSession, agent_id: str) -> List[str]:
    """
    Return unique customer_ids this agent has talked with (via messages or bookings).
    """
    # from messages
    q = select(func.distinct(models.Message.customer_id)).where(models.Message.agent_id == agent_id)
    r = await session.execute(q)
    from_msgs = [row[0] for row in r.fetchall()]

    # from bookings
    q2 = select(func.distinct(models.Booking.customer_id)).where(models.Booking.agent_id == agent_id)
    r2 = await session.execute(q2)
    from_bookings = [row[0] for row in r2.fetchall() if row[0] is not None]

    # union (preserve order: messages first then bookings)
    combined = list(dict.fromkeys(from_msgs + from_bookings))
    return combined
