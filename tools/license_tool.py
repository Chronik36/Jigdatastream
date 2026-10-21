#!/usr/bin/env python3
"""Seller-side tool (never ship this with the private key).

  python tools/license_tool.py keygen  ~/secret/jds-private.key   # once; prints the PUBLIC key to paste into the app
  python tools/license_tool.py issue   ~/secret/jds-private.key "Customer Name" [--days 365]
"""
import argparse
import base64
import json
import os
import secrets
import sys
import time

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

PREFIX = "JDS1-"
b64 = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=").decode()
RAW = dict(encoding=serialization.Encoding.Raw)


def keygen(path):
    if os.path.exists(path):
        sys.exit(f"{path} already exists; refusing to overwrite")
    k = Ed25519PrivateKey.generate()
    with open(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as f:
        f.write(k.private_bytes(**RAW, format=serialization.PrivateFormat.Raw, encryption_algorithm=serialization.NoEncryption()))
    print("Private key saved to", path, "- keep it secret and backed up. Anyone with it can mint licenses.")
    print("PUBLIC key (paste into jigstream/licensing.py PUBLIC_KEY):")
    print(b64(k.public_key().public_bytes(**RAW, format=serialization.PublicFormat.Raw)))


def issue(path, name, days):
    k = Ed25519PrivateKey.from_private_bytes(open(path, "rb").read())
    payload = {"id": secrets.token_hex(6), "name": name}
    if days:
        payload["exp"] = int(time.time() + days * 86400)
    body = b64(json.dumps(payload, separators=(",", ":")).encode())
    print(f"{PREFIX}{body}.{b64(k.sign(body.encode()))}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("keygen").add_argument("path")
    i = sub.add_parser("issue"); i.add_argument("path"); i.add_argument("name"); i.add_argument("--days", type=int, default=0)
    a = ap.parse_args()
    keygen(a.path) if a.cmd == "keygen" else issue(a.path, a.name, a.days)
