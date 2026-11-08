"""Nothing that loads the local model starts below 12 GB of free physical memory and commit space.

Every check here uses made-up readings and stand-ins that fail the test if the model is reached,
so no model is ever loaded to test it.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

import repomind.evaluation.memory as memory
from repomind.cli import main
from repomind.evaluation.memory import START_MINIMUM_GB, MemoryReading, check_before_loading

TOOLS = Path(__file__).resolve().parents[1] / "eval" / "tools"
PLENTY = MemoryReading(20.0, 20.0)


def readings(*values: MemoryReading):
    """A stand-in for reading memory that returns these, then keeps returning the last one."""
    queue = list(values)
    return lambda: queue.pop(0) if len(queue) > 1 else queue[0]


def no_model(*_args, **_kwargs):
    raise AssertionError("the model was about to be used")


class TestTheCheck:
    def test_the_limit_is_twelve_gb(self):
        assert START_MINIMUM_GB == 12.0

    def test_it_allows_a_start_when_every_reading_clears_both_limits(self):
        check = check_before_loading(read=readings(MemoryReading(12.0, 12.0), MemoryReading(15.0, 30.0)), sleep=lambda _s: None)
        assert check.refusal is None
        assert len(check.readings) == 2

    @pytest.mark.parametrize(
        "first, second, refused_for",
        [
            (MemoryReading(9.7, 18.0), PLENTY, "physical memory is 9.70 GB"),
            (MemoryReading(18.0, 11.9), PLENTY, "commit space is 11.90 GB"),
            (PLENTY, MemoryReading(11.99, 20.0), "physical memory is 11.99 GB"),
            (MemoryReading(None, 20.0), PLENTY, "physical memory cannot be read"),
            (PLENTY, MemoryReading(20.0, None), "commit space cannot be read"),
        ],
    )
    def test_one_low_or_unreadable_value_in_any_reading_refuses(self, first, second, refused_for):
        check = check_before_loading(read=readings(first, second), sleep=lambda _s: None)
        assert refused_for in check.refusal

    def test_the_readings_are_a_moment_apart_and_described(self):
        pauses = []
        check = check_before_loading(read=readings(MemoryReading(9.71, 17.33), MemoryReading(9.6, 17.0)), sleep=pauses.append)
        assert pauses == [1.0]
        assert check.describe() == "physical 9.71 GB, commit 17.33 GB; physical 9.60 GB, commit 17.00 GB"

    def test_the_real_reading_has_both_values_on_windows(self):
        reading = memory.read_memory()
        assert reading.physical_gb is None or reading.physical_gb > 0
        if sys.platform == "win32":
            assert reading.commit_gb > 0


@pytest.fixture
def low_memory(monkeypatch):
    """What this machine had when a trial ran that should not have: plenty of commit space, too little RAM."""
    monkeypatch.setattr(memory, "read_memory", lambda: MemoryReading(9.7, 17.3))
    monkeypatch.setattr(memory.time, "sleep", lambda _s: None)


def load_tool(monkeypatch, name: str):
    monkeypatch.setattr(sys, "path", list(sys.path))  # The tools add their own folders; undo that afterwards.
    spec = importlib.util.spec_from_file_location(f"{name}_under_test", TOOLS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestEveryWayOfLoadingTheModel:
    def test_the_selection_trial(self, monkeypatch, tmp_path, low_memory, capsys):
        tool = load_tool(monkeypatch, "selection_trial")
        monkeypatch.setattr(tool, "load_llm_config", no_model)
        monkeypatch.setattr(tool, "OpenAICompatibleClient", no_model)
        monkeypatch.setattr(sys, "argv", ["selection_trial.py", "--output", str(tmp_path / "rows.json")])
        assert tool.main() == 3
        out = capsys.readouterr().out
        assert "physical 9.70 GB, commit 17.30 GB" in out and "Not started: free physical memory is 9.70 GB" in out
        assert not (tmp_path / "rows.json").exists()

    def test_the_selection_trial_checks_again_before_continuing_a_file(self, monkeypatch, tmp_path, low_memory):
        tool = load_tool(monkeypatch, "selection_trial")
        monkeypatch.setattr(tool, "load_llm_config", no_model)
        partial = tmp_path / "rows.json"
        partial.write_text('{"rows": []}', encoding="utf-8")
        monkeypatch.setattr(sys, "argv", ["selection_trial.py", "--output", str(partial)])
        assert tool.main() == 3

    @pytest.mark.parametrize("name", ["evidence_trial", "translation_trial"])
    def test_the_earlier_trials(self, monkeypatch, tmp_path, low_memory, name, capsys):
        tool = load_tool(monkeypatch, name)
        import repomind.qa.llm as llm

        monkeypatch.setattr(llm, "load_llm_config", no_model)
        monkeypatch.setattr(llm, "OpenAICompatibleClient", no_model)
        arguments = (tmp_path / "out.json", 4.0) if name == "evidence_trial" else ("baseline", tmp_path / "out.json", 4.0)
        assert tool.run(*arguments) == 3
        assert "Not started" in capsys.readouterr().out

    def test_the_answer_evaluation_command(self, monkeypatch, tmp_path, low_memory, capsys):
        import repomind.cli as cli

        monkeypatch.setenv("REPOMIND_LLM_BASE_URL", "http://127.0.0.1:11535/v1")
        monkeypatch.setenv("REPOMIND_LLM_MODEL", "qwen2.5:7b")
        monkeypatch.setattr(cli, "_store", no_model)
        monkeypatch.setattr(cli, "_qa_engine", no_model)
        assert main(["evaluate-ask", "--output", str(tmp_path / "out.json")]) == 2
        err = capsys.readouterr().err
        assert "Not starting: free physical memory is 9.70 GB" in err

    def test_the_answer_evaluation_without_a_model_does_not_need_it(self, monkeypatch, tmp_path, low_memory):
        import repomind.cli as cli

        class Reached(Exception):
            pass

        def reached(*_args, **_kwargs):
            raise Reached

        monkeypatch.setattr(cli, "_store", reached)
        with pytest.raises(Reached):
            main(["evaluate-ask", "--no-llm", "--output", str(tmp_path / "out.json")])

    def test_the_real_model_test_is_skipped_unless_asked_for(self, monkeypatch):
        from tests import test_ask_evaluation as real

        monkeypatch.delenv(real.RUN_LLM_TESTS, raising=False)
        monkeypatch.setattr(real, "load_llm_config", no_model)
        monkeypatch.setattr(real.urllib.request, "urlopen", no_model)
        with pytest.raises(pytest.skip.Exception, match="Set REPOMIND_RUN_LLM_TESTS=1"):
            real._running_llm()

    def test_the_real_model_test_is_skipped_below_the_limit_even_when_asked_for(self, monkeypatch, low_memory):
        from tests import test_ask_evaluation as real

        monkeypatch.setenv(real.RUN_LLM_TESTS, "1")
        monkeypatch.setattr(real, "load_llm_config", no_model)
        monkeypatch.setattr(real.urllib.request, "urlopen", no_model)
        with pytest.raises(pytest.skip.Exception, match="free physical memory is 9.70 GB"):
            real._running_llm()


def test_the_trial_summary_keeps_the_negative_stock_question_out_of_the_totals(monkeypatch):
    tool = load_tool(monkeypatch, "selection_trial")
    row = lambda i, s, outcome, negative=False, offered=("a.java:1-2",), chosen=(): {  # noqa: E731
        "id": i, "selection": s, "negative": negative, "outcome": outcome, "gold_offered": True,
        "offered": list(offered), "chosen": list(chosen), "seconds": 2.0 if s == "choose" else 6.0,
        "requests": 1 if s == "choose" else 5, "input_tokens": 1000, "output_tokens": 20,
    }
    stock = ["src/main/java/com/stockguard/simulation/SimulationService.java:196-200", "src/main/resources/db/migration/V2__catalog_and_stock.sql:22-26"]
    rows = [
        row("q1", "choose", "correct"), row("q1", "judge", "none"),
        row("n1", "choose", "wrong", True), row("n1", "judge", "none", True, offered=("b.java:1-2",)),
        row("manual-negative-stock", "choose", "wrong", offered=stock, chosen=stock[:1]),
        row("manual-negative-stock", "judge", "correct", offered=stock, chosen=stock[1:]),
        row("only-one-way", "choose", "correct"),
    ]
    report = tool.summarise(rows)
    assert report["questions_compared"] == 2
    assert report["passages_differ"] == ["n1"]
    assert (report["choose"]["correct"], report["choose"]["unanswerable_given_a_source"], report["choose"]["wrong_in_all"]) == (1, 1, 1)
    assert (report["judge"]["correct"], report["judge"]["refused_with_right_lines_offered"], report["judge"]["wrong_in_all"]) == (0, 1, 0)
    assert (report["choose"]["seconds_mean"], report["judge"]["requests_mean"]) == (2.0, 5)
    facts = report["negative_stock"]
    assert facts["choose"]["chose_simulation_service"] and not facts["choose"]["chose_check_constraint"]
    assert facts["judge"]["chose_check_constraint"] and not facts["judge"]["offered_repository_guard"]
