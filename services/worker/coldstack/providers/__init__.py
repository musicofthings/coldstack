"""Adapter auto-discovery.

Registration happens via the `@register` decorator, which only runs when a module is
imported. Relying on a hand-maintained import list meant four finders and the Reacher
verifier sat on disk fully written, tested, and completely invisible to the running
application - the registry reported three finders where the repo had six.

So discovery is automatic: every module in this package and in `coldstack.verify` is
imported once, on first use. Dropping a new adapter file in is now sufficient, which is
what `CONTRIBUTING.md` already promised.
"""
from __future__ import annotations

import importlib
import pkgutil

# Support modules, not adapters - importing them has no registration side effect.
_SKIP = {"base", "registry", "__init__"}


def adapter_modules() -> list[str]:
    """Every module whose import registers an adapter."""
    out = []
    for package, skip in (("coldstack.providers", _SKIP),
                          ("coldstack.verify", _SKIP | {"cache"})):
        pkg = importlib.import_module(package)
        out += [f"{package}.{i.name}" for i in pkgutil.iter_modules(pkg.__path__)
                if i.name not in skip]
    return out


def load_adapters(*, force: bool = False) -> list[str]:
    """Import every adapter module so its @register decorators run.

    `force` re-executes modules already in sys.modules. Needed because the decorators
    only run on first execution: if the registry module is reloaded its tables come
    back empty, and a plain import - a cached dict lookup - would not refill them. A
    boolean "already loaded" flag was the first attempt and desynced from the tables
    exactly that way, which would have emptied the registry under `uvicorn --reload`.
    """
    loaded = []
    for name in adapter_modules():
        module = importlib.import_module(name)
        if force:
            importlib.reload(module)
        loaded.append(name)
    return loaded
