from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User


class UserRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def get_by_id(self, user_id: int) -> User | None:
        return await self.db.get(User, user_id)

    async def get_by_email(self, email: str) -> User | None:
        result = await self.db.execute(select(User).where(User.email == email))
        return result.scalar_one_or_none()

    async def get_by_bearer_token(self, token: str) -> User | None:
        result = await self.db.execute(select(User).where(User.bearer_token == token))
        return result.scalar_one_or_none()

    async def list_all(self) -> list[User]:
        result = await self.db.execute(select(User).order_by(User.email))
        return list(result.scalars().all())

    async def count_active_admins(self) -> int:
        result = await self.db.execute(
            select(func.count(User.id)).where(User.is_admin.is_(True), User.is_active.is_(True))
        )
        return int(result.scalar_one())

    async def add(self, user: User) -> User:
        self.db.add(user)
        await self.db.flush()
        return user

    async def delete(self, user: User) -> None:
        await self.db.delete(user)
        await self.db.flush()
