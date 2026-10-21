"""Optional offline license keys (signed with Ed25519). DISABLED by default.

Nothing here runs unless JIGSTREAM_REQUIRE_LICENSE=1 is set. A key is `JDS1-<payload>.<signature>` where payload is
base64url JSON ({"id","name","exp"?}) and the signature is made with a private key only the seller holds
(tools/license_tool.py). The app contains just the PUBLIC key, so keys cannot be forged from a copy of the code.

Limits (be honest with yourself about these): a local check can be removed by anyone who edits the source, and an
offline key cannot be revoked. Revocation/real enforcement needs a server; see REVOKED below for a stopgap list.
"""
import base64
import json
import os
import time

PREFIX = "JDS1-"
# Paste the public key printed by `python tools/license_tool.py keygen` here (or set JIGSTREAM_LICENSE_PUBKEY).
PUBLIC_KEY = ""
REVOKED = set()  # license ids to refuse; ship in an update to revoke a leaked key

KEY_FILE = os.path.join(os.environ.get("JIGSTREAM_DATA", "data"), "license.key")


def enforcement_enabled():
    return os.environ.get("JIGSTREAM_REQUIRE_LICENSE", "").lower() in ("1", "true", "yes")


def _b64(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _pubkey():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    raw = os.environ.get("JIGSTREAM_LICENSE_PUBKEY") or PUBLIC_KEY
    if not raw:
        raise ValueError("No license public key configured")
    return Ed25519PublicKey.from_public_bytes(_unb64(raw))


def verify(key, now=None):
    """Return (ok, message, payload)."""
    key = (key or "").strip()
    if not key.startswith(PREFIX) or "." not in key:
        return False, "That doesn't look like a license key", {}
    try:
        from cryptography.exceptions import InvalidSignature
        body, sig = key[len(PREFIX):].split(".", 1)
        try:
            _pubkey().verify(_unb64(sig), body.encode())
        except InvalidSignature:
            return False, "Invalid license key", {}
        payload = json.loads(_unb64(body))
    except ImportError:
        return False, "Install the 'cryptography' package to use licensing", {}
    except (ValueError, KeyError) as e:
        return False, f"License check unavailable: {e}", {}
    if payload.get("id") in REVOKED:
        return False, "This license has been revoked", payload
    if payload.get("exp") and (now or time.time()) > payload["exp"]:
        return False, "This license has expired", payload
    return True, f"Licensed to {payload.get('name', 'unknown')}", payload


def saved_key():
    try:
        return open(KEY_FILE, encoding="utf-8").read().strip()
    except OSError:
        return ""


def save_key(key):
    os.makedirs(os.path.dirname(KEY_FILE) or ".", exist_ok=True)
    with open(KEY_FILE, "w", encoding="utf-8") as f:
        f.write(key.strip())


def status():
    """Current license state; always ok=True when enforcement is off."""
    if not enforcement_enabled():
        return {"enforced": False, "ok": True, "message": "Licensing is off"}
    ok, msg, payload = verify(saved_key())
    return {"enforced": True, "ok": ok, "message": msg if saved_key() else "Enter your license key", "payload": payload}
