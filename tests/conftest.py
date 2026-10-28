import pytest


@pytest.fixture(autouse=True)
def _keys_off(tmp_path_factory, monkeypatch):
    """The shipped public key turns account keys on; tests of other pages run as if keys were off."""
    from jigstream import accountkeys
    empty = tmp_path_factory.mktemp("keys") / "pub.txt"
    empty.write_text("")
    monkeypatch.setattr(accountkeys, "PUBLIC_FILE", empty)
    monkeypatch.delenv("BOTTRACK_TEST_LOCK", raising=False)


@pytest.fixture(autouse=True)
def _own_card_key(tmp_path_factory, monkeypatch):
    """Each test seals settings with a card key of its own, never one left in the working folder."""
    from jigstream import config, wallet
    monkeypatch.setenv("BOTTRACK_CARD_KEY_FILE", str(tmp_path_factory.mktemp("cardkey") / ".card-key"))
    monkeypatch.setattr(wallet, "_key", None)
    monkeypatch.setattr(config, "_sealed_checked", set())
