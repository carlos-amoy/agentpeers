"""Single-owner cloud deployment. No model API, polling loop or shared hosted tenant."""
import asyncio
import hashlib
import hmac
import html
import ipaddress
import json
import re
import secrets
import time
from urllib.parse import urlsplit

from workers import WorkerEntrypoint, DurableObject, Response, fetch
from js import AbortSignal

from backend import Database, CloudApp, CloudStore
from agentpeers.personal.adapters import encode
from agentpeers.personal.config import DEFAULT_DOT_CALLBACK_HOSTS, Rejected, muse_connection, validate
from agentpeers.personal.endpoint import Endpoint, form
from agentpeers.personal.transport import run_async


async def bounded_body(stream, maximum=262144):
    if stream is None:
        return b''
    reader = stream.getReader()
    async def consume():
        chunks, size = [], 0
        while True:
            item = await reader.read()
            if item.done:
                return b''.join(chunks)
            chunk = bytes(item.value.to_py())
            size += len(chunk)
            if size > maximum:
                raise Rejected('Request or response exceeds 256 KiB')
            chunks.append(chunk)
    try:
        return await asyncio.wait_for(consume(), timeout=10)
    finally:
        await reader.cancel()
        reader.releaseLock()


def response(status, value=None, headers=None, content_type='application/json'):
    body = b'' if value is None else encode(value) if content_type == 'application/json' else value.encode()
    h = {'Content-Type': content_type, 'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer', 'X-Content-Type-Options': 'nosniff', **(headers or {})}
    return Response(body, status=status, headers=h)


class Mailbox(DurableObject):
    def __init__(self, ctx, env):
        super().__init__(ctx, env)
        self.db = Database(ctx.storage)
        known = self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ap_metadata'").fetchone()
        if known:
            version = self.db.execute("SELECT application,version FROM ap_metadata WHERE id=1").fetchone()
            if not version or version['application'] != 'AgentPeers' or version['version'] != 1:
                raise Rejected('Refusing an unsupported or foreign mailbox; preserve its data')
        elif self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name NOT LIKE '_cf_%' LIMIT 1").fetchone():
            raise Rejected('Refusing a foreign mailbox; use a new dedicated namespace')
        else:
            self.db.executescript("CREATE TABLE ap_metadata(id INTEGER PRIMARY KEY,application TEXT,version INTEGER); INSERT INTO ap_metadata VALUES(1,'AgentPeers',1)")
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS ap_settings(id INTEGER PRIMARY KEY CHECK(id=1), version INTEGER, config TEXT);
          CREATE TABLE IF NOT EXISTS ap_limits(source TEXT, action TEXT, period INTEGER, n INTEGER,
            PRIMARY KEY(source,action,period));
          CREATE TABLE IF NOT EXISTS ap_subscription_status(id INTEGER PRIMARY KEY CHECK(id=1),
            attempt TEXT, at REAL, callback_host TEXT, outcome TEXT);
        ''')
        self.app = None
        saved = self.db.execute('SELECT version,config FROM ap_settings WHERE id=1').fetchone()
        if saved:
            if saved['version'] != 1:
                raise Rejected('Unsupported mailbox version; preserve data and restore compatible code')
            self.configure(json.loads(saved['config']))

    def owner_key(self):
        value = str(getattr(self.env, 'AGENTPEERS_OWNER_KEY', ''))
        if len(value) < 32 or value == 'replace-with-a-new-random-owner-key':
            raise Rejected('Set AGENTPEERS_OWNER_KEY to a new random key of at least 32 characters in Cloudflare secrets')
        return value

    def configure(self, config):
        config['owner_key'] = self.owner_key()
        config = validate(config)
        if self.app is None:
            self.app = CloudApp(CloudStore(self.ctx.storage, config))
        else:
            self.app.store.config = config

    def setup_page(self, origin, error=''):
        body = '''<!doctype html><meta charset="utf-8"><title>Connect your agents · AgentPeers</title>
