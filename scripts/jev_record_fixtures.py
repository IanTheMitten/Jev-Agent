"""Record a live /v1/systemone response carrying all three answer types.

One-shot experiment script: hits the real Jev API once, writes the raw
response verbatim to tests/fixtures/jev/systemone_all_types.json so later
tests can replay it offline via httpx.MockTransport. Does not print, log,
or persist the API key.
"""

import json
import os
import sys
from pathlib import Path

import httpx

FIXTURE_PATH = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "jev" / "systemone_all_types.json"

REQUEST_BODY = {
    "model": "jev-latest",
    "state": {
        "command": "rm -rf ./build",
        "description": "recursive delete",
    },
    "questions": {
        "verdict": {
            "type": "choice",
            "instructions": "Is this shell command safe to run?",
            "criteria": {
                "approve": "clearly safe",
                "deny": "could damage the system",
                "escalate": "uncertain",
            },
        },
        "risky": {
            "type": "noul",
            "instructions": "Could this command destroy data the user wants to keep?",
        },
        "severity": {
            "type": "score",
            "instructions": "How damaging is this command if run?",
            "criteria": ["harmless", "minor", "moderate", "serious", "catastrophic"],
        },
    },
}


def main() -> int:
    api_key = os.environ.get("TYPESAFE_API_KEY")
    if not api_key:
        print("TYPESAFE_API_KEY is not set in the environment.", file=sys.stderr)
        return 2

    base_url = os.environ.get("TYPESAFE_BASE_URL") or "https://api.typesafe.ai"
    url = f"{base_url}/v1/systemone"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }

    response = httpx.post(url, headers=headers, json=REQUEST_BODY)
    print(f"HTTP status: {response.status_code}")

    body = response.json()
    print(json.dumps(body, indent=2))

    FIXTURE_PATH.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE_PATH.write_text(json.dumps(body, indent=2) + "\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
