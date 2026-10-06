from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest

from scout.errors import ConfigError
from scout.monitor.diff import Change, Delta, Fact, diff, facts_from
from scout.monitor.rules import cleared, parse_rule, triggers
from scout.monitor.watches import Watch, WatchBook
from scout.research.results import Confidence, Finding, Plan, RunResult, Source, Verdict
from scout.research.values import offer_findings
from scout.web.extract import Offer
from tests.helpers import NOW


@pytest.mark.parametrize(
    ("text", "kind", "fields"),
    [
        ("new", "new", {}),
        ("New findings", "new", {}),
        ("any change", "changed", {}),
        ("price below 1,800 usd", "below", {"amount": Decimal("1800"), "currency": "USD"}),
        ("under $1800", "below", {"amount": Decimal("1800"), "currency": "USD"}),
        ("< 99.5", "below", {"amount": Decimal("99.5"), "currency": None}),
        ("price above 2500 EUR", "above", {"amount": Decimal("2500"), "currency": "EUR"}),
        ("drop 5%", "drop", {"percent": Decimal("5")}),
        ("falls by 12.5 %", "drop", {"percent": Decimal("12.5")}),
        ("rise 10%", "rise", {"percent": Decimal("10")}),
        ('mentions "free-threaded"', "mentions", {"phrase": "free-threaded"}),
        ('Mentions "JIT"', "mentions", {"phrase": "JIT"}),  # shown as written, matched in any case
        ("back in stock", "in_stock", {}),
    ],
)
def test_rules_parse(text, kind, fields):
    rule = parse_rule(text)
    assert rule.kind == kind
    assert rule.text == text
    for name, value in fields.items():
        assert getattr(rule, name) == value


def test_unknown_rules_explain_the_grammar():
    with pytest.raises(ConfigError, match="price below 1800 USD"):
        parse_rule("tell me when it is cheap")


def fact(key="value|rtx 5090|price|shop.example", amount="1999", **changes):
    fields = {
        "key": key,
        "claim": f"RTX 5090 costs ${amount}",
        "quote": f"Now ${amount}.",
        "url": "https://shop.example/5090",
        "seen": NOW,
        "entity": "RTX 5090",
        "value": f"${amount}",
        "amount": Decimal(amount),
        "currency": "USD",
    }
    return Fact(**(fields | changes))


def fired(rule_text, deltas, *, baseline=False):
    return triggers([parse_rule(rule_text)], deltas, baseline=baseline)


def test_new_and_changed_are_quiet_on_the_baseline_run():
    new = Delta(Change.NEW, fact())
    assert fired("new", [new], baseline=True) == []
    (trigger,) = fired("new", [new])
    assert trigger.reason == "new: RTX 5090 costs $1999"


def test_changed_needs_a_material_move_or_a_disappearance():
    small = Delta(Change.CHANGED, fact(amount="1990"), fact())
    big = Delta(Change.CHANGED, fact(amount="1849"), fact())
    gone = Delta(Change.GONE, fact())
    assert fired("changed", [small]) == []
    assert "\N{RIGHTWARDS ARROW} $1849" in fired("changed", [big])[0].reason
    assert fired("changed", [gone])[0].reason.startswith("no longer on shop.example")


def test_price_limits_fire_even_on_the_first_run_but_not_for_rates_or_other_currencies():
    cheap = Delta(Change.NEW, fact(amount="1799"))
    assert fired("price below 1800 USD", [cheap], baseline=True)
    assert fired("price below 1700 USD", [cheap]) == []
    assert fired("price below 1800 EUR", [cheap]) == []
    rental = Delta(Change.NEW, fact(amount="0.53", unit="hour"))
    assert fired("price below 1800", [rental]) == []
    assert fired("price above 1000", [Delta(Change.SAME, fact(), fact())])


def test_conditions_hold_on_every_run_and_clear_when_they_stop():
    rule = parse_rule("below 1800")
    under = [
        Delta(Change.NEW, fact(amount="1799")),
        Delta(Change.SAME, fact(amount="1799"), fact(amount="1799")),
        Delta(Change.NOTICED, fact(amount="1750")),
    ]
    held = triggers([rule], under, baseline=False)
    assert len(held) == 3
    assert len({trigger.key for trigger in held}) == 1  # one fact: one key, whatever the price
    assert cleared([rule], under) == []

    over = Delta(Change.CHANGED, fact(amount="1899"), fact(amount="1799"))
    gone = Delta(Change.GONE, fact(amount="1799"))
    assert triggers([rule], [over, gone], baseline=False) == []
    assert cleared([rule], [over, gone]) == [held[0].key, held[0].key]
    assert cleared([parse_rule("new")], [over, gone]) == []  # events never hold


