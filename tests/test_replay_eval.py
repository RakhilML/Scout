import json

import pytest
from click.testing import CliRunner

from scout import app as app_module
from scout.app import App
from scout.cli import main
from scout.errors import ScoutError
from scout.evaluate import EvalCase, RecordedReplies, case_files, score
from scout.llm.base import CompletionRequest, Message
from scout.research.results import Confidence, Finding, Plan, RunResult, Source, Verdict
from scout.settings import Settings
from scout.web.fetch import Document, FetchStatus
from tests.helpers import NOW, ScriptedBackend

PAGE_TEXT = "Python 3.13 was released on October 7, 2024. It adds an experimental JIT (PEP 744)."
PLAN = Plan(queries=("python 3.13",), kind="release", recency=None, planner="model")
EXTRACTION = {
    "answer": "Python 3.13 came out on October 7, 2024.",
    "findings": [
        {
            "claim": "Released October 7, 2024",
            "quote": "Python 3.13 was released on October 7, 2024.",
            "source": 1,
        },
        {"claim": "It removed the GIL", "quote": "The GIL is gone for good.", "source": 1},
    ],
}


def stored_run(store) -> int:
    doc = Document(
        url="https://docs.example/3.13",
        status=FetchStatus.OK,
        fetched_at=NOW,
        text=PAGE_TEXT,
        content_hash="abc123",
    )
    store.put_page(doc)
    sources = (
        Source(1, doc.url, "What's new", "docs.example", "ok", "q", content_hash="abc123"),
        Source(
            2, "https://gone.example", "Gone", "gone.example", "ok", "q", content_hash="missing"
        ),
        Source(
            3,
            "https://blocked.example",
            "B",
            "blocked.example",
            "blocked",
            "q",
            snippet_only=True,
            text="3.13 snippet",
        ),
    )
    result = RunResult(
        goal="What is new in Python 3.13?",
        started_at=NOW,
        finished_at=NOW,
        model="old-model",
        plan=PLAN,
        sources=sources,
        answer="old answer",
        findings=(),
        confidence=Confidence("low", "no findings"),
    )
    return store.add_run(result)


@pytest.fixture
def scripted(monkeypatch):
    backend = ScriptedBackend({"extract": [EXTRACTION] * 3})
    monkeypatch.setattr(app_module, "make_backend", lambda settings: backend)
    return backend


@pytest.fixture
def settings(tmp_path):
    return Settings(data_dir=tmp_path / "data", reports_dir=tmp_path / "reports")


def test_replay_reanalyzes_the_exact_pages_a_run_read(settings, scripted):
    with App(settings) as app:
        run_id = stored_run(app.store)
        before, after = app.replay(run_id)

    assert before.model == "old-model"
    assert after.model == "scripted"
    assert scripted.purposes() == ["extract"]  # no planning, searching or fetching
    prompt = scripted.requests[0].messages[1].content
    assert PAGE_TEXT in prompt
    assert "3.13 snippet" in prompt
    assert [f.verdict for f in after.findings] == [Verdict.VERIFIED, Verdict.UNVERIFIED]
    assert after.warnings[0] == f"replay of run {run_id}; 1 page(s) of it are no longer stored"
    assert after.sources[1].snippet_only


def test_replay_of_an_unknown_run_is_an_error(settings, scripted):
    with App(settings) as app, pytest.raises(ScoutError, match="no run with id 5"):
        app.replay(5)


def case(**changes):
    source = Source(
        1, "https://docs.example/3.13", "What's new", "docs.example", "ok", "q", text=PAGE_TEXT
    )
    fields = {
        "name": "py313",
        "goal": "What is new in Python 3.13?",
        "plan": PLAN,
        "sources": (source,),
        "expect_facts": ("October 7, 2024", "free-threaded"),
        "expect_untrusted": ("removed the GIL",),
    }
    return EvalCase(**(fields | changes))


def test_case_files_round_trip_with_their_text(tmp_path):
    original = case(replies={"extract": (json.dumps(EXTRACTION),)})
    original.save(tmp_path / "py313.json")
    assert EvalCase.load(tmp_path / "py313.json") == original


