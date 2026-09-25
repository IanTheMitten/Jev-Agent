"""Tier B gate tests, red (Task 11, Steps 1-8).

Pins each gate's skip-or-run direction on Jev uncertainty
(docs/specs/2026-09-23-jev-decision-layers.md:143) and the state shape each
gate is allowed to ship off-host.

None of the four gates import ``agent.jev_decide.decide`` yet — every test
here patches ``decide`` at its defining module (the pattern already used by
``tests/test_jev_sites_tier_a.py``) and asserts it was actually consulted.
Since no gate calls it, every test is expected to fail until a later task
wires the gates in.
"""
from __future__ import annotations

import importlib
import json
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agent.jev_client import JevAnswer
from agent.jev_decide import Decision
from agent.turn_finalizer import finalize_turn
from plugins.memory.honcho import HonchoMemoryProvider
from tests.agent.test_turn_finalizer_cleanup_guard import _StubAgent


# ──────────────────────────────────────────────────────────────────────
# Review gate (agent/turn_finalizer.py:698-724)
# ──────────────────────────────────────────────────────────────────────


def _review_messages(final_response, *, with_tool_use=False):
    if not with_tool_use:
        return [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": final_response},
        ]
    return [
        {"role": "user", "content": "look into the deploy failures"},
        {
            "role": "assistant",
            "content": "checking now",
            "tool_calls": [
                {
                    "id": "c1",
                    "function": {
                        "name": "terminal",
                        "arguments": '{"cmd": "SECRET_ARG_MARKER"}',
                    },
                }
            ],
        },
        {"role": "tool", "tool_call_id": "c1", "name": "terminal", "content": "SECRET_RESULT_MARKER"},
        {"role": "assistant", "content": final_response},
    ]


def _fire_both_counters(agent):
    """Both the memory nudge and the skill nudge counters have already fired."""
    agent.valid_tool_names = {"skill_manage"}
    agent._skill_nudge_interval = 1
    agent._iters_since_skill = 1
    agent._spawn_background_review = MagicMock()
    return agent


def _run_finalize(agent, *, final_response="done", with_tool_use=False):
    return finalize_turn(
        agent,
        final_response=final_response,
        api_call_count=1,
        interrupted=False,
        failed=False,
        messages=_review_messages(final_response, with_tool_use=with_tool_use),
        conversation_history=None,
        effective_task_id="task-1",
        turn_id="turn-1",
        user_message="hi",
        original_user_message="hi",
        _should_review_memory=True,
        _turn_exit_reason="text_response(final)",
    )


_REVIEW_SCENARIOS = {
    "confident_yes": (
        Decision(ok=True, answers={"g": JevAnswer(name="g", type="noul", noul=0.9)},
                 status="ok", min_confidence=0.5),
        1,
    ),
    "status_disabled": (
        Decision(ok=False, answers={}, status="disabled", min_confidence=0.5),
        1,
    ),
    "confident_no": (
        Decision(ok=True, answers={"g": JevAnswer(name="g", type="noul", noul=0.1)},
                 status="ok", min_confidence=0.5),
        0,
    ),
    "unconfident": (
        Decision(ok=True, answers={"g": JevAnswer(name="g", type="noul", noul=0.55)},
                 status="ok", min_confidence=0.5),
        0,
    ),
    "status_no_key": (
        Decision(ok=False, answers={}, status="no_key", min_confidence=0.5),
        0,
    ),
    "status_missing_ack": (
        Decision(ok=False, answers={}, status="missing_ack", min_confidence=0.5),
        0,
    ),
    "status_error": (
        Decision(ok=False, answers={}, status="error:JevError", min_confidence=0.5),
        0,
    ),
}


@pytest.mark.parametrize("scenario", sorted(_REVIEW_SCENARIOS))
def test_review_gate_direction(monkeypatch, scenario):
    decision, expected_spawns = _REVIEW_SCENARIOS[scenario]
    mock_decide = MagicMock(return_value=decision)
    monkeypatch.setattr("agent.jev_decide.decide", mock_decide)

    agent = _fire_both_counters(_StubAgent(raise_in=()))
    _run_finalize(agent)

    assert mock_decide.call_count == 1, "review gate must consult Jev before spawning"
    assert agent._spawn_background_review.call_count == expected_spawns