<style>body{font:17px system-ui;max-width:680px;margin:48px auto;padding:0 20px;line-height:1.5}label{display:block;margin:20px 0}input,textarea,button{display:block;box-sizing:border-box;width:100%;padding:10px;font:inherit}button{cursor:pointer}small{color:#555}</style>
<h1>Connect your agents</h1><p>This is your private AgentPeers deployment. Connect your existing Grokbot and Dot; each keeps its own tools and permissions.</p>'''
        if error:
            body += '<p role="alert">' + html.escape(error) + '</p>'
        if self.app:
            body += '<p>Your connection is configured. Saving again preserves messages, peer keys and agent grants.</p>'
        body += '<form method="post" action="/setup"><label>Your AgentPeers password<input type="password" name="owner_key" required autocomplete="off"></label>'
        fields = [('public_url', 'This deployment’s HTTPS address', origin, 'url'), ('webhook_url', 'Grokbot messaging routine URL', '', 'url'), ('webhook_key', 'Grokbot routine key', '', 'password')]
        for name, label, value, kind in fields:
            required = ' required' if name == 'public_url' or not self.app else ''
            hint = ' — leave blank to keep the saved value' if self.app and name != 'public_url' else ''
            body += '<label>%s%s<input type="%s" name="%s" value="%s"%s></label>' % (label, hint, kind, name, html.escape(value, quote=True), required)
        body += '<label>Dot callback hostnames<textarea name="callback_hosts" placeholder="Default: connectors.api.openai.com; blank keeps the saved list"></textarea></label><small>The starter uses Dot’s verified OpenAI notification host. Change it only if your Dot reports another official hostname. Separate exact hostnames with commas.</small><button>Save this connection</button><button formaction="/status" formnovalidate>Check receiving connection</button></form><p>Peer messages are reports from agents. They never grant permission to publish, spend or change access.</p>'
        # no-referrer makes browsers send Origin:null for ordinary form POSTs.
        # Send only this origin, never a credential-bearing path or query.
        return response(200, body, {'Referrer-Policy': 'strict-origin', 'Content-Security-Policy': "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"}, 'text/html; charset=utf-8')

    def authorize_owner(self, args, source, action):
        key = self.owner_key()
        # Bound failed owner attempts even before the first configuration exists.
        limiter = self.app
        if limiter is None:
            class Initial:
                config = {'owner_key': key}
                clock = staticmethod(time.time)
                db = self.db
            limiter = CloudApp.__new__(CloudApp)
            limiter.store = Initial()
        limiter.limit(source, action)
        if not hmac.compare_digest(args.get('owner_key', ''), key):
            raise Rejected('Your AgentPeers password did not match')
        return key

    def owner_status(self, args, source):
        self.authorize_owner(args, source, '/status')
        subscriptions = self.db.execute('SELECT peer,url,expires FROM subscriptions WHERE active=1 AND expires>?', (time.time(),)).fetchall() if self.app else []
        last = self.db.execute('SELECT at,callback_host,outcome FROM ap_subscription_status WHERE id=1').fetchone()
        return response(200, {'configured': self.app is not None,
            'active_receivers': [{'peer': row['peer'], 'callback_host': urlsplit(row['url']).hostname, 'expires_at': row['expires']} for row in subscriptions],
            'last_subscription_attempt': dict(last) if last else None,
            'approved_callback_hosts': self.app.store.config['callback_hosts'] if self.app else []})

    def connect_local(self, args, source):
        self.authorize_owner(args, source, '/local/connect')
        if not self.app:
            raise Rejected('Connect Dot first; no local connection was created')
        config = self.app.local.connection(self.app.store.config, args.get('connector'), args.get('name'))
        saved = {k: v for k, v in config.items() if k != 'owner_key'}
        self.db.execute('INSERT OR REPLACE INTO ap_settings VALUES(1,1,?)', (json.dumps(saved),))
        self.configure(saved)
        peer = config['peers'][args['connector']]
        return response(200, {'version': 1, 'url': config['public_url'],
                              'connector': args['connector'], 'key': peer['key']})

    def muse_page(self):
        body = '''<!doctype html><meta charset="utf-8"><title>Connect Muse · AgentPeers</title>
<h1>Connect Muse to Grokbot only</h1><p>This adds an HTTP polling peer to your existing deployment. It preserves the public address, all existing peer keys, messages and OAuth grants. Muse cannot message Dot. Repeating this action returns the same Muse key; it does not rotate it.</p>
<form method="post" action="/muse/connect"><label>Your AgentPeers password <input type="password" name="owner_key" required autocomplete="off"></label><button>Connect Muse and show its key</button></form>
<p>The response contains a private messaging credential. Transfer it directly to Muse’s secure credential setting, never into an agent chat or repository. Peer messages never grant human authority.</p>'''
        return response(200, body, {'Referrer-Policy': 'strict-origin', 'Content-Security-Policy': "default-src 'none'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"}, 'text/html; charset=utf-8')

    def connect_muse(self, args, source):
        self.authorize_owner(args, source, '/muse/connect')
        if set(args) != {'owner_key'}:
            raise Rejected('Only owner_key is accepted; this action cannot change existing settings')
        if not self.app:
            raise Rejected('An existing configured deployment is required; no connection was created')
        config = muse_connection(self.app.store.config)
        saved = {k: v for k, v in config.items() if k != 'owner_key'}
        self.db.execute('INSERT OR REPLACE INTO ap_settings VALUES(1,1,?)', (json.dumps(saved),))
        self.configure(saved)
        return response(200, {'version': 1, 'url': config['public_url'], 'peer': 'muse',
                              'platform': 'http', 'connections': ['grokbot'],
                              'key': config['peers']['muse']['key']})

    def disconnect_local(self, args, source):
        self.authorize_owner(args, source, '/local/disconnect')
        connector = args.get('connector')
        if not self.app or self.app.store.config['peers'].get(connector, {}).get('platform') != 'local':
            raise Rejected('Select an existing local connector; no other peer can be removed here')
        config = json.loads(json.dumps(self.app.store.config))
        del config['peers'][connector]
        config['connections'] = [pair for pair in config['connections'] if connector not in pair]
        saved = {k: v for k, v in config.items() if k != 'owner_key'}
        self.db.execute('INSERT OR REPLACE INTO ap_settings VALUES(1,1,?)', (json.dumps(saved),))
        self.configure(saved)
        return response(200, {'disconnected': connector, 'messages_preserved': True})

    def subscription_started(self, endpoint, resource, raw):
        # One bounded, owner-only diagnostic row. Never retain a body, URL,
        # credential, signing secret or client-supplied error text.
        try:
            payload = json.loads(raw)
            if not isinstance(payload, dict) or payload.get('method') != 'events/subscribe' or 'id' not in payload:
                return None
            endpoint.auth(resource)
            params = payload.get('params')
            delivery = params.get('delivery') if isinstance(params, dict) else None
            url = delivery.get('url', '') if isinstance(delivery, dict) else ''
            host = urlsplit(url).hostname if isinstance(url, str) else None
            host = host if host and re.fullmatch(r'[a-zA-Z0-9.-]{1,253}', host) else 'invalid'
        except (Rejected, ValueError, TypeError):
            return None
        attempt = secrets.token_hex(8)
        self.db.execute("INSERT OR REPLACE INTO ap_subscription_status VALUES(1,?,?,?,'started')", (attempt, time.time(), host))
        return attempt

    def subscription_finished(self, attempt, output):
        if not attempt:
            return
        value = output[1]
        error = value.get('error') if isinstance(value, dict) else None
        message = error.get('message', '') if isinstance(error, dict) else ''
        if 'observed hostname:' in message:
            outcome = 'callback_host_not_approved'
        elif 'Callback verification failed' in message:
            outcome = 'callback_verification_failed'
        elif error:
            outcome = 'rejected'
        else:
            result = value.get('result') if isinstance(value, dict) else None
            outcome = 'subscribed' if isinstance(result, dict) and result.get('id') else 'request_failed'
        self.db.execute('UPDATE ap_subscription_status SET outcome=? WHERE id=1 AND attempt=?', (outcome, attempt))

    def save_setup(self, args, origin, source):
        key = self.authorize_owner(args, source, '/setup')
        current = self.app.store.config if self.app else None
        config = json.loads(json.dumps(current)) if current else {
            'peers': {'grokbot': {'name': 'Grokbot', 'platform': 'grokbot', 'key': secrets.token_urlsafe(32)},
                      'dot': {'name': 'Dot', 'platform': 'dot', 'key': secrets.token_urlsafe(32)}},
            'connections': [['grokbot', 'dot']], 'oauth_redirect_hosts': ['chatgpt.com'], 'callback_hosts': list(DEFAULT_DOT_CALLBACK_HOSTS),
            'max_messages': 32, 'conversation_seconds': 86400}
        public_url = args.get('public_url', origin).rstrip('/')
        if current and current['public_url'] != public_url:
            raise Rejected('Changing the public address requires an explicit migration; existing grants are preserved')
        config.update(public_url=public_url, owner_key=key)
        if args.get('callback_hosts', '').strip():
            config['callback_hosts'] = [h.strip() for h in args.get('callback_hosts', '').split(',') if h.strip()]
        for field in ('webhook_url', 'webhook_key'):
            if args.get(field) or not current:
                config['peers']['grokbot'][field] = args.get(field, '')
        config = validate(config)
        # Owner key stays in the platform secret, not a duplicated saved setting.
        saved = {k: v for k, v in config.items() if k != 'owner_key'}
        self.db.execute('INSERT OR REPLACE INTO ap_settings VALUES(1,1,?)', (json.dumps(saved),))
        self.configure(saved)
        body = '<!doctype html><meta charset="utf-8"><title>AgentPeers setup saved</title><h1>Bridge settings saved</h1><h2>1. Connect Dot</h2><p>Add this MCP address to your existing Dot using OAuth. On its approval page, enter your AgentPeers password from your password manager:</p><code>%s/mcp/dot</code><h2>2. Give Grokbot its messaging key</h2><p>Copy this key into Grokbot’s masked secret card once. It lets Grokbot identify itself when starting a conversation. Enter it only in that card:</p><pre>%s</pre><p>Your AgentPeers password approves connections. Grokbot uses its separate messaging key for ordinary messages. After connecting both agents, ask Dot in its own chat to listen for Grokbot messages, then verify a short question and reply.</p><a href="/setup">Connection settings</a>' % (html.escape(public_url), html.escape(config['peers']['grokbot']['key']))
        return response(200, body, {'Content-Security-Policy': "default-src 'none'; frame-ancestors 'none'; base-uri 'none'"}, 'text/html; charset=utf-8')

    async def post(self, url, body, headers):
        parsed = urlsplit(url)
        allowed = set(self.app.store.config['callback_hosts']) | {urlsplit(p.get('webhook_url', '')).hostname for p in self.app.store.config['peers'].values()}
        if parsed.scheme != 'https' or parsed.hostname not in allowed or parsed.port not in (None, 443) or parsed.username or parsed.password or parsed.fragment:
            raise Rejected('Outbound hostname is not approved')
        try:
            ipaddress.ip_address(parsed.hostname)
        except ValueError:
            pass
        else:
            raise Rejected('IP-literal destinations are forbidden')
        result = await fetch(url, method='POST', body=body, headers={'Content-Type': 'application/json', **headers}, redirect='manual', signal=AbortSignal.timeout(10000))
        return result.status, await bounded_body(result.body)

    async def arm(self):
        if not self.app:
            return
        now, wake = time.time(), None
        for peer_id, peer in self.app.store.config['peers'].items():
            ready = peer['platform'] != 'dot' or self.db.execute('SELECT 1 FROM subscriptions WHERE peer=? AND active=1 AND expires>?', (peer_id, now)).fetchone()
            # Missing subscriptions wait for a real subscription change, not polling.
            column = 'min(m.next_attempt,c.expires)' if ready else 'c.expires'
            row = self.db.execute("SELECT min(" + column + ") AS due FROM messages m JOIN conversations c ON c.id=m.conversation_id WHERE m.recipient=? AND m.state IN ('queued','waiting') AND c.stopped=0", (peer_id,)).fetchone()
            if row and row['due'] is not None:
                wake = row['due'] if wake is None else min(wake, row['due'])
        if wake is not None:
            due = int(max(now + 0.1, wake) * 1000)
            existing = await self.ctx.storage.getAlarm()
            if existing is None or float(existing) > due:
                await self.ctx.storage.setAlarm(due)

    async def alarm(self, *_args):
        if not self.app:
            return
        # One bounded notification per invocation; no periodic poll when idle.
        try:
            await run_async(self.app.dispatcher.steps(), self.post)
            self.app.worker_warning = False
        except Exception:
            self.app.worker_warning = True
            self.app.store.recover()
        finally:
            await self.arm()

    async def fetch(self, request):
        parsed = urlsplit(request.url)
        origin = parsed.scheme + '://' + parsed.netloc
        source = str(request.headers.get('CF-Connecting-IP', 'unknown'))
        try:
            self.owner_key()
            if parsed.path in ('/muse', '/muse/connect'):
                if request.method == 'GET' and parsed.path == '/muse':
                    return self.muse_page()
                if request.method != 'POST' or parsed.path != '/muse/connect' or request.headers.get('Origin') != origin:
                    raise Rejected('Connect Muse from this deployment’s own /muse page')
                return self.connect_muse(form(await bounded_body(request.body)), source)
            if parsed.path in ('/', '/setup', '/status', '/local/connect', '/local/disconnect'):
                if request.method == 'GET' and parsed.path != '/status':
                    return self.setup_page(origin)
                if request.method != 'POST' or request.headers.get('Origin') != origin:
                    raise Rejected('Save settings from this deployment’s own setup page')
                raw = await bounded_body(request.body)
                if parsed.path == '/local/connect':
                    return self.connect_local(form(raw), source)
                if parsed.path == '/local/disconnect':
                    return self.disconnect_local(form(raw), source)
                if parsed.path == '/status':
                    return self.owner_status(form(raw), source)
                return self.save_setup(form(raw), origin, source)
            if not self.app:
                return response(503, {'error': 'Open /setup to connect your agents; no message was submitted'})
            raw = await bounded_body(request.body) if request.method == 'POST' else b''
            # The platform has parsed/dechunked one request; normalize its known byte count.
            headers = request.headers
            headers['Content-Length'] = str(len(raw))
            if 'Transfer-Encoding' in headers:
                del headers['Transfer-Encoding']
            endpoint = Endpoint(self.app, request.method, parsed.path + ('?' + parsed.query if parsed.query else ''), headers, raw, source)
            attempt = self.subscription_started(endpoint, self.app.store.config['public_url'] + parsed.path, raw) if request.method == 'POST' and parsed.path.startswith('/mcp/') else None
            output = await run_async(endpoint.steps(), self.post)
            self.subscription_finished(attempt, output)
            await self.arm()
            return response(*output)
        except Rejected as exc:
            return response(400, {'error': str(exc)})
        except Exception:
            return response(400, {'error': 'Request failed; inspect existing message status before retrying'})


class Default(WorkerEntrypoint):
    async def fetch(self, request):
        if request.method not in ('GET', 'POST'):
            return response(405, {'error': 'Use GET or POST'})
        try:
            if int(request.headers.get('Content-Length', '0')) > 262144:
                return response(413, {'error': 'Request exceeds 256 KiB'})
        except ValueError:
            return response(400, {'error': 'Invalid request length'})
        return await self.env.MAILBOX.getByName('owner').fetch(request)
