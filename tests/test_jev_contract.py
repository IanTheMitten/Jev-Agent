"""Contract tests for the not-yet-implemented ``agent.jev_client`` (Task 3).

Pins the request shape, header set, response parsing, retry rules, and the
outer wall-clock deadline against ``tests/fixtures/jev/systemone_all_types.json``
and ``docs/specs/2026-09-23-jev-decision-layers.md:232-238``, before any
implementation exists.

Every test imports ``agent.jev_client`` locally (not at module scope) so
collection succeeds even though the module doesn't exist yet; running is
expected red with ``ModuleNotFoundError`` until Task 3 lands.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import httpx
import pytest

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "jev" / "systemone_all_types.json").read_text()
)


def _client(transport, **kwargs):
    from agent.jev_client import JevClient

    return JevClient(api_key="test-key", transport=transport, **kwargs)


# ---------------------------------------------------------------------------
# Contract: fixture replay parses into JevResult
# ---------------------------------------------------------------------------


def test_systemone_parses_fixture():
    from agent.jev_client import choice_question, noul_question, score_question

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=FIXTURE)

    client = _client(httpx.MockTransport(handler))
    result = client.systemone(
        {"command": "rm -rf /"},
        {
            "verdict": choice_question(
                "verdict?", {"approve": None, "deny": None, "escalate": None}
            ),
            "risky": noul_question("risky?"),
            "severity": score_question("severity?", ["lo", "hi"]),
        },
    )

    assert result.model
    assert result.answers["verdict"].choice in {"approve", "deny", "escalate"}
    risky = result.answers["risky"]
    assert isinstance(risky.noul, float)
    assert 0.0 <= risky.noul <= 1.0
    assert risky.confidence is None
    severity = result.answers["severity"]
    assert isinstance(severity.score, float)
    assert severity.legend
    assert isinstance(result.input_tokens, int)
    assert isinstance(result.output_tokens, int)


# ---------------------------------------------------------------------------
# Request shape
# ---------------------------------------------------------------------------


def test_request_shape():
    from agent.jev_client import noul_question

    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["request"] = request
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=FIXTURE)

    client = _client(httpx.MockTransport(handler))
    client.systemone({"k": "v"}, {"risky": noul_question("q")})

    request = captured["request"]
    assert request.url.path.endswith("/v1/systemone")
    assert request.method == "POST"
    assert request.headers["Authorization"] == "Bearer test-key"
    assert request.headers["Accept"] == "application/json"
    assert request.headers["Content-Type"] == "application/json"
    body = captured["body"]
    assert set(body.keys()) == {"state", "questions", "model"}
    assert body["model"] == "jev-latest"


# ---------------------------------------------------------------------------
# Retry
# ---------------------------------------------------------------------------


def test_retry_success_after_500():
    from agent.jev_client import noul_question

    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(500, json={"error": "boom"})
        return httpx.Response(200, json=FIXTURE)

    client = _client(httpx.MockTransport(handler), max_retries=1)
    result = client.systemone({}, {"risky": noul_question("q")})

    assert len(calls) == 2
    assert result.model


def test_retry_exhausted_after_500():
    from agent.jev_client import JevError, noul_question

    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(500, json={"error": "boom"})

    client = _client(httpx.MockTransport(handler))
    with pytest.raises(JevError):
        client.systemone({}, {"risky": noul_question("q")})

    assert len(calls) == 2


@pytest.mark.parametrize("status", [400, 401, 403])
def test_no_retry_on_4xx(status):
    from agent.jev_client import JevError, noul_question

    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(status, json={"error": "no"})

    client = _client(httpx.MockTransport(handler))
    with pytest.raises(JevError):
        client.systemone({}, {"risky": noul_question("q")})

    assert len(calls) == 1


@pytest.mark.parametrize("status", [408, 429])
def test_retry_on_408_429(status):
    from agent.jev_client import JevError, noul_question

    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(status, json={"error": "slow"})

    client = _client(httpx.MockTransport(handler))
    with pytest.raises(JevError):
        client.systemone({}, {"risky": noul_question("q")})

    assert len(calls) == 2


# ---------------------------------------------------------------------------
# Outer deadline
# ---------------------------------------------------------------------------


def test_outer_deadline_stops_retries():
    from agent.jev_client import JevError, noul_question

    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        time.sleep(0.2)
        return httpx.Response(500, json={"error": "boom"})

    client = _client(
        httpx.MockTransport(handler), timeout=0.05, deadline=0.12, max_retries=5
    )
    with pytest.raises(JevError):
        client.systemone({}, {"risky": noul_question("q")})

    assert len(calls) <= 2


# ---------------------------------------------------------------------------
# Malformed responses
# ---------------------------------------------------------------------------


def test_malformed_non_json():
    from agent.jev_client import JevError, noul_question

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not json")

    client = _client(httpx.MockTransport(handler))
    with pytest.raises(JevError):
        client.systemone({}, {"risky": noul_question("q")})


def test_malformed_empty_body():
    from agent.jev_client import JevError, noul_question

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    client = _client(httpx.MockTransport(handler))
    with pytest.raises(JevError):
        client.systemone({}, {"risky": noul_question("q")})


def test_malformed_missing_requested_answer():
    from agent.jev_client import JevError, noul_question

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "model": "jev-1",
                "answers": {},
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        )

    client = _client(httpx.MockTransport(handler))
    with pytest.raises(JevError):
        client.systemone({}, {"risky": noul_question("q")})


# ---------------------------------------------------------------------------
# Question builders
# ---------------------------------------------------------------------------


def test_noul_question_builder():
    from agent.jev_client import noul_question

    assert noul_question("q") == {"type": "noul", "instructions": "q"}


def test_choice_question_builder():
    from agent.jev_client import choice_question

    assert choice_question("q", {"a": None}) == {
        "type": "choice",
        "instructions": "q",
        "criteria": {"a": None},
    }


def test_score_question_builder():
    from agent.jev_client import score_question

    assert score_question("q", ["lo", "hi"]) == {
        "type": "score",
        "instructions": "q",
        "criteria": ["lo", "hi"],
    }


def test_score_question_requires_two_criteria():
    from agent.jev_client import score_question

    with pytest.raises(ValueError):
        score_question("q", ["only"])
