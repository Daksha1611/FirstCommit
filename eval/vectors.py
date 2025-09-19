"""The twelve SoK attack vectors, on the record.

Revised after the standing branch was added. Persistence changes the shape of
two of them: an injection against an errand buys one wrong thing once, while an
injection that lands while a recurring policy is being written keeps paying out
for months - and the mechanism that makes ticks cheap and stable (no model, pure
arithmetic) is exactly what stops anything noticing later.

From the five-dimensional taxonomy in the SoK on autonomous financial agents
(arXiv:2604.15367), Table 1. Each vector carries four things: whether it applies
to this system, the mechanism, what defends it, and the test that proves it.

Two are marked NOT APPLICABLE, with a mechanism-level reason rather than a
convenient one. That is deliberate. The paper's corpus is largely DeFi and
trading agents, where an agent holds a position it can move; this one buys rice
at a listed price. Scoring twelve out of twelve on a grocery buyer would be a
scoreboard we drew ourselves, which is the failure mode an earlier critique of
this project called out by name.

Run:  python -m eval.vectors
"""

from __future__ import annotations

from dataclasses import dataclass

DIMENSIONS = {
    "D1": ("Agent Integrity", "model poisoning · prompt injection · tool compromise"),
    "D2": ("Transaction Authorization", "key management · spending bounds · intent verification"),
    "D3": ("Inter-Agent Trust", "identity spoofing · collusion · protocol manipulation"),
    "D4": ("Market Manipulation", "agent-driven manipulation · adversarial herding · flash attacks"),
    "D5": ("Regulatory Compliance", "KYC/AML gaps · liability attribution · audit trails"),
}


@dataclass(frozen=True)
class Vector:
    code: str
    name: str
    dimension: str
    applicable: bool
    mechanism: str
    defence: str
    test: str = ""
    note: str = ""


