"""Shared fail-open decision wrapper around :class:`agent.jev_client.JevClient`.

Every Jev call site goes through :func:`decide`: it reads config, enforces the
kill switch and per-task acknowledgement keys, derives a comparable confidence
per answer type, and never raises. A call site becomes a branch on
``Decision.ok`` / ``Decision.confident(name)`` and nothing more.

See docs/specs/2026-09-23-jev-decision-layers.md:118-120,230-238.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Mapping

from agent.jev_client import JevAnswer, JevClient

logger = logging.getLogger(__name__)


def _task_config(task: str) -> dict:
    try:
        from agent.auxiliary_client import _get_auxiliary_task_config

        return _get_auxiliary_task_config(task)
    except Exception:
        return {}


def _build_client(cfg: dict) -> JevClient:
    kwargs: dict[str, Any] = {}
    for key in ("base_url", "model", "timeout", "deadline", "max_retries"):
        if key in cfg:
            kwargs[key] = cfg[key]
    api_key = os.environ[cfg.get("api_key_env", "TYPESAFE_API_KEY")]
    return JevClient(api_key=api_key, **kwargs)


@dataclass(frozen=True)
class Decision:
    ok: bool
    answers: Mapping[str, JevAnswer]
    status: str
    min_confidence: float

    def confidence(self, name: str) -> float:
        answer = self.answers.get(name)
        if answer is None:
            return 0.0
        return _answer_confidence(answer)

    def confident(self, name: str) -> bool:
        return name in self.answers and self.confidence(name) >= self.min_confidence


def _answer_confidence(answer: JevAnswer) -> float:
    if answer.type == "noul":
        return abs((answer.noul or 0.0) - 0.5) * 2
    return answer.confidence if answer.confidence is not None else 0.0


def jev_enabled(task, *, require_ack=None) -> bool:
    if _task_config("jev").get("enabled") is not True:
        return False
    task_jev = _task_config(task).get("jev", {})
    if not isinstance(task_jev, dict):
        return False
    if task_jev.get("enabled") is not True:
        return False
    if require_ack is not None and task_jev.get(require_ack) is not True:
        return False
    return True


def decide(task, *, state, questions, require_ack=None) -> Decision:
    global_cfg = _task_config("jev")
    task_cfg = _task_config(task)
    task_jev = task_cfg.get("jev", {})
    if not isinstance(task_jev, dict):
        task_jev = {}
    min_confidence = task_jev.get("min_confidence", 0.5)

    if global_cfg.get("enabled") is not True or task_jev.get("enabled") is not True:
        return Decision(False, {}, "disabled", min_confidence)

    if require_ack is not None and task_jev.get(require_ack) is not True:
        return Decision(False, {}, "missing_ack", min_confidence)

    try:
        api_key = os.environ.get(global_cfg.get("api_key_env", "TYPESAFE_API_KEY"))
        if not api_key:
            return Decision(False, {}, "no_key", min_confidence)

        client = _build_client(global_cfg)
        result = client.systemone(state, questions, model=global_cfg.get("model"))

        for name, answer in result.answers.items():
            if answer.type == "choice":
                question = questions.get(name)
                criteria = question.get("criteria", {}) if question else {}
                if answer.choice not in criteria:
                    raise ValueError(
                        f"jev decide: choice {answer.choice!r} outside declared criteria for {name!r}"
                    )

        return Decision(True, result.answers, "ok", min_confidence)
    except BaseException as exc:
        logger.debug("jev decide(%s) failed: %s", task, exc, exc_info=True)
        return Decision(False, {}, f"error:{type(exc).__name__}", min_confidence)
