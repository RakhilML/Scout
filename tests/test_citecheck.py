"""Cite-checks: does each page a text cites say what the text says it does?"""

import codecs
import json
import re
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
import responses
from click.testing import CliRunner

from scout.app import App
from scout.cli import EXIT_ANSWER_PENDING, main
from scout.errors import AnswerPending, ScoutError
from scout.report import render_html, render_json, render_markdown
from scout.research import factcheck
from scout.research.citations import NONE_LINKED, Cited, cited
from scout.research.deadlinks import dead_links, replacements
from scout.research.factcheck import (
    NOTHING_CITED,
    cited_by,
    cited_label,
    citing,
    sentences,
    summarize,
)
from scout.research.pipeline import Researcher, ResearchOptions
from scout.research.prompts import claims_messages
from scout.research.results import ClaimCheck, Confidence, Finding, RunResult, Source, Verdict
from scout.research.schema import ClaimToCheck
from scout.settings import load_settings
from scout.store import Store
from scout.web import soft404
from scout.web.archive import Snapshot, make_archive
from scout.web.fetch import Document, FetchConfig, Fetcher, FetchStatus
from tests.helpers import (
    CITE_RESULT,
    CITED_TEXT,
    GONE,
    NOW,
    RELEASED_2023,
    UNREAD,
    Clock,
    FakeArchive,
    FakeFetcher,
    FakeResearcher,
    FakeSearch,
    ScriptedBackend,
)

RELEASE = "https://www.python.org/downloads/release/python-3130/"
WHATSNEW = "https://docs.python.org/3/whatsnew/3.13.html"
REALPY = "https://realpython.com/python313-new-features/"
PEP_703 = "https://peps.python.org/pep-0703/"
ANSWER = (
    "Python 3.13 was released on October 7, 2023.[1] It removed the global interpreter lock by "
    "default.[2] Its JIT makes it 40% faster than 3.12 "
    f"([realpython.com]({REALPY})). See [the docs]({WHATSNEW}) for more. "
    "It runs on iOS as a tier 3 platform.[4]\n\n"
    f"Sources\n[1] {RELEASE}\n[2] {WHATSNEW}\n[4] {GONE}\n"
)
PAGES = {
    RELEASE: "Python 3.13.0 is the newest major release of the Python programming language. "
    "Python 3.13.0 was released on October 7, 2024.",
    WHATSNEW: "Python 3.13 was released on October 7, 2024. CPython can run in a free-threaded "
    "mode. The free-threaded mode is experimental and the GIL remains enabled by default.",
    REALPY: "Python 3.13 ships an experimental JIT compiler that you can enable when you build it.",
    GONE: Document(url=GONE, status=FetchStatus.NOT_FOUND, fetched_at=NOW, error="HTTP 404"),
}


def claim(text: str, excerpt: str) -> dict:
    return {"claim": text, "excerpt": excerpt, "query": "q"}


def judged(*evidence: tuple[str, str, int], note: str = "") -> dict:
    return {
        "evidence": [
            {"stance": stance, "quote": quote, "source": source, "says": quote}
            for stance, quote, source in evidence
        ],
        "note": note,
    }


LISTED = {
    "claims": [
        claim(RELEASED_2023, "Python 3.13 was released on October 7, 2023 [1]."),
        claim(
            "Python 3.13 removed the global interpreter lock by default.",
            "It removed the global interpreter lock by default [2].",
        ),
        claim(
            "Python 3.13's JIT makes it 40% faster than Python 3.12.",
            "Its JIT makes it 40% faster than 3.12 [3].",
        ),
        claim(
            "Python 3.13 runs on iOS as a tier 3 platform.",
            "It runs on iOS as a tier 3 platform [4].",
        ),
    ]
}
JUDGE_RELEASE = judged(
    ("refutes", "Python 3.13.0 was released on October 7, 2024.", 1),
    note="The page gives October 7, 2024.",
)
JUDGE_GIL = judged(
    ("refutes", "The free-threaded mode is experimental and the GIL remains enabled by default.", 2)
)
JUDGE_NOTHING = judged(note="The page does not say how fast it is.")


def citer(
    backend, *, fetcher=None, search=None, clock=None, pins=None, archive=None, **options
) -> Researcher:
    return Researcher(
        search=search or FakeSearch({}),
        fetcher=fetcher or FakeFetcher(PAGES),
        backend=backend,
        options=ResearchOptions(**options),
        clock=clock or Clock(),
        pins=pins,
        archive=archive,
    )


def test_links_become_markers_and_a_link_naming_its_site_is_no_words_of_the_text():
    text = (
        f"Python 3.13 has a JIT ([realpython.com]({REALPY})). Read "
        f'[the release notes]({WHATSNEW} "What\'s new") or [{RELEASE}]({RELEASE}). '
        "[Node.js](https://nodejs.org) is not Python."
    )
    assert cited(text) == Cited(
        "Python 3.13 has a JIT [1]. Read the release notes [2] or [3]. Node.js [4] is not Python.",
        {1: REALPY, 2: WHATSNEW, 3: RELEASE, 4: "https://nodejs.org"},
    )


def test_a_perplexity_answer_keeps_its_numbers_and_loses_its_list_of_citations():
    text = (
        "Python 3.13 came out in 2024.[1][2] It has a JIT. [2]\n\n"
        f"**Citations:**\n[1] {RELEASE}\n[2] {WHATSNEW}"
    )
    assert cited(text) == Cited(
        "Python 3.13 came out in 2024 [1][2]. It has a JIT [2].", {1: RELEASE, 2: WHATSNEW}
    )


def test_a_numbered_list_lists_sources_only_when_the_text_cites_its_numbers():
    gemini = (
        "Python 3.13 came out in 2024 [1].\n\n## Sources\n\n"
        f"1. Python Release Python 3.13.0 - python.org, {RELEASE}"
    )
    assert cited(gemini) == Cited("Python 3.13 came out in 2024 [1].", {1: RELEASE})

    steps = "To try it:\n1. Install from https://python.org.\n2. Run python3.13 -X jit."
    assert cited(steps) == Cited(
        "To try it:\n1. Install from [1].\n2. Run python3.13 -X jit.", {1: "https://python.org"}
    )


def test_footnotes_references_and_bare_addresses_are_citations_other_links_are_words():
    wiki = "https://en.wikipedia.org/wiki/Python_(programming_language)"
    text = (
        "Python 3.13 added a JIT[^1] and free threading[^ft]. See the [guide][g], [docs][] and "
        f"<https://peps.python.org/pep-0744/>, or ({wiki}). Write to [us](mailto:a@b.example), "
        "read [the FAQ](/faq) or [below](#notes). ![A chart](https://img.example/c.png)\n\n"
        f"[^1]: The release notes, {WHATSNEW}.\n"
        "[^ft]: PEP 703 <https://peps.python.org/pep-0703/>\n"
        '[g]: https://docs.python.org/3/howto/ "HOWTOs"\n'
        "[docs]: <https://docs.python.org/3/>\n"
        "[faq]: /faq.html"
    )
    assert cited(text) == Cited(
        "Python 3.13 added a JIT[1] and free threading[2]. See the guide [3], docs [4] and [5], "
        "or [6]. Write to us, read the FAQ or below. A chart",
        {
            1: WHATSNEW,
            2: "https://peps.python.org/pep-0703/",
            3: "https://docs.python.org/3/howto/",
            4: "https://docs.python.org/3/",
            5: "https://peps.python.org/pep-0744/",
            6: wiki,
        },
    )
    aside = "Python 3.13 is fast[^n].\n\n[^n]: Not that we measured it."
    assert cited(aside) == Cited(aside, {})  # a footnote without an address is a note


def test_a_given_number_is_kept_and_the_others_take_the_numbers_still_free():
    text = (
        "One [2]. Two [link](https://a.example/x). Three [7]. Four <https://b.example/>. "
        "Again [again](https://a.example/x?utm_source=chatgpt.com). Five [c](https://c.example).\n\n"
        "[2]: https://c.example/"
    )
    assert cited(text) == Cited(
        "One [2]. Two link [1]. Three [7]. Four [3]. Again again [1]. Five c [2].",
        {1: "https://a.example/x", 2: "https://c.example/", 3: "https://b.example/"},
    )


def test_markers_after_a_sentences_end_move_into_it():
    text = (
        'It came out in 2024.[1] It is fast. [2] He said "it is free."[3] Is it?[4] '
        "The U.S.[5] team agrees!"
    )
    moved = cited(text).text
    assert moved == (
        'It came out in 2024 [1]. It is fast [2]. He said "it is free [3]." Is it [4]? '
        "The U.S.[5] team agrees!"
    )
    pieces = ("It came out in 2024", "It is fast", "Is it", "The U.S.")
    assert [cited_by(moved, piece) for piece in pieces] == [(1,), (2,), (4,), (5,)]


@pytest.mark.parametrize(
    ("first", "piece"),
    [
        ('The CEO said prices "will not rise."[1]', "The CEO said prices"),
        (
            "The CEO said \N{LEFT DOUBLE QUOTATION MARK}prices will not rise."
            "\N{RIGHT DOUBLE QUOTATION MARK}[1]",
            "The CEO said",
        ),
        ("Prices held (as the CEO promised in 2024.)[1]", "Prices held"),
        ("The bond is rated A.[1]", "The bond is rated"),
        ("Prices rose in the U.S.[1]", "Prices rose in the"),
        ("Did prices rise?[1]", "Did prices rise"),
    ],
)
def test_the_sentence_after_a_cited_one_cites_only_its_own_pages(first, piece):
    text = f"{first} Sales rose 5% in 2024.[2]\n\n[1] https://a.example/1\n[2] https://a.example/2"
    moved = cited(text).text
    assert (cited_by(moved, piece), cited_by(moved, "Sales rose 5% in 2024")) == ((1,), (2,))


