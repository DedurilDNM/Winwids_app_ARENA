"""Async database engine and session factory.

SQLAlchemy 2.0 async + aiosqlite for the MVP. All database access goes
through `app.db` so migrating to PostgreSQL later is a matter of changing
`DATABASE_URL` (and installing asyncpg), not code.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import DATA_DIR, get_settings


class Base(DeclarativeBase):
    pass


_settings = get_settings()

if _settings.DATABASE_URL.startswith("sqlite"):
    DATA_DIR.mkdir(parents=True, exist_ok=True)

engine = create_async_engine(
    _settings.DATABASE_URL,
    echo=False,
    pool_pre_ping=True,
    connect_args={"check_same_thread": False} if _settings.DATABASE_URL.startswith("sqlite") else {},
)

SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def init_db() -> None:
    # Import models so they are registered on the metadata before create_all.
    from app import models  # noqa: F401

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_add_missing_nullable_columns)


def _add_missing_nullable_columns(sync_conn) -> None:
    """Minimal forward migration for existing databases.

    create_all() never alters existing tables, so columns added to the models
    later (all nullable) are appended here with ALTER TABLE ... ADD COLUMN.
    Anything more complex belongs in an Alembic migration.
    """
    from sqlalchemy import inspect, text

    inspector = inspect(sync_conn)
    for table in Base.metadata.sorted_tables:
        if not inspector.has_table(table.name):
            continue
        existing = {c["name"] for c in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in existing or not column.nullable:
                continue
            col_type = column.type.compile(dialect=sync_conn.dialect)
            sync_conn.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {col_type}'))


async def get_db_session() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session
