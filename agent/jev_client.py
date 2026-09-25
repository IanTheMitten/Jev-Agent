"""HTTP client for the Jev ``/v1/systemone`` decision endpoint.

Owns every transport concern (headers, retries, the outer wall-clock
deadline, and response parsing) in one place so the nine call sites that
consult Jev never reimplement them. The vendor SDK's ``RequestOptions.timeout``
is per attempt with no total retry budget, so the deadline tracked here is
this module's responsibility, not the SDK's.

See docs/specs/2026-09-23-jev-decision-layers.md:230-238 for the error
handling and retry policy this implements.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

import httpx


class JevError(Exception):
    """Raised for any failure talking to Jev: transport, HTTP, or parse errors."""


@dataclass(frozen=True)
class JevAnswer:
    name: str
    type: str
    noul: float | None = None
    choice: str | None = None
    score: float | None = None
    confidence: float | None = None
    legend: dict | None = None
    probabilities: dict | None = None


@dataclass(frozen=True)
class JevResult:
    model: str
    answers: dict[str, JevAnswer]
    input_tokens: int
    output_tokens: int


def noul_question(instructions, *, yes=None, no=None) -> dict:
    question = {"type": "noul", "instructions": instructions}
    if yes is not None:
        question["yes"] = yes
    if no is not None:
        question["no"] = no
    return question


def choice_question(instructions, criteria) -> dict:
    return {"type": "choice", "instructions": instructions, "criteria": criteria}


def score_question(instructions, criteria) -> dict:
    if len(criteria) < 2:
        raise ValueError("score_question requires at least two criteria")
    return {"type": "score", "instructions": instructions, "criteria": criteria}


_RETRY_STATUSES = {408, 429}


def _retry_after_seconds(response: httpx.Response) -> float | None:
    value = response.headers.get("Retry-After")
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _build_answer(name: str, raw: dict) -> JevAnswer:
    if not isinstance(raw, dict) or "type" not in raw:
        raise JevError(f"jev systemone: malformed answer for {name!r}")
    answer_type = raw["type"]
    return JevAnswer(
        name=name,
        type=answer_type,
        noul=raw.get("noul"),
        choice=raw.get("choice"),
        score=raw.get("score"),
        confidence=None if answer_type == "noul" else raw.get("confidence"),
        legend=raw.get("legend"),
        probabilities=raw.get("probabilities"),
    )


class JevClient:
    def __init__(
        self,
        *,
        api_key,
        base_url="https://api.typesafe.ai",
        model="jev-latest",
        timeout=2.0,
        deadline=5.0,
        max_retries=1,
        transport=None,
    ):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.deadline = deadline
        self.max_retries = max_retries
        self._client = httpx.Client(transport=transport, timeout=timeout)

    def systemone(self, state, questions, *, model=None) -> JevResult:
        url = f"{self.base_url}/v1/systemone"
        body = {"state": state, "questions": dict(questions), "model": model or self.model}
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

        deadline_at = time.monotonic() + self.deadline
        backoff = 0.25
        attempt = 0

        while True:
            if time.monotonic() >= deadline_at:
                raise JevError("jev systemone: outer deadline exceeded")

            try:
                response = self._client.post(url, json=body, headers=headers)
            except httpx.TransportError as exc:
                if attempt >= self.max_retries or time.monotonic() >= deadline_at:
                    raise JevError(f"jev systemone: transport error: {exc}") from exc
                attempt += 1
                time.sleep(backoff)
                backoff = min(backoff * 2, 1.0)
                continue

            if response.status_code in _RETRY_STATUSES or response.status_code >= 500:
                if attempt >= self.max_retries or time.monotonic() >= deadline_at:
                    raise JevError(f"jev systemone: HTTP {response.status_code}")
                attempt += 1
                sleep_for = backoff
                retry_after = _retry_after_seconds(response)
                if retry_after is not None and time.monotonic() + retry_after < deadline_at:
                    sleep_for = retry_after
                time.sleep(sleep_for)
                backoff = min(backoff * 2, 1.0)
                continue

            if response.status_code >= 400:
                raise JevError(f"jev systemone: HTTP {response.status_code}")

            return self._parse(response, questions)

    def _parse(self, response: httpx.Response, questions) -> JevResult:
        try:
            payload = response.json()
        except ValueError as exc:
            raise JevError(f"jev systemone: non-JSON response: {exc}") from exc

        if not isinstance(payload, dict) or "answers" not in payload:
            raise JevError("jev systemone: response missing 'answers'")

        raw_answers = payload["answers"]
        answers = {}
        for name in questions:
            if name not in raw_answers:
                raise JevError(f"jev systemone: missing answer for question {name!r}")
            answers[name] = _build_answer(name, raw_answers[name])

        usage = payload.get("usage") or {}
        return JevResult(
            model=payload.get("model", ""),
            answers=answers,
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
        )
