# Two stages: build the console with Node, run the gateway with Python. The
# image carries no Node runtime and no source for the frontend, only its dist.

FROM node:22-slim AS console
WORKDIR /console
COPY frontend/package.json frontend/package-lock.json ./
# Not --omit=dev: vite is a devDependency, and this stage exists to run it. The
# stage is discarded, so nothing here reaches the final image.
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
# The demo token is public by design: it gates writes so a crawler cannot drain
# a shared free tier, and it ships inside a page anyone can read.
ARG VITE_DEMO_TOKEN=""
ENV VITE_DEMO_TOKEN=$VITE_DEMO_TOKEN
RUN npm run build

# ---------------------------------------------------------------------------
# Python 3.12 exactly: biscuit-python has no 3.14 wheel and its source build
# fails on PyO3 0.24. Pinning this in the image is not incidental.
FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    # POCKETCHANGE_EPHEMERAL_KEYS is deliberately NOT set here, and that is a
    # correctness requirement rather than a preference. It used to be, back
    # when a per-instance key was the only honest option on an ephemeral
    # container filesystem. `pocketchange/secrets.py` closed that gap, but
    # `gateway._root_keypair()` checks the ephemeral flag FIRST - so baking it
    # into the image would silently win over Secrets Manager and hand every
    # instance its own key again, which is the exact bug the secret exists to
    # close. See tests/test_image.py.
    #
    # .env is not shipped. A deployment injects real environment variables, and
    # a dotenv file baked into an image is a credential in a registry.
    POCKETCHANGE_NO_DOTENV=1

WORKDIR /app

COPY pyproject.toml ./
COPY pocketchange/ ./pocketchange/
COPY agent/ ./agent/
COPY merchant/ ./merchant/
COPY eval/ ./eval/
# The Cedar policy set, and not optional. `cedar.decide` fails CLOSED when it
# cannot read a policy - which is the correct behaviour for the only rule
# standing between a broker and the money, and means an image built without this
# line answers 503 to every payment.
COPY policies/ ./policies/

RUN pip install --no-cache-dir -e ".[biscuit,agent,policy]"

COPY --from=console /console/dist ./frontend/dist

RUN useradd --create-home --uid 1001 runner && chown -R runner:runner /app
USER runner

# Read from the environment rather than hardcoded: most container runtimes
# choose the port and will not always choose 8080.
ENV PORT=8080
EXPOSE 8080
CMD exec uvicorn pocketchange.gateway:app --host 0.0.0.0 --port ${PORT} --workers 1
