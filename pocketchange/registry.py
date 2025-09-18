"""The agent registry: what agents exist, who owns them, what they may ever hold.

The enterprise brief asks that agents be "cataloged for cross-department use". A
catalogue nobody consults is documentation, so this one is load-bearing: the
gateway refuses to grant an agent a capability its published card does not
declare, and refuses a budget above its published ceiling.

That gives a second, independent bound on authority. Attenuation already stops a
token being widened; the registry stops one being *minted* wider than the agent
was ever approved for. They fail differently, which is the point - a bug in one
does not silently disable the other.

  attenuation   a token cannot exceed its parent          cryptographic
  registry      an agent cannot exceed its approved card  organisational

Versioning matters for the same reason it matters for any deployed software: an
agent whose prompt changed is a different agent, and a procurement team needs to
know which one spent their money.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone


class RegistryError(Exception):
    """Base for everything this module refuses."""


class UnknownAgent(RegistryError):
    """No agent published under that name, or not at that version."""


class NotApproved(RegistryError):
    """Published, but not approved for use."""


class ExceedsCard(RegistryError):
    """A grant beyond what this agent was approved to hold."""


@dataclass(frozen=True)
class AgentCard:
    """What an agent is, and the most it may ever be trusted with."""

    name: str
    version: str
    department: str
    owner: str
    identity: str                       # aip:web:... - the cryptographic id
    capabilities: tuple[str, ...]       # tools it may ever be granted
    max_budget_paise: int               # ceiling it may ever be granted
    model: str = ""
    description: str = ""
    approved: bool = True
    published_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def ref(self) -> str:
        return f"{self.name}@{self.version}"

    def permits(self, tools: tuple[str, ...], budget_paise: int) -> tuple[bool, str]:
        """Would this card allow such a grant? Returns the reason when not."""
        if not self.approved:
            return False, f"{self.ref} is published but not approved"
        extra = set(tools) - set(self.capabilities)
        if extra:
            return False, (
                f"{self.ref} is not approved for {sorted(extra)}; "
                f"its card allows {sorted(self.capabilities)}"
            )
        if budget_paise > self.max_budget_paise:
            return False, (
                f"{self.ref} may hold at most {self.max_budget_paise}p, "
                f"asked for {budget_paise}p"
            )
        return True, ""

    def summary(self) -> dict:
        return {
            "ref": self.ref,
            "department": self.department,
            "owner": self.owner,
            "identity": self.identity,
            "capabilities": list(self.capabilities),
            "max_budget_paise": self.max_budget_paise,
            "model": self.model,
            "approved": self.approved,
            "published_at": self.published_at.isoformat(),
        }


class AgentRegistry:
    """Publish, version, discover. In-process, shaped so DynamoDB can replace it."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cards: dict[str, dict[str, AgentCard]] = {}   # name -> version -> card

    def publish(self, card: AgentCard) -> AgentCard:
        """Register a version. Re-publishing the same version is refused.

        An agent whose prompt or capabilities changed is a different agent, and
        overwriting a version in place would make the audit trail unable to say
        which one spent the money.
        """
        with self._lock:
            versions = self._cards.setdefault(card.name, {})
            if card.version in versions:
                raise RegistryError(
                    f"{card.ref} is already published; publish a new version instead"
                )
            versions[card.version] = card
            return card

    def get(self, name: str, version: str | None = None) -> AgentCard:
        with self._lock:
            versions = self._cards.get(name)
            if not versions:
                raise UnknownAgent(f"no agent published as {name}")
            if version is None:
                return versions[max(versions)]
            if version not in versions:
                raise UnknownAgent(f"{name} has no version {version}")
            return versions[version]

    def by_identity(self, identity: str) -> AgentCard | None:
        """Find the card for a token's delegate id. None if unregistered."""
        with self._lock:
            for versions in self._cards.values():
                for card in versions.values():
                    if card.identity == identity:
                        return card
        return None

    def discover(
        self, *, capability: str | None = None, department: str | None = None
    ) -> list[AgentCard]:
        """Cross-department discovery: who can do this, and who owns them."""
        with self._lock:
            latest = [versions[max(versions)] for versions in self._cards.values()]
        return sorted(
            (
                card for card in latest
                if (capability is None or capability in card.capabilities)
                and (department is None or card.department == department)
            ),
            key=lambda c: (c.department, c.name),
        )

    def check_grant(
        self, identity: str, tools: tuple[str, ...], budget_paise: int
    ) -> None:
        """Raise unless this identity's card permits such a grant.

        An unregistered identity is allowed through: ephemeral sub-agents are
        minted per purchase and cannot be pre-registered. Their authority is
        already bounded by attenuation - the registry governs the *named* agents
        a department deploys, not the short-lived children they create.
        """
        card = self.by_identity(identity)
        if card is None:
            return
        ok, reason = card.permits(tools, budget_paise)
        if not ok:
            raise ExceedsCard(reason)

    def departments(self) -> list[str]:
        with self._lock:
            return sorted({
                card.department
                for versions in self._cards.values()
                for card in versions.values()
            })

    def __len__(self) -> int:
        with self._lock:
            return sum(len(v) for v in self._cards.values())
