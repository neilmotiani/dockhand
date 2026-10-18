"""The ledger tools behind the MCP server (SPEC.md section 8).

Each tool is a plain async function over a `Ledger` (database engine, settings, clock) and
returns a pydantic model with three parts:
- `summary`: one to three sentences a voice assistant can read aloud as they are.
- `cards`: what a screen device shows: title, subtitle, status badge and a snapshot URL.
- compact typed rows for the model to reason over (not the full ledger rows: those carry
  file paths and pipeline internals a model has no use for).

Bad arguments raise `BadArgument`; mcp_server/server.py turns that into a tool execution error
(`isError: true`) so the model can correct itself, as the MCP spec asks.
"""

import difflib
import re
import uuid
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from functools import cached_property
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field
from sqlalchemy import func, or_
from sqlalchemy.ext.asyncio import AsyncEngine

from app.config import Settings
from app.db.models import expected_deliveries
from app.extract.schemas import Carrier
from app.ledger import queries, suppliers
from app.reconcile.cost import ReconcileConfig
from app.reconcile.expected import ExpectedConflict, add_expected
from app.reconcile.store import business_day, day_bounds
from mcp_server import speech
from mcp_server.media import media_key, signed_url
from mcp_server.speech import BadArgument

Status = Literal["matched", "short", "over", "missing", "unexpected", "pending"]

# Orders created through MCP get a deterministic ID (uuid5 of their fields), so a model that
# retries a call, or a user who repeats the request, adds the order once.
ORDER_NAMESPACE = uuid.UUID("0b8f3c1e-5d4a-4f7e-9c2b-6a1d8e3f5b70")
MAX_RANGE_DAYS = 31
MAX_CARDS = 4  # a screen shows a few; the full rows are in the structured result
# How far back and ahead check_delivery looks when no date is given.
CHECK_LOOKBACK_DAYS = 14
CHECK_LOOKAHEAD_DAYS = 7


@dataclass
class Ledger:
    engine: AsyncEngine
    settings: Settings
    clock: Callable[[], datetime]

    @cached_property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.settings.business_timezone)

    @cached_property
    def key(self) -> bytes:
        return media_key(self.settings)

    @property
    def include_simulated(self) -> bool:
        return self.settings.mcp_include_simulated

    def today(self) -> date:
        return business_day(self.clock(), self.tz)

    def image(self, delivery: dict | None) -> str | None:
        if not delivery or not delivery["media"]["snapshot"]:
            return None
        return signed_url(
            self.settings.mcp_public_url,
            self.key,
            delivery["id"],
            self.clock(),
            self.settings.mcp_media_ttl_s,
        )


# --- Output models ---------------------------------------------------------------------------


class Fact(BaseModel):
    label: str
    value: str


class Card(BaseModel):
    title: str
    subtitle: str | None = None
    status: Status | None = None
    image_url: str | None = Field(default=None, description="Signed snapshot URL, expires")
    simulated: bool = False
    facts: list[Fact] = []


class ToolOutput(BaseModel):
    summary: str = Field(description="Plain sentences, ready to be spoken")
    cards: list[Card] = []
    includes_simulated: bool = Field(
        default=False, description="True if any row is simulated demo history"
    )


class DeliveryItem(BaseModel):
    id: uuid.UUID
    detected_at: datetime
    time: str
    carrier: str
    package_count: int
    status: Status
    reason: str | None
    supplier: str | None = Field(description="Supplier of the matched order, if any")
    expected_count: int | None
    reference: str | None
    snapshot_url: str | None
    simulated: bool


class OrderItem(BaseModel):
    id: uuid.UUID
    date: date
    supplier: str
    carrier: str | None
    expected_count: int
    window_start: datetime
    window_end: datetime
    window: str
    reference: str | None
    status: Status
    reason: str | None
    delivered_count: int | None
    delivered_at: str | None
    snapshot_url: str | None
    simulated: bool


