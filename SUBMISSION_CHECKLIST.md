# Submission checklist

The short version of `TODO.md`, kept to what is actually left. `TODO.md` has
the reasoning and the exact commands; this is the list to work from.

**Track:** First Commit (WeMakeDevs × AWS). **Recommendation stands:** submit
Build It properly first; only attempt Ship It if §3 below is done with time to
spare.

---

## Before anything else

- [ ] **Which repo is the actual submission?** `AWS_` and `FirstCommit` now
      hold identical history (same commits, same HEAD). Nothing distinguishes
      them for a judge except the URL. Pick one, say so in the submission
      form, and either delete or clearly mark the other so nobody reviews the
      wrong link.
- [ ] **Request Bedrock model access** in the AWS console (Model access →
      request `claude-haiku-4-5` and `claude-sonnet-5` at minimum). Per-account,
      per-region, and not instant — this is the one item that blocks
      everything else, so it goes first regardless of what else gets done.
- [ ] **Verify the model ids resolve** in your chosen region (some serve them
      only via a `us.`/`eu.`/`apac.`-prefixed cross-region inference profile).
      `TODO.md` §1 has the exact commands.
- [ ] **Run one real model call** (`eval.monitor`) to confirm Bedrock actually
      works end to end — everything Bedrock-shaped is currently exercised only
      by tests and its offline branch.
- [ ] **Set a billing guardrail** before any large run (`aws budgets
      create-budget`, `TODO.md` §1).

## If attempting Ship It (a live deployment)

- [ ] **Durable approvals and agent registry.** Still in process memory —
      `pocketchange/approvals.py` and `registry.py` both say in their own
      docstrings that they are shaped for a durable store and neither has one.
      A payment held for human approval would not survive the container that
      held it. Same pattern as `DynamoCounterparties`; the table is already
      there. ~3 hours, less if only approvals.
- [ ] **Build and push the image, deploy `deploy/template.yaml`.** Rewritten
      for App Runner this session but **never applied to a real account** — no
      AWS credentials on the machine that wrote it, so only a local YAML
      parse, not `aws cloudformation validate-template`. `deploy/README.md`
      §2 and `TODO.md` §4 have the exact steps and the IAM shape.
- [ ] **Check `/status`, not `/`, after it's up** — flags are nested under
      `capabilities` (`curl -s $URL/status | jq .capabilities`). Deployed
      with `EnableBedrock=false` the honest answer is `"model": false`,
      `"monitor": "unconfigured"`, plus a `degraded` string saying why; that
      is the expected state on a Free Plan account, not a failure.
- [ ] **Confirm a run survives the response** — start a run, watch `/stream`,
      check the tree keeps growing after `POST /runs` has already answered.
      This is the exact failure the App Runner rewrite exists to avoid.
- [ ] **Pause or delete the service when not demoing** — the only real way to
      burn credits here is idle compute.

## Submission itself

- [ ] **Record the 3-minute video.** Shot list with timings is in
      `HACKATHON.md`. Judges see the video and nothing else — this is worth
      more than any remaining code work. Budget 3 hours for takes.
- [ ] **Confirm the NOTICE names.** It currently has handles and emails rather
      than legal names, entered as a placeholder — replace with the real ones
      (or confirm handles are what you want on record) before submitting.
- [ ] Consider **renaming the repo** to `pocket-change` — `AWS_` (or a bare
      `FirstCommit`) reads like a scratch directory; the README, the package
      and the project are all called Pocket Change. Depends on the answer to
      the first item above.

## Known, stated gaps — leave these written down rather than let a judge find them

- [ ] `scripts/demo_standing.py` needs a real model; every other demo runs
      offline and the README should say which is which.
- [ ] Prompt injection is demonstrated against a scripted worst-case agent,
      not landed on a live model — a specification test, not yet a live one.
      With Bedrock working this session's work makes real, worth an hour.

## Already done (this session, since the merge)

Kept here so it's not re-attempted: root signing key moved to Secrets Manager
(closes the "single biggest gap" `TODO.md` used to name), the ~1-in-5 test
flake fixed and measured, `/replay`'s `charged_twice` false-positive fixed,
`deploy/template.yaml` rewritten for App Runner. 535 tests passing. Detail and
commit-level reasoning in `TODO.md` and the commit history itself.

---

## If you only have a day

1. Decide the repo question above — everything else is wasted if a judge
   opens the wrong link.
2. Get Bedrock access, verify the model ids resolve, run `eval.monitor` once.
3. Record the video.
4. Submit to **Build It**.

That is a complete, honest submission. Everything else is upside.
