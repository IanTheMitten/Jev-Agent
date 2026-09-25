"""Approval safety: no Jev failure or uncertainty mode may resolve to "approve" (C3).

Dedicated file (spec `docs/specs/2026-09-23-jev-decision-layers.md:250`) so a bulk
edit to the broader site suite (`tests/test_jev_sites_tier_a.py`) can't weaken
these assertions.

``tools.approval._smart_approve`` does not yet import `agent.jev_decide.decide`
(that lands in a later task) — every test here is expected to fail until then.
``decide`` is patched at its defining module (`agent.jev_decide`), the same
pattern already used for ``call_llm`` (patched at `agent.auxiliary_client`,
see `tests/tools/test_smart_approval_policy.py`), because both are expected to
be imported lazily inside the function.
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from agent.jev_client import JevAnswer
from agent.jev_decide import Decision
from tools.approval import _smart_approve


def _decision(*, ok, status, answers=None, min_confidence=0.85):
    return Decision(ok=ok, answers=answers or {}, status=status, min_confidence=min_confidence)


def _choice_answer(choice, confidence):
    return JevAnswer(name="verdict", type="choice", choice=choice, confidence=confidence)


def _make_response(word):
    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = word
    return resp


class TestSmartApproveNeverDefaultsToApprove(unittest.TestCase):
    """No Jev failure or uncertainty mode may resolve to "approve" (C3)."""

    @patch("agent.auxiliary_client.call_llm")
    @patch("agent.jev_decide.decide")
    def test_disabled_status_with_call_llm_raising_escalates(self, mock_decide, mock_call_llm):
        mock_decide.return_value = _decision(ok=False, status="disabled")
        mock_call_llm.side_effect = RuntimeError("network down")

        result = _smart_approve("rm -rf /tmp/x", "recursive delete")

        self.assertEqual(result, "escalate")
        mock_decide.assert_called_once()

    @patch("agent.auxiliary_client.call_llm")
    @patch("agent.jev_decide.decide")
    def test_jev_error_status_escalates(self, mock_decide, mock_call_llm):
        mock_decide.return_value = _decision(ok=False, status="error:JevError")
        mock_call_llm.return_value = _make_response("APPROVE")

        result = _smart_approve("rm -rf /tmp/x", "recursive delete")

        self.assertEqual(result, "escalate")
        mock_decide.assert_called_once()

    @patch("agent.auxiliary_client.call_llm")
    @patch("agent.jev_decide.decide")
    def test_no_key_status_escalates(self, mock_decide, mock_call_llm):
        mock_decide.return_value = _decision(ok=False, status="no_key")
        mock_call_llm.return_value = _make_response("APPROVE")

        result = _smart_approve("rm -rf /tmp/x", "recursive delete")

        self.assertEqual(result, "escalate")
        mock_decide.assert_called_once()

    @patch("agent.auxiliary_client.call_llm")
    @patch("agent.jev_decide.decide")
    def test_missing_ack_status_escalates(self, mock_decide, mock_call_llm):
        mock_decide.return_value = _decision(ok=False, status="missing_ack")
        mock_call_llm.return_value = _make_response("APPROVE")

        result = _smart_approve("rm -rf /tmp/x", "recursive delete")

        self.assertEqual(result, "escalate")
        mock_decide.assert_called_once()

    @patch("agent.auxiliary_client.call_llm")
    @patch("agent.jev_decide.decide")
    def test_ok_but_not_confident_escalates(self, mock_decide, mock_call_llm):
        answers = {"verdict": _choice_answer("approve", confidence=0.5)}
        mock_decide.return_value = _decision(ok=True, status="ok", answers=answers)
        mock_call_llm.return_value = _make_response("APPROVE")

        result = _smart_approve("rm -rf /tmp/x", "recursive delete")

        self.assertEqual(result, "escalate")
        mock_decide.assert_called_once()

    @patch("agent.auxiliary_client.call_llm")
    @patch("agent.jev_decide.decide")
    def test_confident_escalate_choice_escalates(self, mock_decide, mock_call_llm):
        answers = {"verdict": _choice_answer("escalate", confidence=0.95)}
        mock_decide.return_value = _decision(ok=True, status="ok", answers=answers)
        mock_call_llm.return_value = _make_response("APPROVE")

        result = _smart_approve("rm -rf /tmp/x", "recursive delete")

        self.assertEqual(result, "escalate")
        mock_decide.assert_called_once()


class TestSmartApproveConfidentChoicePassesThrough(unittest.TestCase):
    @patch("agent.auxiliary_client.call_llm")
    @patch("agent.jev_decide.decide")
    def test_confident_approve_returns_approve(self, mock_decide, mock_call_llm):
        answers = {"verdict": _choice_answer("approve", confidence=0.95)}
        mock_decide.return_value = _decision(ok=True, status="ok", answers=answers)
        mock_call_llm.return_value = _make_response("ESCALATE")

        result = _smart_approve("echo hi", "benign")

        self.assertEqual(result, "approve")
        mock_decide.assert_called_once()

    @patch("agent.auxiliary_client.call_llm")
    @patch("agent.jev_decide.decide")
    def test_confident_deny_returns_deny(self, mock_decide, mock_call_llm):
        answers = {"verdict": _choice_answer("deny", confidence=0.95)}
        mock_decide.return_value = _decision(ok=True, status="ok", answers=answers)
        mock_call_llm.return_value = _make_response("ESCALATE")

        result = _smart_approve("dd if=/dev/zero of=/dev/sda", "wipe disk")

        self.assertEqual(result, "deny")
        mock_decide.assert_called_once()


class TestSmartApproveMissingAckConsultsCallLLM(unittest.TestCase):
    """Even a confident, above-threshold Jev "approve" is downgraded to
    "missing_ack" when the operator hasn't set the acknowledgement key —
    ``decide`` itself returns "missing_ack" in that case. The site must still
    consult the existing call_llm guard and must never default to approve."""

    @patch("agent.auxiliary_client.call_llm")
    @patch("agent.jev_decide.decide")
    def test_missing_ack_falls_back_to_call_llm_and_never_approves(self, mock_decide, mock_call_llm):
        mock_decide.return_value = _decision(ok=False, status="missing_ack")
        mock_call_llm.return_value = _make_response("ESCALATE")

        result = _smart_approve("rm -rf /tmp/x", "recursive delete")

        self.assertEqual(result, "escalate")
        mock_call_llm.assert_called_once()
        mock_decide.assert_called_once()


if __name__ == "__main__":
    unittest.main()