class DiscrepancyItem(BaseModel):
    date: date
    status: Status
    reason: str | None
    supplier: str | None
    expected_count: int | None
    delivered_count: int | None
    carrier: str | None
    time: str
    snapshot_url: str | None
    simulated: bool


class SupplierItem(BaseModel):
    supplier: str
    orders_due: int
    delivered: int
    on_time: int
    on_time_pct: float | None
    late: int
    short: int
    short_rate_pct: float | None = Field(description="short / delivered")
    boxes_short: int
    missed: int
    problem_rate_pct: float | None = Field(description="orders missing, short or late / due")
    worst: bool
    simulated_orders: int


class DeliveriesOutput(ToolOutput):
    date: date
    deliveries: list[DeliveryItem]
    orders: list[OrderItem]


class CheckOutput(ToolOutput):
    found: bool
    matched_supplier: str | None = Field(description="Supplier name the query resolved to")
    orders: list[OrderItem]
    unexpected_deliveries: list[DeliveryItem] = Field(
        description="Unmatched deliveries whose carrier matches the query (same day only)"
    )
    nearby: list[OrderItem] = Field(
        default=[],
        description="With a date and no order that day: the supplier's previous and next order",
    )


class DiscrepanciesOutput(ToolOutput):
    start_date: date
    end_date: date
    total: int
    counts: dict[str, int]
    discrepancies: list[DiscrepancyItem]


class AddOutput(ToolOutput):
    created: bool = Field(description="False if this exact order was already on the list")
    order: OrderItem


class ScorecardOutput(ToolOutput):
    from_date: date
    to_date: date
    days: int
    worst: str | None = Field(description="Most problems (missing, short or late) per order")
    most_boxes_short: str | None
    suppliers: list[SupplierItem]


# --- Row conversion --------------------------------------------------------------------------


def _delivery_item(ledger: Ledger, row: dict) -> DeliveryItem:
    exp = row["expected"]
    return DeliveryItem(
        id=row["id"],
        detected_at=row["detected_at"],
        time=speech.clock(row["detected_at"], ledger.tz),
        carrier=row["carrier"],
        package_count=row["package_count"],
        status=row["status"],
        reason=row["reason"],
        supplier=exp["supplier"] if exp else None,
        expected_count=exp["expected_count"] if exp else None,
        reference=exp["reference"] if exp else None,
        snapshot_url=ledger.image(row),
        simulated=row["simulated"],
    )


def _order_item(ledger: Ledger, row: dict) -> OrderItem:
    d = row["delivery"]
    return OrderItem(
        id=row["id"],
        date=business_day(row["window_start"], ledger.tz),
        supplier=row["supplier"],
        carrier=row["carrier"],
        expected_count=row["expected_count"],
        window_start=row["window_start"],
        window_end=row["window_end"],
        window=speech.window(row["window_start"], row["window_end"], ledger.tz),
        reference=row["reference"],
        status=row["status"],
        reason=row["reason"],
        delivered_count=d["package_count"] if d else None,
        delivered_at=speech.clock(d["detected_at"], ledger.tz) if d else None,
        snapshot_url=ledger.image(d),
        simulated=row["simulated"],
    )


def _delivery_card(x: DeliveryItem) -> Card:
    who = x.supplier or speech.carrier_label(x.carrier)
    return Card(
        title=f"{who} · {speech.boxes(x.package_count)}",
        subtitle=x.reason or f"Logged at {x.time}",
        status=x.status,
        image_url=x.snapshot_url,
        simulated=x.simulated,
        facts=[
            Fact(label="Time", value=x.time),
            Fact(label="Carrier", value="Not visible" if x.carrier == "Unknown" else x.carrier),
        ],
    )


