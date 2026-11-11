"""Native wake adapters. A successful HTTP call never records an agent read."""
import base64
import hashlib
import hmac
import http.client
import ipaddress
import json
import secrets
import socket
import ssl
from urllib.parse import urlsplit

from .config import Rejected, digest
from .transport import run


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def post_https(url, body, headers):
    """Reject local/redirect targets and pin the checked IP for TLS connection."""
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment or parsed.port not in (None, 443):
        raise Rejected("Outbound destination must use HTTPS port 443 without URL credentials")
    addresses = socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)
    if not addresses:
        raise Rejected("No public destination")
    for entry in addresses:
        ip = ipaddress.ip_address(entry[4][0])
        if not (getattr(ip, "ipv4_mapped", None) or ip).is_global:
            raise Rejected("Private and local network destinations are forbidden")
    family, kind, proto, _, address = addresses[0]
    connection = http.client.HTTPSConnection(parsed.hostname, timeout=10, context=ssl.create_default_context())
    def connect(*_args, **_kwargs):
        sock = socket.socket(family, kind, proto)
        sock.settimeout(10)
        try:
            sock.connect(address)
            return sock
        except BaseException:
            sock.close()
            raise
    connection._create_connection = connect
    try:
        connection.request("POST", (parsed.path or "/") + ("?" + parsed.query if parsed.query else ""), body, {"Content-Type": "application/json", **headers})
        response = connection.getresponse()
        return response.status, response.read(262145)
    finally:
        connection.close()


def signed_post(post, url, secret, subscription_id, event_id, payload, now, old_secret=None):
    body = encode(payload)
    if len(body) > 262144:
        raise Rejected("Event payload exceeds 256 KiB")
    signatures = []
    for key in (secret, old_secret):
        if key:
            raw = base64.b64decode(key.removeprefix("whsec_"), validate=True)
            signed = (event_id + "." + str(int(now)) + ".").encode() + body
            signatures.append("v1," + base64.b64encode(hmac.new(raw, signed, hashlib.sha256).digest()).decode())
    return post(url, body, {"webhook-id": event_id, "webhook-timestamp": str(int(now)),
                           "webhook-signature": " ".join(signatures), "X-MCP-Subscription-Id": subscription_id})


