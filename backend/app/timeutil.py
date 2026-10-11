"""One place for how ClubHub handles time.

The convention, everywhere:

* **Stored and passed around as timezone-aware UTC.** Every `datetime` that is written
  to the database, compared with a stored value, or handed between services carries
  `tzinfo=UTC`. SQLModel 0.0.46+ rejects naive datetimes on write, and a naive value
  is ambiguous the moment a second school in another zone exists.
* **Interpreted and displayed in the school's zone.** A poster states wall-clock time
  ("Thu 7pm") with no zone, so it is read as `CAMPUS_TZ`, then converted to UTC once, here.

`CAMPUS_TZ` is an IANA name (default `America/Toronto`), never a fixed offset: Toronto
is UTC-5 in winter and UTC-4 in summer, and a fixed "EST" would put every event from
March to November an hour off.

Old rows were written as naive UTC. SQLModel reads them back as aware UTC, so nothing
stored needs rewriting.
"""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from app.config import settings

UTC = timezone.utc
CAMPUS_TZ = ZoneInfo(settings.school_timezone)


def utc_now() -> datetime:
    """Now, as aware UTC. Replaces `datetime.utcnow()`, which returns a naive value."""
    return datetime.now(UTC)


def as_utc(value: datetime) -> datetime:
    """Aware UTC from a value that is *already* UTC or carries its own offset.

    A naive input is taken to be UTC (that is how the database and SOP's UTC fields
    store it); an aware one is converted. For a naive *local* time use `local_to_utc`.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def local_to_utc(value: datetime) -> datetime:
    """Aware UTC from a wall-clock time in the school's zone.

    A naive input is read as `CAMPUS_TZ` (the poster case); an aware one is trusted and
    converted, never re-zoned.
    """
    if value.tzinfo is None:
        value = value.replace(tzinfo=CAMPUS_TZ)
    return value.astimezone(UTC)


def to_campus(value: datetime) -> datetime:
    """A UTC value expressed in the school's zone, for display or prompts."""
    return as_utc(value).astimezone(CAMPUS_TZ)