def _order_card(x: OrderItem, today: date) -> Card:
    facts = [
        Fact(label="Window", value=f"{speech.day_phrase(x.date, today)}, {x.window}"),
        Fact(label="Expected", value=speech.boxes(x.expected_count)),
    ]
    if x.delivered_count is not None:
        facts.append(
            Fact(label="Received", value=f"{speech.boxes(x.delivered_count)} at {x.delivered_at}")
        )
    if x.reference:
        facts.append(Fact(label="Reference", value=x.reference))
    return Card(
        title=f"{x.supplier} · {speech.boxes(x.expected_count)}",
        subtitle=x.reason or f"Due {speech.day_phrase(x.date, today)}, {x.window}",
        status=x.status,
        image_url=x.snapshot_url,
        simulated=x.simulated,
        facts=facts,
    )


def _with_note(summary: str, simulated: bool) -> str:
    return f"{summary} {speech.SIMULATED_NOTE}" if simulated else summary


# --- Order status, in words ------------------------------------------------------------------


def order_sentence(x: OrderItem, today: date) -> str:
    when = speech.day_phrase(x.date, today)
    ref = f" ({x.reference})" if x.reference else ""
    s = x.status
    if s == "matched":
        return (
            f"{x.supplier}{ref} delivered {speech.boxes(x.delivered_count or 0)} at "
            f"{x.delivered_at} {when}, as expected."
        )
    if s == "short":
        gap = x.expected_count - (x.delivered_count or 0)
        return (
            f"{x.supplier}{ref} came at {x.delivered_at} {when} but left {x.delivered_count} of "
            f"{speech.boxes(x.expected_count)}: {gap} short."
        )
    if s == "over":
        extra = (x.delivered_count or 0) - x.expected_count
        return (
            f"{x.supplier}{ref} delivered {speech.boxes(x.delivered_count or 0)} at "
            f"{x.delivered_at} {when}, {extra} more than the {x.expected_count} expected."
        )
    if s == "missing":
        return (
            f"{x.supplier}{ref} has not arrived: {speech.boxes(x.expected_count)} were due "
            f"{when}, {x.window}, and no matching delivery was logged."
        )
    due = f"{x.supplier}{ref} is due {when}, {x.window} ({speech.boxes(x.expected_count)})."
    return due if x.date > today else f"{due} Nothing matching has arrived yet."


# --- Tools -----------------------------------------------------------------------------------


async def get_todays_deliveries(ledger: Ledger, date: str | None = None) -> DeliveriesOutput:
    today = ledger.today()
    day = speech.parse_day(date, today)
    async with ledger.engine.connect() as conn:
        drows = await queries.day_deliveries(conn, day, ledger.tz, ledger.include_simulated)
        erows = await queries.day_expected(conn, day, ledger.tz, ledger.include_simulated)
    dl = [_delivery_item(ledger, r) for r in reversed(drows)]  # oldest first reads naturally
    orders = [_order_item(ledger, r) for r in erows]
    when = speech.day_phrase(day, today)

    if dl:
        parts = [
            f"{x.supplier or speech.carrier_label(x.carrier)}, {speech.boxes(x.package_count)} "
            f"at {x.time} ({x.status})"
            for x in dl
        ]
        n = len(dl)
        head = (
            f"{n} {'delivery' if n == 1 else 'deliveries'} logged {when}: {speech.listing(parts)}."
        )
    else:
        head = f"No deliveries logged {when}."
    open_orders = [x for x in orders if x.status in ("pending", "missing")]
    tail = []
    pending = [f"{x.supplier} ({x.window})" for x in open_orders if x.status == "pending"]
    missing = [x.supplier for x in open_orders if x.status == "missing"]
    if pending:
        tail.append(f"Still expected: {speech.listing(pending)}.")
    if missing:
        tail.append(f"Missing: {speech.listing(missing)}.")
    if not orders:
        tail.append(f"No orders were expected {when}.")
    simulated = any(x.simulated for x in dl) or any(x.simulated for x in orders)
    cards = [_delivery_card(x) for x in dl] + [_order_card(x, today) for x in open_orders]
    return DeliveriesOutput(
        summary=_with_note(" ".join([head, *tail]), simulated),
        cards=cards[:MAX_CARDS],
        includes_simulated=simulated,
        date=day,
        deliveries=dl,
        orders=orders,
    )


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


