"""Regression guard for adapter discovery.

Four finders and the Reacher verifier were once fully written, tested, and invisible to
the running application, because registration happens on import and the import list was
maintained by hand. These tests fail if that can happen again.
"""
import pathlib

from coldstack.providers import load_adapters
from coldstack.providers.registry import email_finders, search_providers, verifiers

EXPECTED_FINDERS = {"hunter", "apollo_enrich", "findymail", "prospeo", "leadmagic",
                    "dropcontact", "fake"}
EXPECTED_VERIFIERS = {"hunter", "reacher", "fake"}
EXPECTED_SEARCH = {"apollo", "fake"}


def test_every_adapter_on_disk_is_registered():
    assert EXPECTED_FINDERS <= set(email_finders())
    assert EXPECTED_VERIFIERS <= set(verifiers())
    assert EXPECTED_SEARCH <= set(search_providers())


def test_registry_refills_itself_after_a_module_reload():
    """`uvicorn --reload` reloads modules in place. If the registry tables come back
    empty and nothing refills them, every adapter silently disappears at runtime."""
    import importlib
    import coldstack.providers.registry as reg
    importlib.reload(reg)
    assert "reacher" in reg.verifiers()
    assert EXPECTED_FINDERS <= set(reg.email_finders())


def test_no_adapter_module_is_left_undiscovered():
    """A new file in providers/ or verify/ must be picked up automatically."""
    load_adapters()
    root = pathlib.Path(__file__).resolve().parents[1] / "coldstack"
    on_disk = {p.stem for p in (root / "providers").glob("*.py")} - {
        "__init__", "base", "registry"}
    on_disk |= {p.stem for p in (root / "verify").glob("*.py")} - {"__init__", "cache"}
    registered_modules = {c.__module__.rsplit(".", 1)[-1]
                          for table in (email_finders(), verifiers(), search_providers())
                          for c in table.values()}
    # GenericFinder subclasses are built dynamically inside finders.py.
    registered_modules.add("finders")
    # `finders.py` registers several classes built dynamically; allow module-level match.
    missing = {m for m in on_disk if m not in registered_modules}
    assert not missing, f"adapter modules present but nothing registered from them: {missing}"


def test_every_finder_declares_a_cost():
    """The waterfall ranks on cost per hit; a finder without a price breaks ordering."""
    for name, cls in email_finders().items():
        assert getattr(cls, "est_unit_cost_usd", None) is not None, name
        assert cls.est_unit_cost_usd >= 0, name
