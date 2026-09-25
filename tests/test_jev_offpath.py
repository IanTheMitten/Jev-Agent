"""Turn-path guard (Task 11, Step 9-10).

C6 — a gate must not sit on the user's turn path — is the constraint most
easily broken by a later edit that "tidies" a gate onto the synchronous
path. With every Jev site enabled and the client stubbed to block far past
the outer deadline, ``queue_prefetch`` and ``finalize_turn`` must both
return promptly, and the client must only ever be reached from the worker
thread the Honcho gate already runs in — never from the caller's thread.

Neither gate imports ``agent.jev_decide.decide`` yet, so today neither
function reaches the client at all: the timing assertions below pass
trivially, and the thread-identity assertion (Step 10) fails because the
client is never reached from anywhere. See
docs/specs/2026-09-23-jev-decision-layers.md:256.
"""
from __future__ import annotations

import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

from agent.turn_finalizer import finalize_turn
from plugins.memory.honcho import HonchoMemoryProvider
from tests.agent.test_turn_finalizer_cleanup_guard import _StubAgent


def _enable_every_site(monkeypatch):
    jev_cfg = {"enabled": True, "api_key_env": "TEST_JEV_KEY"}
    task_cfg = {
        "jev": {
            "enabled": True,
            "min_confidence": 0.5,
            "i_understand_turn_digests_leave_host": True,
            "i_understand_memory_content_leaves_host": True,
        }
    }
    monkeypatch.setenv("TEST_JEV_KEY", "secret")
    monkeypatch.setattr(
        "agent.jev_decide._task_config",
        lambda t: jev_cfg if t == "jev" else task_cfg,
    )


def _sleeping_client():
    class _Client:
        def systemone(self, state, questions, *, model=None):
            time.sleep(5.0)
            return None

    return _Client()


def _thread_recording_client(recorded):
    class _Client:
        def systemone(self, state, questions, *, model=None):
            recorded.append(threading.current_thread().name)
            return None

    return _Client()


def _honcho_provider():
    provider = HonchoMemoryProvider()
    provider._manager = MagicMock()
    provider._manager.dialectic_query.return_value = "memory synthesis, in enough detail to matter."
    provider._session_key = "test-session"
    provider._base_context_cache = "existing context"
    provider._dialectic_depth = 1
    provider._dialectic_cadence = 1
    provider._turn_count = 10
    provider._last_dialectic_turn = 5
    provider._dialectic_empty_streak = 0
    provider._config = SimpleNamespace(dialectic_reasoning_level="low")
    provider._context_cadence = 1
    provider._injection_frequency = "first-turn"
    return provider


def _finalizer_agent():
    agent = _StubAgent(raise_in=())
    agent.valid_tool_names = {"skill_manage"}
    agent._skill_nudge_interval = 1
    agent._iters_since_skill = 1
    agent._spawn_background_review = MagicMock()
    return agent


def _run_finalize(agent):
    messages = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "done"},
    ]
    return finalize_turn(
        agent,
        final_response="done",
        api_call_count=1,
        interrupted=False,
        failed=False,
        messages=messages,
        conversation_history=None,
        effective_task_id="task-1",
        turn_id="turn-1",
        user_message="hi",
        original_user_message="hi",
        _should_review_memory=True,
        _turn_exit_reason="text_response(final)",
    )


def test_queue_prefetch_never_waits_on_jev(monkeypatch):
    _enable_every_site(monkeypatch)
    monkeypatch.setattr("agent.jev_decide._build_client", lambda cfg: _sleeping_client())

    provider = _honcho_provider()

    start = time.monotonic()
    provider.queue_prefetch("what did we discuss about the roadmap last quarter?")
    elapsed = time.monotonic() - start

    assert elapsed < 0.5


def test_finalize_turn_never_waits_on_jev(monkeypatch):
    _enable_every_site(monkeypatch)
    monkeypatch.setattr("agent.jev_decide._build_client", lambda cfg: _sleeping_client())

    agent = _finalizer_agent()

    start = time.monotonic()
    _run_finalize(agent)
    elapsed = time.monotonic() - start

    assert elapsed < 0.5


def test_queue_prefetch_reaches_jev_only_from_worker_thread(monkeypatch):
    _enable_every_site(monkeypatch)
    recorded: list[str] = []
    monkeypatch.setattr(
        "agent.jev_decide._build_client", lambda cfg: _thread_recording_client(recorded)
    )

    provider = _honcho_provider()
    provider.queue_prefetch("what did we discuss about the roadmap last quarter?")
    if provider._prefetch_thread is not None:
        provider._prefetch_thread.join(timeout=2.0)

    assert recorded, "the client was never reached by the gate"
    assert recorded[0] != threading.current_thread().name
