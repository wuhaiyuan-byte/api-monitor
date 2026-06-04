import os
import logging
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker
from models import Base
import asyncio

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./api_monitor.db")

engine = create_async_engine(DATABASE_URL, echo=False, future=True)

async_session_maker = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False,
)


async def _migrate():
    """Lightweight schema migration for columns added after initial deploy.

    Idempotent: safe to run on every startup. Uses INFORMATION_SCHEMA (or
    SQLite pragma) to check for column existence, then ALTER TABLE only when
    missing. Wrapped in try/except so a failure here never blocks startup.
    """
    try:
        is_sqlite = engine.dialect.name == "sqlite"

        async with engine.begin() as conn:
            if is_sqlite:
                # SQLite: PRAGMA table_info
                async def sqlite_has_col(table: str, col: str) -> bool:
                    rows = await conn.execute(text(f"PRAGMA table_info({table})"))
                    return any(r[1] == col for r in rows.fetchall())

                async def sqlite_add_col(table: str, col: str, decl: str, default_sql: str | None = None):
                    if not await sqlite_has_col(table, col):
                        sql = f"ALTER TABLE {table} ADD COLUMN {col} {decl}"
                        if default_sql is not None:
                            sql += f" DEFAULT {default_sql}"
                        await conn.execute(text(sql))

                # monitors
                await sqlite_add_col("monitors", "failure_threshold_status", "INTEGER", "2")
                await sqlite_add_col("monitors", "failure_threshold_latency", "INTEGER", "2")
                await sqlite_add_col("monitors", "failure_threshold_body", "INTEGER", "2")
                await sqlite_add_col("monitors", "failure_threshold_timeout", "INTEGER", "2")

                # check_results
                await sqlite_add_col("check_results", "anomaly_type", "VARCHAR(32)", None)

                # alerts
                await sqlite_add_col("alerts", "is_fired", "BOOLEAN", "1")
                await sqlite_add_col("alerts", "consecutive_failures", "INTEGER", "1")
                await sqlite_add_col("alerts", "threshold", "INTEGER", "1")
            else:
                # PostgreSQL / others via INFORMATION_SCHEMA
                async def pg_has_col(table: str, col: str) -> bool:
                    rows = await conn.execute(
                        text(
                            "SELECT 1 FROM information_schema.columns "
                            "WHERE table_name=:t AND column_name=:c"
                        ),
                        {"t": table, "c": col},
                    )
                    return rows.first() is not None

                async def pg_add_col(table: str, col: str, decl: str, default_sql: str | None = None):
                    if not await pg_has_col(table, col):
                        sql = f"ALTER TABLE {table} ADD COLUMN {col} {decl}"
                        if default_sql is not None:
                            sql += f" DEFAULT {default_sql}"
                        await conn.execute(text(sql))

                await pg_add_col("monitors", "failure_threshold_status", "INTEGER", "2")
                await pg_add_col("monitors", "failure_threshold_latency", "INTEGER", "2")
                await pg_add_col("monitors", "failure_threshold_body", "INTEGER", "2")
                await pg_add_col("monitors", "failure_threshold_timeout", "INTEGER", "2")

                await pg_add_col("check_results", "anomaly_type", "VARCHAR(32)", None)

                await pg_add_col("alerts", "is_fired", "BOOLEAN", "TRUE")
                await pg_add_col("alerts", "consecutive_failures", "INTEGER", "1")
                await pg_add_col("alerts", "threshold", "INTEGER", "1")
    except Exception as e:
        # Never let migration failure block the service.
        logging.warning(f"DB migration step failed (non-fatal): {e}")


async def init_db():
    await _migrate()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_session() -> AsyncSession:
    async with async_session_maker() as session:
        yield session