# Words people add when naming a supplier aloud: "the Sysco delivery", "Daybreak order".
_FILLER = re.compile(r"\b(the|delivery|deliveries|order|orders|shipment|drop|drop-off)\b", re.I)


def resolve_supplier(query: str, names: list[str]) -> str | None:
    """The supplier a spoken or typed name refers to: a containment match on letters and digits
    first ("day break" → Daybreak Bakery), then the closest name by edit similarity, because
    speech recognition writes "Cisco" for Sysco."""
    q = _norm(_FILLER.sub(" ", query))
    if not q:
        return None
    by_norm = {_norm(n): n for n in names}
    hits = [n for k, n in by_norm.items() if q in k or k in q]
    if hits:
        return min(hits, key=lambda n: abs(len(_norm(n)) - len(q)))
    # Compare against whole names and first words ("sysko" vs "Sysco Foods" → "sysco").
    candidates: dict[str, str] = {}
    for k, n in by_norm.items():
        candidates.setdefault(k, n)
    for n in names:
        first = _norm(n.split()[0]) if n.split() else ""
        if first:
            candidates.setdefault(first, n)
    close = difflib.get_close_matches(q, list(candidates), n=1, cutoff=0.6)
    return candidates[close[0]] if close else None


def _pick_orders(orders: list[OrderItem], today: date, now: datetime) -> list[OrderItem]:
    """Today's orders if there are any; else the most recent past one; else the next one."""
    todays = [x for x in orders if x.date == today]
    if todays:
        return todays
    past = [x for x in orders if x.window_start <= now]
    if past:
        return [max(past, key=lambda x: x.window_start)]
    return [min(orders, key=lambda x: x.window_start)] if orders else []


