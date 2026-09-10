"""All transports, discovered rather than listed.

The provider registry had a hand-maintained import list and it silently hid four
adapters for a whole phase. This one is built the same way to avoid the same bug: every
module under `transports/` is imported and scanned, so adding a file is enough.
"""
from __future__ import annotations

import importlib
import inspect
import pkgutil

from .base import Transport

_SKIP = {"__init__"}


def _discover() -> dict[str, Transport]:
    found: dict[str, Transport] = {}
    pkg = importlib.import_module("coldstack.sending.transports")
    for info in pkgutil.iter_modules(pkg.__path__):
        if info.name in _SKIP:
            continue
        module = importlib.import_module(f"coldstack.sending.transports.{info.name}")

        # A module may expose a factory returning several transports (the ESP module
        # builds one per vendor spec) or plain classes.
        if hasattr(module, "build_all"):
            found.update(module.build_all())
            continue

        for _, obj in inspect.getmembers(module, inspect.isclass):
            if obj.__module__ != module.__name__:
                continue
            name = getattr(obj, "name", None)
            caps = getattr(obj, "caps", None)
            if not name or caps is None:
                continue
            try:
                found[name] = obj()
            except TypeError:
                continue          # needs constructor arguments; not a default transport
    return found


def all_transports() -> dict[str, Transport]:
    return _discover()


def describe() -> list[dict]:
    """Rows for the 'connect a sender' screen."""
    return [{
        "name": n,
        "family": str(t.caps.family),
        "cold_safe": t.caps.cold_outreach_safe,
        "batch": t.caps.max_batch_size,
        "reply_ingest": t.caps.supports_reply_ingest,
        "notes": t.caps.notes,
    } for n, t in sorted(all_transports().items(),
                         key=lambda kv: (not kv[1].caps.cold_outreach_safe, kv[0]))]
