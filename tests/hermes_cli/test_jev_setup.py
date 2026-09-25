"""Tests for the ``hermes jev`` / ``hermes setup jev`` flow."""
from __future__ import annotations

import argparse

import pytest

from hermes_cli import jev_setup
from hermes_cli.jev_setup import JEV_SITES, apply_selection, site_enabled


def _site(task):
    return next(s for s in JEV_SITES if s.task == task)


def test_site_list_covers_every_decide_call_site():
    assert {s.task for s in JEV_SITES} == {
        "monitor", "goal_judge", "kanban_estimator", "curator_gate",
        "background_review_gate", "approval", "honcho_recall_gate",
        "honcho_dialectic_bailout",
    }


def test_apply_selection_writes_global_and_ack_keys():
    config: dict = {}
    apply_selection(config, {"approval", "monitor"}, key_env="TYPESAFE_API_KEY")

    aux = config["auxiliary"]
    assert aux["jev"] == {"enabled": True, "api_key_env": "TYPESAFE_API_KEY"}
    assert aux["approval"]["jev"] == {
        "enabled": True,
        "min_confidence": 0.85,
        "i_understand_commands_leave_host": True,
    }
    assert aux["monitor"]["jev"] == {"enabled": True, "min_confidence": 0.6}
    assert aux["goal_judge"]["jev"]["enabled"] is False


def test_apply_selection_keeps_tuned_confidence_and_drops_ack_on_disable():
    config = {"auxiliary": {"approval": {"model": "x", "jev": {
        "enabled": True, "min_confidence": 0.95, "i_understand_commands_leave_host": True,
    }}}}
    apply_selection(config, set(), key_env="TYPESAFE_API_KEY")

    approval = config["auxiliary"]["approval"]
    assert approval["model"] == "x"
    assert approval["jev"] == {"enabled": False, "min_confidence": 0.95}
    assert config["auxiliary"]["jev"]["enabled"] is False


def test_site_enabled_requires_ack_for_sensitive_sites():
    config = {"auxiliary": {"approval": {"jev": {"enabled": True}}}}
    assert site_enabled(config, _site("approval")) is False
    config["auxiliary"]["approval"]["jev"]["i_understand_commands_leave_host"] = True
    assert site_enabled(config, _site("approval")) is True


def test_config_written_by_setup_is_honored_by_decide(monkeypatch):
    """Round-trip: what the TUI writes is exactly what jev_enabled() reads."""
    from agent import jev_decide

    config: dict = {}
    apply_selection(config, {"background_review_gate"}, key_env="TYPESAFE_API_KEY")
    monkeypatch.setattr(
        jev_decide, "_task_config", lambda task: config["auxiliary"].get(task, {})
    )
    assert jev_decide.jev_enabled(
        "background_review_gate", require_ack="i_understand_turn_digests_leave_host"
    )
    assert not jev_decide.jev_enabled("approval")


def _patch_prompts(monkeypatch, *, key, yes_no, chosen):
    from hermes_cli import config as config_mod, curses_ui, setup

    saved = {}
    answers = iter(yes_no)
    monkeypatch.setattr(setup, "prompt", lambda *a, **k: key)
    monkeypatch.setattr(setup, "prompt_yes_no", lambda *a, **k: next(answers))
    monkeypatch.setattr(curses_ui, "curses_checklist", lambda *a, **k: chosen)
    monkeypatch.setattr(config_mod, "save_env_value", lambda k, v: saved.__setitem__(k, v))
    monkeypatch.setattr(config_mod, "get_env_value", lambda k: saved.get(k))
    return saved


def test_setup_flow_saves_key_and_enables_chosen_sites(monkeypatch):
    idx = {s.task: i for i, s in enumerate(JEV_SITES)}
    saved = _patch_prompts(
        monkeypatch,
        key="tsk-123456789",
        # test key? no  -> ack approval? yes
        yes_no=[False, True],
        chosen={idx["monitor"], idx["approval"]},
    )
    config: dict = {}
    jev_setup.setup_jev(config)

    assert saved == {"TYPESAFE_API_KEY": "tsk-123456789"}
    assert site_enabled(config, _site("monitor"))
    assert site_enabled(config, _site("approval"))
    assert config["auxiliary"]["jev"]["enabled"] is True
    # The key value itself never lands in config.
    assert "tsk-123456789" not in repr(config)


def test_setup_flow_declined_ack_leaves_site_off(monkeypatch):
    idx = {s.task: i for i, s in enumerate(JEV_SITES)}
    _patch_prompts(
        monkeypatch, key="tsk-123456789", yes_no=[False, False],
        chosen={idx["approval"]},
    )
    config: dict = {}
    jev_setup.setup_jev(config)

    assert not site_enabled(config, _site("approval"))
    assert config["auxiliary"]["jev"]["enabled"] is False


def test_setup_flow_without_key_turns_jev_off(monkeypatch):
    saved = _patch_prompts(monkeypatch, key="", yes_no=[], chosen=set())
    config = {"auxiliary": {"jev": {"enabled": True}}}
    jev_setup.setup_jev(config)

    assert saved == {}
    assert config["auxiliary"]["jev"]["enabled"] is False


def test_failed_ping_can_abort(monkeypatch):
    _patch_prompts(monkeypatch, key="tsk-bad", yes_no=[True, False], chosen=set())
    monkeypatch.setattr(jev_setup, "ping_jev", lambda c, k: (False, "401"))
    config: dict = {}
    jev_setup.setup_jev(config)
    assert "approval" not in config.get("auxiliary", {})


@pytest.mark.parametrize("action,expected", [("on", True), ("off", False)])
def test_on_off_toggle_global_switch(action, expected):
    from hermes_cli.config import load_config

    jev_setup.jev_command(argparse.Namespace(jev_action=action))
    assert load_config()["auxiliary"]["jev"]["enabled"] is expected


def test_cli_registers_jev_subcommand():
    parser = argparse.ArgumentParser()
    jev_setup.register_cli(parser)
    args = parser.parse_args(["status"])
    assert args.jev_action == "status" and args.func is jev_setup.jev_command
    assert parser.parse_args([]).jev_action is None