async def check_delivery(
    ledger: Ledger,
    supplier: str | None = None,
    reference: str | None = None,
    date: str | None = None,
) -> CheckOutput:
    supplier = (supplier or "").strip() or None
    reference = (reference or "").strip() or None
    if not supplier and not reference:
        raise BadArgument("give a supplier name or an order reference")
    today, now = ledger.today(), ledger.clock()
    if date:
        first = last = speech.parse_day(date, today)
    else:
        first = today - timedelta(days=CHECK_LOOKBACK_DAYS)
        last = today + timedelta(days=CHECK_LOOKAHEAD_DAYS)
    start, _ = day_bounds(first, ledger.tz)
    _, end = day_bounds(last, ledger.tz)
    e = expected_deliveries.c

    async with ledger.engine.connect() as conn:
        name = None
        if supplier:
            name = resolve_supplier(
                supplier, await queries.supplier_names(conn, ledger.include_simulated)
            )
        conds = []
        if name:
            conds.append(func.lower(func.trim(e.supplier)) == name.lower())
        if reference:
            conds.append(func.lower(e.reference) == reference.lower())
        rows = (
            await queries.expected_with_status(
                conn,
                e.window_start >= start,
                e.window_start < end,
                queries.live_expected(ledger.include_simulated),
                or_(*conds),
            )
            if conds
            else []
        )
        # Asked about a day with no order ("did Sysco come today?" on a day Sysco doesn't
        # deliver): find the previous and next order, so the answer isn't just "no".
        around: list[dict] = []
        if date and conds and not rows:
            lo, _ = day_bounds(first - timedelta(days=CHECK_LOOKBACK_DAYS), ledger.tz)
            _, hi = day_bounds(first + timedelta(days=CHECK_LOOKAHEAD_DAYS), ledger.tz)
            around = await queries.expected_with_status(
                conn,
                e.window_start >= lo,
                e.window_start < hi,
                queries.live_expected(ledger.include_simulated),
                or_(*conds),
            )
        # A supplier with no order may still have shown up: look for an unmatched delivery
        # whose carrier is the name asked about (Sysco and US Foods deliver themselves).
        stray: list[dict] = []
        if supplier and (date or not rows):
            day = first if date else today
            stray = [
                r
                for r in await queries.day_deliveries(
                    conn, day, ledger.tz, ledger.include_simulated
                )
                if r["status"] == "unexpected"
                and _norm(r["carrier"]) in (_norm(supplier), _norm(name or ""))
            ]

    found = (
        _pick_orders([_order_item(ledger, r) for r in rows], today, now)
        if date is None
        else [_order_item(ledger, r) for r in rows]
    )
    unexpected = [_delivery_item(ledger, r) for r in stray]
    before = [r for r in around if r["window_start"] < start]
    after = [r for r in around if r["window_start"] >= end]
    # expected_with_status sorts by window start: the last before, the first after.
    labelled = [("Most recent", r) for r in before[-1:]] + [("Next", r) for r in after[:1]]
    nearby = [_order_item(ledger, r) for _, r in labelled]
    asked = name or supplier or reference or ""
    if found:
        summary = " ".join(order_sentence(x, today) for x in found)
    else:
        scope = (
            speech.day_phrase(first, today) if date else "in the last two weeks or the next week"
        )
        summary = f"I found no order from {asked} {scope}."
        if supplier and not name and not unexpected:
            summary = f"I don't know a supplier called {supplier}."
        for (label, _), x in zip(labelled, nearby, strict=True):
            summary += f" {label}: {order_sentence(x, today)}"
    if unexpected:
        summary += " " + " ".join(
            f"An unexpected {x.carrier} delivery of {speech.boxes(x.package_count)} was logged "
            f"at {x.time}."
            for x in unexpected
        )
    simulated = any(x.simulated for x in [*found, *unexpected, *nearby])
    cards = [_order_card(x, today) for x in [*found, *nearby]]
    cards += [_delivery_card(x) for x in unexpected]
    return CheckOutput(
        summary=_with_note(summary, simulated),
        cards=cards[:MAX_CARDS],
        includes_simulated=simulated,
        found=bool(found),
        matched_supplier=name,
        orders=found,
        unexpected_deliveries=unexpected,
        nearby=nearby,
    )


async def list_discrepancies(
    ledger: Ledger, start_date: str | None = None, end_date: str | None = None
) -> DiscrepanciesOutput:
    today = ledger.today()
    first = speech.parse_day(start_date, today)
    last = speech.parse_day(end_date, today) if end_date else first
    if last < first:
        raise BadArgument("end_date is before start_date")
    if (last - first).days + 1 > MAX_RANGE_DAYS:
        raise BadArgument(f"date range is limited to {MAX_RANGE_DAYS} days")

    items: list[DiscrepancyItem] = []
    async with ledger.engine.connect() as conn:
        day = first
        while day <= last:
            for r in await queries.day_discrepancies(
                conn, day, ledger.tz, ledger.include_simulated
            ):
                d, x = r["delivery"], r["expected"]
                at = d["detected_at"] if d else x["window_start"]
                items.append(
                    DiscrepancyItem(
                        date=day,
                        status=r["status"],
                        reason=r["reason"],
                        supplier=x["supplier"] if x else None,
                        expected_count=x["expected_count"] if x else None,
                        delivered_count=d["package_count"] if d else None,
                        carrier=d["carrier"] if d else (x["carrier"] if x else None),
                        time=speech.clock(at, ledger.tz),
                        snapshot_url=ledger.image(d),
                        simulated=r["simulated"],
                    )
                )
            day += timedelta(days=1)

    counts = Counter(x.status for x in items)
    span = (
        speech.day_phrase(first, today)
        if first == last
        else f"from {first:%B} {first.day} to {last:%B} {last.day}"
    )
    if items:
        parts = [f"{counts[s]} {s}" for s in queries.DISCREPANCY_ORDER if counts[s]]
        noun = "problem" if len(items) == 1 else "problems"
        head = f"{len(items)} {noun} {span}: {speech.listing(parts)}."
        top = [x.reason for x in items[:3] if x.reason]
        summary = " ".join([head, *top])
    else:
        summary = f"No discrepancies {span}. Everything logged matched what was expected."
    simulated = any(x.simulated for x in items)
    cards = [
        Card(
            title=f"{x.supplier or speech.carrier_label(x.carrier)} · {x.status}",
            subtitle=x.reason,
            status=x.status,
            image_url=x.snapshot_url,
            simulated=x.simulated,
            facts=[Fact(label="When", value=f"{speech.day_phrase(x.date, today)}, {x.time}")],
        )
        for x in items[:MAX_CARDS]
    ]
    return DiscrepanciesOutput(
        summary=_with_note(summary, simulated),
        cards=cards,
        includes_simulated=simulated,
        start_date=first,
        end_date=last,
        total=len(items),
        counts=dict(counts),
        discrepancies=items[:50],
    )


