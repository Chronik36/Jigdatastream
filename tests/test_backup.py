import os
import zipfile
import io

import pytest

from jigstream import backup, db, wallet


def test_backup_and_restore_everything(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BOTTRACK_CARD_KEY_FILE", str(tmp_path / ".card-key"))
    monkeypatch.setenv("PRODUCTS_FILE", str(tmp_path / "products.json"))
    monkeypatch.setattr(wallet, "_key", None)
    (tmp_path / ".env").write_text("GMAIL_ACCOUNTS=me@gmail.com:abcd\nPORT=8765\n")
    (tmp_path / "products.json").write_text('{"products": []}')
    path = str(tmp_path / "live.db")
    con = db.connect(path)
    con.execute("INSERT INTO profiles (name) VALUES ('Profile 1')")
    old_key = wallet.key()
    saved = backup.save("correct horse", path, where=tmp_path / "b")
    assert saved.read_bytes().startswith(backup.MAGIC) and b"Profile 1" not in saved.read_bytes()
    with pytest.raises(ValueError, match="password"):
        backup.unlock(saved.read_bytes(), "wrong password")
    with zipfile.ZipFile(io.BytesIO(backup.unlock(saved.read_bytes(), "correct horse"))) as z:
        assert {"jigstream.db", "env", "card-key", "products.json", "about.json"} <= set(z.namelist())

    # Everything goes wrong: the data, the settings and the card key change.
    con.execute("DELETE FROM profiles")
    (tmp_path / ".env").write_text("PORT=9999\n")
    wallet.set_key(b"x" * 0 + __import__("cryptography.fernet", fromlist=["Fernet"]).Fernet.generate_key())
    assert wallet.key() != old_key
    backup.restore(saved.read_bytes(), "correct horse", path, env_file=str(tmp_path / ".env"))
    assert [r[0] for r in db.connect(path).execute("SELECT name FROM profiles")] == ["Profile 1"]
    assert wallet.key() == old_key
    env = (tmp_path / ".env").read_text()
    assert "PORT=9999" in env and "me@gmail.com" not in env  # this computer's port stays; the inbox is sealed
    from jigstream import config
    sealed = next(line.split("=", 1)[1] for line in env.splitlines() if line.startswith("GMAIL_ACCOUNTS="))
    assert config.unseal(sealed) == "me@gmail.com:abcd"
    assert os.environ["GMAIL_ACCOUNTS"] == "me@gmail.com:abcd"


def test_old_backups_are_trimmed(tmp_path, monkeypatch):
    where = tmp_path / "b"
    where.mkdir()
    for i in range(20):
        (where / f"BotTrack backup 2026-01-{i + 1:02d} 0000.btbackup").write_bytes(b"x")
    monkeypatch.setattr(backup, "make", lambda db_path=None: b"zip")
    backup.save("pw123456", where=where)
    assert len(list(where.glob("*.btbackup"))) == backup.KEEP
