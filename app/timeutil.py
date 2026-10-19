"""Single application timezone: India Standard Time (IST, Asia/Kolkata, UTC+05:30)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional, Union
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
UTC = timezone.utc

# Delta perpetual funding settles every 8h at 00:00 / 08:00 / 16:00 UTC
# → 05:30 / 13:30 / 21:30 IST
FUNDING_HOURS_UTC = (0, 8, 16)


def now_ist() -> datetime:
    """Current wall-clock time in IST (timezone-aware)."""
    return datetime.now(IST)


def to_ist(dt: Optional[datetime]) -> Optional[datetime]:
    """Convert any datetime to IST. Naive values are treated as UTC (exchange/storage)."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(IST)


def ensure_ist(dt: Optional[datetime]) -> Optional[datetime]:
    """If naive, assume already IST wall time; if aware, convert to IST."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=IST)
    return dt.astimezone(IST)


def ist_date_str(dt: Optional[datetime] = None) -> str:
    """Calendar date in IST as YYYY-MM-DD (used for daily risk reset)."""
    return (dt or now_ist()).astimezone(IST).strftime("%Y-%m-%d")


def iso_ist(dt: Optional[datetime]) -> Optional[str]:
    """Serialize datetime as ISO-8601 in IST with +05:30 offset."""
    converted = to_ist(dt)
    if converted is None:
        return None
    return converted.isoformat()


def parse_to_ist(value: Union[str, datetime, None]) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return to_ist(value)
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return to_ist(datetime.fromisoformat(text))
    except ValueError:
        return None


def next_funding_ist(now: Optional[datetime] = None) -> datetime:
    """Next Delta funding settlement time, returned in IST."""
    current = (now or now_ist()).astimezone(UTC)
    # Candidate slots today in UTC
    for hour in FUNDING_HOURS_UTC:
        candidate = current.replace(hour=hour, minute=0, second=0, microsecond=0)
        if candidate > current:
            return candidate.astimezone(IST)
    # Roll to next day's first slot
    tomorrow = (current + timedelta(days=1)).replace(
        hour=FUNDING_HOURS_UTC[0], minute=0, second=0, microsecond=0
    )
    return tomorrow.astimezone(IST)


def funding_countdown(now: Optional[datetime] = None) -> dict:
    current = now_ist() if now is None else to_ist(now) or now_ist()
    nxt = next_funding_ist(current)
    remaining = max(0, int((nxt - current).total_seconds()))
    hours, rem = divmod(remaining, 3600)
    minutes, seconds = divmod(rem, 60)
    return {
        "next_funding_ist": nxt.isoformat(),
        "next_funding_label": nxt.strftime("%d %b %Y, %I:%M %p IST"),
        "seconds_remaining": remaining,
        "countdown": f"{hours:02d}:{minutes:02d}:{seconds:02d}",
        "timezone": "Asia/Kolkata",
    }


def store_naive_ist(dt: Optional[datetime] = None) -> datetime:
    """
    Naive IST datetime for SQLite DateTime columns.
    All app-event timestamps are stored as IST wall clock (no UTC conversion on read for UI).
    """
    aware = dt.astimezone(IST) if dt and dt.tzinfo else (dt.replace(tzinfo=IST) if dt else now_ist())
    return aware.replace(tzinfo=None)