def test_drop_and_rise():
    drop = Delta(Change.CHANGED, fact(amount="1800"), fact(amount="2000"))
    assert fired("drop 5%", [drop])[0].reason.startswith("drop 10.0%")
    assert fired("drop 15%", [drop]) == []
    assert fired("rise 5%", [drop]) == []
    assert fired("rise 5%", [Delta(Change.CHANGED, fact(amount="2200"), fact(amount="2000"))])


def test_mentions_and_in_stock():
    note = fact(key="text|docs.example|abc", claim="3.14 adds free-threaded builds", amount="1")
    assert fired('mentions "FREE-THREADED"', [Delta(Change.NEW, note)], baseline=True)
    assert fired('mentions "jit"', [Delta(Change.NEW, note)]) == []
    assert fired('mentions "free-threaded"', [Delta(Change.NOTICED, note)]) == []

    offer = fact(structured=True, availability="InStock")
    sold_out = fact(structured=True, availability="OutOfStock")
    # Back in stock at the same price: the value did not change, the availability did.
    assert fired("in stock", [Delta(Change.SAME, offer, sold_out)])
    assert fired("in stock", [Delta(Change.SAME, sold_out, offer)]) == []
    assert fired("in stock", [Delta(Change.NEW, fact(availability="InStock"))]) == []  # text


SHOP = "https://shop.example/5090"
NEWS = "https://news.example/gpus"


def run(findings, *, sources=None, started=NOW):
    return RunResult(
        goal="rtx 5090 price",
        started_at=started,
        finished_at=started,
        model="m",
        plan=Plan(queries=("q",), kind="price", recency=None, planner="model"),
        sources=tuple(
            sources
            or [
                Source(
                    1, SHOP, "Shop", "shop.example", "ok", "q", text="Now $1,999. Limited stock."
                ),
                Source(
                    2,
                    NEWS,
                    "News",
                    "news.example",
                    "ok",
                    "q",
                    text="Prices are falling fast this week.",
                ),
            ]
        ),
        answer="",
        findings=tuple(findings),
        confidence=Confidence("medium", ""),
    )


def price_finding(value="$1,999", amount="1999", **changes):
    fields = {
        "claim": f"The 5090 costs {value}",
        "quote": f"Now {value}.",
        "source": 1,
        "verdict": Verdict.VERIFIED,
        "entity": "RTX 5090",
        "attribute": "price",
        "value": value,
        "amount": Decimal(amount),
        "currency": "USD",
    }
    return Finding(**(fields | changes))


def statement(claim="Prices are falling", quote="Prices are falling fast this week.", source=2):
    return Finding(claim=claim, quote=quote, source=source, verdict=Verdict.VERIFIED)


def test_facts_come_from_trusted_findings_only():
    unverified = price_finding(verdict=Verdict.UNVERIFIED)
    facts = facts_from(run([price_finding(), statement(), unverified]))
    assert [f.key.split("|")[0] for f in facts] == ["value", "text"]
    assert facts[0].key == "value|rtx 5090|price|shop.example"


def test_first_run_is_all_new_then_same():
    first = diff([], run([price_finding(), statement()]))
    assert [d.change for d in first] == [Change.NEW, Change.NEW]
    remembered = [d.fact for d in first]
    later = NOW + timedelta(hours=6)
    again = diff(remembered, run([price_finding(), statement()], started=later))
    assert [d.change for d in again] == [Change.SAME, Change.SAME]
    assert all(d.fact.seen == NOW for d in again)  # first-seen time is kept


def test_a_new_price_is_a_change_not_a_new_fact():
    remembered = [d.fact for d in diff([], run([price_finding()]))]
    shop = Source(1, SHOP, "Shop", "shop.example", "ok", "q", text="Now $1,849. Limited stock.")
    (delta,) = diff(remembered, run([price_finding("$1,849", "1849")], sources=[shop]))
    assert delta.change is Change.CHANGED
    assert delta.previous.amount == Decimal("1999")
    assert round(delta.percent, 1) == Decimal("-7.5")


