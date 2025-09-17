"""Environment loading and credential reporting.

This file had no tests and turned out to matter enormously. Nothing in the
gateway process called `load()`, so `.env` was never read, and every component
that decides whether the system is real - the payment rail, the durable ledger,
the monitor - read an empty environment and silently chose its stub. A fully
configured project reported `{"rail": "fake"}` for days.

The lesson that encodes: a configuration layer which fails by returning
plausible defaults needs its *reporting* tested, not just its parsing.

The port to AWS deleted the messiest thing this file did. The previous provider
handed out API keys under two different names depending on where you got them,
which were not interchangeable in practice, so `load()` had to know which one
was more likely to work and mirror it into the other. Bedrock has no key: boto3
resolves a role, an SSO profile or instance metadata on its own. There is
nothing to normalise, and `bedrock.available()` answers the only question that
was ever being asked.
"""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env"


def load(env_file: Path | None = None) -> dict[str, bool]:
    """Load .env and report what is configured.

    Never returns or logs a value - only whether each one is present. Secrets
    that reach a log are secrets that have been disclosed.
    """
    # An explicit opt-out, for tests. conftest.py scrubs credentials from the
    # environment so the suite cannot reach the network - and then this function
    # helpfully put them straight back, because it is called again inside every
    # model invocation. A test that believes it is offline and is not is worse
    # than no isolation at all: it is slow, it spends quota, and it passes for
    # reasons nobody intended.
    if os.getenv("POCKETCHANGE_NO_DOTENV"):
        return _status()

    path = env_file or ENV_FILE
    if path.exists():
        try:
            from dotenv import load_dotenv

            load_dotenv(path)
        except ImportError:
            _load_manually(path)

    return _status()


def _status() -> dict[str, bool]:
    from . import bedrock

    return {
        "bedrock": bedrock.available(),
        "razorpay": bool(os.getenv("RAZORPAY_KEY_ID") and os.getenv("RAZORPAY_KEY_SECRET")),
        "langfuse": bool(os.getenv("LANGFUSE_PUBLIC_KEY") and os.getenv("LANGFUSE_SECRET_KEY")),
    }


def _load_manually(path: Path) -> None:
    """Minimal .env parser, so config never hard-depends on python-dotenv."""
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def report() -> str:
    """One line naming what is configured. Safe to print."""
    status = load()
    return "  ".join(f"{k}:{'yes' if v else 'NO'}" for k, v in status.items())