def test_markers_given_as_a_list_or_a_range_are_each_a_marker():
    text = (
        "Fact one is here.[1, 2] Fact two is there.[3] Fact three.[1,3] Fact four.[2-3] "
        "Fact five.[1\N{EN DASH}2] Not markers: [0-9] or [2020-2024].\n\n"
        "[1] https://a.example/1\n[2] https://a.example/2\n[3] https://a.example/3"
    )
    moved = cited(text).text
    assert moved == (
        "Fact one is here [1][2]. Fact two is there [3]. Fact three [1][3]. Fact four [2][3]. "
        "Fact five [1][2]. Not markers: [0-9] or [2020-2024]."
    )
    assert [cited_by(moved, f"Fact {n}") for n in ("one", "two", "four")] == [
        (1, 2),
        (3,),
        (2, 3),
    ]


def test_a_linked_number_is_words_of_the_text_unless_it_stands_for_a_marker():
    moons = "https://science.nasa.gov/jupiter/moons/"
    text = (
        f"Python 3.13 came out in [2024]({RELEASE}). Jupiter has [95]({moons}) known moons. "
        f"Copilot cites like this.[1]({WHATSNEW}) Or like this[2]({REALPY})[3]({PEP_703}). "
        "See [2024][r].\n\n[r]: https://r.example/"
    )
    assert cited(text) == Cited(
        "Python 3.13 came out in 2024 [4]. Jupiter has 95 [5] known moons. "
        "Copilot cites like this [1]. Or like this[2][3]. See 2024 [6].",
        {1: WHATSNEW, 2: REALPY, 3: PEP_703, 4: RELEASE, 5: moons, 6: "https://r.example/"},
    )


def test_a_numbered_list_of_the_texts_own_never_takes_over_its_citations():
    howto = (
        f"To upgrade:\n1. Read [the release notes]({WHATSNEW}) before you start.\n"
        "2. Install it with [uv](https://docs.astral.sh/uv/).\n\n"
        "Python 3.13 was released on October 7, 2024.[1] Free threading is experimental.[2]\n\n"
        f"Sources\n[1] {RELEASE}\n[2] {PEP_703}"
    )
    assert cited(howto) == Cited(
        "To upgrade:\n1. Read the release notes [3] before you start.\n"
        "2. Install it with uv [4].\n\n"
        "Python 3.13 was released on October 7, 2024 [1]. Free threading is experimental [2].",
        {1: RELEASE, 2: PEP_703, 3: WHATSNEW, 4: "https://docs.astral.sh/uv/"},
    )
    listed = "Python 3.13 is out [1].\n\n1. https://a.example/1\n2. https://a.example/2"
    assert cited(listed) == Cited(
        "Python 3.13 is out [1].", {1: "https://a.example/1", 2: "https://a.example/2"}
    )
    twice = f"Python 3.13 is out [1].\n\n[1]: {RELEASE}\n\n## Sources\n1. {WHATSNEW}"
    assert cited(twice) == Cited(
        "Python 3.13 is out [1].",
        {1: RELEASE},
        (f"the text gives [1] two addresses: {RELEASE} is read, not {WHATSNEW}",),
    )


def test_images_code_and_notes_are_words_of_the_text():
    text = (
        "Build passes [![badge](https://c.example/r.svg)](https://a.example/p).\n"
        "Call `https://api.example/v1` from code.\n[Note]: deprecated\n\n"
        "```\ncurl https://api.example/v2\n[1] https://api.example/v3\n```\n"
        "Python 3.13 added free threading[^1]. Read [https://docs.python.org/3/].\n\n"
        f"[^1]: PEP 703,\n    {PEP_703}\n\n## Works cited\n\n[2] https://b.example/2"
    )
    assert cited(text) == Cited(
        "Build passes badge [3].\nCall `https://api.example/v1` from code.\n"
        "[Note]: deprecated\n\n```\ncurl https://api.example/v2\n[1] https://api.example/v3\n"
        "```\nPython 3.13 added free threading[1]. Read [4].",
        {
            1: PEP_703,
            2: "https://b.example/2",
            3: "https://a.example/p",
            4: "https://docs.python.org/3/",
        },
    )


JIT = "It ships an experimental JIT compiler, off by default."
COPIED = {
    "perplexity": (
        f"Python 3.13 was released on October 7, 2024.[1][2] {JIT}[3]\n\n"
        f"Citations:\n[1] {RELEASE}\n[2] {WHATSNEW}\n[3] {REALPY}",
        "Python 3.13 was released on October 7, 2024 [1][2]. {jit} [3].",
        {1: RELEASE, 2: WHATSNEW, 3: REALPY},
    ),
    "chatgpt search": (
        f"Python 3.13 was released on October 7, 2024. ([python.org]({RELEASE}?utm_source="
        f"chatgpt.com)) {JIT} ([realpython.com]({REALPY}?utm_source=chatgpt.com))",
        "Python 3.13 was released on October 7, 2024 [1]. {jit} [2].",
        {1: f"{RELEASE}?utm_source=chatgpt.com", 2: f"{REALPY}?utm_source=chatgpt.com"},
    ),
    "copilot": (
        f"Python 3.13 was released on October 7, 2024[1]({RELEASE}). {JIT}[2]({REALPY})",
        "Python 3.13 was released on October 7, 2024[1]. {jit} [2].",
        {1: RELEASE, 2: REALPY},
    ),
    "gemini": (
        f"Python 3.13 was released on October 7, 2024 [1]. {JIT[:-1]} [2].\n\n**Sources**\n\n"
        f"1. Python Release Python 3.13.0 | Python.org - {RELEASE}\n"
        f"2. What's New in Python 3.13 - Real Python - {REALPY}",
        "Python 3.13 was released on October 7, 2024 [1]. {jit} [2].",
        {1: RELEASE, 2: REALPY},
    ),
    "claude": (
        f"Python 3.13 was released on October 7, 2024, per the [release page]({RELEASE}). "
        f"{JIT[:-1]} ([Real Python]({REALPY})).",
        "Python 3.13 was released on October 7, 2024, per the release page [1]. "
        "{jit} (Real Python [2]).",
        {1: RELEASE, 2: REALPY},
    ),
    "footnotes": (
        f"Python 3.13 was released on October 7, 2024.[^release] {JIT}[^jit]\n\n"
        f'[^release]: Python Software Foundation, "Python 3.13.0", {RELEASE}\n'
        f"[^jit]: Real Python, <{REALPY}>",
        "Python 3.13 was released on October 7, 2024 [1]. {jit} [2].",
        {1: RELEASE, 2: REALPY},
    ),
}


@pytest.mark.parametrize("copied", COPIED.values(), ids=COPIED.keys())
def test_answers_as_people_copy_them(copied):
    text, expected, pages = copied
    found = cited(text)
    assert found == Cited(expected.format(jit=JIT[:-1]), pages)
    assert cited_by(found.text, "It ships an experimental JIT compiler") == (len(pages),)


def test_a_claim_cites_what_its_sentences_cite_and_nothing_after_them():
    text = "Python 3.13 [1] came out in 2024 [2][3]. It has a JIT [4]. It is fast."
    assert cited_by(text, "Python 3.13 [1] came out in 2024 [2][3].") == (1, 2, 3)
    assert cited_by(text, "came out in 2024") == (1, 2, 3)
    assert cited_by(text, "came out in 2024 [2][3]. It has a JIT") == (1, 2, 3, 4)
    assert cited_by(text, "It is fast.") == ()
    assert cited_by(text, "It was never said.") == ()


def test_only_claims_in_sentences_citing_a_web_page_are_cite_checked():
    excerpts = (
        "Python 3.13 is out [1].",
        "It has a JIT.",
        "It is free software [7].",
        "It is very fast [1][7].",
    )
    text = " ".join(excerpts)
    claims = [ClaimToCheck(claim=c, excerpt=c, query="q") for c in excerpts]
    kept, warnings = citing(claims, text, {1: RELEASE})
    assert [c.excerpt for c in kept] == [excerpts[0], excerpts[3]]
    assert warnings == [
        "the text cites [7] but gives no web address for it",
        "set aside 2 claims in sentences that cite no web page: "
        "scout factcheck checks them against independent pages",
    ]
    # When the model's list is unusable, each sentence is a claim: never with its markers.
    assert [(c.claim, c.excerpt) for c in sentences(text)] == [
        ("Python 3.13 is out.", excerpts[0]),
        ("It has a JIT.", excerpts[1]),
        ("It is free software.", excerpts[2]),
        ("It is very fast.", excerpts[3]),
    ]


def test_a_cite_checks_summary_counts_what_the_cited_pages_said():
    sources = [
        Source(1, RELEASE, "A", "python.org", "ok", "cited as [1]"),
        Source(2, GONE, GONE, "example.org", "not_found", "cited as [2]", snippet_only=True),
        Source(3, REALPY, "C", "realpython.com", "ok", "cited as [3]"),
    ]
    found = [Finding("a", "a", 1, Verdict.VERIFIED), Finding("b", "b", 3, Verdict.VERIFIED)]
    backed = ClaimCheck("a", "a", "q", supports=(1,), pages=(1,))
    contradicted = ClaimCheck("b", "b", "q", refutes=(2,), pages=(3,))
    not_found = ClaimCheck("c", "c", "q", pages=(1, 2))
    unreadable = ClaimCheck("d", "d", "q", pages=(2,))
    claims = [backed, contradicted, not_found, unreadable]

    assert [cited_label(c, sources) for c in claims] == [
        "backed",
        "contradicted",
        "not found",
        "unreadable",
    ]
    assert summarize(claims, found, sources, cited=True) == (
        "Of 4 cited claims: 1 backed, 1 contradicted, 1 not found, 1 unreadable.",
        Confidence(
            "medium",
            "2 of 4 cited claims settled by the pages they cite; 1 cited page could not be read",
        ),
    )
    # One page settles a claim: the question is what that page says, not whether others agree.
    assert summarize([backed], found, sources[:1], cited=True) == (
        "Of 1 cited claim: 1 backed.",
        Confidence("high", "1 of 1 cited claims settled by the pages they cite"),
    )
    assert summarize([unreadable], found, sources, cited=True)[1].level == "low"
    # A page nobody judged is no page that does not say it.
    unjudged = replace(not_found, problems=("not judged (judge: the reply was not JSON)",))
    assert cited_label(unjudged, sources) == "not judged"
    assert summarize([backed, unjudged], found, sources, cited=True) == (
        "Of 2 cited claims: 1 backed, 1 not judged.",
        Confidence(
            "medium",
            "1 of 2 cited claims settled by the pages they cite; 1 cited page could not be read; "
            "1 not judged: the model's reply was unusable",
        ),
    )
    assert summarize([], [], [], cited=True) == (
        NOTHING_CITED,
        Confidence("low", "no claim is in a sentence that cites a page"),
    )


