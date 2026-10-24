"""Post a new BotTrack release and its patch notes to a Discord channel through a webhook.

Used by the release workflow: python3 discord_post.py <build> <releases repo> <notes file> [windows] [android]
with the webhook address in DISCORD_WEBHOOK. Standard library only.
"""
import json
import os
import sys
import urllib.request

TEAL = 0x3DD8EF


def message(build, repo, notes, extras):
    page = f"https://github.com/{repo}/releases/latest"
    lines = [n.strip() for n in notes.splitlines() if n.strip()]
    text = "\n".join(lines)
    if len(text) > 3500:  # Discord allows 4096 characters in a description
        text = text[:3500].rsplit("\n", 1)[0] + "\n- …and more"
    downloads = ["Mac"] + [x for x, on in (("Windows", "windows" in extras), ("Android", "android" in extras)) if on]
    return {
        "username": "BotTrack",
        "embeds": [{
            "title": f"BotTrack build {build} is out",
            "url": page,
            "color": TEAL,
            "description": (text or "Fixes and improvements.") + "\n\n**Update:** BotTrack shows *Update ready* by itself, "
                           "or press Update on Setup.",
            "fields": [{"name": "Download", "value": f"[{', '.join(downloads)}]({page})", "inline": False}],
        }],
    }


def main(argv):
    build, repo, notes_file, *extras = argv
    with open(notes_file) as f:
        body = json.dumps(message(build, repo, f.read(), extras)).encode()
    req = urllib.request.Request(os.environ["DISCORD_WEBHOOK"], data=body, method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": "BotTrack-release"})
    with urllib.request.urlopen(req, timeout=20) as r:
        print("posted to Discord:", r.status)


if __name__ == "__main__":
    main(sys.argv[1:])