def test_a_change_of_currency_has_no_percent():
    remembered = [d.fact for d in diff([], run([price_finding()]))]
    shop = Source(
        1, SHOP, "Shop", "shop.example", "ok", "q", text="Now N{EURO SIGN}1,849. Limited stock."
    )
    euros = price_finding("N{EURO SIGN}1,849", "1849", currency="EUR")
    (delta,) = diff(remembered, run([euros], sources=[shop]))
    assert delta.change is Change.CHANGED
    assert delta.percent is None


def test_reworded_statements_match_the_remembered_fact():
    remembered = [d.fact for d in diff([], run([statement()]))]
    reworded = statement(
        claim="GPU prices are falling fast", quote="Prices are falling fast this week"
    )
    (delta,) = diff(remembered, run([reworded]))
    assert delta.change is Change.SAME
    assert delta.fact.key == remembered[0].key


def test_gone_only_when_the_page_was_read_and_the_quote_left_it():
    remembered = [d.fact for d in diff([], run([price_finding(), statement()]))]
    news_rewritten = Source(
        2, NEWS, "News", "news.example", "ok", "q", text="Prices are steady now."
    )
    shop = Source(1, SHOP, "Shop", "shop.example", "ok", "q", text="Now $1,999. Limited stock.")
    deltas = diff(remembered, run([price_finding()], sources=[shop, news_rewritten]))
    assert [(d.change, d.fact.url) for d in deltas] == [(Change.SAME, SHOP), (Change.GONE, NEWS)]

    news_unreadable = replace(news_rewritten, snippet_only=True, text="")
    quiet = diff(remembered, run([price_finding()], sources=[shop, news_unreadable]))
    assert [d.change for d in quiet] == [Change.SAME]  # an unread page proves nothing


def test_structured_offers_are_gone_when_the_page_stops_publishing_them():
    offer = Finding(
        claim="RTX 5090 FE: 1899 USD",
        quote="schema.org Offer: price 1899 USD",
        source=1,
        verdict=Verdict.VERIFIED,
        entity="RTX 5090 FE",
        attribute="price",
        value="1899 USD",
        amount=Decimal("1899"),
        currency="USD",
        origin="structured-data",
    )
    with_offer = Source(
        1,
        SHOP,
        "Shop",
        "shop.example",
        "ok",
        "q",
        text="Deals.",
        offers=(Offer(product="RTX 5090 FE", price=Decimal("1899"), currency="USD"),),
    )
    remembered = [d.fact for d in diff([], run([offer], sources=[with_offer]))]
    still = diff(remembered, run([], sources=[with_offer]))
    assert [d.change for d in still] == [Change.SAME]  # not reported, but still published
    repriced = replace(with_offer, offers=(replace(with_offer.offers[0], price=Decimal("1849")),))
    assert diff(remembered, run([], sources=[repriced])) == []  # on sale, price unknown to us
    without = replace(with_offer, offers=())
    assert [d.change for d in diff(remembered, run([], sources=[without]))] == [Change.GONE]


def test_an_offer_without_a_product_name_is_followed_by_its_page_title():
    page = Source(1, SHOP, "RTX 5090 FE", "shop.example", "ok", "q", text="Deals.")
    published = replace(page, offers=(Offer(product=None, price=Decimal("1899"), currency="USD"),))
    remembered = [d.fact for d in diff([], run(offer_findings([published]), sources=[published]))]
    repriced = replace(published, offers=(replace(published.offers[0], price=Decimal("1849")),))
    (delta,) = diff(remembered, run(offer_findings([repriced]), sources=[repriced]))
    assert (delta.change, delta.previous.amount, delta.fact.amount) == (
        Change.CHANGED,
        Decimal("1899"),
        Decimal("1849"),
    )
    assert [d.change for d in diff(remembered, run([], sources=[published]))] == [Change.SAME]


def test_a_fact_already_on_the_page_last_time_is_noticed_not_new():
    remembered = [d.fact for d in diff([], run([price_finding()]))]
    pages = {source.url: source for source in run([]).sources}
    # The model reports the news statement only now, but the page said it last time too.
    again = run([price_finding(), statement()])
    assert [d.change for d in diff(remembered, again, earlier=pages)] == [
        Change.SAME,
        Change.NOTICED,
    ]
    # Without the earlier pages nothing shows it is old news.
    assert diff(remembered, again)[1].change is Change.NEW