def test_a_cite_check_reads_only_the_pages_the_text_cites_and_rules_on_each_claim():
    backend = ScriptedBackend(
        {"claims": [LISTED], "judge": [JUDGE_RELEASE, JUDGE_GIL, JUDGE_NOTHING]}
    )
    search, fetcher = FakeSearch({}), FakeFetcher(PAGES)
    result = citer(backend, search=search, fetcher=fetcher).check(ANSWER, cited=True)

    assert search.calls == []
    assert fetcher.fetched == [RELEASE, WHATSNEW, REALPY, GONE]
    assert backend.purposes() == ["claims", "judge", "judge", "judge"]  # [4] costs no request
    assert result.checked_text == CITED_TEXT
    assert result.goal == CITE_RESULT.goal
    assert (result.cited, result.plan.queries) == (True, ())
    claims_prompt = backend.requests[0].messages
    assert "List only claims made in sentences that carry a marker." in claims_prompt[0].content
    assert f"<text>\n{CITED_TEXT}\n</text>" in claims_prompt[1].content
    first_judge = backend.requests[1].messages[1].content
    assert '<source id="1"' in first_judge
    assert '<source id="2"' not in first_judge

    assert [(s.index, s.url, s.query) for s in result.sources] == [
        (1, RELEASE, "cited as [1]"),
        (2, WHATSNEW, "cited as [2]"),
        (3, REALPY, "cited as [3]"),
        (4, GONE, "cited as [4]"),
    ]
    assert [(cited_label(c, result.sources), c.pages) for c in result.claims] == [
        ("contradicted", (1,)),
        ("contradicted", (2,)),
        ("not found", (3,)),
        ("unreadable", (4,)),
    ]
    assert result.claims[3].problems == (UNREAD,)
    assert result.claims[0].note == "The page gives October 7, 2024."
    assert (result.answer, result.confidence) == (CITE_RESULT.answer, CITE_RESULT.confidence)
    assert result.warnings == CITE_RESULT.warnings


TOWER_A, TOWER_B = "https://a.example/eiffel", "https://b.example/eiffel"
TOWER = {
    TOWER_A: "The Eiffel Tower is 330 metres tall, antennas included. It opened in 1889 for the "
    "World's Fair.",
    TOWER_B: "The Eiffel Tower is 324 metres tall. Paris is lovely in spring.",
}
TALL = "The Eiffel Tower is 330 metres tall."
OPENED = "The Eiffel Tower opened in 1889."


def test_pages_that_disagree_dispute_a_claim_and_a_page_it_does_not_cite_never_counts():
    text = f"{TALL}[1][2] It opened in 1889.[1]\n\n[1] {TOWER_A}\n[2] {TOWER_B}"
    listed = {
        "claims": [
            claim(TALL, "The Eiffel Tower is 330 metres tall [1][2]."),
            claim(OPENED, "It opened in 1889 [1]."),
        ]
    }
    disputed = judged(
        ("supports", "The Eiffel Tower is 330 metres tall, antennas included.", 1),
        ("refutes", "The Eiffel Tower is 324 metres tall.", 2),
    )
    backed = judged(
        ("supports", "It opened in 1889 for the World's Fair.", 1),
        ("refutes", "The Eiffel Tower is 324 metres tall.", 2),  # a page this claim never cites
    )
    backend = ScriptedBackend({"claims": [listed], "judge": [disputed, backed]})
    fetcher = FakeFetcher(TOWER)
    result = citer(backend, fetcher=fetcher).check(text, cited=True)

    assert fetcher.fetched == [TOWER_A, TOWER_B]  # [1] is read once for both claims
    assert '<source id="2"' not in backend.requests[2].messages[1].content
    tall, opened = result.claims
    assert [cited_label(c, result.sources) for c in result.claims] == ["disputed", "backed"]
    assert (tall.pages, opened.pages) == ((1, 2), (1,))
    (aside,) = (result.numbered[n - 1] for n in opened.set_aside)
    assert (aside.source, aside.note) == (2, "quote not found in the source")


def test_claims_in_sentences_citing_no_web_page_are_set_aside_before_the_claims_are_cut():
    text = (
        "Python 3.13 is out [1]. It has a JIT compiler. It is free software [7]. "
        "It was released on October 7, 2024 [1]. It is the newest major release [1].\n\n"
        f"[1] {RELEASE}"
    )
    listed = {
        "claims": [
            claim("Python 3.13 has a JIT compiler.", "It has a JIT compiler."),
            claim("Python 3.13 is free software.", "It is free software [7]."),
            claim("Python 3.13 is out.", "Python 3.13 is out [1]."),
            claim(
                "Python 3.13 was released on October 7, 2024.",
                "It was released on October 7, 2024 [1].",
            ),
            claim(
                "Python 3.13 is the newest major release.", "It is the newest major release [1]."
            ),
        ]
    }
    backend = ScriptedBackend({"claims": [listed], "judge": [judged(), judged()]})
    result = citer(backend).check(text, max_claims=2, cited=True)

    assert [c.claim for c in result.claims] == [
        "Python 3.13 is out.",
        "Python 3.13 was released on October 7, 2024.",
    ]
    assert "at most 2" in backend.requests[0].messages[0].content
    assert result.warnings == (
        "the text cites [7] but gives no web address for it",
        "set aside 2 claims in sentences that cite no web page: "
        "scout factcheck checks them against independent pages",
    )


def test_a_cited_page_may_fill_the_context_window_and_says_when_it_did_not_fit():
    long_page = "\n\n".join(
        f"Section {n}: the Python 3.13 release brought change number {n} to the language, "
        "described here at length so that the page runs on and on. " * 4
        for n in range(80)
    )
    text = f"Python 3.13 was released in 2024.[1]\n\n[1] {RELEASE}"
    listed = {
        "claims": [
            claim("Python 3.13 was released in 2024.", "Python 3.13 was released in 2024 [1].")
        ]
    }
    backend = ScriptedBackend({"claims": [listed], "judge": [judged()]})
    result = citer(backend, fetcher=FakeFetcher({RELEASE: long_page})).check(text, cited=True)

    block = backend.requests[1].messages[1].content.split('<source id="1"', 1)[1]
    assert len(block) > ResearchOptions().max_source_chars
    (told,) = (w for w in result.warnings if "characters;" in w)
    assert re.fullmatch(
        r"claim 1: \[1\] is [\d,]+ characters; only the passages closest to the claim were read",
        told,
    )


def test_a_cite_check_waiting_for_an_answer_resumes_with_the_very_same_pages():
    clock = Clock()
    pending = AnswerPending(Path("requests/x.md"))
    backend = ScriptedBackend(
        {
            "claims": [LISTED, LISTED],
            "judge": [JUDGE_RELEASE, pending, JUDGE_RELEASE, JUDGE_GIL, JUDGE_NOTHING],
        }
    )
    fetcher = FakeFetcher(dict(PAGES))
    with Store(":memory:") as store:

        def check() -> RunResult:
            return citer(backend, fetcher=fetcher, clock=clock, pins=store).check(
                ANSWER, cited=True
            )

        with pytest.raises(AnswerPending):
            check()
        read = list(fetcher.fetched)
        clock.advance(3600)
        fetcher.pages[RELEASE] = "Python 3.13.0 was released on October 7, 2023."
        result = check()

    assert read == [RELEASE, WHATSNEW]
    assert fetcher.fetched == [*read, REALPY, GONE]  # pinned pages are not read again
    first, again = backend.requests[:3], backend.requests[3:6]
    assert [r.messages for r in again] == [r.messages for r in first]
    assert cited_label(result.claims[0], result.sources) == "contradicted"


NOT_THERE = "Page not found. The page you asked for has moved or never was; try the search. " * 4


def not_there(url: str) -> Document:
    """What docs.python.org shows here for any address it does not know."""
    return Document(
        url=url,
        status=FetchStatus.OK,
        fetched_at=NOW,
        title="Page not found",
        text=NOT_THERE,
        content_hash="404",
    )


def test_a_resumed_cite_check_reads_its_soft_404_from_the_pins_without_probing_again():
    pending = AnswerPending(Path("requests/x.md"))
    backend = ScriptedBackend(
        {
            "claims": [LISTED, LISTED],
            "judge": [JUDGE_RELEASE, pending, JUDGE_RELEASE, JUDGE_NOTHING],
        }
    )
    made_up, above = soft404.probe(WHATSNEW), soft404.probe(WHATSNEW, 2)
    fetcher = FakeFetcher(
        {
            **PAGES,
            WHATSNEW: not_there(WHATSNEW),
            made_up: not_there(made_up),
            above: not_there(above),
        }
    )
    with Store(":memory:") as store:
        with pytest.raises(AnswerPending):
            citer(backend, fetcher=fetcher, pins=store).check(ANSWER, cited=True)
        result = citer(backend, fetcher=fetcher, pins=store).check(ANSWER, cited=True)

    assert fetcher.fetched == [RELEASE, WHATSNEW, made_up, above, REALPY, GONE]
    assert backend.purposes() == ["claims", "judge", "judge", "claims", "judge", "judge"]
    gil = result.claims[1]
    assert (cited_label(gil, result.sources), gil.problems) == (
        "unreadable",
        (
            "could not read [2] docs.python.org (not found: shows what its site shows for any "
            'unknown address, "Page not found")',
        ),
    )


