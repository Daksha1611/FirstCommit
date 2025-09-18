"""Day-1 spike: can we build an AIP-shaped delegation chain with Biscuit?

Proves the properties Pocket Change depends on:

  1. a three-hop chain mints, attenuates twice, and verifies
  2. attenuation is monotonic - a later holder cannot widen scope
  3. budget caps compose - the tightest cap in the chain wins
  4. depth and expiry are bounded

Money is in paise throughout. Biscuit's Datalog compares integers exactly;
rupees-as-floats would round, and rounding money is how you lose it.

Run:  .venv/bin/python spike/biscuit_chain.py
Needs no cloud credentials.
"""

from datetime import datetime, timezone

from biscuit_auth import (
    Authorizer,
    AuthorizerBuilder,
    Biscuit,
    BiscuitBuilder,
    BlockBuilder,
    KeyPair,
)

RUPEE = 100  # paise

# Timestamps must be datetime objects, not ISO strings. Biscuit's Datalog is
# typed: a string term compared against a date term fails with "Invalid type"
# rather than evaluating false, so every check in the chain silently denies.
EXPIRY = datetime(2026, 8, 31, tzinfo=timezone.utc)
NOW = datetime(2026, 8, 27, 12, tzinfo=timezone.utc)
AFTER_EXPIRY = datetime(2026, 9, 15, tzinfo=timezone.utc)


def mint_root(root: KeyPair, *, budget_paise: int, max_depth: int) -> Biscuit:
    """Block 0 - the authority block, signed by the human principal."""
    builder = BiscuitBuilder(
        """
        identity({id});
        max_depth({depth});
        check if budget($b), $b <= {budget};
        check if depth($d), $d <= {depth};
        check if time($t), $t <= {expiry};
        """,
        {
            "id": "aip:web:pocketchange.dev/principal",
            "depth": max_depth,
            "budget": budget_paise,
            "expiry": EXPIRY,
        },
    )
    return builder.build(root.private_key)


def delegate(
    token: Biscuit,
    *,
    to: str,
    tools: list[str],
    budget_paise: int,
    context: str,
) -> Biscuit:
    """Append a delegation block. Adds checks only - Biscuit cannot remove one.

    `context` is AIP's mandatory non-empty field: the delegator states why.
    """
    if not context.strip():
        raise ValueError("AIP requires a non-empty context on every delegation block")

    block = BlockBuilder(
        """
        delegate({to});
        context({why});
        check if tool($t), {tools}.contains($t);
        check if budget($b), $b <= {budget};
        """,
        {"to": to, "why": context, "tools": tools, "budget": budget_paise},
    )
    return token.append(block)


def authorize(
    token: Biscuit,
    root: KeyPair,
    *,
    tool: str,
    amount_paise: int,
    depth: int,
    now: datetime,
) -> None:
    """Verify the whole chain against one concrete operation.

    Every check in every block must pass. That is what makes widening impossible:
    an earlier block's narrow scope is still enforced when a later block asks for
    something wider.
    """
    authorizer: Authorizer = AuthorizerBuilder(
        """
        tool({tool});
        budget({amount});
        depth({depth});
        time({now});
        allow if true;
        """,
        {"tool": tool, "amount": amount_paise, "depth": depth, "now": now},
    ).build(token)
    authorizer.authorize()


def check(label: str, fn, *, expect_ok: bool) -> bool:
    try:
        fn()
        ok, detail = True, "allowed"
    except Exception as exc:  # noqa: BLE001 - the spike wants the class name
        ok, detail = False, type(exc).__name__
    passed = ok is expect_ok
    print(
        f"  [{'PASS' if passed else 'FAIL'}] {label:<48} "
        f"want={'allow' if expect_ok else 'deny':<6} got={detail}"
    )
    return passed


