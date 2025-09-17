"""AIP identity documents, Ed25519 keys, aip:web: and aip:key: identifiers.

Two identifier forms, and the difference matters operationally:

  aip:web:host/path      A durable agent. Resolvable via DNS/HTTPS, so its key
                         can be rotated behind a stable name.
  aip:key:ed25519:<key>  An ephemeral agent. The identifier *contains* the key,
                         so it is self-certifying - no registration, no lookup,
                         no coordination. Lives for minutes.

The ephemeral form is what lets an agent spin up a short-lived sub-agent and
hand it narrow authority without anyone provisioning anything first.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass

from biscuit_auth import KeyPair, PrivateKey, PublicKey

WEB_PREFIX = "aip:web:"
KEY_PREFIX = "aip:key:ed25519:"


def _b64(raw: bytes) -> str:
    """URL-safe base64 with padding stripped - safe inside an identifier."""
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


@dataclass(frozen=True)
class Identity:
    """An agent's name plus, if we hold it, the key that speaks for that name."""

    aip_id: str
    keypair: KeyPair | None = None

    @property
    def is_ephemeral(self) -> bool:
        return self.aip_id.startswith(KEY_PREFIX)

    @property
    def public_key(self) -> PublicKey:
        if self.keypair is None:
            raise ValueError(f"no key held for {self.aip_id}")
        return self.keypair.public_key

    @property
    def private_key(self) -> PrivateKey:
        """The signing key. Never leaves the process that owns this identity."""
        if self.keypair is None:
            raise ValueError(f"no key held for {self.aip_id}")
        return self.keypair.private_key

    def __repr__(self) -> str:
        # Deliberately does not render the key material. Identities end up in
        # log lines and exception messages, and a private key that reaches a log
        # is a private key that has been disclosed.
        held = "with key" if self.keypair else "public"
        return f"Identity({self.aip_id!r}, {held})"


def web(host: str, path: str, *, keypair: KeyPair | None = None) -> Identity:
    """A durable, DNS-anchored identity: aip:web:host/path."""
    host = host.strip("/")
    path = path.strip("/")
    if not host:
        raise ValueError("web identity needs a host")
    return Identity(f"{WEB_PREFIX}{host}/{path}" if path else f"{WEB_PREFIX}{host}", keypair)


def ephemeral() -> Identity:
    """A fresh self-certifying identity: aip:key:ed25519:<public key>.

    Generates its own key pair. Because the public key is inside the name, any
    verifier can check a signature against the identifier itself.
    """
    kp = KeyPair()
    return Identity(f"{KEY_PREFIX}{_b64(bytes(kp.public_key.to_bytes()))}", kp)


def parse(aip_id: str) -> Identity:
    """Read an identifier back. Ephemeral ids carry their own public key."""
    if aip_id.startswith(KEY_PREFIX):
        raw = aip_id[len(KEY_PREFIX):]
        padded = raw + "=" * (-len(raw) % 4)
        try:
            key_bytes = base64.urlsafe_b64decode(padded)
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"malformed ephemeral identity: {aip_id}") from exc
        if len(key_bytes) != 32:
            raise ValueError(
                f"ephemeral identity carries {len(key_bytes)} bytes, "
                "Ed25519 public keys are 32"
            )
        return Identity(aip_id)
    if aip_id.startswith(WEB_PREFIX):
        return Identity(aip_id)
    raise ValueError(f"not an AIP identifier: {aip_id!r}")
