"""Tier A site tests: cron monitor, goal judge, kanban estimator (Task 6).

None of these three sites import `agent.jev_decide.decide` yet (that lands in
later tasks) — every test here is expected to fail until then. `decide` is
patched at its defining module (`agent.jev_decide`); `call_llm` is patched at
its defining module (`agent.auxiliary_client`) — both sites are expected to
import each lazily inside their function, the pattern already used across
this repo's `call_llm` tests (see `tests/plugins/test_kanban_estimate.py`,
`tests/hermes_cli/test_goals.py`).

Bucket tables (`_WAIT_BUCKETS`, `_TOKEN_BUCKETS`) below are duplicated from
`docs/plans/2026-09-24-jev-decision-layers.md` Tasks 9 and 10, which define
them as the call sites' own module-level constants (not yet created). This
file pins that contract; it does not invent it.

Forbidden: no site file is edited here, and no assertion inspects prompt
wording — only the data (item selection, scores, legend text) that flows
through it.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import cron.scripts.classify_items as classify_items
import hermes_cli.goals as goals
from agent.jev_client import JevAnswer
from agent.jev_decide import Decision

# docs/plans/2026-09-24-jev-decision-layers.md Task 9 step 1.
_WAIT_BUCKETS = [5, 15, 30, 60, 300]
# docs/plans/2026-09-24-jev-decision-layers.md Task 10 step 1.
_TOKEN_BUCKETS = [8000, 25000, 50000, 100000, 200000, 400000]


def _score_answer(name, *, score, confidence, legend):
    return JevAnswer(name=name, type="score", score=score, confidence=confidence, legend=legend)


def _choice_answer(name, *, choice, confidence):
    return JevAnswer(name=name, type="choice", choice=choice, confidence=confidence)


def _llm_response(content):
    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = content
    return resp


# ──────────────────────────────────────────────────────────────────────
# Cron urgency monitor (cron/scripts/classify_items.py)
# ──────────────────────────────────────────────────────────────────────


def _run_classify_main(monkeypatch, argv):
    monkeypatch.setattr(sys, "argv", ["classify_items.py"] + argv)
    return classify_items.main()


def test_monitor_threshold_legend_reason_and_subthreshold_fallback(monkeypatch, tmp_path, capsys):
    items = [
        {"title": "Definitely urgent", "id": "urgent-1"},
        {"title": "Definitely quiet", "id": "quiet-1"},
        {"title": "Needs fallback", "id": "fallback-1"},
    ]
    answers = {
        "item_0": _score_answer("item_0", score=9.0, confidence=0.9, legend={"9": "surface to the user"}),
        "item_1": _score_answer("item_1", score=2.0, confidence=0.9, legend={"2": "very low"}),
        "item_2": _score_answer("item_2", score=8.0, confidence=0.1, legend={"8": "important"}),
    }
    decision = Decision(ok=True, answers=answers, status="ok", min_confidence=0.6)
    mock_decide = MagicMock(return_value=decision)
    mock_call_llm = MagicMock(
        return_value=_llm_response('[{"index": 0, "score": 8, "reason": "fallback reason"}]')
    )
    monkeypatch.setattr("agent.jev_decide.decide", mock_decide)
    monkeypatch.setattr("agent.auxiliary_client.call_llm", mock_call_llm)

    input_file = tmp_path / "items.json"
    input_file.write_text(json.dumps(items))

    _run_classify_main(
        monkeypatch,
        ["--criteria", "test criteria", "--threshold", "7", "--input-file", str(input_file), "--format", "json"],
    )
    out = json.loads(capsys.readouterr().out)

    mock_decide.assert_called_once()

    surfaced_ids = {entry["id"] for entry in out}
    assert surfaced_ids == {"urgent-1", "fallback-1"}

    urgent_entry = next(e for e in out if e["id"] == "urgent-1")
    assert urgent_entry["score"] == 9
    assert urgent_entry["reason"] == "surface to the user"

    fallback_entry = next(e for e in out if e["id"] == "fallback-1")
    assert fallback_entry["score"] == 8
    assert fallback_entry["reason"] == "fallback reason"

    mock_call_llm.assert_called_once()
    fallback_text = json.dumps(mock_call_llm.call_args.kwargs["messages"])
    assert "Needs fallback" in fallback_text
    assert "Definitely urgent" not in fallback_text
    assert "Definitely quiet" not in fallback_text


def test_monitor_disabled_falls_back_to_call_llm_with_every_item(monkeypatch, tmp_path, capsys):
    items = [
        {"title": "Item A", "id": "a"},
        {"title": "Item B", "id": "b"},
    ]
    decision = Decision(ok=False, answers={}, status="disabled", min_confidence=0.6)
    mock_decide = MagicMock(return_value=decision)
    mock_call_llm = MagicMock(
        return_value=_llm_response(
            '[{"index": 0, "score": 8, "reason": "urgent enough"}, {"index": 1, "score": 3, "reason": "quiet"}]'
        )
    )
    monkeypatch.setattr("agent.jev_decide.decide", mock_decide)
    monkeypatch.setattr("agent.auxiliary_client.call_llm", mock_call_llm)

    input_file = tmp_path / "items.json"
    input_file.write_text(json.dumps(items))

    _run_classify_main(
        monkeypatch,
        ["--criteria", "test criteria", "--threshold", "7", "--input-file", str(input_file), "--format", "json"],
    )
    out = json.loads(capsys.readouterr().out)

    mock_decide.assert_called_once()
    mock_call_llm.assert_called_once()
    fallback_text = json.dumps(mock_call_llm.call_args.kwargs["messages"])
    assert "Item A" in fallback_text and "Item B" in fallback_text

    assert out == [{"id": "a", "score": 8, "reason": "urgent enough", "item": items[0]}]


# ──────────────────────────────────────────────────────────────────────
# Goal judge (hermes_cli/goals.py)
# ──────────────────────────────────────────────────────────────────────


def test_goal_judge_confident_done(monkeypatch):
    answers = {"verdict": _choice_answer("verdict", choice="done", confidence=0.9)}
    decision = Decision(ok=True, answers=answers, status="ok", min_confidence=0.75)
    mock_decide = MagicMock(return_value=decision)
    mock_call_llm = MagicMock(return_value=_llm_response('{"verdict": "continue", "reason": "aux used"}'))
    monkeypatch.setattr("agent.jev_decide.decide", mock_decide)
    monkeypatch.setattr("agent.auxiliary_client.call_llm", mock_call_llm)

    verdict, _reason, parse_failed, _wait_directive, transport_failed = goals.judge_goal(
        "ship the feature", "I shipped it"
    )

    mock_decide.assert_called_once()
    mock_call_llm.assert_not_called()
    assert verdict == "done"
    assert parse_failed is False
    assert transport_failed is False


def test_goal_judge_confident_wait_target_pid(monkeypatch):
    pid = 4321
    answers = {
        "verdict": _choice_answer("verdict", choice="wait", confidence=0.9),
        "wait_target": _choice_answer("wait_target", choice=str(pid), confidence=0.9),
    }
    decision = Decision(ok=True, answers=answers, status="ok", min_confidence=0.75)
    mock_decide = MagicMock(return_value=decision)
    mock_call_llm = MagicMock(return_value=_llm_response('{"verdict": "continue", "reason": "aux used"}'))
    monkeypatch.setattr("agent.jev_decide.decide", mock_decide)
    monkeypatch.setattr("agent.auxiliary_client.call_llm", mock_call_llm)

    background_processes = [{"pid": pid, "status": "running", "command": "npm test"}]

    verdict, _reason, _parse_failed, wait_directive, _transport_failed = goals.judge_goal(
        "run the test suite", "kicked off the tests", background_processes=background_processes
    )

    mock_decide.assert_called_once()
    assert verdict == "wait"
    assert wait_directive == {"pid": pid}


def test_goal_judge_confident_wait_seconds_bucket_with_no_background(monkeypatch):
    answers = {
        "verdict": _choice_answer("verdict", choice="wait", confidence=0.9),
        "wait_seconds": _score_answer("wait_seconds", score=3.0, confidence=0.9, legend={"3": "about a minute"}),
    }
    decision = Decision(ok=True, answers=answers, status="ok", min_confidence=0.75)
    mock_decide = MagicMock(return_value=decision)
    mock_call_llm = MagicMock(return_value=_llm_response('{"verdict": "continue", "reason": "aux used"}'))
    monkeypatch.setattr("agent.jev_decide.decide", mock_decide)
    monkeypatch.setattr("agent.auxiliary_client.call_llm", mock_call_llm)

    verdict, _reason, _parse_failed, wait_directive, _transport_failed = goals.judge_goal(
        "run the test suite", "kicked off the tests", background_processes=[]
    )

    mock_decide.assert_called_once()
    assert verdict == "wait"
    assert wait_directive == {"seconds": _WAIT_BUCKETS[3]}


def test_goal_judge_unconfident_verdict_falls_back_to_call_llm(monkeypatch):
    answers = {"verdict": _choice_answer("verdict", choice="done", confidence=0.1)}
    decision = Decision(ok=True, answers=answers, status="ok", min_confidence=0.75)
    mock_decide = MagicMock(return_value=decision)
    mock_call_llm = MagicMock(return_value=_llm_response('{"verdict": "continue", "reason": "aux says continue"}'))
    monkeypatch.setattr("agent.jev_decide.decide", mock_decide)
    monkeypatch.setattr("agent.auxiliary_client.call_llm", mock_call_llm)

    verdict, _reason, _parse_failed, _wait_directive, _transport_failed = goals.judge_goal("ship it", "still working")

    mock_decide.assert_called_once()
    mock_call_llm.assert_called_once()
    assert verdict == "continue"


def test_draft_contract_calls_llm_never_decide(monkeypatch):
    mock_decide = MagicMock()
    mock_call_llm = MagicMock(return_value=_llm_response('{"verification": "tests pass"}'))
    monkeypatch.setattr("agent.jev_decide.decide", mock_decide)
    monkeypatch.setattr("agent.auxiliary_client.call_llm", mock_call_llm)

    goals.draft_contract("Migrate auth to JWT")

    mock_call_llm.assert_called_once()
    mock_decide.assert_not_called()


# ──────────────────────────────────────────────────────────────────────
# Kanban estimator (plugins/kanban/dashboard/plugin_api.py)
# ──────────────────────────────────────────────────────────────────────


def _load_kanban_plugin():
    repo_root = Path(__file__).resolve().parents[1]
    plugin_file = repo_root / "plugins" / "kanban" / "dashboard" / "plugin_api.py"
    spec = importlib.util.spec_from_file_location("hermes_kanban_plugin_jev_test", plugin_file)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_kanban_estimate_confident_choice_and_score(monkeypatch):
    mod = _load_kanban_plugin()
    answers = {
        "complexity": _choice_answer("complexity", choice="M", confidence=0.9),
        "tokens": _score_answer("tokens", score=2.0, confidence=0.9, legend={"2": "about fifty thousand tokens"}),
    }
    decision = Decision(ok=True, answers=answers, status="ok", min_confidence=0.6)
    mock_decide = MagicMock(return_value=decision)
    mock_call_llm = MagicMock(
        return_value=_llm_response('{"est_tokens": 1, "complexity": "S", "rationale": "call_llm fallback"}')
    )
    monkeypatch.setattr("agent.jev_decide.decide", mock_decide)
    monkeypatch.setattr("agent.auxiliary_client.call_llm", mock_call_llm)

    result = mod._run_estimate("Refactor the payments module", "touches five files")

    mock_decide.assert_called_once()
    mock_call_llm.assert_not_called()
    assert result["ok"] is True
    assert result["complexity"] == "M"
    assert result["est_tokens"] == _TOKEN_BUCKETS[2]
    assert result["rationale"] == "M complexity, about fifty thousand tokens (rough estimate)"


def test_kanban_estimate_unconfident_falls_back_to_call_llm(monkeypatch):
    mod = _load_kanban_plugin()
    answers = {
        "complexity": _choice_answer("complexity", choice="M", confidence=0.2),
        "tokens": _score_answer("tokens", score=2.0, confidence=0.9, legend={"2": "about fifty thousand tokens"}),
    }
    decision = Decision(ok=True, answers=answers, status="ok", min_confidence=0.6)
    mock_decide = MagicMock(return_value=decision)
    mock_call_llm = MagicMock(
        return_value=_llm_response('{"est_tokens": 42000, "complexity": "S", "rationale": "aux fallback"}')
    )
    monkeypatch.setattr("agent.jev_decide.decide", mock_decide)
    monkeypatch.setattr("agent.auxiliary_client.call_llm", mock_call_llm)

    result = mod._run_estimate("Tweak a label", "in settings")

    mock_decide.assert_called_once()
    mock_call_llm.assert_called_once()
    assert result["complexity"] == "S"
    assert result["est_tokens"] == 42000