def order_id(
    supplier: str,
    carrier: str | None,
    count: int,
    start: datetime,
    end: datetime,
    reference: str | None,
) -> uuid.UUID:
    key = "|".join(
        [
            _norm(supplier),
            carrier or "",
            str(count),
            start.isoformat(),
            end.isoformat(),
            reference or "",
        ]
    )
    return uuid.uuid5(ORDER_NAMESPACE, key)


async def add_expected_delivery(
    ledger: Ledger,
    supplier: str,
    count: int,
    date: str,
    window_start: str,
    window_end: str,
    carrier: Carrier | None = None,
    reference: str | None = None,
) -> AddOutput:
    supplier = supplier.strip()
    if not supplier:
        raise BadArgument("supplier is required")
    if not 1 <= count <= 10_000:
        raise BadArgument("count must be between 1 and 10000")
    today = ledger.today()
    day = speech.parse_day(date, today)
    if day < today:
        raise BadArgument(f"{day} is in the past; expected deliveries are for today or later")
    start = datetime.combine(day, speech.parse_time(window_start), tzinfo=ledger.tz)
    end = datetime.combine(day, speech.parse_time(window_end), tzinfo=ledger.tz)
    if end <= start:
        raise BadArgument("window_end must be after window_start")
    reference = (reference or "").strip() or None
    carrier_value = carrier.value if carrier and carrier is not Carrier.UNKNOWN else None
    values = {
        "id": order_id(supplier, carrier_value, count, start, end, reference),
        "supplier": supplier,
        "carrier": carrier_value,
        "expected_count": count,
        "window_start": start,
        "window_end": end,
        "reference": reference,
    }
    try:
        res = await add_expected(
            ledger.engine,
            values,
            ledger.tz,
            ledger.clock(),
            ReconcileConfig.from_settings(ledger.settings),
        )
    except ExpectedConflict as e:  # uuid5 of the fields: only on a hash collision
        raise BadArgument(str(e)) from None

    async with ledger.engine.connect() as conn:
        (row,) = await queries.expected_with_status(conn, expected_deliveries.c.id == values["id"])
    item = _order_item(ledger, row)
    when = speech.day_phrase(day, today)
    via = f" by {carrier_value}" if carrier_value else ""
    ref = f", reference {reference}" if reference else ""
    head = "Added" if res.created else "Already on the list"
    summary = (
        f"{head}: {supplier}, {speech.boxes(count)}{via}, {when}, "
        f"{speech.window(start, end, ledger.tz)}{ref}."
    )
    if item.status != "pending":
        summary += " " + order_sentence(item, today)
    return AddOutput(
        summary=summary,
        cards=[_order_card(item, today)],
        created=res.created,
        order=item,
    )


