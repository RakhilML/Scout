from __future__ import annotations

import pytest

from scout.evaluate import EvalCase, EvalScore, load_scores, save_scores
from scout.research.results import Plan, Source

PLAN = Plan(queries=("python 3.13",), kind="release", recency=None, planner="model")
SOURCE = Source(1, "https://docs.example/3.13", "What's new", "docs.example", "ok", "q", text="t")


def scored(case: str) -> EvalScore:
    return EvalScore(case, "m", 2, 1, ("October 7, 2024",), (), (), "medium", 1.0)


def test_case_and_score_files_are_written_whole_or_not_at_all(tmp_path):
    case = EvalCase(name="py313", goal="What is new?", plan=PLAN, sources=(SOURCE,))
    case_file, scores_file = tmp_path / "cases" / "py313.json", tmp_path / "base.json"
    case.save(case_file)
    save_scores([scored("py313")], scores_file)
    before = case_file.read_bytes(), scores_file.read_bytes()
    assert b"\r\n" not in before[0] + before[1]  # the same files on every system
    assert EvalCase.load(case_file) == case
    assert load_scores(scores_file) == {"py313": scored("py313")}

    # Text that cannot be UTF-8 makes the write fail part way.
    with pytest.raises(UnicodeEncodeError):
        EvalCase(name="py313", goal="half \ud800", plan=PLAN, sources=(SOURCE,)).save(case_file)
    with pytest.raises(UnicodeEncodeError):
        save_scores([scored("half \ud800")], scores_file)
    assert (case_file.read_bytes(), scores_file.read_bytes()) == before
