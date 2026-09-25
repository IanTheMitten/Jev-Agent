"""Config-precedence tests for the not-yet-implemented ``agent.jev_decide`` (Task 5).

Pins the kill-switch/per-task precedence, the acknowledgement-key gate, and
the "disabled is free" guarantee against
``docs/specs/2026-09-23-jev-decision-layers.md:118-120``.

Every test imports ``agent.jev_decide`` locally (not at module scope) so
collection succeeds even though the module doesn't exist yet; running is
expected red with ``ModuleNotFoundError`` until Task 5 lands.

Config is faked by monkeypatching ``agent.jev_decide._task_config`` (keyed by
task name, "jev" for the global block) and ``agent.jev_decide._build_client``
(a spy that fails the test if invoked when it shouldn't be) — no file I/O, no
network.
"""
from __future__ import annotations

from agent.jev_client import JevAnswer, JevResult, noul_question


def _fake_task_config(configs):
    def _fn(task):
        return configs.get(task, {})

    return _fn


class _SpyBuildClient:
    """Records calls; raises if invoked with no client configured to return."""

    def __init__(self, client=None):
        self.calls = []
        self.client = client

    def __call__(self, cfg):
        self.calls.append(cfg)
        if self.client is None:
            raise AssertionError("_build_client should not have been called")
        return self.client


def _minimal_questions():
    return {"q": noul_question("q?")}


def _healthy_client():
    result = JevResult(
        model="jev-1",
        answers={"q": JevAnswer(name="q", type="noul", noul=0.9)},
        input_tokens=1,
        output_tokens=1,
    )

    class _Client:
        def systemone(self, state, questions, *, model=None):
            return result

    return _Client()


def test_absent_config_is_disabled(monkeypatch):
    import agent.jev_decide as jev_decide

    spy = _SpyBuildClient()
    monkeypatch.setattr(jev_decide, "_task_config", _fake_task_config({}))
    monkeypatch.setattr(jev_decide, "_build_client", spy)

    d = jev_decide.decide("approval", state={}, questions=_minimal_questions())

    assert d.ok is False
    assert d.status == "disabled"
    assert spy.calls == []


def test_global_kill_switch_overrides_per_task_enabled(monkeypatch):
    import agent.jev_decide as jev_decide

    configs = {
        "jev": {"enabled": False},
        "approval": {"jev": {"enabled": True}},
    }
    spy = _SpyBuildClient()
    monkeypatch.setattr(jev_decide, "_task_config", _fake_task_config(configs))
    monkeypatch.setattr(jev_decide, "_build_client", spy)

    d = jev_decide.decide("approval", state={}, questions=_minimal_questions())

    assert d.ok is False
    assert d.status == "disabled"
    assert spy.calls == []


def test_per_task_enabled_with_global_absent_is_disabled(monkeypatch):
    import agent.jev_decide as jev_decide

    configs = {
        "approval": {"jev": {"enabled": True}},
    }
    spy = _SpyBuildClient()
    monkeypatch.setattr(jev_decide, "_task_config", _fake_task_config(configs))
    monkeypatch.setattr(jev_decide, "_build_client", spy)

    d = jev_decide.decide("approval", state={}, questions=_minimal_questions())

    assert d.ok is False
    assert d.status == "disabled"
    assert spy.calls == []


def test_both_enabled_builds_client(monkeypatch):
    import agent.jev_decide as jev_decide

    monkeypatch.setenv("TEST_JEV_KEY", "secret")
    configs = {
        "jev": {"enabled": True, "api_key_env": "TEST_JEV_KEY"},
        "approval": {"jev": {"enabled": True}},
    }
    spy = _SpyBuildClient(client=_healthy_client())
    monkeypatch.setattr(jev_decide, "_task_config", _fake_task_config(configs))
    monkeypatch.setattr(jev_decide, "_build_client", spy)

    jev_decide.decide("approval", state={}, questions=_minimal_questions())

    assert len(spy.calls) == 1


def test_missing_ack_key_blocks_and_builds_no_client(monkeypatch):
    import agent.jev_decide as jev_decide

    monkeypatch.setenv("TEST_JEV_KEY", "secret")
    configs = {
        "jev": {"enabled": True, "api_key_env": "TEST_JEV_KEY"},
        "approval": {"jev": {"enabled": True}},
    }
    spy = _SpyBuildClient()
    monkeypatch.setattr(jev_decide, "_task_config", _fake_task_config(configs))
    monkeypatch.setattr(jev_decide, "_build_client", spy)

    d = jev_decide.decide(
        "approval",
        state={},
        questions=_minimal_questions(),
        require_ack="i_understand_commands_leave_host",
    )

    assert d.ok is False
    assert d.status == "missing_ack"
    assert spy.calls == []


def test_ack_present_true_proceeds(monkeypatch):
    import agent.jev_decide as jev_decide

    monkeypatch.setenv("TEST_JEV_KEY", "secret")
    configs = {
        "jev": {"enabled": True, "api_key_env": "TEST_JEV_KEY"},
        "approval": {
            "jev": {
                "enabled": True,
                "i_understand_commands_leave_host": True,
            }
        },
    }
    spy = _SpyBuildClient(client=_healthy_client())
    monkeypatch.setattr(jev_decide, "_task_config", _fake_task_config(configs))
    monkeypatch.setattr(jev_decide, "_build_client", spy)

    d = jev_decide.decide(
        "approval",
        state={},
        questions=_minimal_questions(),
        require_ack="i_understand_commands_leave_host",
    )

    assert d.ok is True
    assert len(spy.calls) == 1


def test_ack_present_false_blocks(monkeypatch):
    import agent.jev_decide as jev_decide

    monkeypatch.setenv("TEST_JEV_KEY", "secret")
    configs = {
        "jev": {"enabled": True, "api_key_env": "TEST_JEV_KEY"},
        "approval": {
            "jev": {
                "enabled": True,
                "i_understand_commands_leave_host": False,
            }
        },
    }
    spy = _SpyBuildClient()
    monkeypatch.setattr(jev_decide, "_task_config", _fake_task_config(configs))
    monkeypatch.setattr(jev_decide, "_build_client", spy)

    d = jev_decide.decide(
        "approval",
        state={},
        questions=_minimal_questions(),
        require_ack="i_understand_commands_leave_host",
    )

    assert d.ok is False
    assert d.status == "missing_ack"
    assert spy.calls == []


def test_missing_api_key_env_is_no_key(monkeypatch):
    import agent.jev_decide as jev_decide

    monkeypatch.delenv("TEST_JEV_UNSET_KEY", raising=False)
    configs = {
        "jev": {"enabled": True, "api_key_env": "TEST_JEV_UNSET_KEY"},
        "approval": {
            "jev": {
                "enabled": True,
                "i_understand_commands_leave_host": True,
            }
        },
    }
    spy = _SpyBuildClient()
    monkeypatch.setattr(jev_decide, "_task_config", _fake_task_config(configs))
    monkeypatch.setattr(jev_decide, "_build_client", spy)

    d = jev_decide.decide(
        "approval",
        state={},
        questions=_minimal_questions(),
        require_ack="i_understand_commands_leave_host",
    )

    assert d.ok is False
    assert d.status == "no_key"
    assert spy.calls == []
