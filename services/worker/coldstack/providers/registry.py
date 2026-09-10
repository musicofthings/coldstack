"""Adapter registry. Import a module to register it; nothing else knows vendor names."""
from __future__ import annotations
from typing import Any

_SEARCH: dict[str, Any] = {}
_FINDERS: dict[str, Any] = {}
_VERIFIERS: dict[str, Any] = {}
_SIGNALS: dict[str, Any] = {}


def register(kind: str):
    table = {"search": _SEARCH, "finder": _FINDERS, "verifier": _VERIFIERS, "signal": _SIGNALS}[kind]

    def deco(cls):
        table[cls.name] = cls
        return cls
    return deco


def _ensure_loaded() -> None:
    """Ensure EVERY adapter module has been imported.

    Two earlier attempts at this guard were both wrong in the same way - they asked
    "has loading happened?" instead of "is everything loaded?":

      1. A module-level boolean desynced from the tables, so a reload emptied the
         registry permanently.
      2. Checking whether the tables were non-empty meant that if any single adapter
         module happened to be imported first, the registry concluded it was complete
         and the rest never registered - which is how six finders read as one.

    So the check is against the module list itself, and separately against the tables
    for the reload case.
    """
    import sys

    from . import adapter_modules, load_adapters   # lazy: adapters import from here

    if any(m not in sys.modules for m in adapter_modules()):
        load_adapters()
        return
    if not (_SEARCH or _FINDERS or _VERIFIERS or _SIGNALS):
        # Modules are in sys.modules but the tables are empty: registry was reloaded.
        load_adapters(force=True)


def search_providers() -> dict[str, Any]:
    _ensure_loaded()
    return dict(_SEARCH)


def email_finders() -> dict[str, Any]:
    _ensure_loaded()
    return dict(_FINDERS)


def verifiers() -> dict[str, Any]:
    _ensure_loaded()
    return dict(_VERIFIERS)


def signal_sources() -> dict[str, Any]:
    _ensure_loaded()
    return dict(_SIGNALS)
