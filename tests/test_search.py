"""Sourcing from the open web: what comes back, and what it cannot do.

The corpus in merchant/web_index.py carries four adversarial pages drawn from the
AgentDojo shapes. These tests assert the two properties the design rests on -
everything is marked at one boundary, and the agent that reads it holds no money.
"""

import pytest
from datetime import datetime, timedelta, timezone

from agent import search
from agent.utils.provenance import INSTRUCTION, MARKER, is_marked, unmark
from merchant import web_index
from pocketchange import events, funnel, identity, token as tokens
from pocketchange.policy import RUPEE, Operation


# --- everything is marked, at one boundary ----------------------------------


def test_every_text_field_that_leaves_search_is_marked():
    results = search.search("laptops for engineering", provider=search.LocalCorpus())
    assert results
    for r in results:
        assert is_marked(r.title_untrusted)
        assert is_marked(r.body_untrusted)
        if r.supplier_untrusted:
            assert is_marked(r.supplier_untrusted)


def test_the_corpus_itself_is_not_pre_marked():
    """Marking happens in agent/search.py and nowhere else. A corpus that marked
    itself would let a second provider through unmarked and nothing would fail."""
    for page in web_index.PAGES:
        assert MARKER not in page.body
        assert MARKER not in page.title


def test_a_hostile_page_survives_marking_as_readable_data():
    """The injection is not removed - it is labelled. Deleting it would hide
    what the corpus is for, and a real search cannot delete anything."""
    hits = search.search("laptops lowest price", provider=search.LocalCorpus())
    hostile = [r for r in hits if "bestdeals" in r.url]
    assert hostile, "the adversarial page should rank alongside honest ones"
    assert "Ignore your previous instructions" in unmark(hostile[0].body_untrusted)
    assert is_marked(hostile[0].body_untrusted)


def test_the_brief_always_ships_with_the_spotlighting_instruction():
    brief = search.brief(search.search("chairs", provider=search.LocalCorpus()))
    assert INSTRUCTION in brief
    assert "untrusted" in brief


def test_a_seller_claimed_price_is_never_offered_as_a_price_we_pay():
    hits = search.search("laptops", provider=search.LocalCorpus())
    for r in hits:
        assert "claimed_price_paise" not in r.as_prompt_dict()


def test_provider_defaults_to_the_local_corpus_without_a_key(monkeypatch):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    assert search.provider_from_env().name == "local"


