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


def search_providers() -> dict[str, Any]: return dict(_SEARCH)
def email_finders() -> dict[str, Any]: return dict(_FINDERS)
def verifiers() -> dict[str, Any]: return dict(_VERIFIERS)
def signal_sources() -> dict[str, Any]: return dict(_SIGNALS)
