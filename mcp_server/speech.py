"""Parsing and phrasing for tool arguments and replies.

Tool summaries are read aloud by a voice assistant, so they use words a person would say:
"7:42 AM", "today", "6 boxes", never ISO timestamps. Inputs are parsed leniently for the same
reason: a model turning speech into arguments may write "7am" or "tomorrow".
"""

import re
from datetime import date, datetime, time, timedelta, tzinfo

SIMULATED_NOTE = "Note: this includes simulated demo history, not real deliveries."


class BadArgument(ValueError):
    pass


def parse_day(value: str | None, today: date) -> date:
    v = (value or "today").strip().lower()
    relative = {"today": 0, "yesterday": -1, "tomorrow": 1}
    if v in relative:
        return today + timedelta(days=relative[v])
    try:
        return date.fromisoformat(v)
    except ValueError:
        raise BadArgument(
            f"date must be YYYY-MM-DD, 'today', 'yesterday' or 'tomorrow', not {value!r}"
        ) from None


_TIME = re.compile(r"^(\d{1,2})(?::?(\d{2}))?\s*([ap])?\.?\s*(?:m\.?)?$")


def parse_time(value: str) -> time:
    """'07:00', '7:00', '7', '7am', '7:30 PM', '19:00'."""
    m = _TIME.match(value.strip().lower())
    if not m:
        raise BadArgument(f"time must look like 07:00 or 7:00 AM, not {value!r}")
    hour, minute, ampm = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if ampm:
        if not 1 <= hour <= 12:
            raise BadArgument(f"{value!r}: hour must be 1-12 with AM/PM")
        hour = hour % 12 + (12 if ampm == "p" else 0)
    if hour > 23 or minute > 59:
        raise BadArgument(f"{value!r} is not a time of day")
    return time(hour, minute)


def clock(t: datetime, tz: tzinfo) -> str:
    local = t.astimezone(tz)
    return f"{local.hour % 12 or 12}:{local:%M} {'AM' if local.hour < 12 else 'PM'}"


def window(start: datetime, end: datetime, tz: tzinfo) -> str:
    return f"{clock(start, tz)} to {clock(end, tz)}"


def day_phrase(day: date, today: date) -> str:
    offset = (day - today).days
    if offset == 0:
        return "today"
    if offset == -1:
        return "yesterday"
    if offset == 1:
        return "tomorrow"
    return f"on {day:%A}, {day:%B} {day.day}"


def boxes(n: int) -> str:
    return f"{n} box" if n == 1 else f"{n} boxes"


def carrier_label(carrier: str | None) -> str:
    if not carrier or carrier == "Unknown":
        return "carrier not visible"
    return carrier


def listing(items: list[str], limit: int = 4) -> str:
    """'a, b and c', or 'a, b, c and 2 more' past `limit`."""
    if len(items) > limit:
        items = [*items[:limit], f"{len(items) - limit} more"]
    if len(items) <= 1:
        return "".join(items)
    return f"{', '.join(items[:-1])} and {items[-1]}"


def pct(k: int, n: int) -> float | None:
    return round(100 * k / n, 1) if n else None
