"""Time handling: aware UTC in storage, campus zone only for interpreting local times.

No network. The DB is an in-memory SQLite engine.
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text
from sqlmodel import Session, SQLModel, create_engine, select

from app.models import Club, Event
from app.services.extraction import to_utc as extraction_to_utc
from app.services.sop_sync import is_expired
from app.timeutil import CAMPUS_TZ, as_utc, local_to_utc, to_campus, utc_now

UTC = timezone.utc


@pytest.fixture
def session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def test_campus_zone_is_an_iana_name_not_a_fixed_offset():
    assert CAMPUS_TZ.key == "America/Toronto"


def test_utc_now_is_aware_utc():
    now = utc_now()
    assert now.tzinfo is not None and now.utcoffset() == timedelta(0)


def test_local_time_converts_with_daylight_saving_on_each_side():
    # Clocks fall back at 2am on 2026-11-01: Toronto is UTC-4 before, UTC-5 after.
    summer = local_to_utc(datetime(2026, 10, 31, 19, 0))
    winter = local_to_utc(datetime(2026, 11, 2, 19, 0))
    assert summer == datetime(2026, 10, 31, 23, 0, tzinfo=UTC)
    assert winter == datetime(2026, 11, 3, 0, 0, tzinfo=UTC)


def test_local_to_utc_trusts_an_offset_it_is_given():
    plus_two = datetime(2026, 10, 31, 19, 0, tzinfo=timezone(timedelta(hours=2)))
    assert local_to_utc(plus_two) == datetime(2026, 10, 31, 17, 0, tzinfo=UTC)


def test_as_utc_reads_naive_as_utc_and_converts_aware():
    assert as_utc(datetime(2026, 9, 24, 23, 0)) == datetime(2026, 9, 24, 23, 0, tzinfo=UTC)
    toronto = datetime(2026, 9, 24, 19, 0, tzinfo=CAMPUS_TZ)
    assert as_utc(toronto) == datetime(2026, 9, 24, 23, 0, tzinfo=UTC)


def test_to_campus_round_trips():
    start = datetime(2026, 11, 3, 0, 0, tzinfo=UTC)
    assert to_campus(start).hour == 19 and to_campus(start).day == 2


def test_extraction_to_utc_returns_aware_utc():
    assert extraction_to_utc("2026-10-31T19:00:00") == datetime(2026, 10, 31, 23, 0, tzinfo=UTC)
    assert extraction_to_utc("2026-10-31T19:00:00-04:00") == datetime(2026, 10, 31, 23, 0, tzinfo=UTC)
    assert extraction_to_utc("not a date") is None
    assert extraction_to_utc(None) is None


def test_old_naive_rows_read_back_as_aware_utc_and_stay_queryable(session):
    """Rows written before this change are naive UTC text. They must still load, and
    compare correctly against utc_now(), without any migration of the stored data."""
    club = Club(name="Old Club")
    session.add(club)
    session.commit()
    session.execute(
        text(
            "INSERT INTO event (id, club_id, title, start, source, status) "
            "VALUES ('e1', :cid, 'Old', '2999-01-01 22:00:00.000000', 'sop', 'published')"
        ),
        {"cid": club.id},
    )
    session.commit()

    event = session.get(Event, "e1")
    assert event.start == datetime(2999, 1, 1, 22, 0, tzinfo=UTC)

    upcoming = session.exec(select(Event).where(Event.start >= utc_now())).all()
    assert [e.id for e in upcoming] == ["e1"]


def test_new_rows_round_trip_as_aware_utc(session):
    club = Club(name="New Club")
    session.add(club)
    session.commit()
    start = datetime(2026, 11, 2, 19, 0, tzinfo=CAMPUS_TZ)
    session.add(Event(club_id=club.id, title="New", start=as_utc(start)))
    session.commit()

    stored = session.exec(select(Event)).one()
    assert stored.start == start  # same instant, however it is expressed
    assert stored.start.utcoffset() == timedelta(0)
    assert club.last_updated.tzinfo is not None


def test_listing_expiry_compares_against_aware_now():
    record = {"acf": {"_expiration-date": "2000-01-01 00:00:00"}}
    assert is_expired(record) is True
    assert is_expired({"acf": {"_expiration-date": "2999-01-01 00:00:00"}}) is False
    assert is_expired({"acf": {}}) is False
