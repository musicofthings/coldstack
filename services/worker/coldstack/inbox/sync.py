"""IMAP fetch. Thin on purpose - stdlib imaplib, no extra dependency.

The interesting decisions here are about *not losing messages*:

* UID-based, not sequence-number based. Sequence numbers shift when anything is
  deleted, so a poller using them silently skips mail.
* Resume from the last seen UID rather than from \\Unseen. Marking as read is a side
  effect on the user's real mailbox, and a human opening the inbox first would then
  hide replies from us forever.
* We never delete, move, or flag anything. This is someone's actual mailbox.
"""
from __future__ import annotations

import email
import imaplib
from dataclasses import dataclass
from email.message import Message
from typing import Iterator


@dataclass(slots=True)
class ImapConfig:
    host: str
    user: str
    password: str
    port: int = 993
    folder: str = "INBOX"
    use_ssl: bool = True


@dataclass(slots=True)
class FetchResult:
    messages: list[tuple[int, Message]]
    last_uid: int
    error: str | None = None


def connect(cfg: ImapConfig):
    cls = imaplib.IMAP4_SSL if cfg.use_ssl else imaplib.IMAP4
    conn = cls(cfg.host, cfg.port)
    conn.login(cfg.user, cfg.password)
    return conn


def fetch_since(cfg: ImapConfig, since_uid: int = 0, *, limit: int = 200,
                conn=None) -> FetchResult:
    """Messages with UID > since_uid. `conn` is injectable so this is testable
    without a live server."""
    own = conn is None
    try:
        conn = conn or connect(cfg)
    except (imaplib.IMAP4.error, OSError) as exc:
        return FetchResult([], since_uid, error=str(exc))

    try:
        conn.select(cfg.folder, readonly=True)          # readonly: never flag anything
        typ, data = conn.uid("search", None, f"UID {since_uid + 1}:*")
        if typ != "OK":
            return FetchResult([], since_uid, error=f"search failed: {typ}")

        uids = [int(u) for u in (data[0] or b"").split()]
        # The "UID n:*" form always returns at least the last message even when its UID
        # is <= since_uid, so filter rather than trusting the server's range handling.
        uids = [u for u in uids if u > since_uid][:limit]

        out: list[tuple[int, Message]] = []
        for uid in uids:
            typ, payload = conn.uid("fetch", str(uid), "(RFC822)")
            if typ != "OK" or not payload or not isinstance(payload[0], tuple):
                continue
            out.append((uid, email.message_from_bytes(payload[0][1])))

        last = max([u for u, _ in out], default=since_uid)
        return FetchResult(out, last)
    except (imaplib.IMAP4.error, OSError) as exc:
        return FetchResult([], since_uid, error=str(exc))
    finally:
        if own:
            try:
                conn.logout()
            except Exception:                            # noqa: BLE001 - best effort
                pass


def iter_new(cfg: ImapConfig, since_uid: int = 0, **kw) -> Iterator[tuple[int, Message]]:
    yield from fetch_since(cfg, since_uid, **kw).messages
