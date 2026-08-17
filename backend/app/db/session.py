import sqlite3
from typing import Any

from sqlalchemy import create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from app.core.config import settings


def build_engine_options(database_url: str) -> dict[str, Any]:
    """Build dialect-aware engine options without leaking PostgreSQL settings into SQLite."""
    backend = make_url(database_url).get_backend_name()
    options: dict[str, Any] = {"pool_pre_ping": True}

    if backend == "sqlite":
        options["connect_args"] = {"check_same_thread": False}
        return options

    if backend == "postgresql":
        server_options = " ".join(
            [
                f"-c statement_timeout={settings.DATABASE_STATEMENT_TIMEOUT_MS}",
                (
                    "-c idle_in_transaction_session_timeout="
                    f"{settings.DATABASE_IDLE_TRANSACTION_TIMEOUT_MS}"
                ),
            ]
        )
        options.update(
            {
                "pool_size": settings.DATABASE_POOL_SIZE,
                "max_overflow": settings.DATABASE_MAX_OVERFLOW,
                "pool_timeout": settings.DATABASE_POOL_TIMEOUT_SECONDS,
                "pool_recycle": settings.DATABASE_POOL_RECYCLE_SECONDS,
                "pool_use_lifo": True,
                "connect_args": {
                    "connect_timeout": settings.DATABASE_CONNECT_TIMEOUT_SECONDS,
                    "options": server_options,
                },
            }
        )

    return options


def _enable_sqlite_foreign_keys(dbapi_connection, _connection_record) -> None:
    """Enable SQLite foreign-key enforcement for every application connection."""
    if not isinstance(dbapi_connection, sqlite3.Connection):
        return

    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
    finally:
        cursor.close()


def create_application_engine(database_url: str):
    application_engine = create_engine(database_url, **build_engine_options(database_url))
    if application_engine.dialect.name == "sqlite":
        event.listen(application_engine, "connect", _enable_sqlite_foreign_keys)
    return application_engine


engine = create_application_engine(settings.DATABASE_URL)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
