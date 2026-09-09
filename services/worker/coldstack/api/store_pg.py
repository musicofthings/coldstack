"""Postgres-backed credential store.

Same contract as InMemoryStore, same invariant: plaintext exists only inside the call
that is about to use it. The DB holds the wrapped workspace DEK and per-field sealed
secrets, and neither is any use without CREDENTIAL_MASTER_KEY, which lives outside the
database. A stolen dump is ciphertext.

Schema mapping (migration 0001):
    workspace.dek_wrapped      the workspace DEK, wrapped by the master key
    credential                 one row per (workspace, provider, field); `label` holds
                               the field name, so a provider needing api_key + secret
                               is two rows rather than a blob to parse.
"""
from __future__ import annotations

from dataclasses import dataclass

import psycopg
from psycopg.rows import dict_row

from ..vault import Vault
from .store import CredentialInfo


@dataclass
class PostgresStore:
    dsn: str
    vault: Vault

    def _conn(self) -> psycopg.Connection:
        return psycopg.connect(self.dsn, row_factory=dict_row)

    # ---- workspace DEK -------------------------------------------------
    def _dek(self, conn: psycopg.Connection, workspace_id: str) -> bytes:
        row = conn.execute("SELECT dek_wrapped FROM workspace WHERE id = %s",
                           (workspace_id,)).fetchone()
        if row:
            return bytes(row["dek_wrapped"])
        # Auto-provision on first use so a fresh self-host works without a setup step.
        wrapped = self.vault.new_workspace_dek(workspace_id)
        conn.execute(
            "INSERT INTO workspace (id, name, dek_wrapped) VALUES (%s, %s, %s) "
            "ON CONFLICT (id) DO NOTHING",
            (workspace_id, workspace_id, wrapped))
        row = conn.execute("SELECT dek_wrapped FROM workspace WHERE id = %s",
                           (workspace_id,)).fetchone()
        return bytes(row["dek_wrapped"])

    # ---- CredentialStore ------------------------------------------------
    def put(self, workspace_id: str, provider: str, creds: dict[str, str]) -> CredentialInfo:
        creds = {k: v for k, v in creds.items() if v}
        if not creds:
            raise ValueError("no credential values supplied")
        with self._conn() as conn:
            dek = self._dek(conn, workspace_id)
            sealed = self.vault.seal_many(workspace_id=workspace_id, dek_wrapped=dek,
                                          provider=provider, creds=creds)
            for field, blob in sealed.items():
                conn.execute(
                    """INSERT INTO credential
                         (workspace_id, provider, label, secret_sealed, hint, status)
                       VALUES (%s, %s, %s, %s, %s, 'untested')
                       ON CONFLICT (workspace_id, provider, label) DO UPDATE
                         SET secret_sealed = EXCLUDED.secret_sealed,
                             hint          = EXCLUDED.hint,
                             status        = 'untested',
                             last_tested_at = NULL""",
                    (workspace_id, provider, field, blob, Vault.hint(creds[field])))
            conn.commit()
        return CredentialInfo(provider=provider, fields=sorted(creds),
                              hints={k: Vault.hint(v) for k, v in creds.items()})

    def get(self, workspace_id: str, provider: str) -> dict[str, str] | None:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT label, secret_sealed FROM credential "
                "WHERE workspace_id = %s AND provider = %s", (workspace_id, provider)
            ).fetchall()
            if not rows:
                return None
            dek = self._dek(conn, workspace_id)
        return self.vault.open_many(
            workspace_id=workspace_id, dek_wrapped=dek, provider=provider,
            sealed={r["label"]: bytes(r["secret_sealed"]) for r in rows})

    def list_credentials(self, workspace_id: str) -> list[CredentialInfo]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT provider, label, hint, status FROM credential "
                "WHERE workspace_id = %s ORDER BY provider, label", (workspace_id,)
            ).fetchall()
        by_provider: dict[str, CredentialInfo] = {}
        for r in rows:
            info = by_provider.setdefault(
                r["provider"], CredentialInfo(provider=r["provider"], fields=[], hints={},
                                              status=r["status"]))
            info.fields.append(r["label"])
            info.hints[r["label"]] = r["hint"] or "..."
        return list(by_provider.values())

    def delete(self, workspace_id: str, provider: str) -> bool:
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM credential WHERE workspace_id = %s AND provider = %s",
                               (workspace_id, provider))
            conn.commit()
            return cur.rowcount > 0

    def set_status(self, workspace_id: str, provider: str, status: str,
                   error: str | None = None) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE credential SET status = %s, last_tested_at = now() "
                "WHERE workspace_id = %s AND provider = %s", (status, workspace_id, provider))
            conn.commit()
