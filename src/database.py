from datetime import datetime
from sqlalchemy import create_engine, Column, Integer, String, DateTime, Boolean, ForeignKey, Text, Index
from sqlalchemy.orm import DeclarativeBase, relationship, sessionmaker, Session


class Base(DeclarativeBase):
    pass


class Thought(Base):
    __tablename__ = "thoughts"

    id = Column(Integer, primary_key=True)
    chat_id = Column(String, nullable=False)
    content = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    status = Column(String, default="pending")  # pending | done

    reminders = relationship("Reminder", back_populates="thought", cascade="all, delete-orphan")


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


_engine = None
_SessionLocal = None


def init_db(db_path: str = "/app/data/reminders.db"):
    global _engine, _SessionLocal
    _engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(_engine)
    _SessionLocal = sessionmaker(bind=_engine)


def get_session() -> Session:
    return _SessionLocal()
