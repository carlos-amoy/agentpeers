"""Native Durable Object SQLite adapter for the shared messaging application."""
from contextlib import nullcontext
import hashlib
import hmac
import json
import time
from agentpeers.personal.app import App
from agentpeers.personal.config import Rejected
from agentpeers.personal.store import SCHEMA, Store


class Row(dict):
    def __getitem__(self, key):
        return tuple(self.values())[key] if isinstance(key, int) else super().__getitem__(key)


class Cursor:
    def __init__(self, native):
        columns = list(native.columnNames)
        self.rows = iter(Row(zip(columns, row)) for row in native.raw().toArray())

    def fetchone(self):
        return next(self.rows, None)

    def fetchall(self):
        return list(self.rows)

    def __iter__(self):
        return self.rows


class Database:
    def __init__(self, storage):
        self.storage = storage

    def execute(self, sql, bindings=()):
        return Cursor(self.storage.sql.exec(sql, *bindings))

    def executescript(self, sql):
        self.storage.sql.exec(sql)

    def close(self):
        pass


class CloudStore(Store):
    def __init__(self, storage, config):
        self.storage, self.config, self.clock = storage, config, time.time
        self.lock = nullcontext()  # No await is allowed inside shared store operations.
        self.db = Database(storage)
        self.db.executescript(SCHEMA)
        self.recover()

    def transaction(self, operation):
        # Enclose all writes in one native transaction, including rollback on failure.
        result = []
        def apply():
            result.append(operation())
        self.storage.transactionSync(apply)
        return result[0]

    def send(self, *args, **kwargs):
        # A finite mailbox stops accepting new data instead of deleting old messages.
        mid = kwargs.get('message_id') or (args[4] if len(args) > 4 else None)
        if not mid or not self.db.execute('SELECT 1 FROM messages WHERE id=?', (mid,)).fetchone():
            if self.db.execute('SELECT count(*) FROM messages').fetchone()[0] >= 5000:
                raise Rejected('Mailbox capacity reached; owner maintenance required. Existing messages are preserved.')
        return super().send(*args, **kwargs)


class CloudApp(App):
    def __init__(self, store):
        super().__init__(store, rate_lock=nullcontext())

    def limit(self, source, action):
        now = int(self.store.clock())
        key = hmac.new(self.store.config['owner_key'].encode(), source.encode(), hashlib.sha256).hexdigest()[:32]
        self.store.db.execute('DELETE FROM ap_limits WHERE period<?', (now // 60 - 1,))
        row = self.store.db.execute('SELECT n FROM ap_limits WHERE source=? AND action=? AND period=?', (key, action, now // 60)).fetchone()
        if row and row[0] >= (10 if action in ('/oauth/authorize', '/setup', '/status', '/muse/connect', '/context/admin', '/context/login') else 60):
            raise Rejected('Request limit reached; wait before trying again')
        if self.store.db.execute('SELECT count(*) FROM ap_limits').fetchone()[0] >= 1000 and not row:
            raise Rejected('Request limit reached; wait before trying again')
        self.store.db.execute('INSERT INTO ap_limits VALUES(?,?,?,1) ON CONFLICT(source,action,period) DO UPDATE SET n=n+1', (key, action, now // 60))
