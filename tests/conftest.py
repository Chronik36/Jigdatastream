import pytest


@pytest.fixture(autouse=True)
def _keys_off(tmp_path_factory, monkeypatch):
    """The shipped public key turns account keys on; tests of other pages run as if keys were off."""
    from jigstream import accountkeys
    empty = tmp_path_factory.mktemp("keys") / "pub.txt"
    empty.write_text("")
    monkeypatch.setattr(accountkeys, "PUBLIC_FILE", empty)
    monkeypatch.delenv("BOTTRACK_TEST_LOCK", raising=False)
