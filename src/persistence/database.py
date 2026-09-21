"""SQLAlchemy engine and session configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from src.environment import PROJECT_ROOT, load_project_environment

DEFAULT_DATABASE_URL = "sqlite:///./data/cif_campeador.db"


@dataclass(frozen=True, slots=True)
class DatabaseSettings:
    url: str

    @classmethod
    def from_environment(cls) -> DatabaseSettings:
        load_project_environment()
        configured_url = os.getenv("DATABASE_URL", DEFAULT_DATABASE_URL)
        parsed = make_url(configured_url)
        if parsed.get_backend_name() == "sqlite" and parsed.database:
            database_path = Path(parsed.database)
            if parsed.database != ":memory:" and not database_path.is_absolute():
                parsed = parsed.set(database=str((PROJECT_ROOT / database_path).resolve()))
                configured_url = parsed.render_as_string(hide_password=False)
        return cls(url=configured_url)


class Database:
    """Own the SQLAlchemy engine and create short-lived sessions."""

    def __init__(self, settings: DatabaseSettings) -> None:
        self.settings = settings
        self._ensure_sqlite_parent_exists(settings.url)
        connect_args: dict[str, object] = {}
        if make_url(settings.url).get_backend_name() == "sqlite":
            connect_args = {"check_same_thread": False, "autocommit": False}

        self.engine: Engine = create_engine(
            settings.url,
            connect_args=connect_args,
            pool_pre_ping=True,
        )
        if self.engine.dialect.name == "sqlite":
            self._configure_sqlite(self.engine)

        self.session_factory = sessionmaker(
            bind=self.engine,
            class_=Session,
            expire_on_commit=False,
        )

    def dispose(self) -> None:
        self.engine.dispose()

    @staticmethod
    def _ensure_sqlite_parent_exists(url: str) -> None:
        parsed = make_url(url)
        if parsed.get_backend_name() != "sqlite" or not parsed.database:
            return
        if parsed.database == ":memory:":
            return
        database_path = Path(parsed.database)
        if not database_path.is_absolute():
            database_path = PROJECT_ROOT / database_path
        database_path.parent.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _configure_sqlite(engine: Engine) -> None:
        @event.listens_for(engine, "connect")
        def set_sqlite_pragmas(dbapi_connection, _connection_record) -> None:
            previous_autocommit = dbapi_connection.autocommit
            dbapi_connection.autocommit = True
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute("PRAGMA busy_timeout=5000")
                cursor.execute("PRAGMA journal_mode=WAL")
            finally:
                cursor.close()
                dbapi_connection.autocommit = previous_autocommit
