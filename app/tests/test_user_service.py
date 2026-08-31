import pytest
from pwdlib.hashers.bcrypt import BcryptHasher
from sqlalchemy.ext.asyncio import AsyncSession

from app.exceptions import InvalidUserOperationError
from app.repositories.users import UserRepository
from app.schemas.user import UserCreate
from app.services.users import UserService


@pytest.mark.asyncio
async def test_user_roles_receive_the_correct_credentials(db_session: AsyncSession) -> None:
    repository = UserRepository(db_session)
    service = UserService(repository)

    api_user = await service.create(
        UserCreate(email="USER@example.com", password="correct-horse", is_admin=False)
    )
    administrator = await service.create_admin("admin@example.com", "correct-horse")

    assert api_user.email == "user@example.com"
    assert api_user.bearer_token is not None
    assert api_user.password_hash.startswith("$argon2")
    assert administrator.bearer_token is None


@pytest.mark.asyncio
async def test_promotion_and_demotion_replace_api_credentials(db_session: AsyncSession) -> None:
    service = UserService(UserRepository(db_session))
    acting_admin = await service.create_admin("admin@example.com", "correct-horse")
    user = await service.create(
        UserCreate(email="user@example.com", password="correct-horse", is_admin=False)
    )
    original_token = user.bearer_token

    await service.toggle_admin(user.id, acting_admin)
    assert user.is_admin
    assert user.bearer_token is None

    await service.toggle_admin(user.id, acting_admin)
    assert not user.is_admin
    assert user.bearer_token is not None
    assert user.bearer_token != original_token


@pytest.mark.asyncio
async def test_administrator_can_apply_permitted_user_lifecycle_changes(
    db_session: AsyncSession,
) -> None:
    repository = UserRepository(db_session)
    service = UserService(repository)
    acting_admin = await service.create_admin("admin@example.com", "correct-horse")
    user = await service.create(
        UserCreate(email="user@example.com", password="correct-horse", is_admin=False)
    )
    original_token = user.bearer_token

    await service.regenerate_token(user.id)
    assert user.bearer_token != original_token

    await service.toggle_active(user.id, acting_admin)
    assert not user.is_active
    await service.toggle_active(user.id, acting_admin)
    assert user.is_active

    await service.delete(user.id, acting_admin)
    assert await repository.get_by_id(user.id) is None


@pytest.mark.asyncio
async def test_self_protection_and_last_administrator_are_enforced(
    db_session: AsyncSession,
) -> None:
    service = UserService(UserRepository(db_session))
    administrator = await service.create_admin("admin@example.com", "correct-horse")

    with pytest.raises(InvalidUserOperationError, match="own account"):
        await service.toggle_active(administrator.id, administrator)
    with pytest.raises(InvalidUserOperationError, match="own admin status"):
        await service.toggle_admin(administrator.id, administrator)
    with pytest.raises(InvalidUserOperationError, match="own account"):
        await service.delete(administrator.id, administrator)
    with pytest.raises(InvalidUserOperationError, match="last active"):
        await service.remove_by_email(administrator.email)


@pytest.mark.asyncio
async def test_legacy_bcrypt_passwords_remain_valid(db_session: AsyncSession) -> None:
    service = UserService(UserRepository(db_session))
    user = await service.create_admin("admin@example.com", "temporary-password")
    user.password_hash = BcryptHasher().hash("legacy-password")

    authenticated = await service.authenticate("ADMIN@example.com", "legacy-password")

    assert authenticated.id == user.id
    assert authenticated.last_login_at is not None
