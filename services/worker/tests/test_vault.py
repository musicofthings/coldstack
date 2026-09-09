import pytest
from coldstack.vault import Vault, VaultError, generate_master_key

WS = "ws-1"


@pytest.fixture
def vault_and_dek():
    v = Vault(generate_master_key())
    return v, v.new_workspace_dek(WS)


def test_roundtrip(vault_and_dek):
    v, dek = vault_and_dek
    sealed = v.seal_secret(workspace_id=WS, dek_wrapped=dek, provider="apollo",
                           field="api_key", secret="sk_live_ABCD1234")
    assert v.open_secret(workspace_id=WS, dek_wrapped=dek, provider="apollo",
                         field="api_key", sealed=sealed) == "sk_live_ABCD1234"


def test_ciphertext_is_not_plaintext(vault_and_dek):
    v, dek = vault_and_dek
    sealed = v.seal_secret(workspace_id=WS, dek_wrapped=dek, provider="apollo",
                           field="api_key", secret="sk_live_ABCD1234")
    assert b"sk_live" not in sealed


@pytest.mark.parametrize("swap", [
    {"provider": "hunter"},              # same workspace, different vendor
    {"field": "api_secret"},             # same vendor, different field
    {"workspace_id": "ws-2"},            # different workspace
])
def test_aad_binding_blocks_ciphertext_swap(vault_and_dek, swap):
    """A leaked row must not decrypt anywhere other than exactly where it was written."""
    v, dek = vault_and_dek
    base = dict(workspace_id=WS, dek_wrapped=dek, provider="apollo", field="api_key")
    sealed = v.seal_secret(**base, secret="secret-value")
    with pytest.raises(VaultError):
        v.open_secret(**{**base, **swap}, sealed=sealed)


def test_wrong_master_key_cannot_open(vault_and_dek):
    _, dek = vault_and_dek
    with pytest.raises(VaultError):
        Vault(generate_master_key()).open_secret(
            workspace_id=WS, dek_wrapped=dek, provider="apollo",
            field="api_key", sealed=b"\x00" * 44)


def test_refuses_empty_secret(vault_and_dek):
    v, dek = vault_and_dek
    with pytest.raises(VaultError):
        v.seal_secret(workspace_id=WS, dek_wrapped=dek, provider="apollo",
                      field="api_key", secret="")


def test_hint_never_leaks_the_key():
    assert Vault.hint("sk_live_ABCD1234") == "...1234"
