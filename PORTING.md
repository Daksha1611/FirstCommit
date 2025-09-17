# Porting Pocket Change to AWS

Working notes for the move off Google Cloud. Same rule as `hey.md`: write down the
*reasoning*, not just the outcome, because in three weeks the outcome will be
obvious from the code and the reasoning will not.

Read this before changing anything in `pocketchange/bedrock.py`, `agent/`,
`pocketchange/policy.py` or the storage backends — several of the choices below
look arbitrary until you know what was tried first.

---

## 0. Why any of this

The project was built against Gemini, Vertex, Firestore and Cloud Run because a
previous event required a Google Cloud service. None of that was load-bearing:
the thing the project actually claims — that enforcement is separable from the
reasoning layer — is a claim about the gateway, not about who hosts the model.

So the port is a test of the claim. If swapping the model provider, the agent
framework and the database required touching `pocketchange/token.py`,
`ledger.py` or the `/pay` decision path, then enforcement was never really
separate and the project's headline was decoration.

It mostly did not. What did have to change is listed in §3.

---

## 1. Track: Build It, not Ship It

**Decision.** Target the open-source track and run entirely locally.

**Why.** Build It is scored on AWS *open-source* usage — Strands Agents SDK,
Cedar, SAM CLI + LocalStack, OpenSearch — and explicitly needs no deployment and
no AWS account. Ship It requires a live URL, which is the one thing we are not
doing. The two tracks are judged with equal care, so there is no penalty for
staying local.

It also happens to fit what the repo already had: `agent/strands_buyer.py`
existed before the port as a second agent framework used to prove the gateway
was framework-agnostic. That file stops being an experiment and becomes the
main path.

---

## 2. Decisions

### D1 — Amazon Bedrock replaces the Gemini / Vertex pair

The old path had two doors to the same models: an AI Studio API key and a Vertex
project, with a documented preference order between them. Both are gone.

**Why Bedrock, beyond "it is the AWS one".** The old primary authenticated with a
bare API key belonging to whoever pasted it into `.env`. For a project whose
subject is *bounded authority*, having the trusted monitor authenticate with a
shared secret that no one administers was an embarrassment hiding in plain
sight. Bedrock signs with the ordinary AWS credential chain, so the model is
reached the same way the ledger's table is — an IAM principal, a region, and a
policy that can be read and scoped to named model ids.

**Client.** `AnthropicBedrockMantle` from the `anthropic` SDK rather than raw
`boto3` `bedrock-runtime`. It is the documented Messages-API path onto Bedrock
and it gives schema-enforced structured output (`messages.parse`), which is
exactly what `StructuredChain` was hand-rolling with a JSON-schema hint in the
prompt. `boto3` is still a dependency — Strands and DynamoDB use it.

### D2 — Three model tiers, and the monitor gets the smallest

`WORK_MODEL` for the buyer's reasoning, `SHOPPER_MODEL` for the parallel
fan-out, `JUDGE_MODEL` for the monitor.

This mirrors the old flash/flash-lite split, but the reason is different and
worth stating plainly: the monitor is small **by design**, not to save money.
It is the trusted component in an AI-control arrangement and it answers one
narrow question. A trusted layer should be simple enough to reason about.

### D3 — `spread_models()` keeps its shape but loses its trick

The old implementation returned a *different* model name per parallel agent,
because the free tier metered quota per model name and six agents sharing one
name queued behind one bucket. That was a real finding and a good hack.

It buys nothing on Bedrock, which meters per account and region. Returning
distinct model ids now would spread load across models of *different
capability* for no benefit — strictly worse. So the function returns the same
shopper model N times and throttling is handled by SDK retry, where it belongs.

The signature stays because the graph builder asks for one id per agent, and a
genuine tier split (a stronger model for one strategy) would live here.

### D4 — Region defaults to `us-east-1`, not `ap-south-1`

Every price in this project is in paise and the old Vertex location was
`asia-south1`, so Mumbai is the sentimental choice. Bedrock model availability
is not uniform across regions, and a demo that dies because a model is not
served in `ap-south-1` dies for a reason that has nothing to do with what is
being demonstrated. Overridable via `POCKETCHANGE_BEDROCK_REGION`.

Model ids may also need a geography prefix (`us.`, `eu.`, `apac.`) when the
region serves them through a cross-region inference profile. Which one is a
property of the account, not of the code, so every id is overridable and the
error is allowed to surface rather than being guessed at.

### D5 — Strands Agents SDK replaces google-adk

*(in progress — see §3 for status)*

`LlmAgent` → `strands.Agent`, and ADK's `LoopAgent` / `ParallelAgent`
composition → an explicit orchestrator. The nodes were always thin: name, model,
description, instruction, tools. The topology lived in `builder.py` and moves
there too.

### D6 — Cedar for the policy layer at `/pay`