def test_unreadable_case_is_a_clear_error(tmp_path):
    (tmp_path / "bad.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ScoutError, match="not a readable eval case"):
        EvalCase.load(tmp_path / "bad.json")


def test_score_counts_expected_facts_and_violations():
    trusted = Finding(
        "Released October 7, 2024",
        "Python 3.13 was released on October 7, 2024.",
        1,
        Verdict.VERIFIED,
    )
    wrong = Finding("It removed the GIL", "gone", 1, Verdict.VERIFIED)
    ignored = Finding("free-threaded builds", "x", 1, Verdict.UNVERIFIED)
    result = RunResult(
        goal="g",
        started_at=NOW,
        finished_at=NOW,
        model="m",
        plan=PLAN,
        sources=(),
        answer="",
        findings=(trusted, wrong, ignored),
        confidence=Confidence("medium", ""),
    )
    scored = score(case(), result)
    assert (scored.findings, scored.trusted) == (3, 2)
    assert scored.facts_found == ("October 7, 2024",)
    assert scored.facts_missed == ("free-threaded",)  # only unverified findings mention it
    assert scored.violations == ("removed the GIL",)


def test_recorded_replies_answer_by_step():
    backend = RecordedReplies(case(replies={"extract": ("reply",)}))
    request = CompletionRequest(messages=(Message("user", "x"),), purpose="extract")
    assert backend.complete(request).text == "reply"
    with pytest.raises(ScoutError, match="no recorded reply for the extract step"):
        backend.complete(request)


def test_case_files_expands_folders(tmp_path):
    (tmp_path / "b.json").write_text("{}", encoding="utf-8")
    (tmp_path / "a.json").write_text("{}", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("", encoding="utf-8")
    single = tmp_path / "a.json"
    assert case_files([tmp_path, single]) == [tmp_path / "a.json", tmp_path / "b.json", single]


def test_evaluate_with_recorded_replies_needs_no_model(settings, monkeypatch):
    monkeypatch.setattr(app_module, "make_backend", lambda settings: pytest.fail("no model needed"))
    with App(settings) as app:
        result, scored = app.evaluate(
            case(replies={"extract": (json.dumps(EXTRACTION),)}), recorded=True
        )
    assert scored.facts_found == ("October 7, 2024",)
    assert scored.violations == ()  # the GIL claim's quote is invented, so it is not trusted
    assert result.warnings == ("eval py313",)


def test_export_then_eval_through_the_cli(tmp_path, monkeypatch, scripted):
    env_file = tmp_path / "scout.env"
    env_file.write_text(f"SCOUT_DATA_DIR={tmp_path / 'data'}\n", encoding="utf-8")
    monkeypatch.setenv("SCOUT_ENV_FILE", str(env_file))
    monkeypatch.chdir(tmp_path)
    with App(Settings(data_dir=tmp_path / "data")) as app:
        run_id = stored_run(app.store)
        app.store.put_page(
            Document(
                url="https://gone.example",
                status=FetchStatus.OK,
                fetched_at=NOW,
                text="back",
                content_hash="missing",
            )
        )

    runner = CliRunner()
    exported = runner.invoke(
        main, ["eval", "export", str(run_id), str(tmp_path / "cases" / "py.json")]
    )
    assert exported.exit_code == 0, exported.output
    data = json.loads((tmp_path / "cases" / "py.json").read_text(encoding="utf-8"))
    data["expect"] = {"facts": ["October 7, 2024"], "untrusted": ["removed the GIL"]}
    (tmp_path / "cases" / "py.json").write_text(json.dumps(data), encoding="utf-8")

    evaluated = runner.invoke(main, ["eval", "run", str(tmp_path / "cases"), "--strict"])
    assert evaluated.exit_code == 0, evaluated.output
    assert "1/1" in evaluated.output

    replayed = runner.invoke(main, ["replay", str(run_id), "--no-save"])
    assert replayed.exit_code == 0, replayed.output
    assert "old-model" in replayed.output


def test_strict_eval_fails_on_missed_facts(tmp_path, monkeypatch):
    env_file = tmp_path / "scout.env"
    env_file.write_text(f"SCOUT_DATA_DIR={tmp_path / 'data'}\n", encoding="utf-8")
    monkeypatch.setenv("SCOUT_ENV_FILE", str(env_file))
    case(replies={"extract": (json.dumps(EXTRACTION),)}).save(tmp_path / "py.json")
    result = CliRunner().invoke(
        main, ["eval", "run", str(tmp_path / "py.json"), "--recorded", "--strict"]
    )
    assert result.exit_code == 1  # "free-threaded" is expected but no trusted finding has it
    assert "1/2" in result.output


def test_a_baseline_gates_changes_that_make_things_worse(tmp_path, monkeypatch):
    env_file = tmp_path / "scout.env"
    env_file.write_text(f"SCOUT_DATA_DIR={tmp_path / 'data'}\n", encoding="utf-8")
    monkeypatch.setenv("SCOUT_ENV_FILE", str(env_file))
    cases, baseline = tmp_path / "cases", tmp_path / "baseline.json"
    case(replies={"extract": (json.dumps(EXTRACTION),)}).save(cases / "py.json")
    runner = CliRunner()
    saved = runner.invoke(main, ["eval", "run", str(cases), "--recorded", "--save", str(baseline)])
    assert saved.exit_code == 0, saved.output
    assert json.loads(baseline.read_text(encoding="utf-8"))["py313"]["facts_found"] == [
        "October 7, 2024"
    ]
    same = runner.invoke(
        main, ["eval", "run", str(cases), "--recorded", "--against", str(baseline)]
    )
    assert same.exit_code == 0
    assert "Nothing got worse" in same.output

    # A worse model (or prompt): it no longer quotes the release date.
    worse = dict(EXTRACTION, findings=EXTRACTION["findings"][1:])
    case(replies={"extract": (json.dumps(worse),)}).save(cases / "py.json")
    gated = runner.invoke(
        main, ["eval", "run", str(cases), "--recorded", "--against", str(baseline)]
    )
    assert gated.exit_code == 1
    assert "py313: no longer finds October 7, 2024" in gated.output
