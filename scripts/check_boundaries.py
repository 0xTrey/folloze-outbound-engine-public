#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
ACTIVE_PATHS = [
    ROOT / "main.py",
    ROOT / "runtime_config.py",
    ROOT / "db" / "connection.py",
    ROOT / "connectors" / "apollo_client.py",
    ROOT / "connectors" / "perplexity_client.py",
    ROOT / "connectors" / "brave_client.py",
    ROOT / "connectors" / "x_client.py",
    ROOT / "connectors" / "smartlead_client.py",
    ROOT / "scripts" / "daily_outbound.sh",
    ROOT / "scripts" / "load_env.sh",
]
BANNED = [
    "EE_DATABASE_URL",
    "EE_APOLLO_API_KEY",
    "EE_RESEND_API_KEY",
    "elevationengine.co",
    "resend-elevation-engine",
]


def main() -> int:
    failures: list[str] = []
    for path in ACTIVE_PATHS:
        text = path.read_text()
        lowered = text.lower()
        for banned in BANNED:
            if banned.lower() in lowered:
                failures.append(f"{path}: contains banned token {banned!r}")

    if failures:
        for failure in failures:
            print(failure)
        return 1

    print("Folloze boundary check passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