@pytest.mark.parametrize("cite", [True, False], ids=["cite-check", "fact-check"])
def test_a_checked_address_its_site_now_redirects_home_is_not_checked(cite):
    home = "https://blog.example.com/"
    moved = replace(post("Welcome to the blog. Read our latest posts. " * 6), final_url=home)
    backend, fetcher = ScriptedBackend({}), FakeFetcher({POST: moved})
    with pytest.raises(ScoutError) as refused:
        citer(backend, fetcher=fetcher).check(POST, cited=cite)
    assert str(refused.value) == (
        f"could not read {POST}: redirects to its site's home page, {home}"
    )
    assert (fetcher.fetched, backend.requests) == ([POST], [])


def test_a_cite_check_and_a_fact_check_of_one_text_wait_apart():
    pending = AnswerPending(Path("requests/x.md"))
    backend = ScriptedBackend({"claims": [pending, pending]})
    with Store(":memory:") as store:
        for cite in (True, False):
            with pytest.raises(AnswerPending):
                citer(backend, pins=store).check(ANSWER, cited=cite)
        runs = {row["run"] for row in store._all("SELECT run FROM pins")}
    assert len(runs) == 2


def test_a_cite_check_needs_a_text_that_cites_a_web_page():
    backend, fetcher = ScriptedBackend({}), FakeFetcher(PAGES)
    with pytest.raises(ScoutError, match=r"^the text cites no web page: give its sources as"):
        citer(backend, fetcher=fetcher).check("Python 3.13 came out in 2024 [1].", cited=True)
    assert (fetcher.fetched, backend.requests) == ([], [])


def test_a_claim_is_judged_on_at_most_five_of_the_pages_its_sentence_cites():
    urls = [f"https://site{n}.example/jit" for n in range(1, 8)]
    listing = "\n".join(f"[{n}] {url}" for n, url in enumerate(urls, start=1))
    text = f"Python 3.13 has a JIT compiler.[1][2][3][4][5][6][7]\n\n{listing}"
    listed = {
        "claims": [
            claim(
                "Python 3.13 has a JIT compiler.",
                "Python 3.13 has a JIT compiler [1][2][3][4][5][6][7].",
            )
        ]
    }
    backend = ScriptedBackend({"claims": [listed], "judge": [judged()]})
    fetcher = FakeFetcher(dict.fromkeys(urls, "Python 3.13 has an experimental JIT compiler."))
    result = citer(backend, fetcher=fetcher).check(text, cited=True)

    assert fetcher.fetched == urls[:5]
    assert result.claims[0].pages == (1, 2, 3, 4, 5)
    assert result.warnings == ("claim 1: its sentence cites 7 pages; it was judged on the first 5",)


def test_a_partial_cite_check_says_how_many_of_the_cited_pages_it_read_and_how_to_read_all():
    backend = ScriptedBackend({"claims": [LISTED], "judge": [JUDGE_RELEASE]})
    fetcher = FakeFetcher(PAGES)
    result = citer(backend, fetcher=fetcher).check(ANSWER, max_claims=1, cited=True)

    assert fetcher.fetched == [RELEASE]
    assert result.warnings == (
        "the text cites 4 pages; the claims checked cite 1 of them; "
        "scout factcheck --cited --all checks every cited sentence",
    )

    long = (
        f"{RELEASED_2023}[1] " + "The rest of the post says little. " * 120 + f"\n\n[1] {RELEASE}"
    )
    listed = {"claims": [claim(RELEASED_2023, "Python 3.13 was released on October 7, 2023 [1].")]}
    backend = ScriptedBackend({"claims": [listed], "judge": [JUDGE_RELEASE]})
    cut = citer(backend, context_tokens=4096).check(long, cited=True)
    assert cut.warnings == (
        f"only the first {len(cut.checked_text):,} characters were checked; "
        "scout factcheck --cited --all checks every cited sentence",
    )


PUBLIC = "http://93.184.216.34/python-3.13"
ARTICLE = (
    "<html><head><title>Python 3.13</title></head><body><article><p>"
    + "Python 3.13 was released on October 7, 2024. " * 12
    + "</p></article></body></html>"
)


@responses.activate
def test_a_public_only_cite_check_never_requests_a_private_address():
    responses.add(responses.GET, PUBLIC, body=ARTICLE, content_type="text/html")
    text = (
        "Python 3.13 was released on October 7, 2024 [1]. Its admin page lists 3 users [2]. "
        f"Its dashboard shows 5 builds [3].\n\n[1] {PUBLIC}\n[2] http://10.0.0.5/admin\n"
        "[3] http://localhost:8080/x"
    )
    listed = {
        "claims": [
            claim(
                "Python 3.13 was released on October 7, 2024.",
                "Python 3.13 was released on October 7, 2024 [1].",
            ),
            claim("The admin page lists 3 users.", "Its admin page lists 3 users [2]."),
            claim("The dashboard shows 5 builds.", "Its dashboard shows 5 builds [3]."),
        ]
    }
    backend = ScriptedBackend({"claims": [listed], "judge": [judged()]})
    fetcher = Fetcher(FetchConfig(public_only=True, retries=0))
    try:
        result = citer(backend, fetcher=fetcher, public_only=True).check(text, cited=True)
    finally:
        fetcher.close()

    assert [call.request.url for call in responses.calls] == [PUBLIC]
    assert [s.status for s in result.sources] == ["ok", "refused", "refused"]
    assert backend.purposes() == ["claims", "judge"]
    assert result.claims[1].problems == (
        "could not read [2] 10.0.0.5 (refused: it is on a private network (10.0.0.5))",
    )
    assert (
        result.claims[2]
        .problems[0]
        .startswith("could not read [3] localhost (refused: it is on a private network (")
    )


def test_a_cite_check_asks_for_claims_in_sentences_that_cite():
    today = date(2026, 10, 7)
    plain = claims_messages(CITED_TEXT, today, max_claims=6)
    system, user = claims_messages(CITED_TEXT, today, max_claims=6, cited=True)
    assert claims_messages(CITED_TEXT, today, max_claims=6, cited=False) == plain
    assert user == plain[1]
    assert system.content == plain[0].content + (
        "\nThe text cites web pages with markers like [1]. List only claims made in sentences "
        "that carry a marker. Restate each claim without its markers, and copy its sentence "
        "with them."
    )


def test_a_cite_check_round_trips_and_older_runs_load_as_fact_checks():
    data = json.loads(json.dumps(CITE_RESULT.to_dict()))
    assert data["cited"] is True
    assert data["claims"][3]["pages"] == [4]
    assert RunResult.from_dict(data) == CITE_RESULT
    del data["cited"]
    assert RunResult.from_dict(data) == replace(CITE_RESULT, cited=False)


def test_a_claim_the_model_could_not_judge_is_not_judged_not_missing_from_its_page():
    text = f"Python 3.13 was released on October 7, 2024.[1]\n\n[1] {RELEASE}"
    listed = {
        "claims": [
            claim(
                "Python 3.13 was released on October 7, 2024.",
                "Python 3.13 was released on October 7, 2024 [1].",
            )
        ]
    }
    backend = ScriptedBackend({"claims": [listed], "judge": ["not json"] * 4})
    result = citer(backend).check(text, cited=True)

    (check,) = result.claims
    assert cited_label(check, result.sources) == "not judged"
    assert result.answer == "Of 1 cited claim: 1 not judged."
    assert result.confidence.level == "low"
    assert "**Not judged on [1]**" in render_markdown(result)


@responses.activate
def test_a_cite_check_from_the_command_line_reads_no_private_address_unless_allowed(
    workspace, monkeypatch
):
    responses.add(responses.GET, PUBLIC, body=ARTICLE, content_type="text/html")
    metadata = "http://169.254.169.254/latest/meta-data/iam/security-credentials/"
    router = "http://192.168.1.1/apply.cgi?action=reboot"
    text = (
        "Python 3.13 was released on October 7, 2024 [1]. The role has admin rights [2]. "
        f"The router rebooted [3].\n\n[1] {PUBLIC}\n[2] {metadata}\n[3] {router}"
    )
    listed = {
        "claims": [
            claim(
                "Python 3.13 was released on October 7, 2024.",
                "Python 3.13 was released on October 7, 2024 [1].",
            ),
            claim("The role has admin rights.", "The role has admin rights [2]."),
            claim("The router rebooted.", "The router rebooted [3]."),
        ]
    }
    backend = ScriptedBackend({"claims": [listed], "judge": [judged()]})
    monkeypatch.setattr("scout.app.make_backend", lambda settings: backend)
    checked = CliRunner().invoke(main, ["factcheck", "--cited", "--no-save", "--json", text])

    assert checked.exit_code == 0, checked.output
    assert [call.request.url for call in responses.calls] == [PUBLIC]
    result = RunResult.from_dict(json.loads(checked.output))
    assert [s.status for s in result.sources] == ["ok", "refused", "refused"]
    assert [cited_label(c, result.sources) for c in result.claims] == [
        "not found",
        "unreadable",
        "unreadable",
    ]

    refused = CliRunner().invoke(main, ["factcheck", "--allow-private", "Some text."])
    assert refused.exit_code == 2
    assert "--allow-private applies to --cited only" in refused.output


REVIEW = "http://93.184.216.34/review"
PAGE_A, PAGE_B, INTRANET = "http://93.184.216.35/a", "http://93.184.216.36/b", "http://10.0.0.5/x"
REVIEWED = "Python 3.13 brought many changes that this review goes through one by one. " * 6
REVIEW_PAGE = (
    "<html><head><title>Python 3.13, reviewed</title></head><body><article>"
    f'<p>Python 3.13 was released on October 7, 2024, says <a href="{PAGE_A}">the release '
    'page</a>. It removed the global interpreter lock by default.<sup><a href="#fn2">[2]</a>'
    '</sup> Our <a href="/other">other review</a> says more. The build server has 3 users, per '
    f'<a href="{INTRANET}">the dashboard</a>.</p><p>{REVIEWED}</p>'
    f'<ol><li id="fn2">See <a href="{PAGE_B}">What is new in Python 3.13</a>.</li></ol>'
    "</article></body></html>"
)


