"""Declarative base and shared column types."""
from datetime import datetime, timezone

from sqlalchemy import BigInteger, DateTime, Integer
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.types import TypeDecorator


class UTCDateTime(TypeDecorator):
    """Timezone-aware datetime stored in UTC.

    PostgreSQL keeps the timezone; SQLite (used in tests) drops it, so values
    are converted to UTC on the way in and re-labelled UTC on the way out.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


# 64-bit ids on PostgreSQL; SQLite only auto-increments plain INTEGER keys.
BigIntPK = BigInteger().with_variant(Integer, "sqlite")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass
