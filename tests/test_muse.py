"""Muse regressions with fake credentials, in-memory SQL and no live agents.

The Worker harness exercises production routing/configuration under CPython.
It is not a replacement for the optional workerd integration suite.
"""
import asyncio
import copy
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

from agentpeers.personal import AUTHORITY
from agentpeers.personal.config import Rejected, digest, muse_connection, validate
from test_personal import configuration


ROOT = Path(__file__).resolve().parent.parent


class Result:
    def __init__(self, cursor):
        self.columnNames = [d[0] for d in (cursor.description or [])]
        self.rows = cursor.fetchall()

    def raw(self):
        return self

    def toArray(self):
        return self.rows


class Storage:
    def __init__(self):
        self.connection = sqlite3.connect(':memory:', isolation_level=None)
        self.sql = SimpleNamespace(exec=self.execute)
        self.alarm = None

    def execute(self, statement, *args):
        for part in statement.split(';'):
            if part.strip():
                cursor = self.connection.execute(part, args)
        return Result(cursor)

    def transactionSync(self, operation):
        self.connection.execute('BEGIN IMMEDIATE')
        try:
            operation()
            self.connection.execute('COMMIT')
        except BaseException:
            self.connection.execute('ROLLBACK')
            raise

    async def getAlarm(self):
        return self.alarm

    async def setAlarm(self, value):
        self.alarm = value


class WorkerBase:
    def __init__(self, ctx, env):
        self.ctx, self.env = ctx, env


class Response:
    def __init__(self, body, status=200, headers=None):
        self.body, self.status, self.headers = body, status, headers or {}

    def json(self):
        return json.loads(self.body)


class Body:
    def __init__(self, raw):
        self.raw = raw

    def getReader(self):
        return self

    async def read(self):
        raw, self.raw = self.raw, b''
        return SimpleNamespace(done=not raw, value=SimpleNamespace(to_py=lambda: raw))

    async def cancel(self):
        pass

    def releaseLock(self):
        pass


async def no_network(*args, **kwargs):
    raise AssertionError('Fixture must not make a live request')


def load_worker():
    workers = ModuleType('workers')
    workers.WorkerEntrypoint = workers.DurableObject = WorkerBase
    workers.Response, workers.fetch = Response, no_network
    js = ModuleType('js')
    js.AbortSignal = SimpleNamespace(timeout=lambda timeout: timeout)
    spec = importlib.util.spec_from_file_location('backend', ROOT / 'cloud/backend.py')
    backend = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(backend)
    spec = importlib.util.spec_from_file_location('muse_test_worker', ROOT / 'cloud/worker.py')
    worker = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {'workers': workers, 'js': js, 'backend': backend}):
        spec.loader.exec_module(worker)
    return worker


WORKER = load_worker()