@responses.activate
def test_a_web_address_is_cite_checked_on_the_pages_its_links_cite():
    responses.add(responses.GET, REVIEW, body=REVIEW_PAGE, content_type="text/html")
    responses.add(responses.GET, PAGE_A, body=ARTICLE, content_type="text/html")
    gil = "The free-threaded mode is experimental and the GIL remains enabled by default. "
    responses.add(
        responses.GET,
        PAGE_B,
        body=f"<html><body><article><p>{gil * 6}</p></article></body></html>",
        content_type="text/html",
    )
    listed = {
        "claims": [
            claim(
                "Python 3.13 was released on October 7, 2024.",
                "Python 3.13 was released on October 7, 2024, says the release page [1].",
            ),
            claim(
                "Python 3.13 removed the global interpreter lock by default.",
                "It removed the global interpreter lock by default [2].",
            ),
            claim(
                "The build server has 3 users.",
                "The build server has 3 users, per the dashboard [3].",
            ),
        ]
    }
    backend = ScriptedBackend(
        {
            "claims": [listed],
            "judge": [
                judged(("supports", "Python 3.13 was released on October 7, 2024.", 1)),
                judged(("refutes", gil.strip(), 2)),
            ],
        }
    )
    fetcher = Fetcher(FetchConfig(public_only=True, retries=0))
    try:
        result = citer(backend, fetcher=fetcher, public_only=True).check(REVIEW, cited=True)
    finally:
        fetcher.close()

    assert [call.request.url for call in responses.calls] == [REVIEW, PAGE_A, PAGE_B]
    assert result.goal == f"cite-check: {REVIEW}"
    assert [(s.index, s.url, s.query, s.status) for s in result.sources] == [
        (1, PAGE_A, "cited as [1]", "ok"),
        (2, PAGE_B, "cited as [2]", "ok"),
        (3, INTRANET, "cited as [3]", "refused"),
    ]
    assert [cited_label(c, result.sources) for c in result.claims] == [
        "backed",
        "contradicted",
        "unreadable",
    ]
    assert result.claims[2].problems == (
        "could not read [3] 10.0.0.5 (refused: it is on a private network (10.0.0.5))",
    )
    assert "Our other review says more." in result.checked_text
    assert "by default [2]. Our" in result.checked_text
    assert "](" not in result.checked_text
    claims_prompt = backend.requests[0].messages[1].content
    assert "[^" not in claims_prompt
    assert PAGE_B not in claims_prompt


POST = "https://blog.example.com/python-313"
POST_TEXT = (
    f"Python 3.13 was released on October 7, 2023, says [the release page]({RELEASE}). "
    f"It removed the global interpreter lock by default.[^2]\n[^2]: {WHATSNEW}"
)


def post(text: str) -> Document:
    return Document(
        url=POST, status=FetchStatus.OK, fetched_at=NOW, title="Python 3.13, reviewed", text=text
    )


def test_a_web_address_cite_check_waiting_for_an_answer_resumes_with_the_same_page():
    clock = Clock()
    listed = {
        "claims": [
            claim(
                RELEASED_2023,
                "Python 3.13 was released on October 7, 2023, says the release page [1].",
            ),
            claim(
                "Python 3.13 removed the global interpreter lock by default.",
                "It removed the global interpreter lock by default [2].",
            ),
        ]
    }
    pending = AnswerPending(Path("requests/x.md"))
    backend = ScriptedBackend(
        {"claims": [listed, listed], "judge": [pending, JUDGE_RELEASE, JUDGE_GIL]}
    )
    fetcher = FakeFetcher({**PAGES, POST: post(POST_TEXT)})
    with Store(":memory:") as store:

        def check() -> RunResult:
            return citer(backend, fetcher=fetcher, clock=clock, pins=store).check(POST, cited=True)

        with pytest.raises(AnswerPending):
            check()
        clock.advance(3600)
        fetcher.pages[POST] = post(POST_TEXT.replace("2023", "2022"))
        result = check()

    assert fetcher.linked == [POST]
    assert fetcher.fetched == [POST, RELEASE, WHATSNEW]
    first, again = backend.requests[:2], backend.requests[2:4]
    assert [r.messages for r in again] == [r.messages for r in first]
    assert [cited_label(c, result.sources) for c in result.claims] == ["contradicted"] * 2


def test_a_page_that_links_only_to_its_own_site_has_nothing_to_cite_check():
    backend = ScriptedBackend({})
    fetcher = FakeFetcher({POST: post("Python 3.13 is out. Our older post says more.")})
    with pytest.raises(ScoutError, match=re.escape(NONE_LINKED)):
        citer(backend, fetcher=fetcher).check(POST, cited=True)
    assert (fetcher.fetched, fetcher.linked, backend.requests) == ([POST], [POST], [])

    # A link whose words are an address leaves them in the text: still the page's own site.
    worded = post(
        "I was live-blogging the keynote: https://blog.example.com/2026/keynote-liv... and "
        "<https://www.example.com/about>, as [1] said.\n\n[1] https://docs.example.com/ft"
    )
    with pytest.raises(ScoutError, match=re.escape(NONE_LINKED)):
        citer(backend, fetcher=FakeFetcher({POST: worded})).check(POST, cited=True)


TIER_3 = "Python 3.13 runs on iOS as a tier 3 platform."
TAKEN = datetime(2024, 11, 2, 8, 30, tzinfo=UTC)
COPY = f"https://web.archive.org/web/20241102083000/{GONE}"
ON_IOS = f"Python on phones. {TIER_3} Android support is on its way."
ARCHIVED = f"{CITE_RESULT.answer} 1 of 1 unreadable claims was judged on archived copies of the "


def archived_check(copies=None) -> tuple[RunResult, ScriptedBackend, FakeArchive]:
    """The README's cite-check, its dead [4] judged on the copy *copies* hold."""
    judges = [JUDGE_RELEASE, JUDGE_GIL, JUDGE_NOTHING, judged(("supports", TIER_3, 5))]
    backend = ScriptedBackend({"claims": [LISTED], "judge": judges})
    archive = FakeArchive({GONE: (TAKEN, ON_IOS)} if copies is None else copies)
    return citer(backend, archive=archive).check(ANSWER, cited=True), backend, archive


def test_a_dead_cited_page_is_judged_on_its_archived_copy_beside_its_label():
    result, backend, archive = archived_check()

    assert backend.purposes() == ["claims"] + ["judge"] * 4
    judge = backend.requests[4].messages[1].content
    assert re.search(r'<source id="5" [^>]* site="example.org" archived="2024-11-02">', judge)
    assert '<source id="4"' not in judge
    assert archive.asked == [(GONE, None)]
    ios = result.claims[3]
    assert (cited_label(ios, result.sources), ios.problems) == ("unreadable", (UNREAD,))
    assert ios.archived is not None
    assert (cited_label(ios.archived, result.sources), ios.archived.pages) == ("backed", (5,))
    copy = result.sources[-1]
    assert (copy.index, copy.url, copy.site, copy.copy_of, copy.query) == (
        5,
        COPY,
        "example.org",
        4,
        "archived copy of [4]",
    )
    (finding,) = (result.numbered[n - 1] for n in ios.archived.supports)
    assert (finding.source, finding.quote, finding.trusted) == (5, TIER_3, True)
    assert finding.anchor is not None
    assert result.answer == f"{ARCHIVED}pages it cites: 1 backed."
    assert (result.confidence, result.warnings) == (CITE_RESULT.confidence, CITE_RESULT.warnings)

    claims = render_markdown(result).split("## Claims")[1].split("## Dead links")[0]
    assert (
        "4. **Could not read [4]**: Python 3.13 runs on iOS as a tier 3 platform.\n\n"
        '   In the text: "It runs on iOS as a tier 3 platform \\[4\\]."\n\n'
        "   - could not read \\[4\\] example.org (not found: HTTP 404)\n"
        "   - archived copy of [4] (2024-11-02): backed\n"
        f'     - confirms (finding 3): "{TIER_3}" ([archived copy of [4]]({COPY}#:~:text='
    ) in claims
    assert claims.rstrip().endswith(", example.org, archived 2024-11-02)")


LINK = f"{COPY}#:~:text=Python%203.13%20runs%20on,a%20tier%203%20platform."


def test_a_dead_citation_is_listed_with_the_archived_copy_to_cite_instead():
    result, _, _ = archived_check()

    (dead,) = dead_links(result)
    assert (dead.n, dead.state, dead.claims, dead.backs, dead.contradicts) == (
        4,
        "replace",
        (4,),
        (4,),
        (),
    )
    assert (dead.link, dead.taken, dead.quote.quote) == (LINK, "2024-11-02", TIER_3)
    assert replacements([dead]) == {GONE: LINK}

    markdown = render_markdown(result)
    assert markdown.index("## Claims") < markdown.index("## Dead links")
    assert markdown.index("## Dead links") < markdown.index("## Sources")
    assert (
        "## Dead links\n\n"
        "1 cited page is gone: 1 can be replaced by its archived copy, which backs a claim the "
        "text cites it for.\n\n"
        f"- [4] <{GONE}> (not found: HTTP 404): replace with its archived copy of 2024-11-02, "
        f'which backs claim 4: "{TIER_3}"\n'
        f"  <{LINK}>\n\n## Sources"
    ) in markdown
    page = render_html(result)
    assert (
        f'<li>[4] <a href="{GONE}">{GONE}</a> (not found: HTTP 404): replace with its archived '
        f"copy of 2024-11-02, which backs claim 4: &quot;{TIER_3}&quot;<br>"
        f'<a href="{LINK}">{LINK}</a></li>'
    ) in page.split("<h2>Dead links</h2>")[1].split("<h2>Sources</h2>")[0]
    assert json.loads(render_json(result))["dead_links"] == [
        {
            "n": 4,
            "url": GONE,
            "why": "not found: HTTP 404",
            "state": "replace",
            "copy": COPY,
            "taken": "2024-11-02",
            "moved": None,
            "link": LINK,
            "quote": TIER_3,
            "claims": [4],
            "backs": [4],
            "contradicts": [],
            "note": None,
        }
    ]


