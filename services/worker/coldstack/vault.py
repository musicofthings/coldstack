"""BYOK credential vault — envelope encryption.

Threat model: assume the Postgres dump leaks. A leaked dump must not yield working
third-party API keys, because an open-source tool that stores other people's vendor
credentials is a theft target and a key leak bills the user directly.

Two layers:
    master key (env / KMS, never in the DB)
      -> wraps a per-workspace DEK        (stored in workspace.dek_wrapped)
         -> seals each secret             (stored in credential.secret_sealed)

AES-256-GCM throughout, with authenticated additional data binding every ciphertext to
its workspace and provider. That AAD is what stops a ciphertext-swap: lifting workspace
A's Apollo blob into workspace B's Hunter row fails to decrypt rather than silently
authenticating as someone else's key.

Secrets are write-only from the app's point of view — the API returns `hint` (last four
characters) and a live test result, never the value.
"""
from __future__ import annotations

import base64
import os
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

NONCE_BYTES = 12
KEY_BYTES = 32


class VaultError(Exception):
    pass


def generate_master_key() -> str:
    """For `.env` bootstrap: CREDENTIAL_MASTER_KEY."""
    return base64.b64encode(os.urandom(KEY_BYTES)).decode()


def _load_master(master_key_b64: str) -> bytes:
    if not master_key_b64:
        raise VaultError("CREDENTIAL_MASTER_KEY is not set")
    try:
        key = base64.b64decode(master_key_b64, validate=True)
    except Exception as exc:                              # noqa: BLE001
        raise VaultError("CREDENTIAL_MASTER_KEY is not valid base64") from exc
    if len(key) != KEY_BYTES:
        raise VaultError(f"CREDENTIAL_MASTER_KEY must decode to {KEY_BYTES} bytes, got {len(key)}")
    return key


def _seal(key: bytes, plaintext: bytes, aad: bytes) -> bytes:
    nonce = os.urandom(NONCE_BYTES)
    return nonce + AESGCM(key).encrypt(nonce, plaintext, aad)


def _open(key: bytes, blob: bytes, aad: bytes) -> bytes:
    if len(blob) <= NONCE_BYTES:
        raise VaultError("ciphertext too short")
    try:
        return AESGCM(key).decrypt(blob[:NONCE_BYTES], blob[NONCE_BYTES:], aad)
    except InvalidTag as exc:
        raise VaultError(
            "decryption failed: wrong master key, or this ciphertext belongs to a "
            "different workspace/provider"
        ) from exc


@dataclass(frozen=True, slots=True)
class Vault:
    master_key_b64: str

    # ---- workspace DEK ----
    def new_workspace_dek(self, workspace_id: str) -> bytes:
        """Returns dek_wrapped for `workspace.dek_wrapped`."""
        dek = os.urandom(KEY_BYTES)
        return _seal(_load_master(self.master_key_b64), dek, workspace_id.encode())

    def _dek(self, workspace_id: str, dek_wrapped: bytes) -> bytes:
        return _open(_load_master(self.master_key_b64), dek_wrapped, workspace_id.encode())

    # ---- secrets ----
    @staticmethod
    def _aad(workspace_id: str, provider: str, field: str) -> bytes:
        return f"{workspace_id}|{provider}|{field}".encode()

    def seal_secret(self, *, workspace_id: str, dek_wrapped: bytes, provider: str,
                    field: str, secret: str) -> bytes:
        if not secret:
            raise VaultError("refusing to seal an empty secret")
        return _seal(self._dek(workspace_id, dek_wrapped), secret.encode(),
                     self._aad(workspace_id, provider, field))

    def open_secret(self, *, workspace_id: str, dek_wrapped: bytes, provider: str,
                    field: str, sealed: bytes) -> str:
        return _open(self._dek(workspace_id, dek_wrapped), sealed,
                     self._aad(workspace_id, provider, field)).decode()

    def seal_many(self, *, workspace_id: str, dek_wrapped: bytes, provider: str,
                  creds: dict[str, str]) -> dict[str, bytes]:
        return {f: self.seal_secret(workspace_id=workspace_id, dek_wrapped=dek_wrapped,
                                    provider=provider, field=f, secret=v)
                for f, v in creds.items()}

    def open_many(self, *, workspace_id: str, dek_wrapped: bytes, provider: str,
                  sealed: dict[str, bytes]) -> dict[str, str]:
        return {f: self.open_secret(workspace_id=workspace_id, dek_wrapped=dek_wrapped,
                                    provider=provider, field=f, sealed=b)
                for f, b in sealed.items()}

    @staticmethod
    def hint(secret: str) -> str:
        """What the UI is allowed to see. Never the value."""
        return f"...{secret[-4:]}" if len(secret) >= 4 else "..."