Step 4b of the money path already carried this comment: *"The token cannot
express 'may grant but not spend', so the enforcement point does. Said plainly
because the distinction matters: the per-seller caps below are a cryptographic
guarantee; this line is a rule we chose to apply here."*

That is a policy language asking to exist. Cedar is exactly that, it is AWS
open-source, and it separates the two guarantees the code was already careful to
distinguish — attenuation stays cryptographic in the token, and the rules we
*chose* become readable, testable policy instead of `if` statements buried in a
2000-line module.

**The policy file is deliberately short, and that is the argument, not an
apology.** Two forbid rules and a permit. The temptation was to move the scope
and budget checks in as well and have an impressive-looking `.cedar` file, and
that would have been wrong: a policy engine asked to re-confirm what a signature
already settled is a second opinion on a fact, and a Cedar rule that agreed with
the token would be decoration that could later drift out of agreement. So Cedar
got exactly the part that was a decision:

- `broker-may-not-spend` — was an `if` at line 1173.
- `payout-is-not-enabled` — was an unreachable `raise` at the bottom of
  `/payout`, described in its own comment as "written closed rather than left to
  fall through". It is now a switch in the file a person would actually look in.

**What stayed in Python.** The empty-context refusal. "Did the caller state a
reason" is a question about whether the request is well-formed, not about
whether this principal may act on this resource, and Cedar answers the second
question only.

**Fails closed**, unlike the monitor. The monitor is a second opinion and
blocking every payment because it is unreachable would make the system worse
than having no monitor. This is the only thing standing between a broker and the
money, so an unanswerable policy question refuses with a 503.

**Cost.** The enforcement path is timed and that number is quoted in the README,
so the policy set is parsed once at import into a `PolicySet` handle and reused.
Parsing is the dominant cost of an authorisation call; doing it per payment
would have put a millisecond into the headline figure.

### D7 — DynamoDB replaces Firestore

`ledger.py` already documented the old store as a poor fit: the mandate row is a
hot document, and one sustained write per second to the same document is that
database's least favourite pattern. Its own comment wished for "a relational
database with SELECT ... FOR UPDATE".

DynamoDB gets closer than either. The invariant the two-phase reservation exists
to protect is *reserve only if cap − committed − reserved ≥ amount*, and the old
backend needed a transaction that read the row, did the arithmetic in Python and
wrote the result — three steps whose correctness rested on the transaction's
isolation. DynamoDB does it in one request, where the check and the decrement
are the same operation. There is no window between them because there is no
"between".

**Why `available_paise` is stored rather than derived.** It is exactly
`cap − committed − reserved` and looks like the sort of denormalisation that
rots. It is there because a DynamoDB condition expression *cannot do
arithmetic* — that is allowed in `SET` and nowhere else, so the obvious
condition is not one that can be written. Keeping the difference in a column is
what makes the guard a single atomic comparison instead of a read, a subtraction
in Python, and a hope. Every mutation maintains it in the same write that
changes its inputs, and `scripts/smoke_dynamodb.py` asserts the two still agree.

**A latent bug this closed.** `memory.from_env()` imported
`memory_firestore.FirestoreBank` inside a `try/except`, and that module had never
been written. The `except` swallowed the `ImportError`, so a fully configured
deployment silently kept standing orders in process memory and lost them on the
next deploy — the feature whose entire point is authority that outlives the
conversation did not outlive the process. Nothing failed and no test noticed,
because there were no tests for it.

**Optimistic concurrency on the standing-order bank.** `record_check` enforces a
period budget, which is the same class of invariant as the ledger's cap, so a
plain read-modify-write would reopen exactly the race the ledger exists to
close. Each row carries a version and every mutation is conditional on it.

Runs locally against DynamoDB Local or LocalStack, so no AWS account is needed.

### D8 — The repository is no longer a fork

GitHub offers no self-service way to detach a fork, so the repo was rebuilt: the
fork was renamed aside, a fresh non-fork repository was created under the same
name, and the full history was pushed to it. Every commit and its authorship is
preserved.

### D9 — `LICENSE` and `NOTICE` added

`pyproject.toml` declared Apache-2.0 but no licence file existed, which means the
code was technically all-rights-reserved. Added the Apache-2.0 text and a
`NOTICE` recording the original authorship.

---

## 3. Changed files

### The model boundary