PAGESPEED = "https://developers.google.com/speed/pagespeed/service"
TURNED_OFF = "PageSpeed Service was turned off on August 3rd, 2015."
SHUT_DOWN = (
    "Google turned PageSpeed Service off on August 3rd, 2015 [1]. PageSpeed Service was shut "
    "down in 2014 [1]. It served 9,000 sites [1]. It still runs today [1].\n\n"
    f"[1] {PAGESPEED}"
)


def test_a_copy_is_held_to_what_the_claim_says_as_any_cited_page_is():
    taken = datetime(2023, 1, 20, 23, 40, 50, tzinfo=UTC)
    service = (
        f"Turbocharge your web site with PageSpeed Service. {TURNED_OFF} It served many sites "
        "before it was turned off."
    )
    archive = FakeArchive({PAGESPEED: (taken, service)})
    listed = {
        "claims": [
            claim(
                "Google turned PageSpeed Service off on August 3rd, 2015.",
                "Google turned PageSpeed Service off on August 3rd, 2015 [1].",
            ),
            claim(
                "PageSpeed Service was shut down in 2014.",
                "PageSpeed Service was shut down in 2014 [1].",
            ),
            claim("PageSpeed Service served 9,000 sites.", "It served 9,000 sites [1]."),
            claim("PageSpeed Service still runs today.", "It still runs today [1]."),
        ]
    }
    judges = [
        judged(("supports", TURNED_OFF, 2)),
        judged(("refutes", TURNED_OFF, 2)),  # 2015, not 2014
        judged(("supports", "It served many sites before it was turned off.", 2)),  # no 9,000
        judged(("supports", "PageSpeed Service still runs today.", 2)),  # not on the copy
    ]
    backend = ScriptedBackend({"claims": [listed], "judge": judges})
    gone = Document(url=PAGESPEED, status=FetchStatus.NOT_FOUND, fetched_at=NOW, error="HTTP 404")
    result = citer(backend, fetcher=FakeFetcher({PAGESPEED: gone}), archive=archive).check(
        SHUT_DOWN, cited=True
    )

    assert archive.asked == [(PAGESPEED, None)]
    assert archive.web.fetched == [Snapshot(PAGESPEED, taken).raw]  # read once for four claims
    assert {cited_label(c, result.sources) for c in result.claims} == {"unreadable"}
    assert [cited_label(c.archived, result.sources) for c in result.claims] == [
        "backed",
        "contradicted",
        "not found",
        "not found",
    ]
    aside = [[result.numbered[n - 1].note for n in c.archived.set_aside] for c in result.claims]
    assert aside == [[], [], ["the quote does not contain 9000"], ["quote not found in the source"]]
    assert result.answer == (
        "Of 4 cited claims: 4 unreadable. 4 of 4 unreadable claims were judged on archived copies "
        "of the pages they cite: 1 backed, 1 contradicted, 2 not found."
    )
    assert result.confidence.reason == (
        "0 of 4 cited claims settled by the pages they cite; 1 cited page could not be read"
    )
    claims = render_markdown(result).split("## Claims")[1]
    assert (
        "   - archived copy of [1] (2023-01-20): not found\n"
        '     - set aside (finding 3): "It served many sites before it was turned off." '
        "([archived copy of [1]](https://web.archive.org/web/20230120234050/"
    ) in claims

    # It holds what the page was cited for: cited in its place, the sentence it contradicts told.
    (dead,) = dead_links(result)
    assert (dead.state, dead.claims, dead.backs, dead.contradicts, dead.quote.quote) == (
        "replace",
        (1, 2, 3, 4),
        (1,),
        (2,),
        TURNED_OFF,
    )
    assert (
        f"- [1] <{PAGESPEED}> (not found: HTTP 404): replace with its archived copy of "
        f'2023-01-20, which backs claim 1: "{TURNED_OFF}" It contradicts claim 2: correct that '
        "sentence. It does not state claims 3 and 4: check those sentences or cite another "
        f"source.\n  <https://web.archive.org/web/20230120234050/{PAGESPEED}#:~:text="
    ) in claims


@pytest.mark.parametrize(
    ("copies", "pages", "problem", "shown"),
    [
        ({}, (), "no archived copy of [4]", "   - no archived copy of \\[4\\]"),
        (
            {
                GONE: (
                    TAKEN,
                    Document(
                        url=Snapshot(GONE, TAKEN).raw,
                        status=FetchStatus.EMPTY,
                        fetched_at=NOW,
                        error="no readable main text",
                    ),
                )
            },
            (5,),
            "could not read the archived copy of [4] (empty: no readable main text)",
            "   - archived copy of [4] (2024-11-02): unreadable\n"
            "     - could not read the archived copy of \\[4\\] (empty: no readable main text)",
        ),
    ],
    ids=["no copy", "an unreadable copy"],
)
def test_no_copy_or_an_unreadable_one_costs_no_model_request(copies, pages, problem, shown):
    result, backend, _ = archived_check(copies)
    assert backend.purposes() == ["claims", "judge", "judge", "judge"]
    archived = result.claims[3].archived
    assert archived is not None
    assert (cited_label(archived, result.sources), archived.pages, archived.problems) == (
        "unreadable",
        pages,
        (problem,),
    )
    assert (result.answer, result.confidence) == (CITE_RESULT.answer, CITE_RESULT.confidence)
    assert f"   - could not read \\[4\\] example.org (not found: HTTP 404)\n{shown}" in (
        render_markdown(result)
    )


RELEASED = "Python 3.13 was released on October 7, 2024."


def test_a_dead_page_cited_beside_a_live_one_is_judged_on_its_copy_alone():
    intranet, copied = "http://10.0.0.5/x", "https://web.archive.org/web/2019/https://old.example/"
    text = (
        f"{RELEASED[:-1]} [1][4]. {TIER_3[:-1]} [4]. The admin page lists 3 users [2]. "
        f"The old page lists 5 things [3].\n\n[1] {WHATSNEW}\n[2] {intranet}\n[3] {copied}\n"
        f"[4] {GONE}"
    )
    listed = {
        "claims": [
            claim(RELEASED, f"{RELEASED[:-1]} [1][4]."),
            claim(TIER_3, f"{TIER_3[:-1]} [4]."),
            claim("The admin page lists 3 users.", "The admin page lists 3 users [2]."),
            claim("The old page lists 5 things.", "The old page lists 5 things [3]."),
        ]
    }
    refused = Document(
        url=intranet,
        status=FetchStatus.REFUSED,
        fetched_at=NOW,
        error="not read: it is on a private network (10.0.0.5)",
    )
    archive = FakeArchive({GONE: (TAKEN, f"{RELEASED} {TIER_3}")})
    judges = [
        judged(("supports", RELEASED, 1)),
        judged(("supports", RELEASED, 5)),
        judged(("supports", TIER_3, 5)),
    ]
    backend = ScriptedBackend({"claims": [listed], "judge": judges})
    result = citer(
        backend, fetcher=FakeFetcher({**PAGES, intranet: refused}), archive=archive
    ).check(text, cited=True)

    # A refused, a blocked or an archived page is never looked up; the dead one is, once.
    assert backend.purposes() == ["claims", "judge", "judge", "judge"]
    assert archive.asked == [(GONE, None)]
    on_copy = backend.requests[2].messages[1].content
    assert re.search(r'<source id="5" [^>]* archived="2024-11-02">', on_copy)
    assert '<source id="1"' not in on_copy
    released, ios, *others = result.claims
    assert [cited_label(c, result.sources) for c in result.claims] == ["backed"] + [
        "unreadable"
    ] * 3
    assert [cited_label(c.archived, result.sources) for c in (released, ios)] == ["backed"] * 2
    assert [c.archived for c in others] == [None, None]
    assert result.answer == (
        "Of 4 cited claims: 1 backed, 3 unreadable. 1 of 3 unreadable claims was judged on "
        "archived copies of the pages it cites: 1 backed."
    )
    (dead,) = dead_links(result)
    assert (dead.n, dead.state, dead.claims, dead.backs) == (4, "replace", (1, 2), (1, 2))


DEAD = [f"https://gone{n}.example/stations" for n in (1, 2, 3)]
DEAD_TEXT = (
    " ".join(f"Station {n} opened in {1900 + n} [{n}]." for n in (1, 2, 3))
    + "\n\n"
    + "\n".join(f"[{n}] {url}" for n, url in enumerate(DEAD, start=1))
)


def dead_check(archive: FakeArchive) -> RunResult:
    listed = {
        "claims": [
            claim(f"Station {n} opened in {1900 + n}.", f"Station {n} opened in {1900 + n} [{n}].")
            for n in (1, 2, 3)
        ]
    }
    web = FakeFetcher(
        {
            url: Document(url=url, status=FetchStatus.NOT_FOUND, fetched_at=NOW, error="HTTP 404")
            for url in DEAD
        }
    )
    backend = ScriptedBackend({"claims": [listed]})
    return citer(backend, fetcher=web, archive=archive).check(DEAD_TEXT, cited=True)


def test_once_the_archive_does_not_answer_no_other_page_is_looked_up():
    archive = FakeArchive({}, down="HTTP 429")
    result = dead_check(archive)
    assert archive.asked == [(DEAD[0], None)]
    assert [c.archived.problems for c in result.claims if c.archived] == [
        (f"no archived copy looked up for [{n}]: the Wayback Machine did not answer (HTTP 429)",)
        for n in (1, 2, 3)
    ]
    assert [w for w in result.warnings if "Wayback" in w] == [
        "claim 1: the Wayback Machine did not answer (HTTP 429): later unreadable cited pages "
        "were not looked up"
    ]


def test_a_run_looks_up_a_few_dead_pages_at_most(monkeypatch):
    monkeypatch.setattr(factcheck, "ARCHIVE_LIMIT", 2)
    archive = FakeArchive({})
    result = dead_check(archive)
    assert [url for url, _ in archive.asked] == DEAD[:2]
    assert [c.archived.problems for c in result.claims if c.archived] == [
        ("no archived copy of [1]",),
        ("no archived copy of [2]",),
        ("no archived copy looked up for [3]: at most 2 cited pages are looked up in a run",),
    ]


