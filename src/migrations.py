"""One-off SQLite migrations that can't be expressed via SQLAlchemy
create_all() (which only creates missing tables, never alters existing ones).

Each migration is idempotent — running it twice is a no-op. The migrate()
entry-point is called from init_db() before create_all() so ALTER TABLE
statements land on the pre-existing schema.
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

logger = logging.getLogger(__name__)


def _backup_db_once(db_path: str) -> None:
    """Copy the live DB to a .bak file alongside it before any ALTER runs.

    Skips if the backup file already exists (one-shot) or if the source
    DB doesn't exist yet (fresh install).
    """
    if db_path in (":memory:", ""):
        return
    src = Path(db_path)
    if not src.exists():
        return
    dst = src.with_name(src.name + ".bak-pre-secretary")
    if dst.exists():
        return
    try:
        shutil.copy2(src, dst)
        logger.info("Backed up DB to %s before migration", dst)
    except OSError as e:
        logger.warning("Could not back up DB before migration: %s", e)


def _columns(engine: Engine, table: str) -> set[str]:
    insp = inspect(engine)
    if not insp.has_table(table):
        return set()
    return {c["name"] for c in insp.get_columns(table)}


def _migration_001_add_created_by_to_thoughts(engine: Engine) -> None:
    """Add Thought.created_by_chat_id, backfill with chat_id for legacy rows.

    The application-layer schema declares this column NULLable (for the
    fresh-install case where create_all does the work), but after this
    migration legacy rows are filled in so every existing row has a
    real actor.
    """
    cols = _columns(engine, "thoughts")
    if not cols:
        # Fresh install — no thoughts table yet, create_all will handle it.
        return

    if "created_by_chat_id" not in cols:
        logger.info("Adding thoughts.created_by_chat_id column")
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE thoughts ADD COLUMN created_by_chat_id TEXT"))

    # Backfill any NULL rows (covers both the just-added column and any
    # historical NULLs from interrupted migrations).
    with engine.begin() as conn:
        result = conn.execute(text(
            "UPDATE thoughts SET created_by_chat_id = chat_id "
            "WHERE created_by_chat_id IS NULL"
        ))
        if result.rowcount:
            logger.info("Backfilled created_by_chat_id on %d existing thoughts", result.rowcount)


def migrate(engine: Engine, db_path: str | None = None) -> None:
    """Apply all pending migrations. Idempotent."""
    if db_path:
        _backup_db_once(db_path)
    _migration_001_add_created_by_to_thoughts(engine)
