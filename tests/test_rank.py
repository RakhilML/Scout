from scout.research.rank import BM25, coverage, pack, split_chunks, tokenize


def test_tokenize_reads_every_script():
    assert tokenize("Grafikkarte g\N{LATIN SMALL LETTER U WITH DIAERESIS}nstig kaufen") == [
        "grafikkarte",
        "g\N{LATIN SMALL LETTER U WITH DIAERESIS}nstig",
        "kaufen",
    ]
    # "keemat" (price): a vowel sign is a combining mark, and it stays inside the word
    hindi = (
        "\N{DEVANAGARI LETTER KA}\N{DEVANAGARI VOWEL SIGN II}"
        "\N{DEVANAGARI LETTER MA}\N{DEVANAGARI LETTER TA}"
    )
    assert tokenize(f"RTX 5090 {hindi}") == ["rtx", "5090", hindi]
    assert tokenize("RTX 5090 \N{CJK UNIFIED IDEOGRAPH-4EF7}") == [
        "rtx",
        "5090",
        "\N{CJK UNIFIED IDEOGRAPH-4EF7}",
    ]
    assert (
        coverage(
            "g\N{LATIN SMALL LETTER U WITH DIAERESIS}nstige Grafikkarte",
            [
                "Grafikkarte g\N{LATIN SMALL LETTER U WITH DIAERESIS}nstige Angebote",
                "Gartenschlauch",
            ],
        )[1]
        == 0
    )


def test_tokenize_folds_and_drops_stopwords():
    assert tokenize("What is the RTX\N{NARROW NO-BREAK SPACE}5090's price in 2025?") == [
        "rtx",
        "5090",
        "price",
        "2025",
    ]
    assert tokenize("Python 3.13 vs C++ and C#") == ["python", "3.13", "vs", "c++", "c#"]


def test_split_chunks_respects_paragraphs_and_size():
    text = "\n\n".join(f"Paragraph {i}. " + "word " * 40 for i in range(6))
    chunks = split_chunks(text, target=500)
    assert all(len(chunk) <= 500 for chunk in chunks)
    assert "".join(chunks).replace("\n", "").replace(" ", "") == text.replace("\n", "").replace(
        " ", ""
    )
    assert chunks[0].startswith("Paragraph 0.")


def test_split_chunks_breaks_long_paragraphs_by_sentence_or_hard_cut():
    long_sentence_paragraph = "One sentence here. " * 40 + "x" * 1200
    chunks = split_chunks(long_sentence_paragraph, target=300)
    assert all(len(chunk) <= 300 for chunk in chunks)
    assert chunks[0].startswith("One sentence here.")


def test_bm25_prefers_documents_with_rare_matching_terms():
    docs = [
        tokenize(t) for t in ["python release notes", "python tutorial", "jit compiler in python"]
    ]
    ranker = BM25(docs)
    query = tokenize("python jit")
    assert max(range(3), key=lambda i: ranker.score(query, i)) == 2


def test_pack_gives_every_source_its_best_chunk_then_fills_by_relevance():
    relevant = "The RTX 5090 costs $1,999 at launch. " * 10
    filler = "Unrelated gardening advice about tomatoes. " * 10
    texts = [(1, "\n\n".join([filler, relevant, filler])), (2, filler), (3, relevant)]
    packed = pack(texts, "RTX 5090 price", budget=2000, per_source=10_000)
    assert set(packed) == {1, 2, 3}  # even the useless source keeps one chunk
    assert any("RTX 5090" in chunk for chunk in packed[1])
    assert sum(len(chunk) for chunks in packed.values() for chunk in chunks) <= 2000


def test_pack_drops_the_least_relevant_sources_when_the_budget_is_tight():
    relevant = "The RTX 5090 costs $1,999 at launch. " * 10
    filler = "Unrelated gardening advice about tomatoes. " * 10
    packed = pack(
        [(1, filler), (2, relevant), (3, relevant + " More.")],
        "RTX 5090",
        budget=800,
        per_source=10_000,
    )
    assert set(packed) == {2, 3}


def test_pack_respects_per_source_cap_and_keeps_page_order():
    body = "\n\n".join(f"Section {i}: RTX 5090 benchmark numbers. " * 5 for i in range(8))
    packed = pack([(1, body)], "RTX 5090", budget=10_000, per_source=800)
    assert sum(len(chunk) for chunk in packed[1]) <= 800
    numbers = [int(chunk.split(":")[0].split()[-1]) for chunk in packed[1]]
    assert numbers == sorted(numbers)


def test_pack_of_nothing_is_empty():
    assert pack([], "anything", budget=100, per_source=100) == {}


def test_coverage_penalizes_missing_rare_terms():
    goal = "latest developments in open source LLMs"
    texts = [
        "Best open-source LLMs in 2026: Llama, Qwen, DeepSeek",
        "Best free and open source LMS platforms for teachers",
        "Open source software news",
    ]
    scores = coverage(goal, texts)
    assert scores[0] > scores[1]
    assert scores[0] > scores[2]
    assert all(0.0 <= score <= 1.0 for score in scores)
    assert coverage("", texts) == [0.0, 0.0, 0.0]
