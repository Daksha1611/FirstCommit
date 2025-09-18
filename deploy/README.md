# Running it, and what deploying it would take

Two sections. The first is what actually works today and is what the project is
entered with. The second is an honest account of the gap between that and a
deployment, written down rather than hand-waved.

---

## 1. Local — the supported path

No AWS account, no card, no credentials.

```bash
uv venv && uv pip install -e ".[dev,biscuit,agent,policy]"

# Storage
docker compose -f deploy/docker-compose.yml up -d
export POCKETCHANGE_DDB_ENDPOINT=http://localhost:8000
.venv/bin/python scripts/smoke_dynamodb.py      # 9 gates against real DynamoDB

# The gateway
.venv/bin/uvicorn pocketchange.gateway:app --reload --port 8080
```

That gives you the enforcement point, the Cedar policy, the ledger and the
audit trail. What it does *not* give you is a model: without AWS credentials
`bedrock.available()` is false, so the decomposer falls back to a deterministic
one, the monitor declares that it cannot judge, and `/status` says so plainly
rather than implying a second layer that is not there.

To add the model, `aws configure` with a principal that can call
`bedrock:InvokeModel`, and make sure the model ids in `pocketchange/bedrock.py`
are ones your region actually serves — several regions require the
geography-prefixed inference profile (`us.anthropic...`) instead.

```bash
export AWS_REGION=us-east-1
.venv/bin/python -c "from pocketchange import config; print(config.report())"
# bedrock:yes  razorpay:NO  langfuse:NO
```

### The console

```bash
cd frontend && npm ci && npm run dev     # http://localhost:5173
```

---

## 2. What deploying it would need

`template.yaml` is in this directory and has **never been deployed** — there is
no AWS account behind the machine this was written on, so not even
`aws cloudformation validate-template` has been run against it, only a local
YAML parse. It targets **App Runner**, not Lambda: the funnel keeps running on
a background thread after `POST /runs` has already answered, and Lambda
freezes execution the instant the response returns, so the tree would simply
stop growing wherever it had reached. `/stream` is Server-Sent Events, which
API Gateway does not do comfortably either. It is a design document that
happens to be executable, and the list below is the part that matters more
than the YAML.

### Things you would have to supply

| | |
|---|---|
| An AWS account | with Bedrock **model access granted** for the two model ids in `template.yaml`. This is a per-account, per-region request in the Bedrock console and it is not instant, so it is the item to start first. |
| A region that serves them | model availability is not uniform. `us-east-1` is the safe default; `ap-south-1` is closer to the rupee prices this project quotes but serves fewer models. |
| An image already pushed | App Runner has no build-from-source step: `docker build`, `aws ecr create-repository`, `docker push`, *then* the stack — TODO.md §4 has the exact commands. `ImageIdentifier` in `template.yaml` is that image's URI. |
| Deploy credentials | `aws cloudformation deploy`, with a principal allowed to create the table, the App Runner service, and the two IAM roles. No SAM CLI needed — nothing in the template is a SAM resource type. |
| Razorpay **test** keys | optional. Without them the rail is a simulation and `/facts` reports `"rail": "fake"` rather than pretending otherwise. Never live keys — nothing in this project should move real money. |

### Things that must change before it is honest to call it deployed

These are not deployment chores; they are places where the current code is
correct for a laptop and wrong for a service.

1. ~~**The root signing key.**~~ Fixed: `pocketchange/secrets.py` fetches it
   from Secrets Manager instead of a per-instance disk file, so a redeploy or
   a second instance scaled out for load signs with the same key rather than
   silently revoking every live mandate. `template.yaml` scopes
   `InstanceRole` to exactly that one secret.

2. **The in-process stores.** The ledger, the counterparty book and the standing
   orders are on DynamoDB. Approvals (`approvals.py`) and the agent registry
   (`registry.py`) are not — both say in their own docstrings that they are
   shaped so a durable store can replace them, and neither has been. A payment
   held for human approval would not survive the container that held it. Still
   open.

3. **Concurrency.** Every App Runner instance shares the DynamoDB table, which
   is fine — that is what the conditional writes are for — and now shares the
   root key too. They do not share the in-process approvals/registry state
   above, so #2 is the same fix either way.

4. **The demo write-gate.** `X-Demo-Token` is a brake against a crawler draining
   a shared model quota. It is not authentication, it ships inside a page anyone
   can read, and a public deployment needs something that is.

### What it would cost

Effectively nothing at demo scale, and it is worth saying why rather than
quoting a number: DynamoDB on-demand bills per request and this makes a handful
per payment; App Runner at 1 vCPU / 2 GB is roughly $0.07/hr, a few dollars for
a multi-day event if left running - pause or delete the service between demos,
the same advice TODO.md §5 gives; the only line item that is not rounding error
is Bedrock, billed per token, which is why the monitor runs on the smallest
model and why `/runs` reports `expected_model_calls` before a run starts rather
than after.