VECTORS: tuple[Vector, ...] = (
    Vector(
        "P2T", "Prompt-to-Transaction", "D1", True,
        "Injected text in a supplier listing or review persuades the agent to "
        "move money. Against a standing policy the same injection is DURABLE: "
        "poison the setup once and every later tick executes the result as "
        "arithmetic, with no model in the loop to re-read the text and notice.",
        "Capability split: the agents that read supplier text hold no `pay` "
        "scope, so compliance cannot become a payment. Datamarking on top. For "
        "the durable case, reorder levels are bounded in code before they are "
        "persisted, and a policy is inert until a person approves it.",
        "tests/test_vectors.py::test_p2t_injected_text_cannot_reach_payment",
    ),
    Vector(
        "T2R", "Tool-to-Reasoning", "D1", True,
        "A tool response carries instructions that steer later reasoning. Also "
        "reached the planner and the standing setup, which for a time built "
        "their prompts straight from the catalogue and bypassed marking "
        "entirely.",
        "Datamarking at the tool boundary (spotlighting, arXiv:2403.14720): "
        "every word of supplier text is interleaved with a marker and lands in "
        "a field named `_untrusted`. One shared catalogue builder now renders "
        "the catalogue for every prompt, so a node cannot opt out by not "
        "importing the module where the rule lives.",
        "tests/test_vectors.py::test_t2r_tool_output_is_marked_at_the_boundary",
    ),
    Vector(
        "T2T", "Tool-to-Transaction", "D1", True,
        "A tool result inflates or fabricates what a purchase costs.",
        "The agent names sku, quantity and seller; the price is looked up from the "
        "real offer. A fabricated price is impossible by construction, and the "
        "guard re-checks the total against live offers before authority is minted.",
        "tests/test_vectors.py::test_t2t_a_fabricated_price_cannot_enter_a_cart",
    ),
    Vector(
        "P2K", "Prompt-to-Key", "D2", True,
        "Injected text extracts a credential from the agent.",
        "No agent ever holds a payment credential. The gateway holds the only "
        "Razorpay key and the root signing key; agents hold capability tokens, "
        "which are capped, scoped and short-lived even if disclosed.",
        "tests/test_vectors.py::test_p2k_no_agent_can_reach_a_credential",
    ),
    Vector(
        "M2A", "Model-to-Authorization", "D2", True,
        "The model grants itself authority it was not given - including the "
        "recurring kind: a standing policy turns one sentence into a budget "
        "that spends unattended for months.",
        "Attenuation is monotonic - a token cannot be widened, only narrowed. A "
        "shopper cannot mint itself a payer, and the guard refuses a sub-mandate "
        "capped above its seller's share. A standing policy is inert until a "
        "person approves it, and each tick is triaged so one that has grown "
        "large escalates rather than acting.",
        "tests/test_vectors.py::test_m2a_authority_cannot_be_widened",
    ),
    Vector(
        "S2I", "Supply-to-Integrity", "D3", True,
        "A counterfeit or unknown seller enters the supply path.",
        "A proposal naming a seller that does not carry the product is refused at "
        "proposal time; the guard rejects sellers no search returned.",
        "tests/test_vectors.py::test_s2i_an_unknown_seller_is_refused",
    ),
    Vector(
        "C2E", "Collusion-to-Escrow", "D3", True,
        "Several agents drawing on one pool coordinate to drain more than any one "
        "of them was allowed.",
        "Sub-payers are siblings sharing a single mandate id, so the cumulative "
        "ledger sees one ceiling over the whole tree however many children exist.",
        "tests/test_vectors.py::test_c2e_siblings_cannot_exceed_one_ceiling",
    ),
    Vector(
        "N2C", "Neg-to-Compliance", "D5", True,
        "An agent negotiates or acts outside what compliance would permit, "
        "unrecorded.",
        "Every decision is written to a hash-linked audit chain with its reason - "
        "refusals as carefully as approvals, since a log of successes cannot show "
        "that anything was stopped.",
        "tests/test_vectors.py::test_n2c_refusals_are_recorded_with_reasons",
    ),
    Vector(
        "R2I", "Reg-to-Integrity", "D5", True,
        "Records are altered after the fact, so what happened cannot be "
        "reconstructed.",
        "Each audit entry carries the hash of its predecessor and of its own "
        "content. Editing or removing any entry breaks verification from that "
        "point on. Tamper-evident, not tamper-proof.",
        "tests/test_vectors.py::test_r2i_tampering_breaks_the_chain",
    ),
    Vector(
        "A2M", "Agent-to-Market", "D4", False, "", "",
        note="The buyer holds no position and transacts at listed prices. It "
             "cannot move a price, so there is no market impact to exploit.",
    ),
    Vector(
        "O2P", "Oracle-to-Position", "D4", False, "", "",
        note="No oracle and no position. Prices come from the merchant's own "
             "offers, which are the thing being bought rather than a feed a "
             "position is valued against.",
    ),
    Vector(
        "I2M", "Identity-to-Market", "D4", True,
        "A supplier manufactures its own standing. Reputation here is entirely "
        "self-reported - merchant/sellers.py derives `is_established` from "
        "`trading_months` and `review_count`, both of which live in the seller's "
        "own record, and seller_reputation() hands them straight to the shopper. "
        "A hostile supplier writes a hundred thousand reviews for itself and is "
        "trusted for free.",
        "The gateway keeps its OWN record of who it has paid "
        "(pocketchange/counterparties.py), written at settlement from payments "
        "that actually cleared, and gives it to the monitor beside the seller's "
        "claim. A supplier can inflate its star rating; it cannot make us have "
        "paid it before. The identity on a payment is still agent-supplied and "
        "so still misattributable - what cannot be forged is the history.",
        "tests/test_vectors.py::test_i2m_our_own_record_cannot_be_written_by_a_seller",
        note="Previously marked NOT APPLICABLE on the grounds that there is no "
             "market here to move. That was too convenient: sybil reputation "
             "does not need a market, only a party whose word is taken for its "
             "own standing - which is exactly what review_count was. "
             "MEASURED. An order COUNT alone changes nothing - a routine buy "
             "from an unknown party and from a 34-order party were both allowed. "
             "What moves the verdict is how those dealings WENT: the same "
             "purchase, the same tally, plus refusals and injected pages on the "
             "record, flips ALLOW to ESCALATE. A tally on its own is what a bad "
             "counterparty wants read, which is the sybil problem one level up.",
    ),
)

BY_CODE = {v.code: v for v in VECTORS}


def applicable() -> tuple[Vector, ...]:
    return tuple(v for v in VECTORS if v.applicable)


def not_applicable() -> tuple[Vector, ...]:
    return tuple(v for v in VECTORS if not v.applicable)


def report() -> str:
    lines = ["", "SoK threat taxonomy - coverage", "=" * 74, ""]
    for code, (name, concerns) in DIMENSIONS.items():
        lines.append(f"  {code}  {name}")
        lines.append(f"      {concerns}")
    lines += ["", f"DEFENDED  ({len(applicable())}/12)", "-" * 74]
    for v in applicable():
        lines.append(f"  {v.code}  {v.name:<24} [{v.dimension}]")
        lines.append(f"      attack:  {v.mechanism}")
        lines.append(f"      defence: {v.defence}")
        lines.append(f"      test:    {v.test.split('::')[-1]}")
        lines.append("")
    lines += [f"NOT APPLICABLE  ({len(not_applicable())}/12)", "-" * 74]
    for v in not_applicable():
        lines.append(f"  {v.code}  {v.name:<24} [{v.dimension}]")
        lines.append(f"      {v.note}")
        lines.append("")
    lines += [
        "-" * 74,
        f"  {len(VECTORS) - len(applicable())} vectors are marked not applicable, "
        "with mechanism-level reasons",
        "  rather than defended with checks that would do nothing. Scoring 12/12",
        "  against a taxonomy built for trading agents would be a scoreboard",
        "  drawn by the people being scored.",
        "",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    print(report())
