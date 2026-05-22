from datetime import datetime
from sqlalchemy import (
    create_engine,
    Column,
    Integer,
    String,
    DateTime,
    Boolean,
    ForeignKey,
    Text,
    Index,
    text,
)
from sqlalchemy.orm import DeclarativeBase, relationship, sessionmaker, Session


class Base(DeclarativeBase):
    pass


class Thought(Base):
    __tablename__ = "thoughts"

    id = Column(Integer, primary_key=True)
    chat_id = Column(String, nullable=False)
    # NULL only on legacy rows that the migration hasn't backfilled yet.
    # New rows always set this to the actor's chat_id (= chat_id for owners,
    # secretary's chat_id for secretary contributions).
    created_by_chat_id = Column(String, nullable=True)
    content = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    status = Column(String, default="pending")  # pending | done

    reminders = relationship("Reminder", back_populates="thought", cascade="all, delete-orphan")

    __table_args__ = (
        # Covers both owner's full /list and secretary's filtered-by-creator /list.
        Index("ix_thoughts_chat_creator_status", "chat_id", "created_by_chat_id", "status"),
    )


class Reminder(Base):
    __tablename__ = "reminders"

    id = Column(Integer, primary_key=True)
    thought_id = Column(Integer, ForeignKey("thoughts.id"), nullable=False)
    remind_at = Column(DateTime, nullable=True)       # None = recurring
    repeat_hours = Column(Integer, default=0)         # 0 = one-shot
    last_sent = Column(DateTime, nullable=True)
    is_active = Column(Boolean, default=True)

    thought = relationship("Thought", back_populates="reminders")


class Opportunity(Base):
    __tablename__ = "opportunities"

    id = Column(Integer, primary_key=True)
    chat_id = Column(String, nullable=False)
    title = Column(String, nullable=False)
    customer = Column(String, nullable=True)
    stage = Column(String, default="lead", nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
    deleted_at = Column(DateTime, nullable=True)

    updates = relationship(
        "OpportunityUpdate",
        back_populates="opportunity",
        cascade="all, delete-orphan",
        order_by="OpportunityUpdate.created_at",
    )

    __table_args__ = (
        Index("ix_opp_chat_stage", "chat_id", "stage"),
        Index("ix_opp_chat_deleted", "chat_id", "deleted_at"),
    )


class OpportunityUpdate(Base):
    __tablename__ = "opportunity_updates"

    id = Column(Integer, primary_key=True)
    opportunity_id = Column(Integer, ForeignKey("opportunities.id"), nullable=False)
    note = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    created_by_chat_id = Column(String, nullable=True)

    opportunity = relationship("Opportunity", back_populates="updates")


class Secretary(Base):
    """One person who has permission to operate the bot on behalf of an owner.

    1:1 enforced via a partial unique index on (secretary_chat_id) WHERE
    removed_at IS NULL. After revocation a new Secretary row can be inserted
    for the same chat_id — history is preserved.
    """
    __tablename__ = "secretaries"

    id = Column(Integer, primary_key=True)
    owner_chat_id = Column(String, nullable=False)
    secretary_chat_id = Column(String, nullable=False)
    display_name = Column(String, nullable=True)   # cached from Telegram first_name
    invited_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    removed_at = Column(DateTime, nullable=True)
    removed_by_chat_id = Column(String, nullable=True)

    __table_args__ = (
        Index(
            "ux_secretary_active",
            "secretary_chat_id",
            unique=True,
            sqlite_where=text("removed_at IS NULL"),
        ),
        Index("ix_secretary_owner", "owner_chat_id"),
    )


class Invite(Base):
    """A pending or used invite token for a Secretary slot."""
    __tablename__ = "invites"

    token = Column(String, primary_key=True)
    owner_chat_id = Column(String, nullable=False, index=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    expires_at = Column(DateTime, nullable=False)
    used_by_chat_id = Column(String, nullable=True)
    used_at = Column(DateTime, nullable=True)


class SecretaryEvent(Base):
    """Audit trail for sensitive secretary actions."""
    __tablename__ = "secretary_events"

    id = Column(Integer, primary_key=True)
    at = Column(DateTime, default=datetime.utcnow, nullable=False)
    kind = Column(String, nullable=False)   # invite_created | invite_accepted | revoked | left
    owner_chat_id = Column(String, nullable=False, index=True)
    actor_chat_id = Column(String, nullable=False)
    payload = Column(Text, nullable=True)


_engine = None
_SessionLocal = None


def init_db(db_path: str = "/app/data/reminders.db"):
    global _engine, _SessionLocal
    _engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})

    # Run any pending data migrations before create_all so that ALTER TABLE
    # statements land on the existing schema while it's still the old shape.
    from migrations import migrate
    migrate(_engine, db_path)

    Base.metadata.create_all(_engine)
    _SessionLocal = sessionmaker(bind=_engine)


def get_session() -> Session:
    return _SessionLocal()