def test_an_archived_reading_is_kept_but_memory_learns_only_from_the_live_web():
    result, _, _ = archived_check()
    data = json.loads(render_json(result))
    loaded = RunResult.from_dict(data)
    assert (loaded.claims, loaded.sources[-1].copy_of) == (result.claims, 4)
    del data["claims"][3]["archived"], data["sources"][4]["copy_of"]
    older = RunResult.from_dict(data)
    assert (older.claims[3].archived, older.sources[4].copy_of) == (None, None)

    with Store(":memory:") as store:
        run_id = store.add_run(result)
        stored = store.get_run(run_id)
        learned = [memory.quote for memory in store.recall("Python 3.13 iOS tier 3 platform")]
    assert stored is not None
    assert stored.claims == result.claims
    assert [link.to_dict() for link in dead_links(stored)] == [
        link.to_dict() for link in dead_links(result)
    ]
    assert len(learned) == 2
    assert TIER_3 not in learned


HINT = (
    "1 cited page is gone: --archive judges its claims on archived copies (web.archive.org); "
    "--fix FILE also writes the text with the fixable links replaced"
)


def test_the_command_line_looks_dead_pages_up_only_when_asked(workspace, monkeypatch):
    archive = FakeArchive({GONE: (TAKEN, ON_IOS)})
    monkeypatch.setattr("scout.app.make_archive", lambda spec, fetcher: archive)
    monkeypatch.setattr("scout.app.Fetcher", lambda *args, **options: FakeFetcher(PAGES))
    judges = [JUDGE_RELEASE, JUDGE_GIL, JUDGE_NOTHING]
    backend = ScriptedBackend(
        {"claims": [LISTED, LISTED], "judge": [*judges, *judges, judged(("supports", TIER_3, 5))]}
    )
    monkeypatch.setattr("scout.app.make_backend", lambda settings: backend)

    plain = CliRunner().invoke(main, ["factcheck", "--cited", "--no-save", ANSWER])
    assert plain.exit_code == 0, plain.output
    assert (backend.purposes().count("judge"), archive.asked) == (3, [])
    assert HINT in " ".join(plain.output.split())

    looked = CliRunner().invoke(main, ["factcheck", "--cited", "--archive", "--json", ANSWER])
    assert looked.exit_code == 0, looked.output
    assert (backend.purposes().count("judge"), archive.asked) == (7, [(GONE, None)])
    result = RunResult.from_dict(json.loads(looked.stdout))
    assert cited_label(result.claims[3].archived, result.sources) == "backed"
    assert HINT not in looked.output


def test_the_command_line_offers_and_checks_archived_copies(workspace, monkeypatch):
    researcher, asked = App.researcher, []
    monkeypatch.setattr(
        App,
        "researcher",
        lambda self, **options: asked.append(options) or FakeResearcher(CITE_RESULT),
    )

    def shown(*args: str) -> str:
        done = CliRunner().invoke(main, ["factcheck", "--no-save", *args])
        assert done.exit_code == 0, done.output
        return " ".join(done.output.split())

    assert HINT not in shown("--cited", CITED_TEXT)  # no archive to look in
    monkeypatch.setattr("scout.app.make_archive", lambda spec, fetcher: FakeArchive({}))
    assert HINT not in shown("--cited", "--allow-private", CITED_TEXT)
    assert HINT in shown("--cited", CITED_TEXT)
    page = shown("--cited", "https://blog.example.com/python-313")  # no text to fix
    assert HINT.split("; --fix")[0] in page
    assert "--fix" not in page
    assert [options["archive"] for options in asked] == [False] * 4

    for args, message in (
        (["--archive", "Some text."], "--archive applies to --cited only"),
        (
            ["--cited", "--archive", "--allow-private", CITED_TEXT],
            "--archive sends the addresses of unreadable cited pages to web.archive.org; it does "
            "not combine with --allow-private, whose pages may be intranet addresses",
        ),
    ):
        refused = CliRunner().invoke(main, ["factcheck", *args])
        assert refused.exit_code == 2
        assert message in " ".join(refused.output.split())

    monkeypatch.setattr(App, "researcher", researcher)
    monkeypatch.setattr("scout.app.make_archive", make_archive)
    monkeypatch.setenv("SCOUT_ARCHIVE", "off")
    off = CliRunner().invoke(main, ["factcheck", "--cited", "--archive", CITED_TEXT])
    assert off.exit_code == 1
    assert "error: archive lookups are off (SCOUT_ARCHIVE=off)" in off.output


def test_the_command_line_writes_the_text_with_its_dead_citations_fixed(workspace, monkeypatch):
    archive = FakeArchive({GONE: (TAKEN, ON_IOS)})
    monkeypatch.setattr("scout.app.make_archive", lambda spec, fetcher: archive)
    monkeypatch.setattr("scout.app.Fetcher", lambda *args, **options: FakeFetcher(PAGES))
    judges = [JUDGE_RELEASE, JUDGE_GIL, JUDGE_NOTHING, judged(("supports", TIER_3, 5))]
    backend = ScriptedBackend({"claims": [LISTED, LISTED], "judge": judges * 2})
    monkeypatch.setattr("scout.app.make_backend", lambda settings: backend)
    given = codecs.BOM_UTF8 + ANSWER.replace("\n", "\r\n").encode()
    fixed = given.replace(f"[4] {GONE}".encode(), f"[4] {LINK}".encode())
    Path("answer.md").write_bytes(given)

    args = ["factcheck", "--cited", "--fix", "out.md", "--json", "-f", "answer.md"]
    done = CliRunner().invoke(main, args)
    assert done.exit_code == 0, done.output
    assert archive.asked == [(GONE, None)]
    assert Path("out.md").read_bytes() == fixed  # its BOM and line endings kept
    shown = " ".join(done.stderr.split())
    assert "Fixed text: out.md (1 dead citation replaced by its archived copy)" in shown
    data = json.loads(done.stdout)
    assert data["fixed"] == {"path": "out.md", "replaced": [4]}
    assert [(link["n"], link["link"]) for link in data["dead_links"]] == [(4, LINK)]

    args = ["factcheck", "--cited", "--fix", "answer.md", "--no-save", "-f", "answer.md"]
    in_place = CliRunner().invoke(main, args)
    assert in_place.exit_code == 0, in_place.output
    assert Path("answer.md").read_bytes() == fixed


def test_a_fix_with_nothing_to_replace_leaves_the_text_as_it_was(workspace, monkeypatch):
    monkeypatch.setattr("scout.app.make_archive", lambda spec, fetcher: FakeArchive({}))
    monkeypatch.setattr("scout.app.Fetcher", lambda *args, **options: FakeFetcher(PAGES))
    judges = [JUDGE_RELEASE, JUDGE_GIL, JUDGE_NOTHING]
    backend = ScriptedBackend({"claims": [LISTED], "judge": judges})
    monkeypatch.setattr("scout.app.make_backend", lambda settings: backend)
    Path("answer.md").write_text(ANSWER, encoding="utf-8")  # CRLF on Windows

    args = ["factcheck", "--cited", "--fix", "out.md", "--no-save", "-f", "answer.md"]
    done = CliRunner().invoke(main, args)
    assert done.exit_code == 0, done.output
    assert Path("out.md").read_bytes() == Path("answer.md").read_bytes()
    assert (
        "Fixed text: out.md (unchanged: the dead cited page has no archived copy that backs its "
        "claims; see Dead links)"
    ) in " ".join(done.stderr.split())
    assert "not archived; cite another source (claim 4)" in " ".join(done.stdout.split())

    live = FakeResearcher(replace(CITE_RESULT, claims=CITE_RESULT.claims[:3]))
    monkeypatch.setattr(App, "researcher", lambda self, **options: live)
    unchanged = CliRunner().invoke(main, args)
    assert unchanged.exit_code == 0, unchanged.output
    assert Path("out.md").read_bytes() == Path("answer.md").read_bytes()
    assert "Fixed text: out.md (no checked claim cites a dead page: unchanged)" in " ".join(
        unchanged.stderr.split()
    )


def test_the_command_line_refuses_a_fix_it_cannot_make(workspace, monkeypatch):
    for args, message in (
        (["--fix", "out.md", CITED_TEXT], "--fix applies to --cited only"),
        (
            ["--cited", "--fix", "out.md", "--allow-private", CITED_TEXT],
            "--archive sends the addresses of unreadable cited pages to web.archive.org",
        ),
        (
            ["--cited", "--fix", "out.md", "https://blog.example.com/python-313"],
            "--fix rewrites a text you give (the text itself or -f FILE); for a web page, "
            "--archive lists each dead citation and the archived copy to cite instead",
        ),
        (["--cited", "--fix", "missing/out.md", CITED_TEXT], "--fix: cannot write missing"),
        (["--cited", "--fix", "-", CITED_TEXT], "--fix needs a file to write"),
    ):
        refused = CliRunner().invoke(main, ["factcheck", *args])
        assert refused.exit_code == 2
        assert message in " ".join(refused.output.split())

    monkeypatch.setenv("SCOUT_ARCHIVE", "off")
    monkeypatch.setattr("scout.app.make_archive", make_archive)
    off = CliRunner().invoke(main, ["factcheck", "--cited", "--fix", "out.md", CITED_TEXT])
    assert off.exit_code == 1
    assert "error: archive lookups are off (SCOUT_ARCHIVE=off)" in off.output

    pending = AnswerPending(Path("requests/x.md"))
    monkeypatch.setattr(App, "researcher", lambda self, **options: FakeResearcher(pending))
    waiting = CliRunner().invoke(main, ["factcheck", "--cited", "--fix", "out.md", CITED_TEXT])
    assert waiting.exit_code == EXIT_ANSWER_PENDING
    assert not Path("out.md").exists()


