from datetime import UTC, datetime, timedelta
from typing import Any

from jose import JWTError, jwt
from passlib.context import CryptContext

from app.config import Settings

password_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(password: str) -> str:
    return str(password_context.hash(password))


def verify_password(password: str, password_hash: str) -> bool:
    return bool(password_context.verify(password, password_hash))


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
