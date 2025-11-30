# app/models.py
from __future__ import annotations
from typing import Optional, List, Dict, Any
from sqlmodel import SQLModel, Field, Column, JSON
from datetime import datetime


# ---------------------------------------------------------
# Message Model
# ---------------------------------------------------------
class Message(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    message_id: str = Field(index=True, unique=True)

    customer_id: str = Field(index=True)
    agent_id: Optional[str] = Field(index=True, default=None)

    sender: str  # 'customer' | 'agent' | 'bot' | 'system'
    message: str

    meta: Dict[str, Any] = Field(
        sa_column=Column(JSON), default_factory=dict
    )

    created_at: datetime = Field(
        default_factory=datetime.utcnow, index=True
    )


# ---------------------------------------------------------
# Agent Model
# ---------------------------------------------------------
class Agent(SQLModel, table=True):
    agent_id: str = Field(primary_key=True)
    email: str = Field(index=True, unique=True)

    name: Optional[str] = None
    password_hash: Optional[str] = None

    created_at: datetime = Field(default_factory=datetime.utcnow)


# ---------------------------------------------------------
# Customer Model
# ---------------------------------------------------------
class Customer(SQLModel, table=True):
    customer_id: str = Field(primary_key=True)

    name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None

    created_at: datetime = Field(default_factory=datetime.utcnow)


# ---------------------------------------------------------
# Booking Model (Google Calendar / Any Calendar)
# ---------------------------------------------------------
class Booking(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    booking_ref: str = Field(index=True, unique=True)

    idempotency_key: Optional[str] = Field(index=True, default=None)

    customer_id: Optional[str] = Field(index=True, default=None)
    agent_id: Optional[str] = Field(index=True, default=None)

    calendar_id: str              # Google Calendar calendar ID
    event_id: str                 # Google Calendar event ID
    service_id: str               # “Consultation”, “Doctor visit”, etc.

    start: datetime
    end: datetime

    status: str                   # confirmed | cancelled | rescheduled | pending
    paid: bool = False

    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: Optional[datetime] = None


# ---------------------------------------------------------
# OTP Model
# ---------------------------------------------------------
class OTP(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)

    phone: str = Field(index=True)
    code: str

    valid_until: datetime
    used: bool = False

    created_at: datetime = Field(default_factory=datetime.utcnow)


# ---------------------------------------------------------
# AI Summary Model
# ---------------------------------------------------------
class Summary(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)

    customer_id: Optional[str] = Field(index=True)
    agent_id: Optional[str] = Field(index=True, default=None)

    range_start: Optional[datetime] = None
    range_end: Optional[datetime] = None

    sentences: List[str] = Field(
        sa_column=Column(JSON), default_factory=list
    )
    topics: List[str] = Field(
        sa_column=Column(JSON), default_factory=list
    )

    sentiment: Optional[str] = None
    message_count: Optional[int] = 0

    model_meta: Dict[str, Any] = Field(
        sa_column=Column(JSON), default_factory=dict
    )

    generated_at: datetime = Field(default_factory=datetime.utcnow)

    cache_key: Optional[str] = Field(index=True, unique=True, default=None)
    source_hash: Optional[str] = None


# ---------------------------------------------------------
# Conversation Model
# ---------------------------------------------------------
class Conversation(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)

    customer_id: str = Field(index=True)
    agent_id: Optional[str] = Field(index=True, default=None)

    mode: str = Field(default="bot")  # 'bot' | 'agent' | 'hybrid'
    bot_assist: bool = False
    agent_online: bool = False

    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: Optional[datetime] = None
