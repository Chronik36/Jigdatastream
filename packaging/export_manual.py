"""Write the in-app Manual as one self-contained HTML file (pictures and fonts inside), for each release.

    python packaging/export_manual.py dist/BotTrack-Manual.html
"""
import base64
import mimetypes
import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
STATIC = ROOT / "jigstream" / "static"
# Only the manual itself: no sidebar, top bar or banners that need the running app.
PRINT = """<style>
.side,.top,.banner,.mnav,#qe,.toast{display:none!important}
.shell{margin-left:0!important}
body main{padding-top:24px!important;max-width:1200px;margin:0 auto}
</style>"""


def inline(html):
    def data(m):
        path = STATIC / m.group(2).split("?")[0]
        if not path.is_file():
            return m.group(0)
        kind = mimetypes.guess_type(path.name)[0] or ("font/woff2" if path.suffix == ".woff2" else "application/octet-stream")
        return f'{m.group(1)}data:{kind};base64,{base64.b64encode(path.read_bytes()).decode()}'
    return re.sub(r'((?:src=|href=|url\()["\']?)/static/([^"\')\s]+)', data, html)


def main(out):
    out = Path(out).resolve()  # before moving into the temp folder
    cwd = os.getcwd()
    with tempfile.TemporaryDirectory() as tmp:
        os.chdir(tmp)
        os.environ.update(BOTTRACK_HOME=tmp, UI_THEME="light")
        from jigstream import accountkeys
        empty = Path(tmp) / "pub.txt"
        empty.write_text("")
        accountkeys.PUBLIC_FILE = empty  # the manual opens without a key
        from jigstream.app import create_app
        html = create_app(str(Path(tmp) / "m.db")).test_client().get("/manual").get_data(as_text=True)
        os.chdir(cwd)
    html = html.replace("</head>", PRINT + "</head>", 1)
    html = re.sub(r"<title>.*?</title>", "<title>BotTrack Manual</title>", html, count=1, flags=re.S)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(inline(html))
    print(f"wrote {out} ({out.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "dist/BotTrack-Manual.html")
