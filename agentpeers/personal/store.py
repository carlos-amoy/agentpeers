"""Durable peer conversations, independent of transport and vendor session databases."""
import sqlite3
import threading
import time
import uuid

from . import AUTHORITY
from .config import Rejected, identifier


SCHEMA = """
          CREATE TABLE IF NOT EXISTS conversations(id TEXT PRIMARY KEY, a TEXT, b TEXT,
            scope TEXT, expires REAL, stopped INTEGER DEFAULT 0);
          CREATE TABLE IF NOT EXISTS messages(id TEXT PRIMARY KEY, conversation_id TEXT,
            sender TEXT, recipient TEXT, body TEXT, reply_to TEXT UNIQUE, expects TEXT,
            created REAL, read_at REAL, claimed_at REAL, state TEXT, attempts INTEGER DEFAULT 0,
            next_attempt REAL DEFAULT 0, detail TEXT, callback_hash TEXT, lease TEXT);
          CREATE TABLE IF NOT EXISTS subscriptions(id TEXT PRIMARY KEY, peer TEXT,
            url TEXT, secret TEXT, old_secret TEXT, rotate_until REAL, expires REAL, active INTEGER);
          CREATE TABLE IF NOT EXISTS audit(seq INTEGER PRIMARY KEY, message_id TEXT, at REAL, stage TEXT);
          CREATE TABLE IF NOT EXISTS oauth_clients(id TEXT PRIMARY KEY, redirects TEXT);
          CREATE TABLE IF NOT EXISTS oauth_codes(hash TEXT PRIMARY KEY, client TEXT,
            redirect TEXT, challenge TEXT, resource TEXT, expires REAL);
          CREATE TABLE IF NOT EXISTS oauth_tokens(hash TEXT PRIMARY KEY, client TEXT,
            resource TEXT, kind TEXT, expires REAL);

          CREATE INDEX IF NOT EXISTS messages_pending ON messages(state,next_attempt,created);
          CREATE INDEX IF NOT EXISTS messages_recipient ON messages(recipient,read_at,created);
          CREATE INDEX IF NOT EXISTS messages_conversation ON messages(conversation_id);
          CREATE INDEX IF NOT EXISTS audit_message ON audit(message_id,seq);
"""


