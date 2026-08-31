import secrets
from datetime import UTC, datetime, timedelta
from hmac import compare_digest
from typing import Any

from jose import JWTError, jwt
from pwdlib import PasswordHash
from pwdlib.hashers.argon2 import Argon2Hasher
from pwdlib.hashers.bcrypt import BcryptHasher

from app.config import Settings

password_hasher = PasswordHash((Argon2Hasher(), BcryptHasher()))
SESSION_COOKIE_NAME = "glo30_session"
CSRF_COOKIE_NAME = "glo30_csrf"


def hash_password(password: str) -> str:
    return password_hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    return password_hasher.verify(password, password_hash)


def create_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def csrf_token_is_valid(cookie_token: str | None, form_token: str | None) -> bool:
    return (
        cookie_token is not None
        and form_token is not None
        and compare_digest(cookie_token, form_token)
    )


def create_access_token(subject: str, settings: Settings) -> str:
    expires_at = datetime.now(UTC) + timedelta(minutes=settings.access_token_expire_minutes)
    payload = {"sub": subject, "exp": expires_at, "type": "session"}
    return str(
        jwt.encode(
            payload,
            settings.secret_key.get_secret_value(),
            algorithm=settings.algorithm,
        )
    )


def decode_access_token(token: str, settings: Settings) -> str | None:
    try:
        payload: dict[str, Any] = jwt.decode(
            token,
            settings.secret_key.get_secret_value(),
            algorithms=[settings.algorithm],
        )
    except JWTError:
        return None

    subject = payload.get("sub")
    if not isinstance(subject, str) or payload.get("type") != "session":
        return None
    return subject
