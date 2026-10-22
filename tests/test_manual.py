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
    assert 'id="phone"' not in html  # chapters for pages this app doesn't have yet are left out
    images = re.findall(r'src="/static/(manual/[^"]+)"', html)
    assert len(images) >= 20
    assert all((STATIC / i).is_file() for i in images)
