from scout.textutil import clean, fold, junk_ratio, looks_like_junk, shorten

NNBSP = "\N{NARROW NO-BREAK SPACE}"
NBSP = "\N{NO-BREAK SPACE}"
NB_HYPHEN = "\N{NON-BREAKING HYPHEN}"
APOSTROPHE = "\N{RIGHT SINGLE QUOTATION MARK}"
LQUOTE, RQUOTE = "\N{LEFT DOUBLE QUOTATION MARK}", "\N{RIGHT DOUBLE QUOTATION MARK}"
EM_DASH = "\N{EM DASH}"
BAD = "\N{REPLACEMENT CHARACTER}"


def test_clean_replaces_lookalike_spaces_and_hyphens():
    # The saved reports wrote "RTX 4090" with a narrow no-break space, which defeated search.
    text = f"RTX{NNBSP}4090 and open{NB_HYPHEN}source{NBSP}models"
    assert clean(text) == "RTX 4090 and open-source models"


def test_clean_drops_invisible_characters_and_tidies_whitespace():
    assert clean("  a\N{ZERO WIDTH SPACE}b \t c\r\n\n\n\nd  ") == "ab c\n\nd"


def test_clean_keeps_typographic_punctuation():
    text = f"It{APOSTROPHE}s {LQUOTE}fast{RQUOTE} {EM_DASH} really"
    assert clean(text) == text


def test_fold_makes_equivalent_spellings_equal():
    fancy = f"It{APOSTROPHE}s  {LQUOTE}FAST{RQUOTE}{EM_DASH}open{NB_HYPHEN}source"
    assert fold(fancy) == fold('it\'s "fast"-open-source')


def test_junk_ratio_flags_decoded_binary():
    assert junk_ratio("") == 0.0
    assert junk_ratio("plain text\nwith lines\tand tabs") == 0.0
    assert looks_like_junk(f"[&{BAD}Se{BAD}{BAD}zxDe{BAD}9d {BAD}{BAD}\x00\x01\x02\x7f\x85")
    assert not looks_like_junk(
        f"A normal sentence with one odd char {BAD} in a long paragraph " * 5
    )


def test_shorten_prefers_word_boundaries():
    assert shorten("short", 10) == "short"
    assert shorten("the quick brown fox jumps", 16) == "the quick brown\N{HORIZONTAL ELLIPSIS}"
    assert len(shorten("x" * 50, 10)) == 10


def test_clean_drops_control_characters_a_terminal_would_run():
    assert clean("before\x1b[2Jafter\x07 and\ttab\nline") == "before[2Jafter and tab\nline"
