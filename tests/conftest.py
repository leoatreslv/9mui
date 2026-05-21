"""Shared pytest fixtures.

We initialize the database against an in-memory SQLite per test so each
test starts clean and tests don't leak state into each other.
"""

import os
import sys
from pathlib import Path

import pytest

# Make src/ importable so tests can `from database import ...` etc.
SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

import database  # noqa: E402


@pytest.fixture
def db_session():
    """Initialise a fresh in-memory database and yield a session.

    Using a shared in-memory URI plus a single connection so all
    sessions in the test see the same data.
    """
    # Re-init globals each test
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    database._engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
    )
    database.Base.metadata.create_all(database._engine)
    database._SessionLocal = sessionmaker(bind=database._engine)

    session = database.get_session()
    try:
        yield session
    finally:
        session.close()
        database._engine.dispose()
        database._engine = None
        database._SessionLocal = None