def test_a_value_changes_only_when_its_old_evidence_left_the_page():
    both = Source(
        1, SHOP, "Shop", "shop.example", "ok", "q", text="Now $1,999. Partner: Now $1,849."
    )
    remembered = [d.fact for d in diff([], run([price_finding()], sources=[both]))]
    other_pick = run([price_finding("$1,849", "1849")], sources=[both])

    # The same page, and the model picked the other price: both were there before. Quiet.
    quiet = diff(remembered, other_pick, earlier={SHOP: both})
    assert [(d.change, d.fact.amount) for d in quiet] == [
        (Change.NOTICED, Decimal("1849")),
        (Change.SAME, Decimal("1999")),
    ]
    # A second price that was not there before is news, and the first one still stands.
    only_old = replace(both, text="Now $1,999.")
    added = diff(remembered, other_pick, earlier={SHOP: only_old})
    assert [d.change for d in added] == [Change.NEW, Change.SAME]
    assert added[0].fact.key == "value|rtx 5090|price|shop.example|$1,849"


def test_the_model_forgetting_a_fact_does_not_make_it_gone():
    remembered = [d.fact for d in diff([], run([price_finding(), statement()]))]
    (delta,) = diff(remembered, run([price_finding()]))[1:]
    assert (delta.change, delta.fact.url) == (Change.SAME, NEWS)  # its quote is still there


def test_statements_with_different_numbers_are_different_facts():
    page = Source(2, NEWS, "News", "news.example", "ok", "q", text="Python 3.13 adds a JIT.")
    first = statement("Python 3.13 adds a JIT", "Python 3.13 adds a JIT.")
    remembered = [d.fact for d in diff([], run([first], sources=[page]))]
    newer = replace(page, text="Python 3.14 adds a JIT.")
    second = statement("Python 3.14 adds a JIT", "Python 3.14 adds a JIT.")
    deltas = diff(remembered, run([second], sources=[newer]))
    assert [d.change for d in deltas] == [Change.NEW, Change.GONE]


def watch(**changes):
    fields = {"name": "gpu-watch", "goal": "cheapest RTX 5090", "every": "6h"}
    return Watch(**(fields | changes))


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"name": "GPU Watch"}, "lowercase"),
        ({"name": "gpu\n"}, "lowercase"),
        ({"goal": " "}, "no goal"),
        ({"cron": "0 9 * * *"}, "exactly one"),
        ({"every": None}, "exactly one"),
        ({"every": "soon"}, "30m, 6h"),
        ({"every": "5m"}, "at most every 15 minutes"),
        ({"every": None, "cron": "61 * * * *"}, "bad cron"),
        ({"alerts": ("when cheap",)}, "cannot understand"),
    ],
)
def test_invalid_watches_are_rejected(changes, message):
    with pytest.raises(ConfigError, match=message):
        watch(**changes)


def test_watch_book_round_trip_and_editing(tmp_path):
    book = WatchBook(tmp_path / "watches.yaml")
    assert book.load() == []
    first = watch(alerts=("price below 1800 USD", "new"), notify=("ntfy://gpu",))
    book.add(first)
    book.add(watch(name="py", goal="Python 3.14 news", every=None, cron="0 9 * * *"))
    assert book.load()[0] == first
    assert "stop_when_alerted" not in (tmp_path / "watches.yaml").read_text(encoding="utf-8")

    with pytest.raises(ConfigError, match="already exists"):
        book.add(first)
    assert book.update("gpu-watch", paused=True).paused
    book.remove("py")
    assert [w.name for w in book.load()] == ["gpu-watch"]
    with pytest.raises(ConfigError, match="no watch named"):
        book.remove("py")


def test_watch_book_rejects_bad_files(tmp_path):
    path = tmp_path / "watches.yaml"
    path.write_text("watches:\n  - {name: a, goal: g, every: 6h, colour: red}\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="unknown setting"):
        WatchBook(path).load()
    path.write_text(
        "watches:\n  - {name: a, goal: g, every: 6h}\n  - {name: a, goal: h, every: 1d}\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="duplicate"):
        WatchBook(path).load()
    path.write_text("watches: [", encoding="utf-8")
    with pytest.raises(ConfigError, match="not valid YAML"):
        WatchBook(path).load()
