# What is left

Everything between here and a submission, in the order it should be done.

Times are honest estimates for someone who already knows this codebase. Costs
assume the **$100 of AWS credits** on the account.

---

## Where it stands

**Merged to `main`** (PR #1, merge commit `ecdeb98`, 2026-09-18). A judge landing
on the repo now sees the AWS code.

- Bedrock, Strands, Cedar and DynamoDB in place; 528 tests passing
- `scripts/smoke_dynamodb.py` 9/9 against a real DynamoDB Local
- `LICENSE`, `NOTICE`, `PORTING.md`, `HACKATHON.md`
- Repo standalone, old fork deleted

Not done: **Bedrock has never been called.** Nothing below matters until that is
true, so it is first.

Housekeeping, whenever: the `worktree-aws-port` branch still exists and can be
deleted; a local clone sitting on the pre-merge commit needs `git pull`.

---

## 1 · Before anything else (≈30 min, ~$0.50)

- [ ] **Request Bedrock model access.** Console → Bedrock → Model access, in the
      region you will use. **Do this first regardless of everything else** — it
      is a per-account, per-region approval and it is not instant. Request at
      minimum `claude-haiku-4-5` and `claude-sonnet-5`; add `claude-opus-5` if
      you want the strongest decomposer.
- [ ] **Verify the model ids actually resolve.** Several regions serve these
      only through a cross-region inference profile, where the id gains a `us.`
      / `eu.` / `apac.` prefix. This is the single most likely thing to break on
      demo day.

      ```bash
      aws configure                      # or SSO
      export AWS_REGION=us-east-1
      aws bedrock list-foundation-models --query 'modelSummaries[?contains(modelId,`claude`)].modelId' --output text
      .venv/bin/python -c "from pocketchange import config; print(config.report())"
      # want: bedrock:yes
      ```

      If the ids differ from the defaults in `pocketchange/bedrock.py`, set
      `POCKETCHANGE_BEDROCK_MODEL` / `_SHOPPER_MODEL` / `_JUDGE_MODEL` rather
      than editing the file.

- [ ] **Run one real model path end to end.** Everything Bedrock-shaped is
      currently covered by tests and its offline branch only.

      ```bash
      .venv/bin/python -m eval.monitor       # ~12 calls, a few cents
      ```

- [ ] **Set a billing guardrail before the first big run.** Two minutes, and it
      is the difference between noticing an overspend and finding out later.

      ```bash
      aws budgets create-budget --account-id $(aws sts get-caller-identity --query Account --output text) \
        --budget '{"BudgetName":"pocketchange","BudgetLimit":{"Amount":"60","Unit":"USD"},"TimeUnit":"MONTHLY","BudgetType":"COST"}'
      ```

---

## 2 · Pick the track

You now have credits, so **Ship It is open** — and it is the larger prize
(₹2,00,000 + $3,000 vs ₹1,50,000 + $2,000). It requires a live URL.

**My recommendation: do Build It properly, and only then attempt Ship It.**
Not out of caution — because of §3. There are two real correctness problems
that a deployment exposes and a laptop hides, and shipping a URL that quietly
loses mandates is worse for the "AWS Implementation" score than not shipping
one. Build It is judged with the same care, and the project is already there.

If time is short after §3, stay on Build It and spend the hours on the video.

---

## 3 · The two blockers that only appear when deployed

These are not deployment chores. They are places where the current code is
correct for one process on a laptop and **wrong for a service**.

- [ ] **The root signing key is generated per instance.**
      `POCKETCHANGE_EPHEMERAL_KEYS=1` is right locally. On any platform that can
      restart or scale the container, every restart silently revokes every live
      mandate — tokens signed by the old key stop verifying, and the failure
      looks like forgery rather than like a key change.

      Fix: store the Ed25519 root key in Secrets Manager, load it at startup.
      Roughly: generate once, `aws secretsmanager create-secret`, read it in
      `gateway.State()` where the ephemeral branch currently is, and give the
      service's role `secretsmanager:GetSecretValue` on that one ARN.
      **~2 hours.** This is the single biggest gap in the project.

- [ ] **Approvals and the agent registry are still in process memory.**
      `pocketchange/approvals.py` and `registry.py` both say in their own
      docstrings that they are shaped so a durable store can replace them, and
      neither has been. A payment held for human approval does not survive the
      container that held it — which is exactly the flow you would demo.

      Fix: same pattern as `DynamoCounterparties`; the table and key layout are
      already there. **~3 hours for both**, less if you only do approvals.

      With one always-on instance you can get away without this for a demo. Say
      so if asked, rather than letting it be discovered.

---

## 4 · Deploying (Ship It)

### Use App Runner, not Lambda

**`deploy/template.yaml` targets Lambda and that is the wrong shape for this
application.** Recording it here rather than quietly leaving it:

The funnel runs on a **background thread after `POST /runs` has already
answered**. Lambda freezes execution the moment the response returns, so the
tree would simply stop growing at whatever node it had reached, with no error —
the identical failure the README describes hitting on Cloud Run before
`--no-cpu-throttling`. `/stream` is also Server-Sent Events, which API Gateway
does not do comfortably.

**App Runner** is the right target: a container, always-on, SSE works, threads
keep running, and it is on the hackathon's Ship It service list. `Dockerfile`
already builds the console and the gateway into one image, so there is one URL
and no CORS to configure.

- [x] **Rewrite `deploy/template.yaml`.** Dropped the Lambda function and
      HTTP API; the table stays, and an `AWS::AppRunner::Service` plus its
      two IAM roles (ECR pull, runtime) replace them. `KeySecretName` is
      named as a parameter but never created here — the service mints it on
      its own first boot (`pocketchange/secrets.py`), the same pattern as
      `dynamo.create_table_if_absent()`. Still unvalidated: no AWS account
      behind this machine, so only a local YAML parse, not
      `aws cloudformation validate-template`.

### Steps

```bash
# 1. the table
aws dynamodb create-table --table-name pocketchange \
  --attribute-definitions AttributeName=pk,AttributeType=S AttributeName=sk,AttributeType=S \
  --key-schema AttributeName=pk,KeyType=HASH AttributeName=sk,KeyType=RANGE \
  --billing-mode PAY_PER_REQUEST
# or: .venv/bin/python -c "from pocketchange import dynamo; dynamo.create_table_if_absent()"

# 2. the image
aws ecr create-repository --repository-name pocketchange
aws ecr get-login-password | docker login --username AWS --password-stdin <acct>.dkr.ecr.<region>.amazonaws.com
docker build --build-arg VITE_DEMO_TOKEN=<a-random-string> -t <acct>.dkr.ecr.<region>.amazonaws.com/pocketchange:v1 .
docker push <acct>.dkr.ecr.<region>.amazonaws.com/pocketchange:v1

# 3. the service — console is fine, or `aws apprunner create-service`
#    1 vCPU / 2 GB, port 8080, health check path /status
```

Instance role needs exactly:

| | |
|---|---|
| `dynamodb:*Item`, `Query` | on the `pocketchange` table only |
| `bedrock:InvokeModel` | on the two or three model ARNs only, never `*` |
| `secretsmanager:GetSecretValue` | on the root-key secret only (after §3) |

Environment: `POCKETCHANGE_DDB_TABLE=pocketchange`,
`POCKETCHANGE_NO_DOTENV=1`, `AWS_REGION=<region>`, the model id overrides, and
`POCKETCHANGE_DEMO_TOKEN` to gate writes.

- [ ] **Check `/status` first, not `/`.** It reports which layers are actually
      live — but the flags are **nested under `capabilities`**, not top level
      (`curl -s $URL/status | jq .capabilities`). With Bedrock you want
      `"model": true` and `"monitor": "bedrock"`; on a Free Plan account with
      `EnableBedrock=false` the honest answer is `"model": false`,
      `"monitor": "unconfigured"` and a `degraded` string saying why.
- [ ] **Confirm a run survives the response.** Start a run, watch `/stream`, and
      check the tree keeps growing after `POST /runs` has returned. This is the
      thing Lambda would have broken.

---

## 5 · What it will cost against $100

Rough, and worth re-checking: **Bedrock is partner-priced and differs from
first-party API rates.**

| | |
|---|---|
| App Runner, 1 vCPU / 2 GB | ~$0.07/hr ≈ **$5–7** for a four-day event if left running |
| DynamoDB on-demand | pennies — a handful of requests per payment |
| ECR, CloudWatch | rounding error |
| **Bedrock** | the only line that matters |

A full 121-node funnel run is ~40 decomposer calls plus one monitor call per
payment. Very roughly, per complete run:

| decomposer on | ≈ per run |
|---|---|
| `claude-haiku-4-5` | ~$0.15 |
| `claude-sonnet-5` | ~$0.30 |
| `claude-opus-5` | ~$0.90 |

So $100 is not a constraint — it is **~100 full runs on Opus**, and you will do
perhaps twenty. Practical advice anyway:

- [ ] Leave the **monitor on Haiku**. It is the right model for it on the merits
      (small, fast, trusted, one narrow question), not to save money.
- [ ] Use `decomposer=departmental` while iterating on the console. It is
      deterministic and costs nothing; switch to `model` for the recorded run.
- [ ] `POST /intake` reports `expected_model_calls` **before** a run starts.
      Look at it.
- [ ] **Pause or delete the App Runner service when not demoing.** Idle
      compute is the only way to quietly burn credits here.

---

## 6 · Submission

- [ ] **Record the 3-minute video.** Shot list with timings is in
      `HACKATHON.md`. Judges see the video and nothing else — it is worth more
      than any remaining code work. **Budget 3 hours** for takes.
- [ ] **Confirm the NOTICE names.** I used handles and emails rather than guess
      at legal names. Replace with the real ones.
- [ ] Consider **renaming the repo** to `pocket-change`. `AWS_` reads like a
      scratch directory; the README, the package and the project are all called
      Pocket Change. I can do this and update the remote.
- [ ] Optional: the **AWS Builder Center blog post** (top 5 win keyboards).
      `PORTING.md` is most of a post already — the boto3 double-serialisation
      trap and the healthy-but-useless container are both genuinely useful to
      other people.

---

## 7 · Known gaps — state these, do not let them be found

Already written up in `PORTING.md` §5 and `HACKATHON.md`. The short list:

- [x] **A flake, ~1 run in 5.** Confirmed: a `POST /runs` test's daemon thread
      kept running past its own test and into whichever test collected next,
      writing to the `gateway.state` that test had already replaced - it
      surfaced as a different assertion on different runs (`KeyError: 'token'`
      in `test_t2t_a_fabricated_price_cannot_enter_a_cart`, `KeyError:
      'approval_id'` in `test_repurchase.py`, a stray 403 in
      `test_repurchase.py` again), not always the one name this file had. Fixed
      by an autouse fixture in `conftest.py` that joins every `run-*` thread at
      the end of the test that spawned it. Measured ~1 failure in 5 full runs
      before, ~1 in 33 after; still not provably zero, so leaving this line
      rather than closing it silently.
- [x] `/replay` reported `charged_twice` by diffing the mandate's whole
      committed total, so it could not tell "this replay charged again" from
      "something else settled while I was looking". Fixed: it now reads
      `response.replayed` from the same `pay()` call, which is set inside the
      idempotency check that decides whether a new reservation opens at all.
- [ ] `scripts/demo_standing.py` needs a real model. Every other demo runs
      offline; the README should say which is which.
- [ ] Prompt injection is demonstrated against a scripted worst-case agent, not
      landed on a live model. That makes it a specification test. With Bedrock
      working you could now run it for real — a genuinely stronger claim, and
      maybe an hour's work.

---

## If you only have a day

1. Get Bedrock access, verify the model ids resolve, run `eval.monitor` once (§1)
2. Record the video (§6)
3. Submit to **Build It**

That is a complete, honest submission. Everything else is upside.