def main() -> int:
    root = KeyPair()
    now = NOW

    # Block 0 - the orchestrator's authority, signed by the human.
    t0 = mint_root(root, budget_paise=5000 * RUPEE, max_depth=3)

    # Two SIBLING branches, not one chain. This is forced, not stylistic:
    # attenuation only ever narrows, so anything descended from the shopper can
    # never hold `pay`. Making the payer a child of the shopper is impossible -
    # and making the shopper hold `pay` would hand the pay capability to the one
    # agent that reads untrusted catalog text. Siblings are the only safe shape,
    # and they give us CaMeL's control/data separation for free.

    # Branch A - spends money, never reads untrusted text.
    payer = delegate(
        t0,
        to="aip:key:ed25519:ephemeral-payer",
        tools=["pay"],
        budget_paise=899 * RUPEE,
        context="paying for the assembled cart",
    )

    # Branch B - reads untrusted text, cannot spend. Two hops deep.
    shopper = delegate(
        t0,
        to="aip:web:pocketchange.dev/shopper",
        tools=["search", "cart"],
        budget_paise=1500 * RUPEE,
        context="weekly grocery run under a 1500 rupee cap",
    )
    browser = delegate(
        shopper,
        to="aip:key:ed25519:ephemeral-browser",
        tools=["search"],
        budget_paise=0,
        context="reading listings for the grocery run",
    )

    print(
        f"\npayer   {payer.block_count()} blocks, {len(payer.to_base64())} b64 chars"
        f"\nshopper {shopper.block_count()} blocks"
        f"\nbrowser {browser.block_count()} blocks, depth-3 chain\n"
    )

    results = [
        check(
            "payer pays 899, within every cap",
            lambda: authorize(payer, root, tool="pay", amount_paise=899 * RUPEE, depth=1, now=now),
            expect_ok=True,
        ),
        check(
            "shopper searches, depth-3 browser searches",
            lambda: (
                authorize(shopper, root, tool="search", amount_paise=0, depth=1, now=now),
                authorize(browser, root, tool="search", amount_paise=0, depth=2, now=now),
            ),
            expect_ok=True,
        ),
        # The injection defense, stated as a property rather than a hope.
        check(
            "shopper attempts to pay",
            lambda: authorize(shopper, root, tool="pay", amount_paise=100 * RUPEE, depth=1, now=now),
            expect_ok=False,
        ),
        check(
            "browser (reads untrusted text) attempts to pay",
            lambda: authorize(browser, root, tool="pay", amount_paise=100 * RUPEE, depth=2, now=now),
            expect_ok=False,
        ),
        check(
            "browser attempts cart, granted to its parent only",
            lambda: authorize(browser, root, tool="cart", amount_paise=0, depth=2, now=now),
            expect_ok=False,
        ),
        check(
            "payer attempts payout never granted upstream",
            lambda: authorize(
                payer, root, tool="payout", amount_paise=5000 * RUPEE, depth=1, now=now
            ),
            expect_ok=False,
        ),
        check(
            "payer spends 1200 - under block 0, over its own cap",
            lambda: authorize(payer, root, tool="pay", amount_paise=1200 * RUPEE, depth=1, now=now),
            expect_ok=False,
        ),
        check(
            "operation at depth 4, max_depth is 3",
            lambda: authorize(payer, root, tool="pay", amount_paise=899 * RUPEE, depth=4, now=now),
            expect_ok=False,
        ),
        check(
            "same token after its expiry",
            lambda: authorize(
                payer, root, tool="pay", amount_paise=899 * RUPEE, depth=1, now=AFTER_EXPIRY
            ),
            expect_ok=False,
        ),
        check(
            "chain parsed against a different root key",
            lambda: Biscuit.from_base64(payer.to_base64(), KeyPair().public_key),
            expect_ok=False,
        ),
        check(
            "delegation with an empty context",
            lambda: delegate(shopper, to="x", tools=["search"], budget_paise=1, context="   "),
            expect_ok=False,
        ),
    ]

    passed = sum(results)
    print(f"\n{passed}/{len(results)} gates passed\n")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