class Events:
    def __init__(self, store, post=post_https):
        self.store, self.post = store, post

    def subscribe(self, actor, params):
        return run(self.subscribe_steps(actor, params), self.post)

    def subscribe_steps(self, actor, params):
        if self.store.config["peers"][actor]["platform"] != "dot":
            raise Rejected("This peer does not use the Dot event adapter")
        delivery = params.get("delivery", {})
        if params.get("name") != "message.created" or params.get("arguments") != {"recipient": actor} or delivery.get("mode") != "webhook":
            raise Rejected("Subscribe to message.created filtered to your recipient ID")
        url, secret = delivery.get("url", ""), delivery.get("secret", "")
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.hostname not in self.store.config["callback_hosts"] or parsed.username or parsed.password or parsed.fragment or parsed.port not in (None, 443):
            import re
            host = parsed.hostname or "missing"
            safe_host = host if re.fullmatch(r"[a-zA-Z0-9.-]{1,253}", host) else "invalid"
            raise Rejected("Callback must use an exact owner-approved public HTTPS hostname; observed hostname: " + safe_host)
        try:
            key = base64.b64decode(secret.removeprefix("whsec_"), validate=True)
        except (ValueError, TypeError, AttributeError):
            raise Rejected("Invalid webhook signing key") from None
        if not secret.startswith("whsec_") or not 24 <= len(key) <= 64:
            raise Rejected("Signing key must contain 24–64 base64 bytes with whsec_ prefix")
        ttl = params.get("ttlMs", 86400000)
        if ttl is None:
            ttl = 604800000  # grant a finite lifetime and let the host refresh
        if isinstance(ttl, bool) or not isinstance(ttl, (float, int)) or not 1000 <= ttl <= 604800000:
            raise Rejected("Subscription ttlMs must be 1000–604800000")
        sid = "sub_" + digest(actor + "\n" + url + "\nmessage.created")
        with self.store.lock:
            other = self.store.db.execute("SELECT 1 FROM subscriptions WHERE peer=? AND id<>? AND active=1 AND expires>?", (actor, sid, self.store.clock())).fetchone()
            if other:
                raise Rejected("Peer already has an active subscription; unsubscribe it before moving the receiving conversation")
        challenge = secrets.token_urlsafe(32)
        try:
            status, body = yield signed_post(lambda *request: request, url, secret, sid, secrets.token_hex(16), {"type": "verification", "challenge": challenge}, self.store.clock())
            echoed = json.loads(body).get("challenge", "")
            if not 200 <= status < 300 or not isinstance(echoed, str) or not hmac.compare_digest(echoed, challenge):
                raise Rejected("Callback challenge failed")
        except Exception:
            raise Rejected("Callback verification failed; subscription was not saved") from None
        with self.store.lock:
            now = self.store.clock()
            old = self.store.db.execute("SELECT * FROM subscriptions WHERE id=?", (sid,)).fetchone()
            previous = old["secret"] if old and old["secret"] != secret else old["old_secret"] if old and old["rotate_until"] > now else None
            rotation_end = now + 300 if old and old["secret"] != secret else old["rotate_until"] if old else 0
            other = self.store.db.execute("SELECT 1 FROM subscriptions WHERE peer=? AND id<>? AND active=1 AND expires>?", (actor, sid, now)).fetchone()
            if other:
                raise Rejected("A concurrent subscription was saved; inspect the existing receiver before changing it")
            self.store.db.execute("INSERT OR REPLACE INTO subscriptions VALUES(?,?,?,?,?,?,?,1)", (sid, actor, url, secret, previous, rotation_end, now + ttl / 1000))
            self.store.db.execute("UPDATE messages SET next_attempt=0 WHERE recipient=? AND state='waiting' AND detail='Dot subscription missing or expired'", (actor,))
        from datetime import datetime, timezone
        return {"id": sid, "refreshBefore": datetime.fromtimestamp(now + ttl / 1000, timezone.utc).isoformat(), "cursor": None, "truncated": False}

    def unsubscribe(self, actor, params):
        with self.store.lock:
            self.store.db.execute("UPDATE subscriptions SET active=0 WHERE peer=? AND url=?", (actor, params.get("delivery", {}).get("url")))
        return {}


class GrokbotAdapter:
    notification_retry_safe = False
    receiving = "native_routine_configured"

    @staticmethod
    def deliver(post, config, peer, row, callback, subscription, now):
        payload = {"schemaVersion": 1, "message_id": row["id"], "sender": row["sender"], "recipient": row["recipient"],
                   "callback_url": config["public_url"] + "/callbacks/" + row["id"] + "/" + callback}
        return post(peer["webhook_url"], encode(payload), {"Authorization": "Bearer " + peer["webhook_key"]})


class DotAdapter:
    notification_retry_safe = True  # Stable event IDs plus recipient-side atomic claim.
    receiving = "event_subscription"

    @staticmethod
    def deliver(post, config, peer, row, callback, subscription, now):
        from datetime import datetime, timezone
        event = {"eventId": row["id"], "name": "message.created", "timestamp": datetime.fromtimestamp(row["created"], timezone.utc).isoformat(),
                 "data": {"message_id": row["id"], "recipient": row["recipient"], "sender": row["sender"], "conversation_id": row["conversation_id"]}, "cursor": None}
        return signed_post(post, subscription["url"], subscription["secret"], subscription["id"], row["id"], event, now,
                           subscription["old_secret"] if subscription["rotate_until"] > now else None)


# New vendors implement delivery and explicitly declare uncertainty behavior here.
# No vendor-specific session, credential or conversation code belongs in Store.
class LocalAdapter:
    notification_retry_safe = False
    receiving = "origin_session_retrieval"


class HttpPollingAdapter:
    notification_retry_safe = True  # No outbound submission can have occurred.
    receiving = "http_polling"


