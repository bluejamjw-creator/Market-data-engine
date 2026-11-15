"""HTTP endpoints: health, database check, status and candidates."""
from datetime import datetime, timezone

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

UTC = timezone.utc


@pytest.fixture
def client(db):
    from app.main import app

    with TestClient(app) as test_client:
        yield test_client


def test_health_reports_ok_with_database_and_timezone(client):
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok" and body["database"] == "ok"
    assert body["timezone"] == "Europe/London"
    assert body["scheduler"] == "stopped"  # disabled in tests
    assert body["scan_in_progress"] is False


def test_database_health_endpoint(client):
    response = client.get("/health/db")
    assert response.status_code == 200
    assert response.json() == {"database": "ok", "detail": "ok", "target": "sqlite"}


def test_health_stays_up_but_degraded_when_database_is_down(client, monkeypatch):
    import app.health as health

    monkeypatch.setattr(health, "check_connection", lambda: (False, "OperationalError: connection refused"))
    assert client.get("/health").json()["status"] == "degraded"
    assert client.get("/health").status_code == 200
    assert client.get("/health/db").status_code == 503
    assert client.get("/status").status_code == 503


def test_status_before_any_scan(client):
    body = client.get("/status").json()
    assert body["database"] == "ok" and body["candles_stored"] == 0
    assert body["last_scan"] is None and body["most_recent_collection"] is None
    assert [job["time"] for job in body["schedule"]] == ["07:30", "14:15", "18:00"]
    assert client.get("/scans/latest").status_code == 404
    assert client.get("/candidates").status_code == 404


def test_root_and_admin_endpoint_disabled_without_token(client):
    assert client.get("/").json()["service"] == "market-data-engine"
    assert client.post("/admin/scan").status_code == 404


def test_latest_scan_and_candidates_are_served(client, db):
    from app.models import CandidateLevel, Exchange, Instrument, ScanCandidate, ScanRun

    now = datetime(2026, 11, 12, 18, 0, tzinfo=UTC)
    with db.session_scope() as session:
        exchange = Exchange(code="US", name="US", region="US", timezone="America/New_York")
        session.add(exchange)
        session.flush()
        instrument = Instrument(symbol="NVDA", name="NVIDIA", asset_class="equity", region="US",
                                exchange_id=exchange.id, currency="USD")
        run = ScanRun(scan_type="review", slot_key="2026-11-12:review", scope="all", status="success",
                      started_at=now, finished_at=now, candidates_total=1, near_ready_count=1)
        session.add_all([instrument, run])
        session.flush()
        candidate = ScanCandidate(
            scan_run_id=run.id, instrument_id=instrument.id, classification="NEAR-READY",
            setup_type="BREAKOUT_PREP", setup_description="Tight base under a pivot.",
            score_total=71.0, score_max_verified=97.0, provisional=True, price=180.0, price_time=now,
            trigger_price=182.0, entry_low=182.0, entry_high=182.8, stop_price=177.3, target_price=192.5,
            reward_risk=2.1, est_cost_pct=0.1, data_quality_status="PASS",
            component_status={"pattern": {"points": 19}}, reasons=["Tight base under a pivot."],
        )
        session.add(candidate)
        session.flush()
        session.add(CandidateLevel(candidate_id=candidate.id, level_type="trigger", price=182.0,
                                   basis="pivot: highest high of the last 20 bars"))
    body = client.get("/scans/latest").json()
    assert body["scan"]["status"] == "success" and len(body["candidates"]) == 1
    item = body["candidates"][0]
    assert item["symbol"] == "NVDA" and item["classification"] == "NEAR-READY"
    assert item["score_is_provisional"] is True and item["reward_risk"] == 2.1
    assert item["entry_zone"] == [182.0, 182.8] and item["levels"][0]["type"] == "trigger"
    assert client.get("/candidates", params={"classification": "ready"}).json()["candidates"] == []
    assert len(client.get("/candidates", params={"classification": "near-ready"}).json()["candidates"]) == 1
    assert client.get("/status").json()["last_scan"]["slot"] == "2026-11-12:review"
