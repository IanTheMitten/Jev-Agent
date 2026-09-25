"""Interactive Jev setup: API key, global switch, and per-site toggles.

Before this existed, turning Jev on meant exporting ``TYPESAFE_API_KEY`` by
hand and uncommenting up to nine nested ``auxiliary.*.jev`` blocks in
``config.yaml``. This module collapses that into one flow reachable from
``hermes setup jev`` or ``hermes jev``:

  1. paste the key (masked) — saved to ``~/.hermes/.env`` like every other key
  2. optionally ping ``/v1/systemone`` to confirm the key works
  3. pick which decision sites to route through Jev (checklist)
  4. confirm the per-site acknowledgement for sites that send content off-host

The key value never touches ``config.yaml``; only the env var *name* does.
See docs/jev-decision-layers.md for what each site sends.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from hermes_cli.cli_output import print_error, print_info, print_success, print_warning
from hermes_cli.colors import Colors, color

DEFAULT_KEY_ENV = "TYPESAFE_API_KEY"
KEY_URL = "https://typesafe.ai"


@dataclass(frozen=True)
class JevSite:
    task: str
    label: str
    min_confidence: float
    ack: Optional[str] = None
    leaves_host: str = ""
    recommended: bool = False


# Order and defaults mirror the commented block in cli-config.yaml.example.
JEV_SITES: tuple[JevSite, ...] = (
    JevSite("monitor", "Cron urgency monitor", 0.6, recommended=True),
    JevSite("goal_judge", "Goal-loop judge", 0.75, recommended=True),
    JevSite("kanban_estimator", "Kanban token/complexity estimator", 0.6, recommended=True),
    JevSite("curator_gate", "Curator review-fork gate", 0.5, recommended=True),
    JevSite(
        "background_review_gate",
        "Background memory/skill review gate",
        0.5,
        ack="i_understand_turn_digests_leave_host",
        leaves_host="the last user/assistant turn text (clipped) and tool names",
        recommended=True,
    ),
    JevSite(
        "approval",
        "Smart approval guard",
        0.85,
        ack="i_understand_commands_leave_host",
        leaves_host="the shell command text being approved",
    ),
    JevSite(
        "honcho_recall_gate",
        "Honcho recall gate + reasoning level",
        0.5,
        ack="i_understand_memory_content_leaves_host",
        leaves_host="the user's message (clipped)",
    ),
    JevSite(
        "honcho_dialectic_bailout",
        "Honcho dialectic bail-out",
        0.6,
        ack="i_understand_memory_content_leaves_host",
        leaves_host="the user's message and the synthesized memory answer (clipped)",
    ),
)


def _aux(config: dict) -> dict:
    aux = config.get("auxiliary")
    if not isinstance(aux, dict):
        aux = {}
        config["auxiliary"] = aux
    return aux


def _global_cfg(config: dict) -> dict:
    aux = _aux(config)
    jev = aux.get("jev")
    if not isinstance(jev, dict):
        jev = {}
        aux["jev"] = jev
    return jev


def _site_cfg(config: dict, task: str) -> dict:
    aux = _aux(config)
    task_cfg = aux.get(task)
    if not isinstance(task_cfg, dict):
        task_cfg = {}
        aux[task] = task_cfg
    jev = task_cfg.get("jev")
    if not isinstance(jev, dict):
        jev = {}
        task_cfg["jev"] = jev
    return jev


def key_env_name(config: dict) -> str:
    aux = config.get("auxiliary") if isinstance(config, dict) else None
    jev = aux.get("jev") if isinstance(aux, dict) else None
    name = jev.get("api_key_env") if isinstance(jev, dict) else None
    return name if isinstance(name, str) and name.strip() else DEFAULT_KEY_ENV


def site_enabled(config: dict, site: JevSite) -> bool:
    aux = config.get("auxiliary") if isinstance(config, dict) else None
    task_cfg = aux.get(site.task) if isinstance(aux, dict) else None
    jev = task_cfg.get("jev") if isinstance(task_cfg, dict) else None
    if not isinstance(jev, dict) or jev.get("enabled") is not True:
        return False
    return site.ack is None or jev.get(site.ack) is True


def global_enabled(config: dict) -> bool:
    aux = config.get("auxiliary") if isinstance(config, dict) else None
    jev = aux.get("jev") if isinstance(aux, dict) else None
    return isinstance(jev, dict) and jev.get("enabled") is True


def apply_selection(config: dict, enabled_tasks: set[str], *, key_env: str) -> None:
    """Write the global switch and per-site blocks for *enabled_tasks*.

    Deselected sites are turned off (``enabled: false``) rather than removed,
    so a tuned ``min_confidence`` survives a later re-enable.
    """
    glob = _global_cfg(config)
    glob["enabled"] = bool(enabled_tasks)
    glob["api_key_env"] = key_env
    for site in JEV_SITES:
        jev = _site_cfg(config, site.task)
        on = site.task in enabled_tasks
        jev["enabled"] = on
        jev.setdefault("min_confidence", site.min_confidence)
        if site.ack:
            if on:
                jev[site.ack] = True
            else:
                jev.pop(site.ack, None)


def set_global(config: dict, enabled: bool) -> None:
    _global_cfg(config)["enabled"] = enabled


def _redact(value: str) -> str:
    if not value:
        return "not set"
    if len(value) <= 8:
        return "set (****)"
    return f"{value[:4]}…{value[-4:]}"


def ping_jev(config: dict, api_key: str) -> tuple[bool, str]:
    """Send one trivial noul question. Returns (ok, human-readable detail)."""
    from agent.jev_client import JevClient, noul_question

    glob = config.get("auxiliary", {}).get("jev", {}) if isinstance(config, dict) else {}
    kwargs: dict[str, Any] = {"timeout": 5.0, "deadline": 10.0, "max_retries": 0}
    for key in ("base_url", "model"):
        if isinstance(glob, dict) and glob.get(key):
            kwargs[key] = glob[key]
    try:
        client = JevClient(api_key=api_key, **kwargs)
        result = client.systemone(
            {"probe": "hermes setup connectivity check"},
            {"ok": noul_question("Is this a connectivity check?")},
        )
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
    answer = result.answers.get("ok")
    if answer is None:
        return False, "response carried no answer"
    return True, f"noul={answer.noul}"


def _site_row(site: JevSite) -> str:
    row = f"{site.label}  [{site.task}]"
    if site.leaves_host:
        row += "  (sends content off-host)"
    return row


def setup_jev(config: dict) -> None:
    """Interactive flow used by ``hermes setup jev`` and ``hermes jev``."""
    from hermes_cli.config import get_env_value, save_env_value
    from hermes_cli.curses_ui import curses_checklist
    from hermes_cli.setup import print_header, prompt, prompt_yes_no

    print_header("Jev Decision Layers")
    print_info("Jev answers yes/no, pick-one and score questions at gate points")
    print_info("(approval, cron urgency, goal judge, review forks) so those calls")
    print_info("skip a full LLM round-trip. Every site falls back to stock Hermes")
    print_info("behavior when Jev is off, unreachable, or unsure.")
    print()

    key_env = key_env_name(config)
    current = get_env_value(key_env) or ""
    print_info(f"Key variable: {key_env}  (current: {_redact(current)})")
    print_info(f"Get a key at: {KEY_URL}")
    entered = prompt(
        "  Paste Jev API key (Enter to keep current)" if current else "  Paste Jev API key",
        password=True,
    )
    api_key = entered or current
    if entered:
        save_env_value(key_env, entered)
        print_success(f"  Saved {key_env} to ~/.hermes/.env")
    elif not current:
        print_warning("  No key entered — Jev stays off. Re-run 'hermes jev' any time.")
        set_global(config, False)
        return

    if prompt_yes_no("  Test the key against the Jev API now?", default=True):
        ok, detail = ping_jev(config, api_key)
        if ok:
            print_success(f"  Jev reachable ({detail})")
        else:
            print_error(f"  Jev check failed: {detail}")
            if not prompt_yes_no("  Continue enabling sites anyway?", default=False):
                return

    fresh = not any(site_enabled(config, s) for s in JEV_SITES)
    preselected = {
        i for i, s in enumerate(JEV_SITES)
        if (s.recommended if fresh else site_enabled(config, s))
    }
    chosen = curses_checklist(
        "Route which decision sites through Jev?",
        [_site_row(s) for s in JEV_SITES],
        preselected,
    )

    enabled: set[str] = set()
    for i in sorted(chosen):
        site = JEV_SITES[i]
        if site.ack and not site_enabled(config, site):
            print()
            print(color(f"  ─── {site.label} ───", Colors.CYAN))
            print_info(f"  When enabled, {site.leaves_host} is sent to Jev.")
            if not prompt_yes_no("  Acknowledge and enable?", default=True):
                print_info("  Left on the stock path.")
                continue
        enabled.add(site.task)

    apply_selection(config, enabled, key_env=key_env)
    print()
    if enabled:
        print_success(f"Jev enabled for {len(enabled)}/{len(JEV_SITES)} sites.")
    else:
        print_info("No sites selected — Jev global switch turned off.")
    print_info("Turn everything off instantly with: hermes jev off")


def print_status(config: dict) -> None:
    from hermes_cli.config import get_env_value

    key_env = key_env_name(config)
    key = get_env_value(key_env) or ""
    state = "on" if global_enabled(config) else "off"
    print(color("◆ Jev", Colors.CYAN, Colors.BOLD))
    print(f"  Global switch   {state}")
    print(f"  {key_env:<15} {_redact(key)}")
    for site in JEV_SITES:
        mark = "✓" if site_enabled(config, site) else "·"
        print(f"  {mark} {site.label:<40} [{site.task}]")


def jev_command(args) -> None:
    """Entry point for ``hermes jev [setup|status|test|on|off]``."""
    from hermes_cli.config import get_env_value, load_config, save_config

    action = getattr(args, "jev_action", None) or "setup"
    config = load_config()

    if action == "status":
        print_status(config)
        return
    if action == "test":
        key = get_env_value(key_env_name(config)) or ""
        if not key:
            print_error(f"{key_env_name(config)} is not set. Run 'hermes jev' first.")
            raise SystemExit(1)
        ok, detail = ping_jev(config, key)
        (print_success if ok else print_error)(
            f"Jev {'reachable' if ok else 'check failed'}: {detail}"
        )
        if not ok:
            raise SystemExit(1)
        return
    if action in ("on", "off"):
        set_global(config, action == "on")
        save_config(config)
        print_success(f"Jev global switch {action}.")
        return

    from hermes_cli.setup import is_interactive_stdin

    if not is_interactive_stdin():
        print_error("'hermes jev' needs a terminal. Non-interactive alternative:")
        print_info(f"  hermes config set auxiliary.jev.enabled true  (and set {DEFAULT_KEY_ENV} in ~/.hermes/.env)")
        raise SystemExit(1)
    setup_jev(config)
    save_config(config)


def register_cli(parser) -> None:
    sub = parser.add_subparsers(dest="jev_action")
    sub.add_parser("setup", help="Interactive key + site setup (default)")
    sub.add_parser("status", help="Show key, global switch and per-site state")
    sub.add_parser("test", help="Ping the Jev API with the saved key")
    sub.add_parser("on", help="Turn the global Jev switch on")
    sub.add_parser("off", help="Turn the global Jev switch off (kill switch)")
    parser.set_defaults(func=jev_command)
