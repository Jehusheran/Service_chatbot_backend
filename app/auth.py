# app/auth.py
import os
import time
from datetime import datetime, timedelta, timezone

from jose import jwt, JWTError
from passlib.context import CryptContext
from dotenv import load_dotenv

load_dotenv()

# -------------------------------------------------------------------
# JWT CONFIG
# -------------------------------------------------------------------
JWT_SECRET = os.getenv("JWT_SECRET", "devsecret")
JWT_ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")

# Access token defaults to 8 hours
ACCESS_TOKEN_EXPIRE_MINUTES = int(
    os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "480")
)

# Password hashing context
pwd_context = CryptContext(
    schemes=["bcrypt"],
    deprecated="auto"
)


# -------------------------------------------------------------------
# PASSWORD HASHING
# -------------------------------------------------------------------
def hash_password(password: str) -> str:
    """Hash password using bcrypt."""
    return pwd_context.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    """Verify password == hashed."""
    return pwd_context.verify(plain, hashed)


# -------------------------------------------------------------------
# JWT CREATION
# -------------------------------------------------------------------
def create_access_token(
    subject: str,
    expires_delta: timedelta | None = None,
    additional_claims: dict | None = None
) -> str:
    """
    Create JWT access token.
    - exp must be a UNIX timestamp (int)
    - subject is agent_id or customer_id
    """

    if expires_delta is None:
        expires_delta = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)

    expire_at = datetime.now(timezone.utc) + expires_delta
    expire_ts = int(expire_at.timestamp())

    payload = {
        "sub": str(subject),
        "exp": expire_ts,
        "iat": int(time.time()),
        "type": "access",
    }

    if additional_claims:
        payload.update(additional_claims)

    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


# -------------------------------------------------------------------
# REFRESH TOKENS (OPTIONAL)
# -------------------------------------------------------------------
def create_refresh_token(subject: str) -> str:
    """Long-lived refresh token (default 30 days)."""

    expires = timedelta(days=30)
    expire_at = datetime.now(timezone.utc) + expires

    payload = {
        "sub": str(subject),
        "exp": int(expire_at.timestamp()),
        "iat": int(time.time()),
        "type": "refresh",
    }

    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


# -------------------------------------------------------------------
# TOKEN VALIDATION
# -------------------------------------------------------------------
def decode_access_token(token: str) -> str | None:
    """
    Validates and decodes token.
    Returns user_id (subject) or None if invalid.
    """
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        return payload.get("sub")
    except JWTError:
        return None


def validate_token_type(token: str, token_type: str) -> bool:
    """
    Enforce 'access' or 'refresh' type.
    """
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        return payload.get("type") == token_type
    except JWTError:
        return False


# -------------------------------------------------------------------
# OPTIONAL: RETURN FULL PAYLOAD
# -------------------------------------------------------------------
def decode_token_full(token: str) -> dict | None:
    """
    Return entire payload dict (sub, exp, type etc.)
    """
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except JWTError:
        return None
