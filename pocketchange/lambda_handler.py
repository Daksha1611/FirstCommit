"""The gateway, behind API Gateway.

Twelve lines, and they are the only code in this repository that knows the
application might be running in Lambda. That is the point: `gateway.app` is an
ordinary ASGI application, Mangum translates one event shape into another, and
nothing in the money path learns where it is.

Mangum is an optional dependency (`pip install -e ".[lambda]"`) because
importing it is the only reason it would ever be installed, and a local run has
no reason to.

Two things a reader should know before deploying this, both in deploy/README.md
at more length: the root signing key must come from KMS or Secrets Manager
rather than being generated per instance, and the in-process stores that this
project still uses for approvals and the agent registry do not survive a cold
start.
"""

from __future__ import annotations

from .gateway import app


def handler(event, context):
    from mangum import Mangum

    return Mangum(app, lifespan="off")(event, context)