class Store:
    def __init__(self, path, config, clock=time.time):
        self.config, self.clock = config, clock
        self.lock = threading.RLock()
        self.db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        application = self.db.execute("PRAGMA application_id").fetchone()[0]
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        populated = self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' LIMIT 1").fetchone()
        if application not in (0, 0x41504752) or version not in (0, 1) or populated and application != 0x41504752:
            self.db.close()
            raise Rejected("Refusing a foreign or unsupported database; use the separate personal-agent directory")
        self.db.execute("PRAGMA application_id=1095780178")  # 0x41504752, APGR
        self.db.execute("PRAGMA user_version=1")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self.recover()

    def recover(self):
        # Never replay a possibly accepted native Grokbot run after a crash.
        with self.lock:
            from .adapters import ADAPTERS
            for row in self.db.execute("SELECT id,recipient FROM messages WHERE state='dispatching'").fetchall():
                platform = self.config["peers"].get(row["recipient"], {}).get("platform")
                adapter = ADAPTERS.get(platform)
                state = "queued" if adapter and adapter.notification_retry_safe else "unknown"
                self.db.execute("UPDATE messages SET state=?,detail='Service restarted during submission',lease=NULL WHERE id=?", (state, row["id"]))

    def transaction(self, operation):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            value = operation()
            self.db.execute("COMMIT")
            return value
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def close(self):
        with self.lock:
            self.db.close()

    def event(self, mid, stage):
        self.db.execute("INSERT INTO audit(message_id,at,stage) VALUES(?,?,?)", (mid, self.clock(), stage))

    def connected(self, actor, target):
        factual = getattr(self, "factual", None)
        if factual and factual.connected(actor, target):
            return True
        context = getattr(self, "context", None)
        if context and "context-requester-1" in (actor, target) and context.connected(actor, target):
            return True
        return actor != target and any(set(pair) == {actor, target} for pair in self.config.get("connections", []))

    def resolve(self, actor, address):
        peers = self.config["peers"]
        candidates = [pid for pid, peer in peers.items() if address in (pid, peer["name"]) and self.connected(actor, pid)]
        if len(candidates) != 1:
            raise Rejected("Destination must be one uniquely named, owner-connected peer")
        return candidates[0]

    def peers(self, actor):
        from .adapters import ADAPTERS
        with self.lock:
            result = []
            for pid, peer in self.config["peers"].items():
                if not self.connected(actor, pid):
                    continue
                subscribed = self.db.execute("SELECT 1 FROM subscriptions WHERE peer=? AND active=1 AND expires>?", (pid, self.clock())).fetchone()
                receiving = ADAPTERS[peer["platform"]].receiving
                result.append({"id": pid, "name": peer["name"], "platform": peer["platform"],
                               "receiving": "subscription_active" if subscribed else "subscription_needed" if receiving == "event_subscription" else receiving,
                               "note": "Replies to local-session-originated conversations only. For a new question use a paired research handoff to one exact launcher; this machine peer cannot receive new conversations." if peer['platform'] == 'local' else "Receiving configuration is not proof the agent is available or has read a message."})
            return result

    def _message(self, mid):
        row = self.db.execute("SELECT * FROM messages WHERE id=?", (mid,)).fetchone()
        if not row:
            raise Rejected("Unknown message")
        return dict(row)

    def _allowed(self, actor, row):
        if actor not in (row["sender"], row["recipient"]) or not self.connected(row["sender"], row["recipient"]):
            raise Rejected("Message is outside this peer's connected conversation")

    def _public(self, row):
        conv = self.db.execute("SELECT scope FROM conversations WHERE id=?", (row["conversation_id"],)).fetchone()
        result = {k: row[k] for k in ("id", "conversation_id", "sender", "recipient", "body", "reply_to", "expects", "created", "read_at", "claimed_at", "state", "attempts", "detail")}
        if result["read_at"] is not None:
            result["detail"] = "Agent acknowledged reading this message"
        result.update(claimed_scope=conv["scope"], authority=AUTHORITY)
        result["replies"] = [r[0] for r in self.db.execute("SELECT id FROM messages WHERE reply_to=?", (row["id"],))]
        return result

    def get(self, actor, mid):
        with self.lock:
            row = self._message(mid)
            self._allowed(actor, row)
            result = self._public(row)
            result["history"] = [dict(r) for r in self.db.execute("SELECT at,stage FROM audit WHERE message_id=? ORDER BY seq", (mid,))]
            return result

    def send(self, actor, to, body, scope="", message_id=None, reply_to=None, expects="reply"):
        mid = identifier(message_id or str(uuid.uuid4()))
        if not isinstance(body, str) or not body.strip() or len(body.encode()) > 32000:
            raise Rejected("Message must contain 1–32000 UTF-8 bytes")
        if expects not in ("none", "reply"):
            raise Rejected("expects must be none or reply")
        with self.lock:
            target = self.resolve(actor, to)
            if self.config['peers'].get(target, {}).get('platform') == 'local' and not reply_to:
                raise Rejected('A local connector is not an individual session. New messages require a paired research handoff; only replies to session-originated conversations can use this peer. Nothing was sent.')
            original = self._message(reply_to) if reply_to else None
            if original:
                self._allowed(actor, original)
                if original["recipient"] != actor or original["sender"] != target:
                    raise Rejected("Reply must return to the original sender")
                if not original["read_at"]:
                    raise Rejected("Read and acknowledge the original before replying")
                conv = self.db.execute("SELECT * FROM conversations WHERE id=?", (original["conversation_id"],)).fetchone()
                cid, scope = conv["id"], conv["scope"]
            else:
                if not isinstance(scope, str) or not scope.strip() or len(scope) > 2000:
                    raise Rejected("Name the human-authorized scope (1–2000 characters)")
                cid = mid
            old = self.db.execute("SELECT * FROM messages WHERE id=?", (mid,)).fetchone()
            if old:
                if (old["sender"], old["recipient"], old["body"], old["reply_to"], old["expects"], old["conversation_id"]) != (actor, target, body, reply_to, expects, cid):
                    raise Rejected("Message ID already belongs to different content")
                if self.db.execute("SELECT scope FROM conversations WHERE id=?", (cid,)).fetchone()[0] != scope:
                    raise Rejected("Message ID already belongs to a different scope")
                return {**self._public(dict(old)), "duplicate": True}
            if original:
                if conv["stopped"] or conv["expires"] <= self.clock():
                    raise Rejected("Conversation stopped or expired")
                if original["expects"] == "none":
                    raise Rejected("The sender did not request another reply")
                if self.db.execute("SELECT 1 FROM messages WHERE reply_to=?", (reply_to,)).fetchone():
                    raise Rejected("Original already has a reply; inspect its status instead of sending again")
                count = self.db.execute("SELECT count(*) FROM messages WHERE conversation_id=?", (cid,)).fetchone()[0]
                if count >= self.config.get("max_messages", 32):
                    raise Rejected("Conversation message limit reached; stop and consult the owner")
            def commit():
                if not original:
                    self.db.execute("INSERT INTO conversations(id,a,b,scope,expires) VALUES(?,?,?,?,?)", (cid, actor, target, scope, self.clock() + self.config.get("conversation_seconds", 86400)))
                state = "available" if self.config["peers"][target]["platform"] in ("local", "http") else "queued"
                self.db.execute("INSERT INTO messages(id,conversation_id,sender,recipient,body,reply_to,expects,created,state) VALUES(?,?,?,?,?,?,?,?,?)", (mid, cid, actor, target, body, reply_to, expects, self.clock(), state))
                self.event(mid, "saved")
            self.transaction(commit)
            return self._public(self._message(mid))

    def read(self, actor, mid):
        # Fetching content alone is NOT a read receipt. The receiver acknowledges separately.
        with self.lock:
            row = self._message(mid)
            self._allowed(actor, row)
            return self._public(row)

    def ack(self, actor, mid):
        with self.lock:
            row = self._message(mid)
            self._allowed(actor, row)
            if row["recipient"] != actor:
                raise Rejected("Only the recipient may acknowledge after reading")
            if not row["read_at"]:
                self.db.execute("UPDATE messages SET read_at=?,state='read',lease=NULL WHERE id=?", (self.clock(), mid))
                self.event(mid, "agent_acknowledged")
            return self._public(self._message(mid))

    def claim(self, actor, mid):
        with self.lock:
            if not mid:
                candidate = self.db.execute("SELECT m.id FROM messages m JOIN conversations c ON c.id=m.conversation_id WHERE m.recipient=? AND m.claimed_at IS NULL AND m.read_at IS NULL AND c.stopped=0 AND c.expires>? ORDER BY m.created LIMIT 1", (actor, self.clock())).fetchone()
                if not candidate:
                    return {"claimed": False, "reason": "empty"}
                mid = candidate[0]
            row = self._message(mid)
            self._allowed(actor, row)
            if row["recipient"] != actor:
                raise Rejected("Only the recipient can claim a message")
            conv = self.db.execute("SELECT * FROM conversations WHERE id=?", (row["conversation_id"],)).fetchone()
            if row["claimed_at"] is not None or row["read_at"] is not None:
                return {"claimed": False, "reason": "already_claimed", "id": mid}
            if conv["stopped"] or conv["expires"] <= self.clock():
                return {"claimed": False, "reason": "conversation_stopped_or_expired", "id": mid}
            self.db.execute("UPDATE messages SET claimed_at=?,state='processing',lease=NULL WHERE id=?", (self.clock(), mid))
            self.event(mid, "agent_claimed")
            return {**self._public(self._message(mid)), "claimed": True}

    def inbox(self, actor):
        with self.lock:
            if self.config["peers"].get(actor, {}).get("platform") == "http":
                # Keep stopped/expired evidence readable by ID, but do not
                # return it as work on every subsequent client poll.
                rows = self.db.execute("SELECT m.* FROM messages m JOIN conversations c ON c.id=m.conversation_id WHERE m.recipient=? AND m.read_at IS NULL AND c.stopped=0 AND c.expires>? ORDER BY m.created LIMIT 100", (actor, self.clock())).fetchall()
            else:
                rows = self.db.execute("SELECT * FROM messages WHERE recipient=? AND read_at IS NULL ORDER BY created LIMIT 100", (actor,)).fetchall()
            return [self._public(dict(r)) for r in rows if self.connected(actor, r["sender"])]

    def stop(self, actor, cid):
        with self.lock:
            conv = self.db.execute("SELECT * FROM conversations WHERE id=?", (cid,)).fetchone()
            if not conv or actor not in (conv["a"], conv["b"]):
                raise Rejected("Unknown conversation for this peer")
            self.db.execute("UPDATE conversations SET stopped=1 WHERE id=?", (cid,))
            self.db.execute("UPDATE messages SET state='stopped',lease=NULL WHERE conversation_id=? AND state IN ('queued','waiting')", (cid,))
            return {"conversation_id": cid, "stopped": True, "note": "An already running agent or in-flight request cannot be cancelled by this tool."}
