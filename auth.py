"""
auth.py
=======
Authentication module for the Text-to-SQL system.

Provides:
  • Password hashing (bcrypt via passlib)
  • JWT token creation and verification (PyJWT with HS256)
  • Database credential encryption (Fernet symmetric encryption)
  • FastAPI dependency for extracting authenticated user context

Public surface
--------------
    hash_password       – Hash a plaintext password with bcrypt
    verify_password     – Verify a plaintext password against a hash
    create_access_token – Create a JWT with user_id, email, role
    decode_access_token – Decode and validate a JWT
    encrypt_value       – Encrypt a string with Fernet
    decrypt_value       – Decrypt a Fernet-encrypted string
    get_current_user    – FastAPI dependency returning UserContext from JWT
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone, timedelta
from typing import Optional

import jwt
from cryptography.fernet import Fernet, InvalidToken
from dotenv import load_dotenv
from fastapi import HTTPException, Request
from passlib.context import CryptContext

from rbac import Role, UserContext

load_dotenv()

logger = logging.getLogger("auth")

# =========================================================
# CONFIGURATION
# =========================================================

JWT_SECRET_KEY    = os.environ.get("JWT_SECRET_KEY", "change-me-in-production")
JWT_ALGORITHM     = "HS256"
JWT_EXPIRY_HOURS  = int(os.environ.get("JWT_EXPIRY_HOURS", "24"))

ENCRYPTION_KEY    = os.environ.get("ENCRYPTION_KEY", "")

import hashlib

# =========================================================
# PASSWORD HASHING  (bcrypt)
# =========================================================

_pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(plain_password: str) -> str:
    """Hash a plaintext password using bcrypt (12 rounds) pre-hashed with SHA-256."""
    pre_hashed = hashlib.sha256(plain_password.encode("utf-8")).hexdigest()
    return _pwd_context.hash(pre_hashed)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a plaintext password against a bcrypt hash, pre-hashed with SHA-256."""
    pre_hashed = hashlib.sha256(plain_password.encode("utf-8")).hexdigest()
    return _pwd_context.verify(pre_hashed, hashed_password)



# =========================================================
# JWT TOKENS
# =========================================================


def create_access_token(
    user_id: str,
    email: str,
    role: str,
    expires_hours: int | None = None,
) -> str:
    """
    Create a signed JWT token.

    Payload contains:
      - sub: user_id (UUID string)
      - email: user email
      - role: viewer / editor / admin
      - exp: expiry timestamp
      - iat: issued-at timestamp
    """
    now = datetime.now(timezone.utc)
    exp = now + timedelta(hours=expires_hours or JWT_EXPIRY_HOURS)

    payload = {
        "sub":   user_id,
        "email": email,
        "role":  role,
        "iat":   now,
        "exp":   exp,
    }

    return jwt.encode(payload, JWT_SECRET_KEY, algorithm=JWT_ALGORITHM)


def decode_access_token(token: str) -> dict:
    """
    Decode and validate a JWT token.

    Returns the payload dict.
    Raises jwt.ExpiredSignatureError or jwt.InvalidTokenError on failure.
    """
    return jwt.decode(token, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM])


# =========================================================
# CREDENTIAL ENCRYPTION  (Fernet)
# =========================================================

def _get_fernet() -> Fernet:
    """Return a Fernet cipher using the ENCRYPTION_KEY from env."""
    if not ENCRYPTION_KEY:
        raise RuntimeError(
            "ENCRYPTION_KEY is not set in .env. "
            "Generate one with: python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
        )
    return Fernet(ENCRYPTION_KEY.encode())


def encrypt_value(plain: str) -> str:
    """Encrypt a plaintext string and return the ciphertext as a string."""
    f = _get_fernet()
    return f.encrypt(plain.encode()).decode()


def decrypt_value(encrypted: str) -> str:
    """Decrypt a Fernet-encrypted string back to plaintext."""
    f = _get_fernet()
    try:
        return f.decrypt(encrypted.encode()).decode()
    except InvalidToken:
        raise ValueError("Failed to decrypt value — wrong key or corrupted data.")


# =========================================================
# FASTAPI DEPENDENCY — get_current_user
# =========================================================

def get_current_user(request: Request) -> UserContext:
    """
    FastAPI dependency: extract and validate the JWT from the
    ``Authorization: Bearer <token>`` header.

    Returns a UserContext with user_id, role, and ip_address.

    Raises:
        HTTPException 401 on missing/invalid/expired token.
    """
    auth_header = request.headers.get("Authorization", "")

    if not auth_header.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail="Missing or invalid Authorization header. Use: Bearer <token>",
        )

    token = auth_header[7:]  # Strip "Bearer "

    try:
        payload = decode_access_token(token)
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=401,
            detail="Token has expired. Please log in again.",
        )
    except jwt.InvalidTokenError as exc:
        raise HTTPException(
            status_code=401,
            detail=f"Invalid token: {exc}",
        )

    # Resolve role
    role_str = payload.get("role", "viewer")
    try:
        role = Role(role_str)
    except ValueError:
        role = Role.VIEWER

    ip = request.client.host if request.client else ""

    return UserContext(
        user_id=payload["sub"],
        role=role,
        ip_address=ip,
    )


def get_optional_user(request: Request) -> Optional[UserContext]:
    """
    Like get_current_user but returns None instead of 401
    when no token is present. Useful for public endpoints
    that have optional auth.
    """
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return None
    try:
        return get_current_user(request)
    except HTTPException:
        return None
