"""Database engine, sessions and helpers.

Railway supplies `DATABASE_URL` as `postgresql://...` (older setups use
`postgres://...`). SQLAlchemy needs an explicit driver, so every accepted
form is normalised to `postgresql+psycopg://` (psycopg 3).
"""
import logging
from contextlib import contextmanager
from typing import Iterable, Iterator, Optional, Sequence

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings

log = logging.getLogger(__name__)

_engine: Optional[Engine] = None
_session_factory: Optional[sessionmaker] = None

_PG_PREFIXES = (
    "postgres://",
    "postgresql://",
    "postgresql+psycopg2://",
    "postgresql+asyncpg://",
    "postgresql+pg8000://",
)


def normalise_database_url(url: str) -> str:
    """Return a SQLAlchemy URL that uses the psycopg 3 driver for PostgreSQL."""
    if not url or not url.strip():
        raise ValueError("DATABASE_URL is not set")
    url = url.strip().strip('"').strip("'")
    for prefix in _PG_PREFIXES:
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


def get_database_url() -> str:
    return normalise_database_url(get_settings().database_url or "")


def describe_database_target(url: Optional[str] = None) -> str:
    """Host/port/database only - safe for logs."""
    try:
        parsed = make_url(url or get_database_url())
        if parsed.get_backend_name() == "sqlite":
            return "sqlite"
        return f"{parsed.get_backend_name()}://{parsed.host}:{parsed.port or 5432}/{parsed.database}"
    except Exception:
        return "unavailable"


def get_engine() -> Engine:
    global _engine, _session_factory
    if _engine is None:
        url = get_database_url()
        if url.startswith("sqlite"):
            _engine = create_engine(url, connect_args={"check_same_thread": False})
        else:
            _engine = create_engine(
                url,
                pool_pre_ping=True,
                pool_size=3,
                max_overflow=3,
                pool_recycle=1800,
                connect_args={"connect_timeout": 10},
            )
        _session_factory = sessionmaker(bind=_engine, expire_on_commit=False)
        log.info("Database engine created for %s", describe_database_target(url))
    return _engine


def reset_engine() -> None:
    """Dispose the engine (used by tests when DATABASE_URL changes)."""
    global _engine, _session_factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _session_factory = None


def new_session() -> Session:
    get_engine()
    assert _session_factory is not None
    return _session_factory()


@contextmanager
def session_scope() -> Iterator[Session]:
    session = new_session()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def check_connection() -> tuple:
    """Return (ok, detail). Never raises and never includes credentials."""
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        return True, "ok"
    except Exception as exc:  # noqa: BLE001
        from app.logging_setup import redact

        return False, redact(f"{type(exc).__name__}: {exc}")[:300]


def _chunks(rows: Sequence[dict], size: int) -> Iterable[Sequence[dict]]:
    for start in range(0, len(rows), size):
        yield rows[start:start + size]


def upsert_rows(
    session: Session,
    model,
    rows: Sequence[dict],
    conflict_cols: Sequence[str],
    update_cols: Sequence[str] = (),
) -> int:
    """Insert rows; on a unique-key conflict update `update_cols` (or do nothing).

    This is what makes collection idempotent: re-fetching a candle never
    creates a duplicate row.
    """
    if not rows:
        return 0
    dialect = session.get_bind().dialect.name
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    elif dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert
    else:  # pragma: no cover
        raise RuntimeError(f"Unsupported database dialect: {dialect}")
    for chunk in _chunks(list(rows), 400):
        stmt = insert(model).values(list(chunk))
        if update_cols:
            stmt = stmt.on_conflict_do_update(
                index_elements=list(conflict_cols),
                set_={col: getattr(stmt.excluded, col) for col in update_cols},
            )
        else:
            stmt = stmt.on_conflict_do_nothing(index_elements=list(conflict_cols))
        session.execute(stmt)
    return len(rows)
