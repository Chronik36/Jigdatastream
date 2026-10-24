import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location("discord_post", Path(__file__).parents[1] / "packaging" / "discord_post.py")
discord_post = importlib.util.module_from_spec(spec)
spec.loader.exec_module(discord_post)


def test_release_message_for_discord():
    m = discord_post.message("120", "Chronik36/BotTrack-releases", "- Order sheet\n\n- Gmail linking\n", ["windows", ""])
    e = m["embeds"][0]
    assert e["title"] == "BotTrack build 120 is out" and e["url"].endswith("/releases/latest")
    assert e["description"].startswith("- Order sheet\n- Gmail linking")
    assert e["fields"][0]["value"].startswith("[Mac, Windows]")
    long = discord_post.message("1", "r/x", "\n".join(f"- change {i} " + "x" * 80 for i in range(100)), [])
    assert len(long["embeds"][0]["description"]) < 4096 and "and more" in long["embeds"][0]["description"]