async def get_supplier_scorecard(
    ledger: Ledger, supplier: str | None = None, days: int = 30
) -> ScorecardOutput:
    if not 1 <= days <= 365:
        raise BadArgument("days must be between 1 and 365")
    today = ledger.today()
    async with ledger.engine.connect() as conn:
        body = await suppliers.supplier_stats(
            conn, today, ledger.tz, days, ledger.include_simulated
        )
    rows = body["suppliers"]
    name = None
    if supplier:
        name = resolve_supplier(supplier, [r["supplier"] for r in rows])
        if name is None:
            raise BadArgument(
                f"no orders from a supplier called {supplier!r} in the last {days} days; "
                f"known suppliers: {', '.join(r['supplier'] for r in rows) or 'none'}"
            )
        rows = [r for r in rows if r["supplier"] == name]

    items = [
        SupplierItem(
            supplier=r["supplier"],
            orders_due=r["due"],
            delivered=r["delivered"],
            on_time=r["on_time"]["k"],
            on_time_pct=speech.pct(r["on_time"]["k"], r["on_time"]["n"]),
            late=r["late"],
            short=r["short_rate"]["k"],
            short_rate_pct=speech.pct(r["short_rate"]["k"], r["short_rate"]["n"]),
            boxes_short=r["boxes_short"],
            missed=r["missed"],
            problem_rate_pct=speech.pct(r["problem_rate"]["k"], r["problem_rate"]["n"]),
            worst=r["worst"],
            simulated_orders=r["simulated_orders"],
        )
        for r in rows
    ]
    shorted = [x for x in items if x.boxes_short]
    most_short = (
        max(shorted, key=lambda x: (x.boxes_short, x.short, x.supplier)).supplier
        if shorted
        else None
    )
    worst = body["worst"] if body["worst"] in {x.supplier for x in items} else None

    def line(x: SupplierItem) -> str:
        return (
            f"{x.supplier}: {x.on_time} of {x.orders_due} orders on time, {x.short} short "
            f"({speech.boxes(x.boxes_short)} in all), {x.missed} missed"
        )

    period = f"the last {days} days"
    if not items:
        summary = f"No orders were due in {period}."
    elif name:
        summary = f"Over {period}, {line(items[0])}."
    else:
        bits = []
        if most_short:
            m = next(x for x in items if x.supplier == most_short)
            bits.append(
                f"{most_short} shorts you the most: {m.short} short "
                f"{'delivery' if m.short == 1 else 'deliveries'}, {speech.boxes(m.boxes_short)} "
                f"missing in {period}."
            )
        if worst and worst != most_short:
            w = next(x for x in items if x.supplier == worst)
            bits.append(
                f"{worst} has the most problems overall: {w.problem_rate_pct:g}% of its orders "
                "were missing, short or late."
            )
        if not bits:
            bits.append(f"No supplier was short, late or missing in {period}.")
        summary = " ".join(bits)
    simulated = any(x.simulated_orders for x in items)
    ordered = sorted(items, key=lambda x: (-x.boxes_short, -(x.problem_rate_pct or 0), x.supplier))
    cards = [
        Card(
            title=x.supplier,
            simulated=x.simulated_orders > 0,
            facts=[
                Fact(label="On time", value=f"{x.on_time} of {x.orders_due}"),
                Fact(label="Boxes short", value=str(x.boxes_short)),
                Fact(label="Missed", value=str(x.missed)),
            ],
        )
        for x in ordered[:MAX_CARDS]
    ]
    return ScorecardOutput(
        summary=_with_note(summary, simulated),
        cards=cards,
        includes_simulated=simulated,
        from_date=date.fromisoformat(body["from"]),
        to_date=date.fromisoformat(body["to"]),
        days=days,
        worst=worst,
        most_boxes_short=most_short,
        suppliers=ordered,
    )
