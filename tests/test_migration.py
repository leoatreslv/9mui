"""Migration tests.

Spin up a pre-secretary SQLite schema, seed some rows, run migrate(),
assert created_by_chat_id is populated and that the new column is in
place.
"""

import os
import tempfile

import pytest
from sqlalchemy import create_engine, text


PRE_MIGRATION_SCHEMA = """
CREATE TABLE thoughts (
    id INTEGER PRIMARY KEY,
    chat_id TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at DATETIME NOT NULL,
    status TEXT DEFAULT 'pending'
);

CREATE TABLE reminders (
    id INTEGER PRIMARY KEY,
    thought_id INTEGER NOT NULL,
    remind_at DATETIME,
    repeat_hours INTEGER DEFAULT 0,
    last_sent DATETIME,
    is_active BOOLEAN DEFAULT 1
);
"""


@pytest.fixture
def legacy_db():
    """A SQLite file simulating the pre-secretary schema with seed data."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
    with engine.begin() as conn:
        for stmt in PRE_MIGRATION_SCHEMA.strip().split(";"):
            stmt = stmt.strip()
            if stmt:
                conn.execute(text(stmt))
        # Seed: two owners, three thoughts.
        conn.execute(text(
            "INSERT INTO thoughts (chat_id, content, created_at, status) VALUES "
            "('111', 'Buy milk', '2025-01-01 00:00:00', 'pending'),"
            "('111', 'Call dentist', '2025-01-02 00:00:00', 'pending'),"
            "('222', 'Pay rent', '2025-01-03 00:00:00', 'pending')"
        ))
    try:
        yield path, engine
    finally:
        engine.dispose()
        os.unlink(path)
        bak = path + ".bak-pre-secretary"
        if os.path.exists(bak):
            os.unlink(bak)


def test_migration_adds_created_by_column_and_backfills(legacy_db):
    path, engine = legacy_db
    from migrations import migrate

    migrate(engine, path)

    with engine.begin() as conn:
        rows = conn.execute(text(
            "SELECT chat_id, created_by_chat_id FROM thoughts ORDER BY id"
        )).fetchall()
    assert len(rows) == 3
    # Backfilled: every row's created_by matches its chat_id
    for chat_id, created_by in rows:
        assert created_by == chat_id


def test_migration_is_idempotent(legacy_db):
    path, engine = legacy_db
    from migrations import migrate

    migrate(engine, path)
    migrate(engine, path)  # second run is a no-op
    migrate(engine, path)  # third run is also a no-op

    with engine.begin() as conn:
        rows = conn.execute(text(
            "SELECT created_by_chat_id FROM thoughts ORDER BY id"
        )).fetchall()
    assert [r[0] for r in rows] == ["111", "111", "222"]


def test_migration_creates_backup_file(legacy_db):
    path, engine = legacy_db
    from migrations import migrate

    migrate(engine, path)
    backup = path + ".bak-pre-secretary"
    assert os.path.exists(backup)
    assert os.path.getsize(backup) > 0


def test_migration_backup_skipped_on_second_run(legacy_db):
    path, engine = legacy_db
    from migrations import migrate

    migrate(engine, path)
    backup = path + ".bak-pre-secretary"
    first_mtime = os.path.getmtime(backup)

    # Touch the live DB so it's newer than the backup
    import time
    time.sleep(0.01)
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO thoughts (chat_id, content, created_at, status, created_by_chat_id) "
            "VALUES ('111', 'post-migration', '2025-02-01', 'pending', '111')"
        ))

    migrate(engine, path)
    assert os.path.getmtime(backup) == first_mtime, \
        "Backup should not be re-taken on second migrate()"


def test_migration_on_fresh_install_is_a_noop(tmp_path):
    """First-ever boot: no DB file yet, migrate() must not crash."""
    from migrations import migrate
    path = str(tmp_path / "fresh.db")
    engine = create_engine(f"sqlite:///{path}")
    # No tables exist yet
    migrate(engine, path)
    # No backup written (source didn't exist)
    assert not os.path.exists(path + ".bak-pre-secretary")
    engine.dispose()