ADAPTERS = {"grokbot": GrokbotAdapter(), "dot": DotAdapter(), "local": LocalAdapter(),
            "http": HttpPollingAdapter()}


class Dispatcher:
    def __init__(self, store, post=post_https):
        self.store, self.post = store, post

    def once(self):
        return run(self.steps(), self.post)

    def steps(self):
        s = self.store
        with s.lock:
            now = s.clock()
            s.db.execute("UPDATE messages SET state='expired' WHERE state IN ('queued','waiting') AND conversation_id IN (SELECT id FROM conversations WHERE expires<=?)", (now,))
            row = s.db.execute("SELECT m.* FROM messages m JOIN conversations c ON c.id=m.conversation_id WHERE m.state IN ('queued','waiting') AND m.next_attempt<=? AND c.stopped=0 AND c.expires>? ORDER BY m.created LIMIT 1", (now, now)).fetchone()
            if not row:
                return False
            row = dict(row)
            peer = s.config["peers"].get(row["recipient"])
            if not peer or not s.connected(row["sender"], row["recipient"]):
                s.db.execute("UPDATE messages SET state='blocked',detail='Connection revoked' WHERE id=?", (row["id"],))
                return True
            adapter = ADAPTERS[peer["platform"]]
            if adapter.receiving == "http_polling":
                # Normally saved as available. Safely handle queued records too,
                # without a callback credential, webhook or delivery attempt.
                s.db.execute("UPDATE messages SET state='available',detail='Available for recipient polling',lease=NULL,callback_hash=NULL WHERE id=?", (row["id"],))
                s.event(row["id"], "available_for_polling")
                return True
            subscription = None
            if adapter.receiving == "event_subscription":
                factual = getattr(s, "factual", None)
                if factual and factual.handles(row["id"]):
                    try:
                        subscription = factual.delivery_subscription(row["id"])
                    except Rejected:
                        s.db.execute("UPDATE messages SET state='blocked',detail='Approved factual receiver or permission changed; owner review required' WHERE id=?", (row["id"],))
                        s.event(row["id"], "factual_delivery_blocked")
                        return True
                else:
                    subscription = s.db.execute("SELECT * FROM subscriptions WHERE peer=? AND active=1 AND expires>? ORDER BY expires DESC LIMIT 1", (row["recipient"], now)).fetchone()
                if not subscription:
                    s.db.execute("UPDATE messages SET state='waiting',detail='Dot subscription missing or expired',next_attempt=? WHERE id=?", (now + 10, row["id"]))
                    return True
                subscription = dict(subscription)
            lease = secrets.token_hex(16)
            callback = secrets.token_urlsafe(32)
            s.db.execute("UPDATE messages SET state='dispatching',attempts=attempts+1,lease=?,callback_hash=? WHERE id=?", (lease, digest(callback), row["id"]))
            s.event(row["id"], "native_submission_started")
        try:
            status, _ = yield adapter.deliver(lambda *request: request, s.config, peer, row, callback, subscription, now)
            if 200 <= status < 300:
                state, detail = "accepted", "Native transport accepted; agent reading is unverified"
            elif status == 429 or adapter.notification_retry_safe and status >= 500:
                state, detail = "waiting", "Native transport temporarily refused delivery (HTTP %d)" % status
            else:
                state, detail = "blocked", "Native transport refused delivery (HTTP %d)" % status
        except Exception:
            # Exception text can contain endpoint URLs or credentials. Never persist it.
            state = "waiting" if adapter.notification_retry_safe else "unknown"
            detail = "Notification transport failed" if state == "waiting" else "Native submission outcome unknown; inspect before retrying"
        with s.lock:
            attempts = row["attempts"] + 1
            if state == "waiting" and attempts >= 3:
                state, detail = "blocked", "Notification retry limit reached; inspect the existing message"
            s.db.execute("UPDATE messages SET state=?,detail=?,next_attempt=?,lease=NULL WHERE id=? AND lease=? AND state='dispatching'", (state, detail, s.clock() + min(300, 10 * 2 ** attempts), row["id"], lease))
            s.event(row["id"], "transport_" + state)
        return True