def test_review_gate_state_shape(monkeypatch):
    decision = _REVIEW_SCENARIOS["confident_yes"][0]
    mock_decide = MagicMock(return_value=decision)
    monkeypatch.setattr("agent.jev_decide.decide", mock_decide)

    agent = _fire_both_counters(_StubAgent(raise_in=()))
    _run_finalize(agent, with_tool_use=True)

    state = mock_decide.call_args.kwargs["state"]
    assert set(state.keys()) == {"user", "assistant", "tools_used", "iters"}
    assert isinstance(state["tools_used"], list)
    assert all(isinstance(name, str) for name in state["tools_used"])
    assert "terminal" in state["tools_used"]

    serialized = json.dumps(state)
    assert "SECRET_ARG_MARKER" not in serialized
    assert "SECRET_RESULT_MARKER" not in serialized


# ──────────────────────────────────────────────────────────────────────
# Curator gate (agent/curator.py:1641-1682)
# ──────────────────────────────────────────────────────────────────────


@pytest.fixture
def curator_env(tmp_path, monkeypatch):
    """Isolated HERMES_HOME + freshly reloaded curator + skill_usage modules.

    Duplicated from ``tests/agent/test_curator.py`` — this fixture is
    intentionally re-declared per test file across the curator suite rather
    than imported (see ``tests/agent/test_curator_reports.py``).
    """
    home = tmp_path / ".hermes"
    (home / "skills").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))

    import tools.skill_usage as usage
    importlib.reload(usage)
    import agent.curator as curator
    importlib.reload(curator)

    monkeypatch.setattr(curator, "_load_config", lambda: {})
    monkeypatch.setattr(usage, "_prune_builtins_enabled", lambda: False)

    yield {"home": home, "curator": curator, "usage": usage}

    for t in threading.enumerate():
        if t.name == "curator-review" and t.is_alive():
            t.join(timeout=10.0)


def _write_skill(skills_dir: Path, name: str):
    d = skills_dir / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: x\n---\n", encoding="utf-8",
    )
    return d


def _seed_one_candidate(curator_env):
    u = curator_env["usage"]
    skills_dir = curator_env["home"] / "skills"
    _write_skill(skills_dir, "a")
    u.mark_agent_created("a")


_CURATOR_SCENARIOS = {
    "confident_yes": (
        Decision(ok=True, answers={"g": JevAnswer(name="g", type="noul", noul=0.9)},
                 status="ok", min_confidence=0.5),
        1,
    ),
    "confident_no": (
        Decision(ok=True, answers={"g": JevAnswer(name="g", type="noul", noul=0.1)},
                 status="ok", min_confidence=0.5),
        0,
    ),
    "unconfident": (
        Decision(ok=True, answers={"g": JevAnswer(name="g", type="noul", noul=0.55)},
                 status="ok", min_confidence=0.5),
        1,
    ),
    "status_disabled": (
        Decision(ok=False, answers={}, status="disabled", min_confidence=0.5),
        1,
    ),
}


@pytest.mark.parametrize("scenario", sorted(_CURATOR_SCENARIOS))
def test_curator_gate_direction(curator_env, monkeypatch, scenario):
    decision, expected_runs = _CURATOR_SCENARIOS[scenario]
    mock_decide = MagicMock(return_value=decision)
    monkeypatch.setattr("agent.jev_decide.decide", mock_decide)
    _seed_one_candidate(curator_env)

    c = curator_env["curator"]
    mock_review = MagicMock(return_value={
        "final": "", "summary": "s", "model": "", "provider": "",
        "tool_calls": [], "error": None,
    })
    monkeypatch.setattr(c, "_run_llm_review", mock_review)

    c.run_curator_review(synchronous=True, consolidate=True)

    assert mock_decide.call_count == 1, "curator gate must consult Jev before the fork"
    assert mock_review.call_count == expected_runs


def test_curator_gate_state_shape(curator_env, monkeypatch):
    decision = _CURATOR_SCENARIOS["confident_yes"][0]
    mock_decide = MagicMock(return_value=decision)
    monkeypatch.setattr("agent.jev_decide.decide", mock_decide)
    _seed_one_candidate(curator_env)

    c = curator_env["curator"]
    monkeypatch.setattr(c, "_run_llm_review", MagicMock(return_value={
        "final": "", "summary": "s", "model": "", "provider": "",
        "tool_calls": [], "error": None,
    }))

    c.run_curator_review(synchronous=True, consolidate=True)

    state = mock_decide.call_args.kwargs["state"]
    assert isinstance(state, list)
    assert state
    for entry in state:
        assert set(entry.keys()) == {"name", "state", "use_count", "activity_count", "last_activity_at"}

    serialized = json.dumps(state)
    assert "SKILL.md" not in serialized
    assert "description: x" not in serialized


