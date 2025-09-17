"""Where the root signing key lives.

Before this, `gateway.State()` minted a fresh keypair on every construction. That
made restarting the gateway a silent revocation of every outstanding token - a
mandate a person signed at 10:00 stopped verifying the moment the process
bounced, with no event anywhere saying why.

The key is stored unencrypted on disk, and that is a demo compromise stated
plainly rather than hidden: in a real deployment this belongs in a KMS or an HSM,
and the principal would sign with hardware-backed keys. What the file does fix is
the correctness bug. It does not make the key safe.

Rotation is supported the way AIP §3.7 describes - overlapping validity, where a
new key signs and both keys verify - because a system that cannot rotate a key
also cannot respond to one being disclosed.
"""

from __future__ import annotations

import os
from pathlib import Path

from biscuit_auth import Algorithm, KeyPair, PrivateKey, PublicKey

DEFAULT_PATH = Path(os.getenv("POCKETCHANGE_KEY_DIR", ".keys"))
ROOT_KEY_FILE = "root.key"
PREVIOUS_KEY_FILE = "root.previous.key"


def _read(path: Path) -> KeyPair | None:
    """Load a stored key, or None if there is not a usable one there.

    `PrivateKey.from_bytes` needs the algorithm named explicitly - the raw 32
    bytes do not say which curve they belong to.
    """
    if not path.exists():
        return None
    try:
        return KeyPair.from_private_key(
            PrivateKey.from_bytes(path.read_bytes(), Algorithm.Ed25519)
        )
    except (ValueError, OSError, TypeError):
        # A corrupt or unreadable key file is treated as a missing one. Broader
        # than this would hide real bugs - which it already did once.
        return None


def load_or_create(directory: Path | None = None) -> KeyPair:
    """The root key, created on first use and reused thereafter."""
    folder = directory or DEFAULT_PATH
    path = folder / ROOT_KEY_FILE

    existing = _read(path)
    if existing is not None:
        return existing

    keypair = KeyPair()
    folder.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(keypair.private_key.to_bytes()))
    # Readable only by the owner. A key file with default permissions is a key
    # file anyone on the machine can copy.
    path.chmod(0o600)
    return keypair


def rotate(directory: Path | None = None) -> tuple[KeyPair, PublicKey | None]:
    """Mint a new root key, keeping the old one for verification.

    Returns the new pair and the retired public key. Tokens signed by the old key
    keep verifying until they expire, which is what makes rotation something you
    can actually do rather than an outage.
    """
    folder = directory or DEFAULT_PATH
    path = folder / ROOT_KEY_FILE
    previous_path = folder / PREVIOUS_KEY_FILE

    retiring = _read(path)
    folder.mkdir(parents=True, exist_ok=True)
    if retiring is not None:
        previous_path.write_bytes(bytes(retiring.private_key.to_bytes()))
        previous_path.chmod(0o600)

    fresh = KeyPair()
    path.write_bytes(bytes(fresh.private_key.to_bytes()))
    path.chmod(0o600)
    return fresh, retiring.public_key if retiring else None


def previous_public_key(directory: Path | None = None) -> PublicKey | None:
    """The retired key, if there is one, so old tokens still verify."""
    retired = _read((directory or DEFAULT_PATH) / PREVIOUS_KEY_FILE)
    return retired.public_key if retired else None
