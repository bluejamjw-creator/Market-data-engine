"""Scan schedule: Europe/London times, daylight saving, weekends, no overlap."""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

pytest.importorskip("apscheduler")

from app.scheduler import SCHEDULE, build_trigger, create_scheduler, describe_jobs, schedule_timezone  # noqa: E402
from app.services.scan_service import scan_scope, slot_key_for  # noqa: E402

LONDON = ZoneInfo("Europe/London")
UTC = timezone.utc


def test_schedule_is_three_london_scans():
    assert [(e["id"], e["hour"], e["minute"]) for e in SCHEDULE] == [
        ("morning", 7, 30), ("us_premarket", 14, 15), ("review", 18, 0)]
    assert str(schedule_timezone()) == "Europe/London"


def test_jobs_cannot_overlap_and_use_london_time():
    scheduler = create_scheduler()
    jobs = {job.id: job for job in scheduler.get_jobs()}
    assert set(jobs) == {"morning", "us_premarket", "review"}
    for job in jobs.values():
        assert job.max_instances == 1 and job.coalesce is True
        assert str(job.trigger.timezone) == "Europe/London"


@pytest.mark.parametrize("after,expected_utc", [
    # winter (GMT): 07:30 London is 07:30 UTC
    (datetime(2026, 11, 16, 0, 0, tzinfo=LONDON), datetime(2026, 11, 16, 7, 30, tzinfo=UTC)),
    # summer (BST): 07:30 London is 06:30 UTC
    (datetime(2026, 7, 6, 0, 0, tzinfo=LONDON), datetime(2026, 7, 6, 6, 30, tzinfo=UTC)),
    # the day the clocks go forward (29 March 2026)
    (datetime(2026, 3, 28, 12, 0, tzinfo=LONDON), datetime(2026, 3, 29, 6, 30, tzinfo=UTC)),
    # the day the clocks go back (25 October 2026)
    (datetime(2026, 10, 24, 12, 0, tzinfo=LONDON), datetime(2026, 10, 25, 7, 30, tzinfo=UTC)),
])
def test_morning_scan_follows_daylight_saving(after, expected_utc):
    fire = build_trigger(SCHEDULE[0]).get_next_fire_time(None, after)
    assert fire.astimezone(UTC) == expected_utc
    assert (fire.astimezone(LONDON).hour, fire.astimezone(LONDON).minute) == (7, 30)


def test_us_premarket_and_review_times_in_summer():
    after = datetime(2026, 7, 6, 0, 0, tzinfo=LONDON)
    premarket = build_trigger(SCHEDULE[1]).get_next_fire_time(None, after)
    review = build_trigger(SCHEDULE[2]).get_next_fire_time(None, after)
    assert premarket.astimezone(UTC) == datetime(2026, 7, 6, 13, 15, tzinfo=UTC)
    assert review.astimezone(UTC) == datetime(2026, 7, 6, 17, 0, tzinfo=UTC)


def test_describe_jobs_reports_next_runs():
    jobs = describe_jobs()
    assert [job["time"] for job in jobs] == ["07:30", "14:15", "18:00"]
    assert all(job["timezone"] == "Europe/London" and job["next_run"] for job in jobs)


def test_weekends_are_crypto_only():
    assert scan_scope(datetime(2026, 11, 14, 7, 30, tzinfo=UTC)) == "crypto_only"   # Saturday
    assert scan_scope(datetime(2026, 11, 15, 18, 0, tzinfo=UTC)) == "crypto_only"   # Sunday
    assert scan_scope(datetime(2026, 11, 16, 7, 30, tzinfo=UTC)) == "all"           # Monday
    # Friday 23:30 UTC in summer is already Saturday in London
    assert scan_scope(datetime(2026, 7, 10, 23, 30, tzinfo=UTC)) == "crypto_only"


def test_scheduled_slots_have_one_key_per_day():
    morning = datetime(2026, 11, 16, 7, 30, tzinfo=UTC)
    assert slot_key_for("morning", morning) == "2026-11-16:morning"
    assert slot_key_for("morning", morning.replace(minute=45)) == "2026-11-16:morning"
    assert slot_key_for("manual", morning) != slot_key_for("manual", morning.replace(minute=45))