| File | Change | Why |
|---|---|---|
| `PORTING.md` | new | This file. |
| `pocketchange/bedrock.py` | new | Bedrock client, the three model tiers, schema-enforced structured output, and the `available()` check `/status` polls. Replaces the old two-door provider selection. |
| `pocketchange/providers.py` | trimmed | Lost `gemini_client`, `vertex_available` and the Vertex constants. The OpenAI-compatible fallback chain stays — see the file's own docstring for why it survives a port that removed its original reason. |
| `pocketchange/config.py` | rewritten | Deleted the credential mirroring. The old provider issued keys under two non-interchangeable names, so `load()` had to guess which one worked. Bedrock has no key. |
| `pocketchange/monitor.py` | `GeminiMonitor` → `BedrockMonitor` | The trusted layer now signs with the AWS credential chain, and runs on the smallest tier deliberately. |
| `pocketchange/gateway.py` | consolidated | Five inlined "do we have a model" environment checks became one `have_model()` over `bedrock.available()`. **They had drifted** — two looked for a project id and two did not, so one process could report a model on `/status` and refuse to use one on `/runs`. Capability strings now read `bedrock`. |
| `pocketchange/tracing.py` | `instrument_adk` → `instrument_agents` | Strands emits OTel GenAI conventions itself; one fewer package in between. |

### The agent layer

| File | Change | Why |
|---|---|---|
| `agent/runtime.py` | new | One place that knows how a node becomes a Strands agent, so node modules stay a name, a brief and a tool list. |
| `agent/models/llm.py` | rewritten | `StructuredChain` now calls Bedrock with the schema *enforced* rather than described in the prompt. Both live-failure guards (reply ceiling, timeout) survive. |
| `agent/graph/builder.py` | rewritten | ADK expressed the loop and the fan-out through class composition; Strands has no equivalent, so the topology is an explicit loop over a `Buyer` of `Stage`s. The shape is now inspectable without a model, which is what the assembly tests assert on. |
| `agent/nodes/*.py` | ported | `LlmAgent(...)` → `build_agent(...)`. The five affected nodes were always thin. |
| `agent/buyer.py` | ported | Also dropped model probing: the old provider needed a live one-token call to find which model name answered that day. |
| `agent/tools.py` | `finish()` lost `tool_context` | It existed only to set an escalation flag that broke out of ADK's loop. The loop is ordinary Python now and reads `tools.finished` — so an agent can no longer end an errand in a way no test could observe. |

### The policy layer

| File | Change | Why |
|---|---|---|
| `policies/pay.cedar` | new | The authorisation policy, readable in under a minute. |
| `pocketchange/cedar.py` | new | Loads and evaluates it; maps a decision back to the message and status the gateway answers with. Parsed once at import. |
| `pocketchange/gateway.py` | two `if`s removed | Broker separation and the payout switch now come from the policy file. A new `_role()` derives the principal's role from the token chain — never from the request body, since a role the caller can assert is not a role. |

### Storage

| File | Change | Why |
|---|---|---|
| `pocketchange/dynamo.py` | new | Table handle, local-endpoint support, table creation, and the cancellation-reason reader that keeps three different failures distinguishable. |
| `pocketchange/ledger.py` | `FirestoreLedger` → `DynamoLedger` | One conditional write instead of a read-modify-write transaction. Carries a long note about boto3's document interface — the first version serialised values by hand and every transaction leg failed with `unhashable type: dict`, because the resource's client serialises them already. |
| `pocketchange/counterparties.py` | `FirestoreCounterparties` → `DynamoCounterparties` | Native string sets do the mandate-id union atomically, so the read-modify-write transaction disappears entirely. One partition, so `all()` is a Query rather than a Scan. |
| `pocketchange/memory.py` | `DynamoBank` written from scratch | There was no durable bank — see D7. Version-guarded writes, because it enforces a budget. |
| `scripts/smoke_dynamodb.py` | replaces `smoke_firestore.py` | The suite proves the invariants against a mock, which cannot tell you your credentials resolve or that the real service accepts your condition expressions. |

### Tests

| File | Change | Why |
|---|---|---|
| `tests/test_ledger_backends.py` | new | 17 properties × both backends. The gateway "never learns which backend it has" was a claim with nothing checking it, and the durable ledger had no tests at all — for the module the project calls "the file the project exists for". |
| `tests/test_store_backends.py` | new | The same treatment for the counterparty book and the standing-order bank. |
| `tests/test_cedar.py` | new | Each rule end to end, plus fail-closed and hostile entity ids. Cedar identifies policies positionally, so the annotation mapping is the fragile part and is tested through the round trip rather than directly. |
| `tests/test_gateway.py` | **flake fixed** | `test_replay_...does_not_charge_twice` failed about one run in four, and had before this port. `/replay` reports `charged_twice` by comparing the mandate's *whole* committed total, while the run that produced the payment is still settling siblings — so a concurrent settlement was indistinguishable from a double charge. The test now waits for the ledger to go quiet. See §5: the endpoint is still imprecise. |
| `tests/conftest.py` | hardened | **The trap of the port.** Clearing `AWS_*` does not take a process offline — boto3 also reads `~/.aws/config`, an SSO cache and instance metadata. On any machine where `aws configure` had been run, the suite quietly went back on the network. `POCKETCHANGE_NO_BEDROCK` is checked before boto3 is consulted at all. |
| `tests/test_bedrock.py` | new | Availability, region precedence, and the model-spreading reversal. |
| `tests/test_llm.py` | rewritten | Stubs one function instead of installing a fake module tree into `sys.modules` — itself an argument for keeping the model boundary narrow. |
| `tests/test_monitor.py` | rewritten | Same, plus a new assertion that an unconfigured monitor declares it cannot judge. |
| `tests/test_config.py` | trimmed | The credential-mirroring tests had nothing left to test. |
| `tests/test_graph.py` | inverted | `test_every_agent_gets_its_own_quota` became `test_every_agent_draws_on_the_same_model`. See D3. |
| `frontend/{app,main}.js`, `index.html` | updated | Capability strings and the model line; the test-count stat the suite asserts against. |
| `pyproject.toml` | updated | `boto3` is a hard dependency now (the health check needs it). `gcp` and the ADK extras are gone; `agent` and `policy` extras replace them. |

