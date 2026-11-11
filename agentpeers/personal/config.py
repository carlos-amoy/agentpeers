"""Owner-managed configuration. No configuration or credential tool is exposed to peers."""
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
from urllib.parse import urlsplit


# Verified against Dot's native subscription challenge. Only starter creation
# uses this default; loading an existing owner's allowlist never widens it.
DEFAULT_DOT_CALLBACK_HOSTS = ("connectors.api.openai.com",)


class Rejected(ValueError):
    pass


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._:-]{0,99}", value):
        raise Rejected("Use a stable ID of 1–100 letters, digits, dots, colons, underscores or hyphens")
    return value


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def private_file(path):
    path = Path(path)
    for target, directory in [(path.parent, True), (path, False)]:
        s = target.lstat()
        if s.st_uid != os.getuid() or s.st_mode & 0o077 or stat.S_ISLNK(s.st_mode):
            raise Rejected("Configuration and its directory must be owned, private and not symlinks")
        if directory and not stat.S_ISDIR(s.st_mode) or not directory and not stat.S_ISREG(s.st_mode):
            raise Rejected("Expected a private regular configuration file")
    return path


def load(path):
    return validate(json.loads(private_file(path).read_text()))


def validate(config):
    base = urlsplit(config["public_url"])
    if base.scheme != "https" or not base.hostname or base.username or base.password or base.query or base.fragment or base.path not in ("", "/") or base.port not in (None, 443):
        raise Rejected("public_url must be an HTTPS origin on port 443")
    config["public_url"] = config["public_url"].rstrip("/")
    if len(config.get("owner_key", "")) < 32:
        raise Rejected("owner_key must contain at least 32 random characters")
    peers = config.get("peers", {})
    if not peers:
        raise Rejected("Configure at least one peer")
    tokens, names = set(), set()
    for peer_id, peer in peers.items():
        identifier(peer_id)
        if peer.get("platform") not in ("grokbot", "dot", "local", "http"):
            raise Rejected("Implemented personal adapters: grokbot, dot, local, http")
        name = peer.get("name", peer_id)
        if not isinstance(name, str) or not name.strip() or len(name) > 100 or name in names:
            raise Rejected("Peer names must be unique and contain 1–100 characters")
        names.add(name)
        peer["name"] = name
        token = peer.get("key", "")
        if len(token) < 32 or token in tokens or token == config["owner_key"]:
            raise Rejected("Each peer needs a distinct random key of at least 32 characters")
        tokens.add(token)
        if peer["platform"] == "grokbot":
            target = urlsplit(peer.get("webhook_url", ""))
            if target.scheme != "https" or not target.hostname or target.username or target.password or target.fragment or target.port not in (None, 443):
                raise Rejected("Grokbot requires its native HTTPS routine URL")
            if not peer.get("webhook_key"):
                raise Rejected("Grokbot requires its native routine key")
    for pair in config.get("connections", []):
        if not isinstance(pair, list) or len(pair) != 2 or pair[0] == pair[1] or any(p not in peers for p in pair):
            raise Rejected("Each connection must name two different configured peer IDs")
    config.setdefault("callback_hosts", [])
    config.setdefault("oauth_redirect_hosts", ["chatgpt.com"])
    for key, default, lower, upper in [("max_messages", 32, 2, 1000), ("conversation_seconds", 86400, 60, 604800)]:
        value = config.setdefault(key, default)
        if isinstance(value, bool) or not isinstance(value, int) or not lower <= value <= upper:
            raise Rejected("Invalid conversation bound: " + key)
    for field in ("callback_hosts", "oauth_redirect_hosts"):
        if not isinstance(config[field], list) or any(not isinstance(h, str) or not re.fullmatch(r"[a-zA-Z0-9.-]+", h) for h in config[field]):
            raise Rejected(field + " must contain exact hostnames")
    return config


def muse_connection(current):
    """Owner-only additive configuration; never replace existing peer credentials."""
    config = json.loads(json.dumps(current))
    peers = config["peers"]
    if peers.get("grokbot", {}).get("platform") != "grokbot":
        raise Rejected("An existing grokbot peer is required; no connection was created")
    existing = peers.get("muse")
    if existing and existing.get("platform") != "http":
        raise Rejected("The muse ID already belongs to another adapter; preserve its configuration")
    if any("muse" in pair and set(pair) != {"grokbot", "muse"}
           for pair in config.get("connections", [])):
        raise Rejected("Muse already has another connection; owner review is required")
    if not existing:
        peers["muse"] = {"name": "Muse", "platform": "http", "key": secrets.token_urlsafe(32)}
    if not any(set(pair) == {"grokbot", "muse"} for pair in config.get("connections", [])):
        config.setdefault("connections", []).append(["grokbot", "muse"])
    return validate(config)
