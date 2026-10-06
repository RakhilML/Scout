"""Value history: how each price or number a watch follows moved over its runs."""

from __future__ import annotations

import csv
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import TextIO

from scout.store import Observation

_BARS = (
    "\N{LOWER ONE EIGHTH BLOCK}\N{LOWER ONE QUARTER BLOCK}\N{LOWER THREE EIGHTHS BLOCK}"
    "\N{LOWER HALF BLOCK}\N{LOWER FIVE EIGHTHS BLOCK}\N{LOWER THREE QUARTERS BLOCK}"
    "\N{LOWER SEVEN EIGHTHS BLOCK}\N{FULL BLOCK}"
)


@dataclass(frozen=True, slots=True)
class Series:
    key: str
    label: str
    currency: str | None
    unit: str | None
    points: tuple[tuple[datetime, Decimal], ...]  # oldest first

    @property
    def first(self) -> Decimal:
        return self.points[0][1]

    @property
    def last(self) -> Decimal:
        return self.points[-1][1]

    @property
    def low(self) -> Decimal:
        return min(amount for _, amount in self.points)

    @property
    def high(self) -> Decimal:
        return max(amount for _, amount in self.points)

    @property
    def change(self) -> Decimal | None:
        """Percent change from the first observation to the last."""
        return None if self.first == 0 else (self.last - self.first) / self.first * 100

    @property
    def measure(self) -> str:
        """What the numbers are in: "USD", "USD per hour"."""
        per = f"per {self.unit}" if self.unit else None
        return " ".join(part for part in (self.currency, per) if part)


def series(observations: Sequence[Observation]) -> list[Series]:
    """One series per fact and measure (a price that moved to another currency starts a new
    one), in the order they were first observed."""
    grouped: dict[tuple[str, str | None, str | None], list[Observation]] = {}
    for o in observations:
        grouped.setdefault((o.key, o.currency, o.unit), []).append(o)
    return [
        Series(
            key=key,
            label=group[-1].label,
            currency=currency,
            unit=unit,
            points=tuple((o.observed_at, o.amount) for o in group),
        )
        for (key, currency, unit), group in grouped.items()
    ]


def sparkline(values: Sequence[Decimal], *, width: int = 24) -> str:
    """The last *width* values as bars from low to high."""
    shown = list(values)[-width:]
    if not shown:
        return ""
    low, high = min(shown), max(shown)
    if low == high:
        return _BARS[3] * len(shown)
    top = len(_BARS) - 1
    return "".join(_BARS[round((value - low) / (high - low) * top)] for value in shown)


def write_csv(observations: Sequence[Observation], out: TextIO) -> None:
    writer = csv.writer(out)
    writer.writerow(["observed_at", "label", "amount", "currency", "unit", "run_id", "key"])
    for o in observations:
        writer.writerow(
            [
                o.observed_at.isoformat(),
                _cell(o.label),
                o.amount,
                _cell(o.currency),
                _cell(o.unit),
                o.run_id,
                _cell(o.key),
            ]
        )


def _cell(text: str | None) -> str | None:
    """Web text a spreadsheet would run as a formula is kept as text."""
    if text and text[0] in "=+-@\t\r":
        return f"'{text}"
    return text
