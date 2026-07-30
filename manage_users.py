import argparse
import asyncio

from sqlalchemy import select

from app.database import AsyncSessionLocal, ensure_data_directories
from app.exceptions import DuplicateUserError
from app.models.user import User
from app.repositories.users import UserRepository
from app.services.users import UserService


async def create_admin(email: str, password: str) -> None:
    async with AsyncSessionLocal() as session:
        try:
            user = await UserService(UserRepository(session)).create_admin(email, password)
            await session.commit()
        except DuplicateUserError:
            await session.rollback()
            raise SystemExit(f"User {email} already exists") from None
    print(f"Created administrator {user.email}")


async def remove_user(email: str) -> None:
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.email == email.lower()))
        user = result.scalar_one_or_none()
        if user is None:
            raise SystemExit(f"User {email} does not exist")
        await session.delete(user)
        await session.commit()
    print(f"Removed user {email.lower()}")


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Manage GLO-30 API administrators")
    subparsers = parser.add_subparsers(dest="command", required=True)

    create_parser = subparsers.add_parser("create", help="Create an administrator")
    create_parser.add_argument("email")
    create_parser.add_argument("password")

    remove_parser = subparsers.add_parser("remove", help="Remove a user")
    remove_parser.add_argument("email")
    return parser.parse_args()


def main() -> None:
    ensure_data_directories()
    arguments = parse_arguments()
    if arguments.command == "create":
        asyncio.run(create_admin(arguments.email, arguments.password))
    else:
        asyncio.run(remove_user(arguments.email))


if __name__ == "__main__":
    main()