def test_curator_gate_off_never_consults_jev(curator_env, monkeypatch):
    mock_decide = MagicMock(return_value=_CURATOR_SCENARIOS["confident_yes"][0])
    monkeypatch.setattr("agent.jev_decide.decide", mock_decide)
    _seed_one_candidate(curator_env)

    c = curator_env["curator"]
    mock_review = MagicMock()
    monkeypatch.setattr(c, "_run_llm_review", mock_review)

    c.run_curator_review(synchronous=True, consolidate=False)

    assert mock_decide.call_count == 0
    assert mock_review.call_count == 0


# ──────────────────────────────────────────────────────────────────────
# Honcho recall gate + reasoning level + dialectic bail-out
# (plugins/memory/honcho/__init__.py:919-960, :1042, :1120-1137)
# ──────────────────────────────────────────────────────────────────────


def _honcho_provider(*, depth=1, dialectic_cadence=1, turn_count=10,
                      last_dialectic_turn=5, empty_streak=0):
    provider = HonchoMemoryProvider()
    provider._manager = MagicMock()
    provider._manager.dialectic_query.return_value = "memory synthesis, in enough detail to matter."
    provider._session_key = "test-session"
    provider._base_context_cache = "existing context"
    provider._dialectic_depth = depth
    provider._dialectic_cadence = dialectic_cadence
    provider._turn_count = turn_count
    provider._last_dialectic_turn = last_dialectic_turn
    provider._dialectic_empty_streak = empty_streak
    provider._config = SimpleNamespace(dialectic_reasoning_level="low")
    provider._context_cadence = 1
    # Skip the unrelated context-prefetch branch so only the dialectic gate
    # under test fires.
    provider._injection_frequency = "first-turn"
    return provider


_QUERY = "what did we discuss about the roadmap last quarter?"


_HONCHO_RECALL_SCENARIOS = {
    "confident_yes": (
        Decision(ok=True, answers={"g": JevAnswer(name="g", type="noul", noul=0.9)},
                 status="ok", min_confidence=0.5),
        1,
    ),
    "confident_no": (
        Decision(ok=True, answers={"g": JevAnswer(name="g", type="noul", noul=0.1)},
                 status="ok", min_confidence=0.5),
        0,
    ),
    "unconfident": (
        Decision(ok=True, answers={"g": JevAnswer(name="g", type="noul", noul=0.55)},
                 status="ok", min_confidence=0.5),
        1,
    ),
    "status_disabled": (
        Decision(ok=False, answers={}, status="disabled", min_confidence=0.5),
        1,
    ),
}


@pytest.mark.parametrize("scenario", sorted(_HONCHO_RECALL_SCENARIOS))
def test_honcho_recall_gate_direction(monkeypatch, scenario):
    decision, expected_calls = _HONCHO_RECALL_SCENARIOS[scenario]
    mock_decide = MagicMock(return_value=decision)
    monkeypatch.setattr("agent.jev_decide.decide", mock_decide)

    provider = _honcho_provider()
    mock_dialectic = MagicMock(return_value="synthesized memory content, long enough to count.")
    provider._run_dialectic_depth = mock_dialectic

    provider.queue_prefetch(_QUERY)
    if provider._prefetch_thread is not None:
        provider._prefetch_thread.join(timeout=2.0)

    assert mock_decide.call_count == 1, "honcho recall gate must consult Jev before dialectic"
    assert mock_dialectic.call_count == expected_calls


def test_honcho_recall_gate_confident_no_advances_cadence_without_reset(monkeypatch):
    decision = _HONCHO_RECALL_SCENARIOS["confident_no"][0]
    monkeypatch.setattr("agent.jev_decide.decide", MagicMock(return_value=decision))

    provider = _honcho_provider(turn_count=10, last_dialectic_turn=5, empty_streak=3)
    mock_dialectic = MagicMock(return_value="synthesized memory content, long enough to count.")
    provider._run_dialectic_depth = mock_dialectic

    provider.queue_prefetch(_QUERY)
    if provider._prefetch_thread is not None:
        provider._prefetch_thread.join(timeout=2.0)

    assert mock_dialectic.call_count == 0
    assert provider._last_dialectic_turn == 10
    assert provider._dialectic_empty_streak == 3


_HONCHO_LEVEL_SCENARIOS = [(0, "low"), (1, "medium"), (2, "high")]


@pytest.mark.parametrize(("score", "expected_level"), _HONCHO_LEVEL_SCENARIOS)
def test_honcho_reasoning_level_confident_bump(monkeypatch, score, expected_level):
    decision = Decision(
        ok=True,
        answers={"level": JevAnswer(name="level", type="score", score=score, confidence=0.9)},
        status="ok", min_confidence=0.6,
    )
    mock_decide = MagicMock(return_value=decision)
    monkeypatch.setattr("agent.jev_decide.decide", mock_decide)

    provider = _honcho_provider(depth=1)
    provider._run_dialectic_depth("short query", use_query_rewrite=False)

    assert mock_decide.call_count == 1, "reasoning level must consult Jev"
    level = provider._manager.dialectic_query.call_args.kwargs.get("reasoning_level")
    assert level == expected_level


