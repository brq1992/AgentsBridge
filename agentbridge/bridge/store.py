import contextlib
import hashlib
import json
import math
import re
import sqlite3
import time
import uuid


class BridgeError(Exception):
    pass


def uid(prefix='m'):
    return prefix + '_' + uuid.uuid4().hex


class Store:
    def __init__(self, path):
        self.path = str(path)
        with self.connect() as c:
            c.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY, alias TEXT UNIQUE NOT NULL, tool TEXT NOT NULL,
                native_id TEXT NOT NULL, project TEXT NOT NULL, generation TEXT NOT NULL,
                enabled INTEGER NOT NULL, metadata TEXT NOT NULL,
                UNIQUE(tool,native_id));
            CREATE TABLE IF NOT EXISTS messages (
                id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, parent_id TEXT,
                kind TEXT NOT NULL, sender TEXT NOT NULL, sender_gen TEXT NOT NULL,
                receiver TEXT NOT NULL, receiver_gen TEXT NOT NULL, project TEXT NOT NULL,
                body TEXT NOT NULL, expect_reply INTEGER NOT NULL, final INTEGER NOT NULL,
                hop INTEGER NOT NULL, created REAL NOT NULL, expires REAL NOT NULL,
                delivery TEXT NOT NULL, outcome TEXT NOT NULL, received_at REAL,
                final_reply_id TEXT, fingerprint TEXT NOT NULL, error TEXT);
            CREATE UNIQUE INDEX IF NOT EXISTS one_final ON messages(parent_id) WHERE final=1;
            CREATE TABLE IF NOT EXISTS events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL,
                message_id TEXT, event TEXT NOT NULL, detail TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS clients (
                token_hash TEXT PRIMARY KEY, session_id TEXT NOT NULL,
                generation TEXT NOT NULL, directory TEXT UNIQUE NOT NULL);
            ''')

    @contextlib.contextmanager
    def connect(self):
        c = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute('PRAGMA busy_timeout=10000')
        c.execute('PRAGMA synchronous=FULL')
        try:
            c.execute('BEGIN IMMEDIATE')
            yield c
            c.commit()
        except BaseException:
            c.rollback()
            raise
        finally:
            c.close()

    def event(self, c, mid, event, detail=None):
        c.execute('INSERT INTO events(at,message_id,event,detail) VALUES(?,?,?,?)',
                  (time.time(), mid, event, json.dumps(detail or {}, ensure_ascii=False)))

    def _session(self, c, identity):
        row = c.execute('SELECT * FROM sessions WHERE id=? OR alias=?', (identity, identity)).fetchone()
        if not row or not row['enabled']:
            raise BridgeError('binding unavailable: ' + identity)
        return dict(row)

    def bind(self, tool, native_id, alias, project, metadata):
        native_id = str(uuid.UUID(native_id))
        sid = tool + ':' + native_id
        with self.connect() as c:
            old = c.execute('SELECT * FROM sessions WHERE id=?', (sid,)).fetchone()
            encoded = json.dumps(metadata, sort_keys=True)
            if old and old['enabled'] and old['metadata'] == encoded and old['project'] == project:
                generation = old['generation']
            else:
                generation = uid('g')
            try:
                c.execute('''INSERT INTO sessions VALUES(?,?,?,?,?,?,1,?)
                    ON CONFLICT(id) DO UPDATE SET alias=excluded.alias, project=excluded.project,
                    generation=excluded.generation, enabled=1, metadata=excluded.metadata''',
                    (sid, alias, tool, native_id, project, generation, encoded))
            except sqlite3.IntegrityError as e:
                raise BridgeError('alias already bound; unbind/rename explicitly') from e
            return self._session(c, sid)

    def sessions(self):
        with self.connect() as c:
            return [dict(r) for r in c.execute('SELECT * FROM sessions')]

    def unbind(self, identity):
        with self.connect() as c:
            s = self._session(c, identity)
            c.execute('UPDATE sessions SET enabled=0 WHERE id=?', (s['id'],))
            c.execute('DELETE FROM clients WHERE session_id=?', (s['id'],))
            return {'unbound': s['id']}

    def refresh_endpoint(self, identity, expected_generation, metadata):
        """Administrator-verified same-session endpoint renewal, not a replacement binding.

        Claude: a new process/pipe for the same conversation UUID. Codex: a new CLI path for
        the same thread and CODEX_HOME (the app deletes versioned CLI folders on update).
        The generation is kept, so clients and in-flight messages stay valid.
        """
        with self.connect() as c:
            s = self._session(c, identity)
            if s['generation'] != expected_generation:
                raise BridgeError('endpoint refresh requires the unchanged active binding')
            old = json.loads(s['metadata'])
            if s['tool'] == 'codex':
                if set(metadata) != {'cli', 'codex_home'} or metadata['codex_home'] != old.get('codex_home'):
                    raise BridgeError('Codex refresh may change only the CLI path')
            elif s['tool'] != 'claude':
                raise BridgeError('unsupported endpoint refresh')
            c.execute('UPDATE sessions SET metadata=? WHERE id=?', (json.dumps(metadata, sort_keys=True), s['id']))
            self.event(c, None, 'endpoint_refreshed', {'session': s['id'], 'generation': s['generation'],
                       'old_pid': old.get('pid'), 'new_pid': metadata.get('pid')})
            return self._session(c, s['id'])

    def add_client(self, identity, directory, token, publish=None):
        with self.connect() as c:
            s = self._session(c, identity)
            c.execute('DELETE FROM clients WHERE session_id=?', (s['id'],))
            c.execute('INSERT INTO clients VALUES(?,?,?,?)',
                      (hashlib.sha256(token.encode()).hexdigest(), s['id'], s['generation'], directory))
            if publish:
                publish()
            return s['id']

    def authenticate_client(self, token):
        with self.connect() as c:
            row = c.execute('''SELECT s.* FROM clients c JOIN sessions s ON s.id=c.session_id
                WHERE c.token_hash=? AND c.generation=s.generation AND s.enabled=1''',
                (hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
            return dict(row) if row else None

    def clients(self):
        with self.connect() as c:
            return [dict(r) for r in c.execute('''SELECT c.directory,c.session_id FROM clients c
                JOIN sessions s ON s.id=c.session_id WHERE c.generation=s.generation AND s.enabled=1''')]

    def client_directory(self, identity, fallback):
        return next((r['directory'] for r in self.clients() if r['session_id'] == identity), str(fallback))

    def _valid(self, c, msg):
        for column in ('sender', 'receiver'):
            try:
                s = self._session(c, msg[column])
            except BridgeError:
                return False
            if s['generation'] != msg[column + '_gen'] or s['project'] != msg['project']:
                return False
        return True

    def _insert(self, c, sender, receiver, body, kind, expect_reply, ttl, mid,
                parent=None, final=False):
        if not isinstance(mid, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', mid):
            raise BridgeError('message ID must be 1-128 ASCII letters, digits, underscore or hyphen')
        if not isinstance(body, str) or len(body.encode('utf8')) > 32768:
            raise BridgeError('body exceeds 32 KiB UTF-8')
        if not isinstance(ttl, (float, int)) or not math.isfinite(ttl) or ttl <= 0 or ttl > 86400:
            raise BridgeError('TTL must be in (0, 86400] seconds')
        if sender['project'] != receiver['project']:
            raise BridgeError('cross-project delivery denied')
        hop = parent['hop'] + 1 if parent else 0
        if hop > 4:
            raise BridgeError('hop limit exceeded')
        fingerprint = hashlib.sha256(json.dumps([
            sender['id'], sender['generation'], receiver['id'], receiver['generation'],
            body, kind, bool(expect_reply), ttl, parent['id'] if parent else None, final
        ], ensure_ascii=False).encode()).hexdigest()
        old = c.execute('SELECT * FROM messages WHERE id=?', (mid,)).fetchone()
        if old:
            if old['fingerprint'] != fingerprint:
                raise BridgeError('idempotency ID reused with different payload')
            return dict(old)
        pending = c.execute("SELECT count(*) FROM messages WHERE receiver=? AND delivery IN ('accepted','dispatched','submitting','submitted','delivery_uncertain')", (receiver['id'],)).fetchone()[0]
        if pending >= 50:
            raise BridgeError('receiver has 50 unconfirmed messages')
        now = time.time()
        c.execute('''INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,NULL,?,NULL)''',
                  (mid, parent['conversation_id'] if parent else uid('c'), parent['id'] if parent else None,
                   kind, sender['id'], sender['generation'], receiver['id'], receiver['generation'],
                   sender['project'], body, int(expect_reply), int(final), hop, now, now + ttl,
                   'accepted', 'pending' if kind == 'request' else 'not_applicable', fingerprint))
        self.event(c, mid, 'accepted', {'kind': kind, 'parent_id': parent['id'] if parent else None})
        return dict(c.execute('SELECT * FROM messages WHERE id=?', (mid,)).fetchone())

    def send(self, sender, receiver, body, mid=None, ttl=86400, expect_reply=True):
        with self.connect() as c:
            return self._insert(c, self._session(c, sender), self._session(c, receiver),
                                body, 'request', expect_reply, ttl, mid or uid())

    def ack(self, mid, actor, context=None):
        with self.connect() as c:
            r = c.execute('SELECT * FROM messages WHERE id=?', (mid,)).fetchone()
            if not r:
                raise BridgeError('unknown message')
            m = dict(r)
            s = self._session(c, actor)
            if m['receiver'] != s['id']:
                raise BridgeError('ack actor is not the bound receiver')
            if not self._valid(c, m):
                return {'should_execute': False, 'reason': 'binding_revoked'}
            if m['received_at'] is not None:
                return {'should_execute': False, 'reason': 'already_received', 'kind': m['kind']}
            if m['expires'] <= time.time():
                c.execute("UPDATE messages SET delivery='expired' WHERE id=?", (mid,))
                self.event(c, mid, 'expired_before_claim')
                return {'should_execute': False, 'reason': 'expired'}
            if m['delivery'] in ('blocked', 'expired', 'failed'):
                return {'should_execute': False, 'reason': m['delivery']}
            c.execute("UPDATE messages SET delivery='received',received_at=?,error=NULL WHERE id=?", (time.time(), mid))
            self.event(c, mid, 'received', {'actor': s['id'], 'context': context or {}})
            return {'should_execute': m['kind'] == 'request', 'kind': m['kind'], 'body': m['body'],
                    'expect_reply': bool(m['expect_reply']), 'parent_id': m['parent_id'], 'message_id': mid,
                    'sender': m['sender'], 'receiver': m['receiver'], 'project': m['project']}

    def reply(self, mid, actor, body, outcome='completed'):
        if outcome not in ('completed', 'blocked'):
            raise BridgeError('invalid outcome')
        with self.connect() as c:
            r = c.execute('SELECT * FROM messages WHERE id=?', (mid,)).fetchone()
            if not r:
                raise BridgeError('unknown request')
            m = dict(r)
            s = self._session(c, actor)
            if m['receiver'] != s['id'] or not self._valid(c, m):
                raise BridgeError('reply identity or binding invalid')
            if m['kind'] != 'request' or not m['expect_reply']:
                raise BridgeError('only reply-requesting requests accept final replies')
            if m['received_at'] is None:
                raise BridgeError('ack/claim required before reply')
            if m['final_reply_id']:
                old = dict(c.execute('SELECT * FROM messages WHERE id=?', (m['final_reply_id'],)).fetchone())
                if old['body'] != body or m['outcome'] != outcome:
                    raise BridgeError('conflicting final result')
                return old
            result = self._insert(c, s, self._session(c, m['sender']), body, 'reply', False,
                                  86400, uid(), parent=m, final=True)
            c.execute('UPDATE messages SET outcome=?,final_reply_id=? WHERE id=?', (outcome, result['id'], mid))
            self.event(c, mid, 'result_saved', {'reply_id': result['id'], 'outcome': outcome})
            return result

    def status(self, mid=None):
        with self.connect() as c:
            rows = c.execute('SELECT * FROM messages WHERE id=?', (mid,)) if mid else c.execute('SELECT * FROM messages ORDER BY created')
            result = []
            for row in rows:
                m = dict(row)
                m['reply_delivery'] = None
                if m['final_reply_id']:
                    m['reply_delivery'] = c.execute('SELECT delivery FROM messages WHERE id=?', (m['final_reply_id'],)).fetchone()[0]
                m['events'] = [dict(e) for e in c.execute('SELECT * FROM events WHERE message_id=? ORDER BY seq', (m['id'],))]
                result.append(m)
            return result

    def next_delivery(self):
        with self.connect() as c:
            c.execute("UPDATE messages SET delivery='expired' WHERE received_at IS NULL AND expires<=? AND delivery IN ('accepted','submitted','delivery_uncertain')", (time.time(),))
            for r in c.execute("SELECT * FROM messages WHERE delivery='accepted' ORDER BY created").fetchall():
                m = dict(r)
                if not self._valid(c, m):
                    c.execute("UPDATE messages SET delivery='blocked',error='binding revoked' WHERE id=?", (m['id'],))
                    self.event(c, m['id'], 'binding_revoked')
                    continue
                return m, self._session(c, m['receiver'])
            return None

    def begin_submission(self, mid):
        with self.connect() as c:
            m = dict(c.execute('SELECT * FROM messages WHERE id=?', (mid,)).fetchone())
            if m['delivery'] != 'accepted' or not self._valid(c, m) or m['expires'] <= time.time():
                return False
            c.execute("UPDATE messages SET delivery='submitting' WHERE id=?", (mid,))
            self.event(c, mid, 'submission_intent')
            return True

    def finish_submission(self, mid, state, detail=None):
        with self.connect() as c:
            error = json.dumps(detail) if detail and state != 'submitted' else None
            c.execute("UPDATE messages SET delivery=?,error=? WHERE id=? AND delivery IN ('accepted','submitting')", (state, error, mid))
            self.event(c, mid, 'native_' + state, detail)

    def recover(self):
        with self.connect() as c:
            ids = [r[0] for r in c.execute("SELECT id FROM messages WHERE delivery='submitting'")]
            for mid in ids:
                c.execute("UPDATE messages SET delivery='delivery_uncertain',error='coordinator restarted during native submission' WHERE id=?", (mid,))
                self.event(c, mid, 'recovery_uncertain')
            return ids
