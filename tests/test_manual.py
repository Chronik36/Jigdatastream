import re
from pathlib import Path

import jigstream.app as appmod

STATIC = Path(appmod.__file__).parent / "static"


def test_manual_page_covers_every_tab_and_its_pictures_exist(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    html = appmod.create_app(str(tmp_path / "m.db")).test_client().get("/manual").get_data(as_text=True)
    assert 'href="/manual"' in html
    for section in ["start", "dashboard", "orders", "inbox", "expenses", "checkouts", "profiles", "mass-distribute",
                    "cards", "emails", "setup", "setup-updates", "setup-inboxes", "setup-discord", "setup-compare",
                    "training", "logs", "faq"]:
        assert f'id="{section}"' in html, section
    assert 'id="phone"' in html  # shown now that the phone companion is part of the app
    images = re.findall(r'src="/static/(manual/[^"]+)"', html)
    assert len(images) >= 20
    assert all((STATIC / i).is_file() for i in images)


def test_release_manual_export_skips_the_filevault_screen(tmp_path, monkeypatch):
    """Build 152's BotTrack-Manual.html was the FileVault redirect page; the export must be the real manual."""
    import importlib.util
    from jigstream import filevault
    monkeypatch.setattr(filevault, "is_on", lambda fresh=False: False)  # a build Mac without FileVault
    monkeypatch.delenv("FILEVAULT_MODE", raising=False)
    root = Path(appmod.__file__).parent.parent
    spec = importlib.util.spec_from_file_location("export_manual", root / "packaging" / "export_manual.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    out = tmp_path / "BotTrack-Manual.html"
    mod.main(str(out))
    html = out.read_text()
    assert 'id="welcome"' in html and 'id="faq"' in html and "Redirecting" not in html
    with __import__("pytest").raises(SystemExit):
        mod.check(302, "/filevault", "<h1>Redirecting...</h1>")
