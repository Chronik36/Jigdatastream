from jigstream import backup, db, storage
from jigstream.app import create_app


def test_storage_page_and_freeing_space(tmp_path, monkeypatch):
    data = tmp_path / "BotTrack"
    (data / "models").mkdir(parents=True)
    (data / "models" / "blob").write_bytes(b"x" * 3_000_000)
    (data / "logs").mkdir()
    monkeypatch.setattr("jigstream.macapp.DATA", data)
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path / "bk"))
    path = str(tmp_path / "s.db")
    app = create_app(path)
    con = db.connect(path)
    from jigstream import peersync
    peersync.install(con)  # a link was tried once, never with another computer
    con.execute("INSERT INTO sync_log (tbl, rid, gid, at) VALUES ('x', 1, 'g', 0)")
    con.commit()
    for i in range(5):
        (tmp_path / "bk").mkdir(exist_ok=True)
        (tmp_path / "bk" / f"BotTrack backup 2026-10-0{i + 1} 1200.btbackup").write_bytes(b"b" * 1000)
    c = app.test_client()
    html = c.get("/storage").get_data(as_text=True)
    assert "AI model" in html and "Free up space" in html
    assert c.post("/storage/tidy").status_code == 302
    assert len(backup.listing()) == storage.KEEP_BACKUPS
    assert con.execute("SELECT COUNT(*) FROM sync_log").fetchone()[0] == 0
    assert (data / "models" / "blob").exists()
