from datetime import UTC, datetime

from sqlalchemy import DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class CachedTile(Base):
    __tablename__ = "cached_tiles"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    tile_id: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    object_key: Mapped[str] = mapped_column(String(500), nullable=False)
    file_path: Mapped[str] = mapped_column(String(500), nullable=False)
    last_used_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), index=True, nullable=False
    )
