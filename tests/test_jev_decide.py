"""Tests for the not-yet-implemented ``agent.jev_decide.decide()`` (Task 5).

Pins confidence derivation (including the ``noul`` asymmetry from
``docs/specs/2026-09-23-jev-decision-layers.md:236-238``) and the
never-raises guarantee.

Every test imports ``agent.jev_decide`` locally (not at module scope) so
collection succeeds even though the module doesn't exist yet; running is
expected red with ``ModuleNotFoundError`` until Task 5 lands.

The client is faked by monkeypatching ``agent.jev_decide._build_client``;
config is faked by monkeypatching ``agent.jev_decide._task_config`` via
``_enable()`` below — no file I/O, no network.
"""
from __future__ import annotations

import pytest

from agent.jev_client import JevAnswer, JevError, JevResult, choice_question, noul_question, score_question


def _enable(monkeypatch, jev_decide, task, *, min_confidence=None):
    task_jev = {"enabled": True}
    if min_confidence is not None:
        task_jev["min_confidence"] = min_confidence
    configs = {
        "jev": {"enabled": True, "api_key_env": "TEST_JEV_KEY"},
        task: {"jev": task_jev},
    }
    monkeypatch.setenv("TEST_JEV_KEY", "secret")
    monkeypatch.setattr(jev_decide, "_task_config", lambda t: configs.get(t, {}))


def _fake_client(*, result=None, exc=None):
    class _Client:
        def systemone(self, state, questions, *, model=None):
            if exc is not None:
                raise exc
            return result

    return _Client()


def test_choice_confidence_above_threshold(monkeypatch):
    import agent.jev_decide as jev_decide

    _enable(monkeypatch, jev_decide, "goal_judge", min_confidence=0.85)
    answer = JevAnswer(name="verdict", type="choice", choice="approve", confidence=0.9)
    result = JevResult(model="jev-1", answers={"verdict": answer}, input_tokens=1, output_tokens=1)
    monkeypatch.setattr(jev_decide, "_build_client", lambda cfg: _fake_client(result=result))

    d = jev_decide.decide(
        "goal_judge",
        state={},
        questions={"verdict": choice_question("q", {"approve": None, "deny": None, "escalate": None})},
    )

    assert d.ok is True
    assert d.confident("verdict") is True


def test_choice_confidence_below_threshold(monkeypatch):
    import agent.jev_decide as jev_decide

    _enable(monkeypatch, jev_decide, "goal_judge", min_confidence=0.85)
    answer = JevAnswer(name="verdict", type="choice", choice="approve", confidence=0.80)
    result = JevResult(model="jev-1", answers={"verdict": answer}, input_tokens=1, output_tokens=1)
    monkeypatch.setattr(jev_decide, "_build_client", lambda cfg: _fake_client(result=result))

    d = jev_decide.decide(
        "goal_judge",
        state={},
        questions={"verdict": choice_question("q", {"approve": None, "deny": None, "escalate": None})},
    )

    assert d.ok is True
    assert d.confident("verdict") is False


@pytest.mark.parametrize(
    ("noul", "expected_confidence", "expected_confident"),
    [
        (0.5, 0.0, False),
        (1.0, 1.0, True),
        (0.0, 1.0, True),
        (0.75, 0.5, True),
    ],
)
def test_noul_confidence_derivation(monkeypatch, noul, expected_confidence, expected_confident):
    import agent.jev_decide as jev_decide

    _enable(monkeypatch, jev_decide, "review_gate", min_confidence=0.5)
    answer = JevAnswer(name="g", type="noul", noul=noul, confidence=None)
    result = JevResult(model="jev-1", answers={"g": answer}, input_tokens=1, output_tokens=1)
    monkeypatch.setattr(jev_decide, "_build_client", lambda cfg: _fake_client(result=result))

    d = jev_decide.decide("review_gate", state={}, questions={"g": noul_question("q")})

    assert d.confidence("g") == expected_confidence
    assert d.confident("g") is expected_confident


def test_per_answer_confidence_in_batch(monkeypatch):
    import agent.jev_decide as jev_decide

    _enable(monkeypatch, jev_decide, "monitor", min_confidence=0.6)
    answers = {
        "a": JevAnswer(name="a", type="score", score=3.0, confidence=0.9),
        "b": JevAnswer(name="b", type="score", score=1.0, confidence=0.2),
    }
    result = JevResult(model="jev-1", answers=answers, input_tokens=1, output_tokens=1)
    monkeypatch.setattr(jev_decide, "_build_client", lambda cfg: _fake_client(result=result))

    d = jev_decide.decide(
        "monitor",
        state={},
        questions={
            "a": score_question("a?", ["lo", "hi"]),
            "b": score_question("b?", ["lo", "hi"]),
        },
    )

    assert d.confident("a") is True
    assert d.confident("b") is False
    assert d.ok is True


def test_client_jev_error_is_uncertain_never_raises(monkeypatch):
    import agent.jev_decide as jev_decide

    _enable(monkeypatch, jev_decide, "kanban_estimator")
    monkeypatch.setattr(jev_decide, "_build_client", lambda cfg: _fake_client(exc=JevError("boom")))

    d = jev_decide.decide("kanban_estimator", state={}, questions={"q": noul_question("q")})

    assert d.ok is False
    assert d.status.startswith("error:")


def test_client_value_error_is_uncertain_never_raises(monkeypatch):
    import agent.jev_decide as jev_decide

    _enable(monkeypatch, jev_decide, "kanban_estimator")
    monkeypatch.setattr(jev_decide, "_build_client", lambda cfg: _fake_client(exc=ValueError("bad")))

    d = jev_decide.decide("kanban_estimator", state={}, questions={"q": noul_question("q")})

    assert d.ok is False
    assert d.status.startswith("error:")


def test_client_keyboard_interrupt_is_uncertain_never_raises(monkeypatch):
    import agent.jev_decide as jev_decide

    _enable(monkeypatch, jev_decide, "kanban_estimator")
    monkeypatch.setattr(jev_decide, "_build_client", lambda cfg: _fake_client(exc=KeyboardInterrupt()))

    d = jev_decide.decide("kanban_estimator", state={}, questions={"q": noul_question("q")})

    assert d.ok is False
    assert d.status.startswith("error:")


def test_choice_outside_declared_criteria_is_uncertain(monkeypatch):
    import agent.jev_decide as jev_decide

    _enable(monkeypatch, jev_decide, "kanban_estimator")
    answer = JevAnswer(name="verdict", type="choice", choice="not_a_label", confidence=0.9)
    result = JevResult(model="jev-1", answers={"verdict": answer}, input_tokens=1, output_tokens=1)
    monkeypatch.setattr(jev_decide, "_build_client", lambda cfg: _fake_client(result=result))

    d = jev_decide.decide(
        "kanban_estimator",
        state={},
        questions={"verdict": choice_question("q", {"approve": None, "deny": None})},
    )

    assert d.ok is False
    assert d.status.startswith("error:")


def test_confident_on_absent_name_returns_false(monkeypatch):
    import agent.jev_decide as jev_decide

    _enable(monkeypatch, jev_decide, "kanban_estimator")
    answer = JevAnswer(name="verdict", type="choice", choice="approve", confidence=0.9)
    result = JevResult(model="jev-1", answers={"verdict": answer}, input_tokens=1, output_tokens=1)
    monkeypatch.setattr(jev_decide, "_build_client", lambda cfg: _fake_client(result=result))

    d = jev_decide.decide(
        "kanban_estimator",
        state={},
        questions={"verdict": choice_question("q", {"approve": None, "deny": None})},
    )

    assert d.confident("absent-name") is False
