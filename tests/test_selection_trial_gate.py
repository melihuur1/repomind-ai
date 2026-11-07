"""The 7B trial must not start below its memory limit. Checked with stand-ins, so no model is ever loaded."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

TOOL = Path(__file__).resolve().parents[1] / "eval" / "tools" / "selection_trial.py"


@pytest.fixture
def tool(monkeypatch):
    spec = importlib.util.spec_from_file_location("selection_trial", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)

    def no_model(*_args, **_kwargs):
        raise AssertionError("the model was about to be used")

    monkeypatch.setattr(module, "load_llm_config", no_model)
    monkeypatch.setattr(module, "OpenAICompatibleClient", no_model)
    return module


@pytest.mark.parametrize("readings", [[9.7, 9.7], [12.5, 11.9], [11.9, 12.5], [None, 13.0], [13.0, None]])
def test_it_does_not_start_unless_both_readings_clear_the_limit(tool, monkeypatch, tmp_path, readings, capsys):
    values = iter(readings)
    monkeypatch.setattr(tool, "available_memory_gb", lambda: next(values))
    monkeypatch.setattr(sys, "argv", ["selection_trial.py", "--output", str(tmp_path / "rows.json")])
    assert tool.main() == 3
    assert "Not started" in capsys.readouterr().out
    assert not (tmp_path / "rows.json").exists()


def test_it_says_what_it_measured(tool, monkeypatch, tmp_path, capsys):
    values = iter([9.7, 9.6])
    monkeypatch.setattr(tool, "available_memory_gb", lambda: next(values))
    monkeypatch.setattr(sys, "argv", ["selection_trial.py", "--output", str(tmp_path / "rows.json")])
    tool.main()
    assert "9.70 GB, 9.60 GB" in capsys.readouterr().out