---

### Deployment artefacts

| File | Change | Why |
|---|---|---|
| `deploy/cloudrun.sh`, `deploy/cloudbuild.yaml` | deleted | Replaced, not ported. |
| `deploy/docker-compose.yml` | new | DynamoDB Local — the entire infrastructure for running this on a laptop. Carries a long comment about the failure it took an hour to diagnose: the container runs as uid 1000, a named volume is created root-owned, and the result is a container that starts, prints its configuration, **passes a TCP health check**, accepts connections and then never answers a request, while the real error repeats in a log nobody is tailing. From the client it looks like an unexplained read timeout against a healthy container. |
| `deploy/template.yaml` | new | The stack as a SAM template. **Never deployed, never validated** — `sam validate` has not been run and there is no SAM CLI on the machine. It says so at the top. `bedrock:InvokeModel` is scoped to the two model ids rather than `*`, because a wildcard in a project arguing for bounded authority would be funny in the wrong way. |
| `deploy/README.md` | new | What running it actually takes, and the four things that are correct for a laptop and wrong for a service. |
| `pocketchange/lambda_handler.py` | new | Twelve lines, the only code that knows it might be in Lambda. |
| `Dockerfile` | fixed | **Now copies `policies/`.** Cedar fails closed, so an image built without that line answers 503 to every payment. |
| `.env.example` | rewritten | There is no model API key in it any more, and that absence is the point. |

---

## 4. What deliberately did not change

- `pocketchange/token.py` — attenuation, signing, verification. Untouched.
- `pocketchange/ledger.py` reservation semantics — only the storage backend moved.
- The `/pay` step order. Eight steps, fail-closed at each, same as before.
- `pocketchange/audit.py` — the hash chain is storage-agnostic.

That list is the actual result of the port, and it is the thing worth saying out
loud in the demo: the reasoning layer and the database were both replaced
wholesale, and the part that enforces the limits did not move.

---

## 5. Open

- **Bedrock has never been called.** No AWS credentials have been used from this
  machine, so every Bedrock path is exercised only by its tests and its offline
  branch. This is the largest untested surface in the port and the first thing
  to check before a demo: `aws configure`, then
  `python -c "from pocketchange import config; print(config.report())"` should
  say `bedrock:yes`, and a `/runs` with `decomposer=model` should work.
- Region/model-id pairs need that same live check — see D4.
- DynamoDB, by contrast, **has** been run for real: `scripts/smoke_dynamodb.py`
  passes 9/9 against DynamoDB Local in the compose file, so the condition
  expressions and transactions are known to be ones the actual service accepts
  rather than only ones moto tolerates.
- `/replay` reports `charged_twice` by diffing the mandate's whole committed
  total, so it cannot distinguish "this replay charged again" from "something
  else settled while I was looking". The test now avoids the race; the endpoint
  still has it. The honest fix is to measure the delta attributable to the
  replayed idempotency key, not the mandate.
- The Cedar principal's entity id is currently the role string, because the two
  rules only read `principal.role`. A rule that needed to name an individual
  agent would want the delegate id there instead.
- **A second flake, not chased.** `test_t2t_a_fabricated_price_cannot_enter_a_cart`
  failed once in roughly five full runs with `KeyError: 'token'`, which means
  its `surface` fixture's `POST /delegate` came back without one. It passes in
  isolation and it predates this port — nothing here touches `/delegate`. The
  likely shape is the same as the replay flake that *was* fixed: a background
  run from an earlier test still working against the module-global
  `gateway.state` that the next test has already replaced. Worth an hour
  sometime; the fix is probably for the gateway tests to join their background
  threads rather than for this test to retry.
- `scripts/demo_standing.py` needs a real model and fails without credentials.
  That is true of the pre-port code too - it is a demo *of* the model-backed
  standing-order path - but it is the one demo the README does not flag as
  runnable offline, and it should.
