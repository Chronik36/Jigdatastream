import py_compile
from pathlib import Path

from jigstream import macapp

ROOT = Path(__file__).resolve().parent.parent


def test_windows_folders(monkeypatch, tmp_path):
    monkeypatch.setattr(macapp, "WINDOWS", True)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert macapp.default_data() == tmp_path / "BotTrack"
    assert macapp.resources(tmp_path / "Programs/BotTrack") == tmp_path / "Programs/BotTrack"
    assert not macapp.migrate_old_install(tmp_path / "d", tmp_path / "old")
    monkeypatch.setattr(macapp, "WINDOWS", False)
    assert macapp.resources(Path("/Applications/BotTrack.app")) == Path("/Applications/BotTrack.app/Contents/Resources")


def test_windows_launcher_and_installer_are_in_place():
    py_compile.compile(str(ROOT / "packaging/windows/BotTrack.pyw"), doraise=True, cfile=str(ROOT / ".pytest_launcher.pyc"))
    (ROOT / ".pytest_launcher.pyc").unlink()
    iss = (ROOT / "packaging/windows/BotTrack.iss").read_text()
    assert "PrivilegesRequired=lowest" in iss and "BotTrack.pyw" in iss
    assert "BotTrack-Setup.exe" in (ROOT / ".github/workflows/build-mac.yml").read_text() or \
        "win/*.exe" in (ROOT / ".github/workflows/build-mac.yml").read_text()