def test_honcho_reasoning_level_unconfident_matches_char_heuristic(monkeypatch):
    decision = Decision(
        ok=True,
        answers={"level": JevAnswer(name="level", type="score", score=2, confidence=0.1)},
        status="ok", min_confidence=0.6,
    )
    mock_decide = MagicMock(return_value=decision)
    monkeypatch.setattr("agent.jev_decide.decide", mock_decide)

    provider = _honcho_provider(depth=1)
    query = "x" * 150  # >=120 chars -> char heuristic bumps "low" by one
    expected_level = provider._apply_reasoning_heuristic("low", query)

    provider._run_dialectic_depth(query, use_query_rewrite=False)

    assert mock_decide.call_count == 1
    level = provider._manager.dialectic_query.call_args.kwargs.get("reasoning_level")
    assert level == expected_level


# A short, unstructured pass result the length-and-bullets heuristic judges
# insufficient — isolates the Jev score as the only thing driving bail-out.
_SHORT_RESULT = "short"
# A long, structured pass result the length-and-bullets heuristic judges
# sufficient on its own.
_STRUCTURED_RESULT = "## Summary\n\n" + ("well-grounded detail " * 10)


def test_honcho_bailout_confident_high_score_stops_after_first_pass(monkeypatch):
    decision = Decision(
        ok=True,
        answers={"g": JevAnswer(name="g", type="score", score=3, confidence=0.9)},
        status="ok", min_confidence=0.6,
    )
    mock_decide = MagicMock(return_value=decision)
    monkeypatch.setattr("agent.jev_decide.decide", mock_decide)

    provider = _honcho_provider(depth=3)
    provider._manager.dialectic_query.side_effect = [_SHORT_RESULT, _SHORT_RESULT, _SHORT_RESULT]

    provider._run_dialectic_depth("query text", use_query_rewrite=False)

    assert mock_decide.call_count >= 1
    assert provider._manager.dialectic_query.call_count == 1


def test_honcho_bailout_confident_low_score_continues(monkeypatch):
    decision = Decision(
        ok=True,
        answers={"g": JevAnswer(name="g", type="score", score=2, confidence=0.9)},
        status="ok", min_confidence=0.6,
    )
    mock_decide = MagicMock(return_value=decision)
    monkeypatch.setattr("agent.jev_decide.decide", mock_decide)

    provider = _honcho_provider(depth=3)
    provider._manager.dialectic_query.side_effect = [_SHORT_RESULT, _SHORT_RESULT, _SHORT_RESULT]

    provider._run_dialectic_depth("query text", use_query_rewrite=False)

    assert mock_decide.call_count >= 1
    assert provider._manager.dialectic_query.call_count == 3


@pytest.mark.parametrize(
    "decision",
    [
        Decision(ok=True, answers={"g": JevAnswer(name="g", type="score", score=3, confidence=0.1)},
                 status="ok", min_confidence=0.6),
        Decision(ok=False, answers={}, status="disabled", min_confidence=0.6),
    ],
    ids=["unconfident", "status_disabled"],
)
def test_honcho_bailout_uncertain_defers_to_signal_sufficient(monkeypatch, decision):
    mock_decide = MagicMock(return_value=decision)
    monkeypatch.setattr("agent.jev_decide.decide", mock_decide)

    provider = _honcho_provider(depth=3)
    provider._manager.dialectic_query.side_effect = [_STRUCTURED_RESULT, _SHORT_RESULT, _SHORT_RESULT]

    provider._run_dialectic_depth("query text", use_query_rewrite=False)

    assert mock_decide.call_count >= 1
    # _signal_sufficient(_STRUCTURED_RESULT) is True -> bail after pass 0.
    assert provider._manager.dialectic_query.call_count == 1


def test_honcho_bailout_never_consults_jev_at_depth_one(monkeypatch):
    mock_decide = MagicMock(return_value=Decision(
        ok=True,
        answers={"g": JevAnswer(name="g", type="score", score=3, confidence=0.9)},
        status="ok", min_confidence=0.6,
    ))
    monkeypatch.setattr("agent.jev_decide.decide", mock_decide)

    provider = _honcho_provider(depth=1)
    provider._manager.dialectic_query.return_value = _SHORT_RESULT

    provider._run_dialectic_depth("query text", use_query_rewrite=False)

    assert mock_decide.call_count == 0