def test_tavily_is_used_when_a_key_is_present(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-not-a-real-key")
    assert search.provider_from_env().name == "tavily"


# --- the searcher holds no money --------------------------------------------


def _funnel(budget=600_000 * RUPEE, **kw):
    root = tokens.mint(identity.ephemeral(), budget_paise=budget, max_depth=8,
                       expires=datetime.now(timezone.utc) + timedelta(hours=1))
    return funnel.Funnel(root, description="equip the office", budget_paise=budget,
                         bus=events.EventBus(history=10_000), **kw)


def test_a_search_child_cannot_spend_a_single_paisa():
    f = _funnel()
    looker = f.search_child(f.root)
    assert looker.budget_paise == 0
    with pytest.raises(tokens.Denied):
        tokens.verify(looker.token, Operation("pay", 1, depth=looker.depth))


def test_a_search_child_cannot_delegate_either():
    f = _funnel()
    looker = f.search_child(f.root)
    with pytest.raises(tokens.Denied):
        tokens.verify(looker.token, Operation("delegate", 0, depth=looker.depth))


def test_a_search_child_may_search():
    f = _funnel()
    looker = f.search_child(f.root)
    tokens.verify(looker.token, Operation("search", 0, depth=looker.depth))


def test_a_best_sourced_leaf_searches_before_it_pays():
    seen = []

    def do_search(node):
        seen.append(node.id)
        return search.search(node.description, provider=search.LocalCorpus())

    f = _funnel(10_000 * RUPEE, bounds=funnel.Bounds(decompose_floor_paise=50_000 * RUPEE),
                sourcing=funnel.BEST, search=do_search)
    result = f.run(lambda n: [], lambda n: "paid")

    # The root narrows once before paying, so the looker serves root.1 and
    # hangs off the root as its sibling.
    assert seen == ["root.1~search"]
    payer = result.root.children[0]
    assert payer.children == []
    assert result.leaves == [payer]
    looker = f.root.helpers[0]
    assert looker.state == funnel.EXECUTED
    assert looker.budget_paise == 0
    # The searching and settled events landed on the looker, not the payer.
    kinds = {e.kind for e in f.bus.history() if e.node_id == "root.1~search"}
    assert events.SEARCHING in kinds


def test_without_a_search_callable_best_does_not_pretend_to_have_searched():
    f = _funnel(10_000 * RUPEE, bounds=funnel.Bounds(decompose_floor_paise=50_000 * RUPEE),
                sourcing=funnel.BEST)
    result = f.run(lambda n: [], lambda n: "paid")
    assert result.root.helpers == []
    assert result.root.children[0].helpers == []
    assert events.SEARCHING not in {e.kind for e in f.bus.history()}


def test_a_single_word_value_is_still_marked():
    """Interleaving nothing is a no-op, so one-token untrusted text used to come
    out looking trusted. Every supplier name is one token."""
    from agent.utils.provenance import mark

    assert is_marked(mark("meridian-systems"))
    assert unmark(mark("meridian-systems")) == "meridian-systems"
    assert mark(mark("meridian-systems")) == mark("meridian-systems")   # idempotent
    assert unmark(mark("a longer phrase here")) == "a longer phrase here"


def test_a_node_that_pays_cannot_also_read_the_open_web():
    """The separation the whole design rests on, and the one nothing tested.

    A `best`-sourced node spawns a looker and used to keep `search` for itself
    as well, so the agent reading supplier-written pages could also spend. The
    looker was decorative and two published claims were false. Nothing failed,
    because nothing checked - so this is the check.
    """
    captured = {}

    def spy(node):
        captured[node.id] = node
        return []

    # Floor above the depth-2 share, so depth 2 produces genuine leaves rather
    # than branches that were sized to split and then had nothing to split into.
    f = _funnel(600_000 * RUPEE, sourcing=funnel.BEST, search=spy,
                bounds=funnel.Bounds(max_nodes=200,
                                     decompose_floor_paise=100_000 * RUPEE))
    result = f.run(
        lambda n: ([] if n.depth >= 2 else
                   [funnel.SubTask(f"{n.description} / {i}", n.budget_paise // 3)
                    for i in range(3)]),
        lambda n: "paid",
    )

    # Only the LEAVES can be held to this. A branch must hold everything it
    # confers - a parent without `pay` cannot grant `pay` - so branches carry
    # pay and search together and are named as brokers, which the gateway
    # refuses. The cryptographic property lives at the leaves.
    # Partitioned by AUTHORITY, not by tree shape: a node with no children may
    # still be a branch that found nothing to split into.
    leaves = [n for n in result.root.walk()
              if not n.is_helper and n.depth > 0 and not tokens.is_broker(n.token)]
    assert leaves
    for node in leaves:
        tokens.verify(node.token, Operation("pay", 1, depth=node.depth))
        with pytest.raises(tokens.Denied):
            tokens.verify(node.token, Operation("search", 0, depth=node.depth))

    branches = [n for n in result.root.walk()
                if not n.is_helper and n.depth > 0 and tokens.is_broker(n.token)]
    assert branches, "the tree should have at least one branch"

    lookers = [n for n in result.root.walk() if n.is_helper]
    assert lookers
    for looker in lookers:
        tokens.verify(looker.token, Operation("search", 0, depth=looker.depth))
        with pytest.raises(tokens.Denied):
            tokens.verify(looker.token, Operation("pay", 1, depth=looker.depth))
