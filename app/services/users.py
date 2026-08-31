import asyncio
import secrets
from datetime import UTC, datetime

from app.exceptions import (
    DuplicateUserError,
    InactiveUserError,
    InvalidCredentialsError,
    InvalidUserOperationError,
    UserNotFoundError,
)
from app.models.user import User
from app.repositories.users import UserRepository
from app.schemas.user import UserCreate
from app.services.auth import hash_password, verify_password


class UserService:
    def __init__(self, repository: UserRepository) -> None:
        self.repository = repository

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