@pytest.mark.parametrize(
    ("url", "status", "error", "looked_up"),
    [
        ("https://gone.example/a", FetchStatus.NOT_FOUND, "HTTP 404", True),
        ("https://moved.example/a", FetchStatus.HTTP_ERROR, "HTTP 410", True),
        (
            "https://expired.example/a",
            FetchStatus.NETWORK_ERROR,
            "NameResolutionError: Failed to resolve 'expired.example'",
            True,
        ),
        ("https://www.nytimes.com/2015/a.html", FetchStatus.SKIPPED, "on the skip list", False),
        ("https://blocked.example/a", FetchStatus.BLOCKED, "HTTP 403", False),
        ("https://slow.example/a", FetchStatus.TIMEOUT, "no response within 12s", False),
        ("https://down.example/a", FetchStatus.SERVER_ERROR, "HTTP 503", False),
        (
            "http://jira/browse/SEC-1234",
            FetchStatus.NETWORK_ERROR,
            "Failed to resolve 'jira'",
            False,
        ),
        ("http://wiki.corp/runbooks", FetchStatus.NETWORK_ERROR, "Failed to resolve 'wiki'", False),
        ("http://build.internal/x", FetchStatus.NETWORK_ERROR, "Failed to resolve 'build'", False),
        ("http://203.0.113.9/x", FetchStatus.NOT_FOUND, "HTTP 404", False),
        (f"https://web.archive.org/web/2019/{GONE}", FetchStatus.NOT_FOUND, "HTTP 404", False),
    ],
)
def test_only_a_page_that_is_gone_from_a_public_name_is_looked_up_in_the_archive(
    url, status, error, looked_up
):
    page = Source(1, url, url, "site", status.value, "cited as [1]", snippet_only=True, error=error)
    assert factcheck.archivable(page) is looked_up


def test_a_skipped_page_spends_none_of_the_lookups_a_dead_one_needs(monkeypatch):
    monkeypatch.setattr(factcheck, "ARCHIVE_LIMIT", 1)
    paywalled = "https://www.nytimes.com/2015/08/04/technology/pagespeed.html"
    web = FakeFetcher(
        {
            DEAD[0]: Document(
                url=DEAD[0], status=FetchStatus.SKIPPED, fetched_at=NOW, error="on the skip list"
            ),
            **{
                url: Document(url=url, status=FetchStatus.NOT_FOUND, fetched_at=NOW, error="404")
                for url in DEAD[1:]
            },
        }
    )
    archive = FakeArchive({})
    listed = {
        "claims": [
            claim(f"Station {n} opened in {1900 + n}.", f"Station {n} opened in {1900 + n} [{n}].")
            for n in (1, 2, 3)
        ]
    }
    citer(ScriptedBackend({"claims": [listed]}), fetcher=web, archive=archive).check(
        DEAD_TEXT, cited=True
    )
    assert [url for url, _ in archive.asked] == [DEAD[1]]
    assert paywalled not in str(archive.asked)


def test_a_copy_the_archive_did_not_serve_stops_the_lookups():
    throttled = Document(url="x", status=FetchStatus.BLOCKED, fetched_at=NOW, error="HTTP 429")
    archive = FakeArchive(dict.fromkeys(DEAD, (TAKEN, throttled)))
    result = dead_check(archive)
    assert [url for url, _ in archive.asked] == DEAD[:1]
    assert result.claims[0].archived.problems == (
        "no archived copy looked up for [1]: the Wayback Machine did not answer (HTTP 429)",
    )


def test_a_dead_page_cited_by_a_dated_page_is_looked_up_as_its_author_read_it():
    written = date(2024, 10, 8)
    page = replace(post(f"{TIER_3[:-1]} [1].\n\n[1] {GONE}"), published=written)
    listed = {"claims": [claim(TIER_3, f"{TIER_3[:-1]} [1].")]}
    archive = FakeArchive({})
    citer(
        ScriptedBackend({"claims": [listed]}),
        fetcher=FakeFetcher({POST: page, GONE: PAGES[GONE]}),
        archive=archive,
    ).check(POST, cited=True)
    assert archive.asked == [(GONE, datetime(2024, 10, 8, 23, 59, 59, 999999, tzinfo=UTC))]


def test_a_copy_the_model_could_not_judge_counts_as_no_judgment():
    judges = [JUDGE_RELEASE, JUDGE_GIL, JUDGE_NOTHING, *["not json"] * 4]
    backend = ScriptedBackend({"claims": [LISTED], "judge": judges})
    result = citer(backend, archive=FakeArchive({GONE: (TAKEN, ON_IOS)})).check(ANSWER, cited=True)
    assert cited_label(result.claims[3].archived, result.sources) == "not judged"
    assert result.answer == CITE_RESULT.answer  # no copy was judged


def test_a_lookup_is_told_and_a_long_copy_is_named_by_the_citation_it_stands_for():
    steps: list[str] = []
    judges = [JUDGE_RELEASE, JUDGE_GIL, JUDGE_NOTHING, judged()]
    backend = ScriptedBackend({"claims": [LISTED], "judge": judges})
    archive = FakeArchive({GONE: (TAKEN, " ".join([ON_IOS] * 3000))})
    result = citer(backend, archive=archive).check(ANSWER, cited=True, progress=steps.append)
    assert "looking up [4] on the Wayback Machine" in steps
    assert any(
        re.fullmatch(r"claim 4: the archived copy of \[4\] is [\d,]+ characters; .*", warning)
        for warning in result.warnings
    )


def fixing(monkeypatch, archive: FakeArchive, *, judge=None) -> ScriptedBackend:
    """The command line's model, web and archive, for --fix runs of ANSWER."""
    monkeypatch.setattr("scout.app.make_archive", lambda spec, fetcher: archive)
    monkeypatch.setattr("scout.app.Fetcher", lambda *args, **options: FakeFetcher(PAGES))
    judges = judge or [JUDGE_RELEASE, JUDGE_GIL, JUDGE_NOTHING, judged(("supports", TIER_3, 5))]
    backend = ScriptedBackend({"claims": [LISTED], "judge": judges})
    monkeypatch.setattr("scout.app.make_backend", lambda settings: backend)
    Path("answer.md").write_text(ANSWER, encoding="utf-8", newline="")
    return backend


def test_a_fix_in_place_keeps_what_was_saved_to_the_file_while_the_check_ran(
    workspace, monkeypatch
):
    backend = fixing(monkeypatch, FakeArchive({GONE: (TAKEN, ON_IOS)}))
    added = "\nA paragraph written while Scout was checking.\n"
    complete = backend.complete

    def edited(request):
        if request.purpose == "judge" and added not in Path("answer.md").read_text("utf-8"):
            with Path("answer.md").open("a", encoding="utf-8", newline="") as file:
                file.write(added)
        return complete(request)

    monkeypatch.setattr(backend, "complete", edited)
    args = ["factcheck", "--cited", "--fix", "answer.md", "--no-save", "-f", "answer.md"]
    assert CliRunner().invoke(main, args).exit_code == 0
    now = Path("answer.md").read_text(encoding="utf-8")
    assert (added in now, f"[4] {LINK}" in now, f"[4] {GONE}" in now) == (True, True, False)


def test_a_fix_that_cannot_be_written_keeps_the_check(workspace, monkeypatch):
    fixing(monkeypatch, FakeArchive({GONE: (TAKEN, ON_IOS)}))
    refused = CliRunner().invoke(
        main, ["factcheck", "--cited", "--fix", "answer.md/out.md", "-f", "answer.md"]
    )
    assert (refused.exit_code, "--fix: cannot write answer.md" in refused.output) == (2, True)

    def full(path, text):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr("scout.cli.research.write_atomic", full)
    failed = CliRunner().invoke(
        main, ["factcheck", "--cited", "--fix", "out.md", "-f", "answer.md"]
    )
    assert failed.exit_code == 1
    assert "error: could not write out.md: No space left on device; the check is kept as run 1" in (
        " ".join(failed.output.split())
    )
    with Store(load_settings().db_path) as store:
        assert [run.id for run in store.recent_runs(limit=5)] == [1]


def test_a_fix_says_when_the_archive_did_not_answer(workspace, monkeypatch):
    fixing(
        monkeypatch,
        FakeArchive({}, down="HTTP 504"),
        judge=[JUDGE_RELEASE, JUDGE_GIL, JUDGE_NOTHING],
    )
    done = CliRunner().invoke(main, ["factcheck", "--cited", "--fix", "out.md", "-f", "answer.md"])
    assert done.exit_code == 0, done.output
    assert (
        "Fixed text: out.md (unchanged: the dead cited page was not looked up; run the same "
        "command later)"
    ) in " ".join(done.stderr.split())


def test_a_fix_replaces_a_citation_its_site_now_redirects_home(workspace, monkeypatch):
    archive = FakeArchive({GONE: (TAKEN, ON_IOS)})
    backend = fixing(monkeypatch, archive)
    home = Document(
        url=GONE,
        status=FetchStatus.OK,
        fetched_at=NOW,
        final_url="https://example.org/",
        title="Example Domain",
        text="This domain is for use in illustrative examples in documents. " * 5,
        content_hash="home",
    )
    web = FakeFetcher({**PAGES, GONE: home})
    monkeypatch.setattr("scout.app.Fetcher", lambda *args, **options: web)
    args = ["factcheck", "--cited", "--fix", "out.md", "--json", "-f", "answer.md"]
    done = CliRunner().invoke(main, args)
    assert done.exit_code == 0, done.output

    assert backend.purposes() == ["claims"] + ["judge"] * 4  # the last on the copy
    assert archive.asked == [(GONE, None)]  # the cited address, not the home page
    assert Path("out.md").read_bytes() == ANSWER.replace(f"[4] {GONE}", f"[4] {LINK}").encode()
    data = json.loads(done.stdout)
    moved = "redirects to its site's home page, https://example.org/"
    assert (data["sources"][3]["status"], data["sources"][3]["error"]) == ("not_found", moved)
    assert [(link["why"], link["link"]) for link in data["dead_links"]] == [
        (f"not found: {moved}", LINK)
    ]
