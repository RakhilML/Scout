import re
from urllib.parse import unquote

import pytest

from scout.web.fragments import directive, link

# What a directive may never hold raw: its own syntax, and what ends a link in Markdown or text.
_RAW = re.compile(r"[-,& ()\[\]\n|]")


def placed(text: str, passage: str) -> str | None:
    start = text.index(passage)
    return directive(text, start, start + len(passage))


def terms(found: str) -> list[str]:
    assert found.startswith("text=")
    spelled = found.removeprefix("text=").split(",")
    assert not any(_RAW.search(term) for term in spelled)
    return [unquote(term) for term in spelled]


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://x.org/p", "https://x.org/p#:~:text=a"),
        ("https://x.org/p#sec", "https://x.org/p#sec:~:text=a"),
        ("https://x.org/p#sec:~:text=old", "https://x.org/p#sec:~:text=a"),
        ("http://x.org/p#:~:text=old,older", "http://x.org/p#:~:text=a"),
    ],
)
def test_a_link_opens_the_page_at_the_quote(url, expected):
    assert link(url, "text=a") == expected


@pytest.mark.parametrize("url", ["https://x.org/p", "javascript:alert(1)", "file:///etc/passwd"])
def test_without_a_place_or_a_web_page_a_link_is_the_address(url):
    assert link(url, None) == url
    if not url.startswith("https"):
        assert link(url, "text=a") == url


def test_terms_are_the_pages_characters_percent_encoded():
    page = "Co-founded in 2001, the PSF & caf\N{LATIN SMALL LETTER E WITH ACUTE} met."
    assert (
        placed(page, page)
        == "text=Co%2Dfounded%20in%202001%2C%20the%20PSF%20%26%20caf%C3%A9%20met."
    )


def test_a_short_quote_is_its_own_term_and_a_long_one_is_given_by_its_ends():
    page = "Intro. Python 3.13 ships an experimental JIT. The end."
    assert placed(page, "Python 3.13 ships an experimental JIT.") == (
        "text=Python%203.13%20ships%20an%20experimental%20JIT."
    )
    words = " ".join(f"w{n}" for n in range(20))
    assert (
        placed(f"Before. {words} After.", words) == "text=w0%20w1%20w2%20w3,w16%20w17%20w18%20w19"
    )
    assert directive("   \n  ", 0, 6) is None


def test_terms_grow_until_their_first_match_is_the_quotes_own():
    span = (
        "Alpha beta gamma delta epsilon eta theta iota kappa lambda mu nu one two three four "
        "xi one two three four"
    )
    page = f"alpha beta gamma delta epsilon zeta stopped here.\n{span}\nThe end."
    # Its first five words are on the page before it, in another case; its last four are in
    # it before its end.
    assert terms(placed(page, span)) == [
        "Alpha beta gamma delta epsilon eta",
        "xi one two three four",
    ]


def test_a_quote_over_list_items_or_table_cells_never_spans_one_in_a_term():
    page = "Results.\n- Revenue grew in every quarter\n- Costs fell by a third\nThe end."
    found = placed(page, "- Revenue grew in every quarter\n- Costs fell by a third")
    assert terms(found) == ["Revenue grew in every", "fell by a third"]

    table = "| Name | Value |\n|---|---|\n| Revenue | $3.9 million[2] (2022) |\n| Staff | 12 |"
    found = placed(table, "| Revenue | $3.9 million[2] (2022) |")
    assert terms(found) == ["Revenue", "$3.9 million[2] (2022)"]
    assert "%7C" not in found


def test_a_start_said_earlier_on_the_page_is_told_apart_by_the_words_before_it():
    page = (
        "Visitors reach the 2nd floor at 116 m by lift.\n"
        "| Floor | Height | Access |\n|---|---|---|\n"
        "| 1st floor | 57 m | Lift or stairs. |\n"
        "| 2nd floor | 116 m | Lift, or 674 steps on foot from the ground |"
    )
    found = placed(page, "| 2nd floor | 116 m | Lift, or 674 steps on foot from the ground |")
    prefix, rest = found.split("-,", 1)
    assert unquote(prefix) == "text=stairs."  # across a row, as browsers read a prefix
    assert terms(f"text={rest}") == ["2nd floor", "foot from the ground"]

    items = "Python is a language.\nMore words.\n- Python\n- Ruby is also a language people use."
    found = placed(items, "- Python\n- Ruby is also a language people use.")
    assert found.startswith("text=words.-,Python,")


def test_a_label_joined_to_its_value_on_one_line_is_a_block_of_its_own():
    page = "The release went well.\n- Released: October 7, 2024\n- Size: 30 MB"
    for passage in ("- Released: October 7, 2024", "Released: October 7, 2024"):
        assert terms(placed(page, passage)) == ["Released:", "October 7, 2024"]


def test_a_term_too_long_to_link_gives_no_directive():
    paragraph = "\N{CJK UNIFIED IDEOGRAPH-4E00}" * 200
    assert directive(paragraph, 0, len(paragraph)) is None
