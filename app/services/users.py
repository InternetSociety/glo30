import asyncio
import hashlib
import logging
import secrets
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from app.config import Settings, settings
from app.exceptions import (
    DuplicateUserError,
    InactiveUserError,
    InvalidCredentialsError,
    InvalidPasswordResetCodeError,
    InvalidUserOperationError,
    UserNotFoundError,
)
from app.models.user import User
from app.repositories.users import UserRepository
from app.schemas.user import UserCreate
from app.services.auth import hash_password, verify_password
from app.services.email import send_password_reset

logger = logging.getLogger("uvicorn.error")
PASSWORD_RESET_EXPIRY = timedelta(minutes=30)
PasswordResetSender = Callable[[str, str, Settings], None]


class UserService:
    def __init__(
        self,
        repository: UserRepository,
        app_settings: Settings = settings,
        password_reset_sender: PasswordResetSender = send_password_reset,
    ) -> None:
        self.repository = repository
        self.settings = app_settings
        self.password_reset_sender = password_reset_sender

    async def create(self, user_create: UserCreate) -> User:
        normalized_email = user_create.email.lower()
        if await self.repository.get_by_email(normalized_email):
            raise DuplicateUserError("A user with that email already exists")

        user = User(
            email=normalized_email,
            password_hash=await asyncio.to_thread(hash_password, user_create.password),
            bearer_token=None if user_create.is_admin else self._new_bearer_token(),
            is_active=True,
            is_admin=user_create.is_admin,
        )
        return await self.repository.add(user)

    async def create_admin(self, email: str, password: str) -> User:
        return await self.create(UserCreate(email=email, password=password, is_admin=True))

    async def get(self, user_id: int) -> User:
        user = await self.repository.get_by_id(user_id)
        if user is None:
            raise UserNotFoundError("User not found")
        return user

    async def authenticate(self, email: str, password: str) -> User:
        user = await self.repository.get_by_email(email.lower())
        if user is None or not await asyncio.to_thread(
            verify_password,
            password,
            user.password_hash,
        ):
            raise InvalidCredentialsError("Incorrect email or password")
        if not user.is_active:
            raise InactiveUserError("Inactive user")
        user.last_login_at = datetime.now(UTC)
        return user

    async def request_password_reset(self, email: str) -> None:
        user = await self.repository.get_by_email(email.lower())
        if user is None or not user.is_active or not self.settings.smtp_enabled:
            return

        reset_code = secrets.token_urlsafe(48)
        user.reset_token_hash = self._reset_token_digest(reset_code)
        user.reset_token_expires_at = datetime.now(UTC) + PASSWORD_RESET_EXPIRY
        try:
            await asyncio.to_thread(
                self.password_reset_sender,
                user.email,
                reset_code,
                self.settings,
            )
        except Exception as exc:
            user.reset_token_hash = None
            user.reset_token_expires_at = None
            logger.warning(
                "Password reset email delivery failed (%s)",
                type(exc).__name__,
            )

    async def reset_password(self, reset_code: str, new_password: str) -> User:
        user = await self.repository.get_by_reset_token_hash(self._reset_token_digest(reset_code))
        if user is None or not user.is_active or self._reset_code_has_expired(user):
            raise InvalidPasswordResetCodeError("The reset code is invalid or expired")

        user.password_hash = await asyncio.to_thread(hash_password, new_password)
        user.reset_token_hash = None
        user.reset_token_expires_at = None
        return user

    async def list_visible_to(self, current_user: User) -> list[User]:
        return await self.repository.list_all() if current_user.is_admin else [current_user]

    async def regenerate_token(self, user_id: int) -> User:
        user = await self.get(user_id)
        if user.is_admin:
            raise InvalidUserOperationError("Admin users do not have API bearer tokens")
        user.bearer_token = self._new_bearer_token()
        return user

    async def toggle_active(self, user_id: int, acting_user: User) -> User:
        user = await self.get(user_id)
        if user.id == acting_user.id:
            raise InvalidUserOperationError("You cannot deactivate your own account")
        if user.is_admin and user.is_active:
            await self._ensure_not_last_active_admin()
        user.is_active = not user.is_active
        return user

    async def toggle_admin(self, user_id: int, acting_user: User) -> User:
        user = await self.get(user_id)
        if user.id == acting_user.id:
            raise InvalidUserOperationError("You cannot change your own admin status")
        if user.is_admin and user.is_active:
            await self._ensure_not_last_active_admin()
        user.is_admin = not user.is_admin
        user.bearer_token = None if user.is_admin else self._new_bearer_token()
        return user

    async def delete(self, user_id: int, acting_user: User) -> None:
        user = await self.get(user_id)
        if user.id == acting_user.id:
            raise InvalidUserOperationError("You cannot delete your own account")
        if user.is_admin and user.is_active:
            await self._ensure_not_last_active_admin()
        await self.repository.delete(user)

    async def remove_by_email(self, email: str) -> User:
        user = await self.repository.get_by_email(email.lower())
        if user is None:
            raise UserNotFoundError("User not found")
        if user.is_admin and user.is_active:
            await self._ensure_not_last_active_admin()
        await self.repository.delete(user)
        return user

    async def _ensure_not_last_active_admin(self) -> None:
        if await self.repository.count_active_admins() <= 1:
            raise InvalidUserOperationError("The last active administrator cannot be changed")

    @staticmethod
    def _new_bearer_token() -> str:
        return secrets.token_urlsafe(32)

    @staticmethod
    def _reset_token_digest(reset_code: str) -> str:
        return hashlib.sha256(reset_code.encode()).hexdigest()

    @staticmethod
    def _reset_code_has_expired(user: User) -> bool:
        if user.reset_token_expires_at is None:
            return True
        expires_at = user.reset_token_expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        return expires_at <= datetime.now(UTC)
