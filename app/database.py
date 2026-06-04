import os
from datetime import datetime, timezone
from sqlalchemy import (
    create_engine, Column, String, Integer, Float, Boolean,
    DateTime, Text, Index, event as sa_event
)
from sqlalchemy.orm import declarative_base, sessionmaker, Session
from sqlalchemy.exc import OperationalError

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./store_intelligence.db")

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}

engine = create_engine(DATABASE_URL, connect_args=connect_args)

# Enable WAL mode for SQLite for better concurrency
@sa_event.listens_for(engine, "connect")
def set_sqlite_pragma(dbapi_connection, connection_record):
    if DATABASE_URL.startswith("sqlite"):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


class EventModel(Base):
    __tablename__ = "events"

    event_id = Column(String, primary_key=True)
    store_id = Column(String, nullable=False, index=True)
    camera_id = Column(String, nullable=False)
    visitor_id = Column(String, nullable=False, index=True)
    event_type = Column(String, nullable=False, index=True)
    timestamp = Column(DateTime, nullable=False, index=True)
    zone_id = Column(String, nullable=True)
    dwell_ms = Column(Integer, default=0)
    is_staff = Column(Boolean, default=False, index=True)
    confidence = Column(Float, default=1.0)
    metadata_json = Column(Text, default="{}")
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        Index("ix_events_store_type", "store_id", "event_type"),
        Index("ix_events_store_ts", "store_id", "timestamp"),
        Index("ix_events_store_visitor", "store_id", "visitor_id"),
    )


class PosTransaction(Base):
    __tablename__ = "pos_transactions"

    transaction_id = Column(String, primary_key=True)
    store_id = Column(String, nullable=False, index=True)
    timestamp = Column(DateTime, nullable=False, index=True)
    basket_value_inr = Column(Float, default=0.0)


class VisitorSession(Base):
    __tablename__ = "visitor_sessions"

    session_id = Column(String, primary_key=True)  # store_id:visitor_id
    store_id = Column(String, nullable=False, index=True)
    visitor_id = Column(String, nullable=False, index=True)
    first_seen = Column(DateTime, nullable=True)
    last_seen = Column(DateTime, nullable=True)
    has_entry = Column(Boolean, default=False)
    has_zone_visit = Column(Boolean, default=False)
    has_billing = Column(Boolean, default=False)
    has_purchase = Column(Boolean, default=False)
    is_staff = Column(Boolean, default=False)
    reentry_count = Column(Integer, default=0)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    Base.metadata.create_all(bind=engine)


def check_db() -> bool:
    try:
        with engine.connect() as conn:
            conn.execute(__import__("sqlalchemy").text("SELECT 1"))
        return True
    except OperationalError:
        return False
