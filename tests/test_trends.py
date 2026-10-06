import csv
import io
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

from click.testing import CliRunner

from scout.cli import main
from scout.monitor.diff import Change, Delta, Fact
from scout.monitor.trends import series, sparkline, write_csv
from scout.settings import load_settings
from scout.store import Observation, Store
from tests.helpers import NOW, SAMPLE_RESULT


def observed(key: str, amount: str, hours: int, unit: str | None = None) -> Observation:
    return Observation(
        key=key,
        run_id=hours,
        observed_at=NOW + timedelta(hours=hours),
        label=f"RTX 5090 ({key})",
        amount=Decimal(amount),
        currency="USD",
        unit=unit,
    )


HISTORY = [
    observed("shop", "1999", 0),
    observed("rent", "0.53", 0, unit="hour"),
    observed("shop", "1849", 6),
    observed("shop", "1899", 12),
]


def test_series_follow_each_fact_in_order():
    shop, rent = series(HISTORY)
    assert [amount for _, amount in shop.points] == [Decimal(a) for a in ("1999", "1849", "1899")]
    assert (shop.first, shop.last, shop.low, shop.high) == (1999, 1899, 1849, 1999)
    assert round(shop.change, 1) == Decimal("-5.0")
    assert (shop.measure, rent.measure) == ("USD", "USD per hour")


def test_a_value_that_changes_currency_starts_a_new_series():
    euros = replace(observed("shop", "1749", 18), currency="EUR")
    *_, in_euros = series([*HISTORY, euros])
    assert (in_euros.key, in_euros.measure, in_euros.change) == ("shop", "EUR", 0)
    assert len(series([*HISTORY, euros])) == 3


def test_sparkline_scales_between_low_and_high():
    line = sparkline([Decimal(v) for v in (1, 4, 8)])
    assert line == "\N{LOWER ONE EIGHTH BLOCK}\N{LOWER HALF BLOCK}\N{FULL BLOCK}"
    assert sparkline([Decimal(3)] * 3) == "\N{LOWER HALF BLOCK}" * 3
    assert len(sparkline([Decimal(v) for v in range(100)], width=10)) == 10
    assert sparkline([]) == ""


def test_csv_has_one_row_per_observation():
    out = io.StringIO()
    write_csv(HISTORY, out)
    rows = list(csv.DictReader(io.StringIO(out.getvalue())))
    assert [row["amount"] for row in rows] == ["1999", "0.53", "1849", "1899"]
    assert rows[1]["unit"] == "hour"


def test_csv_cells_never_run_as_spreadsheet_formulas():
    out = io.StringIO()
    write_csv([replace(HISTORY[0], label='=HYPERLINK("http://x.example")')], out)
    (row,) = csv.DictReader(io.StringIO(out.getvalue()))
    assert row["label"] == """'=HYPERLINK("http://x.example")"""


def test_scout_watch_trend(workspace):
    runner = CliRunner()
    runner.invoke(main, ["watch", "add", "gpu", "cheapest RTX 5090"])
    assert "No values yet" in runner.invoke(main, ["watch", "trend", "gpu"]).output

    with Store(load_settings().db_path) as store:
        for o in HISTORY:  # as runs record what they saw
            fact = Fact(
                key=o.key,
                claim=o.label,
                quote=f"Now ${o.amount}.",
                url="https://shop.example/5090",
                seen=o.observed_at,
                entity=o.label,
                value=f"${o.amount}",
                amount=o.amount,
                currency=o.currency,
                unit=o.unit,
            )
            run = replace(SAMPLE_RESULT, started_at=o.observed_at)
            store.record("gpu", run, [Delta(Change.SAME, fact, fact)])
    target = workspace / "gpu.csv"
    shown = runner.invoke(main, ["watch", "trend", "gpu", "--csv", str(target)])
    assert "(shop)" in shown.output
    assert "-5.0%" in shown.output
    assert "1,999" in shown.output
    assert len(target.read_text(encoding="utf-8").splitlines()) == 5
