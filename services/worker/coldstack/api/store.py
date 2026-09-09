"""Credential storage behind one seam.

P0 ships the in-memory implementation so the slice runs with no database. The
Postgres implementation lands in P1 against the `workspace` / `credential` tables in
migration 0001 - the interface below is what it has to satisfy.

Whichever backend is active, the same invariant holds: plaintext secrets exist only
inside a request that is about to call a provider. They are sealed on write and never
returned - `list_credentials` yields hints, never values.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from ..vault import Vault


@dataclass(slots=True)
class CredentialInfo:
    provider: str
    fields: list[str]
    hints: dict[str, str]
    status: str = "untested"
    last_error: str | None = None


class CredentialStore(Protocol):
    def put(self, workspace_id: str, provider: str, creds: dict[str, str]) -> CredentialInfo: ...
    def get(self, workspace_id: str, provider: str) -> dict[str, str] | None: ...
    def list_credentials(self, workspace_id: str) -> list[CredentialInfo]: ...
    def delete(self, workspace_id: str, provider: str) -> bool: ...
    def set_status(self, workspace_id: str, provider: str, status: str, error: str | None) -> None: ...


@dataclass
class InMemoryStore:
    """Process-local. Everything is still sealed with the vault, so a heap dump or an
    accidental log of this object yields ciphertext rather than working API keys."""
    vault: Vault
    _deks: dict[str, bytes] = field(default_factory=dict)
    _sealed: dict[tuple[str, str], dict[str, bytes]] = field(default_factory=dict)
    _meta: dict[tuple[str, str], CredentialInfo] = field(default_factory=dict)

    def _dek(self, workspace_id: str) -> bytes:
        if workspace_id not in self._deks:
            self._deks[workspace_id] = self.vault.new_workspace_dek(workspace_id)
        return self._deks[workspace_id]

    def put(self, workspace_id: str, provider: str, creds: dict[str, str]) -> CredentialInfo:
        creds = {k: v for k, v in creds.items() if v}
        if not creds:
            raise ValueError("no credential values supplied")
        self._sealed[(workspace_id, provider)] = self.vault.seal_many(
            workspace_id=workspace_id, dek_wrapped=self._dek(workspace_id),
            provider=provider, creds=creds)
        info = CredentialInfo(provider=provider, fields=sorted(creds),
                              hints={k: Vault.hint(v) for k, v in creds.items()})
        self._meta[(workspace_id, provider)] = info
        return info

    def get(self, workspace_id: str, provider: str) -> dict[str, str] | None:
        sealed = self._sealed.get((workspace_id, provider))
        if not sealed:
            return None
        return self.vault.open_many(workspace_id=workspace_id,
                                    dek_wrapped=self._dek(workspace_id),
                                    provider=provider, sealed=sealed)

    def list_credentials(self, workspace_id: str) -> list[CredentialInfo]:
        return [i for (ws, _), i in sorted(self._meta.items()) if ws == workspace_id]

    def delete(self, workspace_id: str, provider: str) -> bool:
        self._meta.pop((workspace_id, provider), None)
        return self._sealed.pop((workspace_id, provider), None) is not None

    def set_status(self, workspace_id: str, provider: str, status: str,
                   error: str | None = None) -> None:
        info = self._meta.get((workspace_id, provider))
        if info:
            info.status = status
            info.last_error = error
