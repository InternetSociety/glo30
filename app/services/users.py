import secrets
from datetime import UTC, datetime

from app.exceptions import (
    DuplicateUserError,
    InvalidUserOperationError,
    UserNotFoundError,
)
from app.models.user import User
from app.repositories.users import UserRepository
from app.schemas.user import UserCreate
from app.services.auth import hash_password


class UserService:
    def __init__(self, repository: UserRepository) -> None:
        self.repository = repository

    async def create(self, user_create: UserCreate) -> User:
        normalized_email = user_create.email.lower()
        if await self.repository.get_by_email(normalized_email):
            raise DuplicateUserError("A user with that email already exists")

        user = User(
            email=normalized_email,
            password_hash=hash_password(user_create.password),
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
        user.is_active = not user.is_active
        return user

    async def toggle_admin(self, user_id: int, acting_user: User) -> User:
        user = await self.get(user_id)
        if user.id == acting_user.id:
            raise InvalidUserOperationError("You cannot change your own admin status")
        user.is_admin = not user.is_admin
        user.bearer_token = None if user.is_admin else self._new_bearer_token()
        return user

    async def delete(self, user_id: int, acting_user: User) -> None:
        user = await self.get(user_id)
        if user.id == acting_user.id:
            raise InvalidUserOperationError("You cannot delete your own account")
        await self.repository.delete(user)

    async def mark_login(self, user: User) -> None:
        user.last_login_at = datetime.now(UTC)

    @staticmethod
    def _new_bearer_token() -> str:
        return secrets.token_urlsafe(32)