class MuseTests(unittest.TestCase):
    def setUp(self):
        self.storage = Storage()
        self.addCleanup(self.storage.connection.close)
        self.config = validate(configuration())
        self.owner = self.config['owner_key']
        self.mailbox = WORKER.Mailbox(SimpleNamespace(storage=self.storage),
                                    SimpleNamespace(AGENTPEERS_OWNER_KEY=self.owner))
        self.save(self.config)
        # No real credential generation, even for this disposable fixture.
        self.random_key = patch('agentpeers.personal.config.secrets.token_urlsafe', return_value='m' * 40).start()
        self.addCleanup(patch.stopall)

    def save(self, config):
        saved = {k: v for k, v in config.items() if k != 'owner_key'}
        self.mailbox.db.execute('INSERT OR REPLACE INTO ap_settings VALUES(1,1,?)', (json.dumps(saved),))
        self.mailbox.configure(saved)

    def request(self, path, payload=None, token=None, origin='https://bridge.example', method='POST'):
        headers = {}
        if token is not None:
            headers['Authorization'] = 'Bearer ' + token
        if origin is not None:
            headers['Origin'] = origin
        if isinstance(payload, dict):
            raw = json.dumps(payload).encode()
            headers['Content-Type'] = 'application/json'
        else:
            raw = (payload or '').encode()
            headers['Content-Type'] = 'application/x-www-form-urlencoded'
        request = SimpleNamespace(url='https://bridge.example' + path, method=method,
                                  headers=headers, body=Body(raw))
        return asyncio.run(self.mailbox.fetch(request))

    def connect(self, **overrides):
        return self.request('/muse/connect', urlencode({'owner_key': self.owner, **overrides}))

    def call(self, peer, tool, **arguments):
        key = self.mailbox.app.store.config['peers'][peer]['key']
        return self.request('/v1/call', {'tool': tool, 'arguments': arguments}, token=key)

    def incoming(self, mid='muse-question'):
        return self.call('grokbot', 'send_message', to='Muse', message='Fixture question',
                         scope='Fixture only; no actions', message_id=mid)

    def test_additive_owner_configuration_is_idempotent_and_persistent(self):
        db = self.mailbox.db
        db.execute('INSERT INTO oauth_tokens VALUES(?,?,?,?,?)', (digest('fixture-dot-oauth'), 'client', 'https://bridge.example/mcp/dot', 'access', 9999999999))
        self.mailbox.app.store.send('grokbot', 'dot', 'Preserve', 'Fixture', 'old-message')
        before = copy.deepcopy(self.mailbox.app.store.config)
        first = self.connect()
        self.assertEqual(first.status, 200, first.body)
        self.assertEqual(first.headers['Cache-Control'], 'no-store')
        self.assertEqual(first.json()['connections'], ['grokbot'])
        self.assertEqual(first.json()['peer'], 'muse')
        self.assertEqual(first.json()['platform'], 'http')
        self.assertEqual(first.json(), self.connect().json())
        self.random_key.assert_called_once_with(32)
        config = self.mailbox.app.store.config
        self.assertEqual({k: v for k, v in config['peers'].items() if k != 'muse'}, before['peers'])
        self.assertEqual(config['connections'], before['connections'] + [['grokbot', 'muse']])
        self.assertEqual({k: v for k, v in config.items() if k not in ('peers', 'connections')},
                         {k: v for k, v in before.items() if k not in ('peers', 'connections')})
        self.assertNotIn('owner_key', json.loads(db.execute('SELECT config FROM ap_settings').fetchone()[0]))
        # Reconstruct the DO using the same storage, preserving identity and grants.
        self.mailbox = WORKER.Mailbox(self.mailbox.ctx, self.mailbox.env)
        self.assertEqual(self.call('muse', 'list_peers').json()['self'], 'muse')
        self.assertEqual(self.mailbox.app.oauth.actor('fixture-dot-oauth', 'https://bridge.example/mcp/dot'), 'dot')
        self.assertEqual(self.mailbox.app.store.read('dot', 'old-message')['body'], 'Preserve')
        self.assertEqual(self.call('grokbot', 'list_peers').status, 200)
        self.assertEqual(self.call('dot', 'list_peers').status, 200)

    def test_owner_auth_origin_fields_and_first_install_are_guarded(self):
        before = self.mailbox.db.execute('SELECT config FROM ap_settings').fetchone()[0]
        for owner in ('wrong', self.config['peers']['grokbot']['key'], self.config['peers']['dot']['key']):
            self.assertEqual(self.connect(owner_key=owner).status, 400)
        for origin in (None, 'null', 'https://attacker.example'):
            self.assertEqual(self.request('/muse/connect', urlencode({'owner_key': self.owner}), origin=origin).status, 400)
        self.assertEqual(self.connect(public_url='https://other.example').status, 400)
        self.assertEqual(self.connect(connections='dot').status, 400)
        self.assertEqual(self.request('/muse/connect', method='GET').status, 400)
        page = self.request('/muse', method='GET')
        self.assertEqual(page.status, 200)
        self.assertEqual(page.headers['Referrer-Policy'], 'strict-origin')
        self.assertIn("form-action 'self'", page.headers['Content-Security-Policy'])
        self.assertNotIn(self.owner.encode(), page.body)
        self.assertEqual(self.mailbox.db.execute('SELECT config FROM ap_settings').fetchone()[0], before)
        self.random_key.assert_not_called()
        self.mailbox.app = None
        self.assertEqual(self.connect().status, 400)
        self.random_key.assert_not_called()

    def test_owner_endpoint_rate_limit(self):
        for _ in range(10):
            self.assertEqual(self.connect(owner_key='wrong').status, 400)
        result = self.connect()
        self.assertEqual(result.status, 400)
        self.assertIn('Request limit', result.json()['error'])
        self.random_key.assert_not_called()

    def test_conflicting_peer_and_connections_are_preserved(self):
        for kind in ('dot', 'local'):
            current = copy.deepcopy(self.config)
            current['peers']['muse'] = {'name': 'Existing', 'platform': kind, 'key': 'm' * 40}
            with self.assertRaises(Rejected):
                muse_connection(current)
            self.assertEqual(current['peers']['muse']['platform'], kind)
        current = copy.deepcopy(self.config)
        current['peers']['muse'] = {'name': 'Muse', 'platform': 'http', 'key': 'm' * 40}
        current['connections'].append(['muse', 'dot'])
        before = copy.deepcopy(current)
        with self.assertRaises(Rejected):
            muse_connection(current)
        self.assertEqual(current, before)
        del current['peers']['grokbot']
        with self.assertRaises(Rejected):
            muse_connection(current)
        self.random_key.assert_not_called()

    def test_polling_round_trip_has_explicit_claim_read_and_scope(self):
        self.assertEqual(self.connect().status, 200)
        sent = self.incoming().json()
        self.assertEqual((sent['state'], sent['attempts'], sent['read_at']), ('available', 0, None))
        self.assertEqual(sent['authority'], AUTHORITY)
        self.assertIsNone(self.storage.alarm)
        self.assertFalse(self.mailbox.app.dispatcher.once())
        inbox = self.call('muse', 'inbox').json()
        self.assertEqual([m['id'] for m in inbox['messages']], [sent['id']])
        self.assertIsNone(self.call('muse', 'read_message', message_id=sent['id']).json()['read_at'])
        self.assertEqual(self.call('muse', 'reply', message_id=sent['id'], reply_id='early', message='Too early').status, 400)
        self.assertTrue(self.call('muse', 'claim_message', message_id='').json()['claimed'])
        self.assertFalse(self.call('muse', 'claim_message', message_id=sent['id']).json()['claimed'])
        self.call('muse', 'acknowledge', message_id=sent['id'])
        answer = self.call('muse', 'reply', message_id=sent['id'], reply_id='muse-answer', message='Fixture answer').json()
        self.assertEqual((answer['sender'], answer['recipient'], answer['expects']), ('muse', 'grokbot', 'none'))
        self.assertEqual(answer['claimed_scope'], sent['claimed_scope'])
        self.assertEqual(answer['authority'], AUTHORITY)
        self.assertEqual(self.call('muse', 'inbox').json()['messages'], [])
        self.assertTrue(self.call('muse', 'reply', message_id=sent['id'], reply_id='muse-answer', message='Fixture answer').json()['duplicate'])

    def test_muse_can_initiate_but_bearer_identity_cannot_be_spoofed(self):
        self.connect()
        fields = dict(to='grokbot', message='Fixture', scope='Fixture only', message_id='muse-first')
        sent = self.call('muse', 'send_message', **fields).json()
        self.assertEqual((sent['sender'], sent['recipient']), ('muse', 'grokbot'))
        self.assertTrue(self.call('muse', 'send_message', **fields).json()['duplicate'])
        self.assertEqual(self.call('muse', 'send_message', **{**fields, 'sender': 'dot'}).status, 400)
        self.assertEqual(self.request('/v1/call', {'tool': 'list_peers', 'arguments': {}}, token=self.owner).status, 401)
        self.assertEqual(self.request('/v1/call', {'tool': 'list_peers', 'arguments': {}}, token='invalid').status, 401)
        payload = {'tool': 'send_message', 'arguments': {**fields, 'message_id': 'outer-spoof'}, 'sender': 'dot', 'actor': 'grokbot'}
        self.assertEqual(self.request('/v1/call', payload, token='m' * 40).json()['sender'], 'muse')
        self.assertNotEqual(self.request('/mcp/dot', {'id': 1, 'method': 'ping'}, token='m' * 40).status, 200)
        self.assertEqual(self.request('/v1/local', {'session': 'anything', 'tool': 'list_peers', 'arguments': {}}, token='m' * 40).status, 400)
        self.assertEqual(self.connect(owner_key='m' * 40).status, 400)

    def test_muse_and_dot_cannot_discover_send_read_claim_or_ack_each_other(self):
        self.connect()
        self.assertEqual([p['id'] for p in self.call('muse', 'list_peers').json()['peers']], ['grokbot'])
        self.assertNotIn('muse', [p['id'] for p in self.call('dot', 'list_peers').json()['peers']])
        self.assertEqual(next(p for p in self.call('grokbot', 'list_peers').json()['peers'] if p['id'] == 'muse')['receiving'], 'http_polling')
        self.incoming()
        self.mailbox.app.store.send('grokbot', 'dot', 'Private fixture', 'Fixture only', 'dot-only')
        for actor, to, mid in [('muse', 'dot', 'dot-only'), ('dot', 'muse', 'muse-question')]:
            self.assertEqual(self.call(actor, 'send_message', to=to, message='No', scope='No', message_id=actor + '-denied').status, 400)
            for tool in ('read_message', 'claim_message', 'acknowledge', 'message_status'):
                self.assertEqual(self.call(actor, tool, message_id=mid).status, 400)
            self.assertEqual(self.call(actor, 'reply', message_id=mid, reply_id=actor + '-reply-denied', message='No').status, 400)

    def test_queued_polling_records_never_create_callbacks_or_network_requests(self):
        self.connect()
        self.incoming()
        self.mailbox.db.execute("UPDATE messages SET state='queued' WHERE id='muse-question'")
        with patch.object(self.mailbox.app.dispatcher, 'post', side_effect=AssertionError('No HTTP push')):
            self.assertTrue(self.mailbox.app.dispatcher.once())
        row = self.mailbox.app.store._message('muse-question')
        self.assertEqual((row['state'], row['attempts'], row['callback_hash']), ('available', 0, None))
        self.random_key.assert_called_once()

    def test_stopped_expired_and_revoked_work_is_not_polled_or_claimed(self):
        self.connect()
        for mid in ('stopped', 'expired', 'revoked'):
            self.incoming(mid)
        self.call('grokbot', 'stop_conversation', conversation_id='stopped')
        self.mailbox.db.execute("UPDATE conversations SET expires=0 WHERE id='expired'")
        self.assertEqual([m['id'] for m in self.call('muse', 'inbox').json()['messages']], ['revoked'])
        for mid in ('stopped', 'expired'):
            self.assertFalse(self.call('muse', 'claim_message', message_id=mid).json()['claimed'])
            self.assertEqual(self.call('muse', 'read_message', message_id=mid).status, 200)
        self.mailbox.app.store.config['connections'].remove(['grokbot', 'muse'])
        self.assertEqual(self.call('muse', 'inbox').json()['messages'], [])
        self.assertEqual(self.call('muse', 'claim_message', message_id='revoked').status, 400)


if __name__ == '__main__':
    unittest.main()
