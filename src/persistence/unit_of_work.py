"""SQLAlchemy unit-of-work implementation."""

from __future__ import annotations

from collections.abc import Callable
from typing import Self, TypeAlias

from sqlalchemy.orm import Session

from .database import Database
from .repositories import (
    SqlAlchemyCustomerRepository,
    SqlAlchemyDocumentRepository,
    SqlAlchemyInvoiceRepository,
)


class SqlAlchemyUnitOfWork:
    def __init__(self, session_factory: Callable[[], Session]) -> None:
        self._session_factory = session_factory
        self.session: Session | None = None

    def __enter__(self) -> Self:
        self.session = self._session_factory()
        self.customers = SqlAlchemyCustomerRepository(self.session)
        self.documents = SqlAlchemyDocumentRepository(self.session)
        self.invoices = SqlAlchemyInvoiceRepository(self.session)
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        if self.session is None:
            return
        try:
            if exc_type is not None:
                self.session.rollback()
        finally:
            self.session.close()
            self.session = None

    def commit(self) -> None:
        self._require_session().commit()

    def rollback(self) -> None:
        self._require_session().rollback()

    def _require_session(self) -> Session:
        if self.session is None:
            raise RuntimeError("Unit of work must be used as a context manager")
        return self.session


UnitOfWorkFactory: TypeAlias = Callable[[], SqlAlchemyUnitOfWork]


def create_unit_of_work_factory(database: Database) -> UnitOfWorkFactory:
    return lambda: SqlAlchemyUnitOfWork(database.session_factory)
