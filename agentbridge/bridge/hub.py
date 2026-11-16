"""Authenticated global directory and transactional RPC over outbound TLS WebSockets."""
import contextlib
from datetime import datetime, timedelta, timezone
import hashlib
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import secrets
import sqlite3
import ssl
import threading
import time
import uuid

from .store import BridgeError, Store, uid


class HubStore(Store):
    def __init__(self, path):
        self._transaction = threading.local()
        # Unlike local bindings, identical native UUIDs on different machines are distinct.
        with contextlib.closing(sqlite3.connect(path)) as c:
            c.execute('''CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY, alias TEXT UNIQUE NOT NULL, tool TEXT NOT NULL,
                native_id TEXT NOT NULL, project TEXT NOT NULL, generation TEXT NOT NULL,
                enabled INTEGER NOT NULL, metadata TEXT NOT NULL)''')
            c.commit()
        super().__init__(path)
        with self.connect() as c:
            c.executescript('''
                CREATE TABLE IF NOT EXISTS nodes (
                    id TEXT PRIMARY KEY, label TEXT UNIQUE NOT NULL, token_hash TEXT UNIQUE NOT NULL,
                    projects TEXT NOT NULL, revoked INTEGER NOT NULL DEFAULT 0,
                    machine_id TEXT, os_user_id TEXT, last_seen REAL NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS rpc_results (
                    node_id TEXT NOT NULL, request_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
                    result TEXT NOT NULL, created REAL NOT NULL, PRIMARY KEY(node_id,request_id));
                CREATE TABLE IF NOT EXISTS report_versions (
                    message_id TEXT PRIMARY KEY, sequence INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS invitations (
                    code_hash TEXT PRIMARY KEY,node_id TEXT NOT NULL,expires REAL NOT NULL,
                    remote_ip TEXT NOT NULL,used INTEGER NOT NULL DEFAULT 0);
            ''')

    @contextlib.contextmanager
    def connect(self):
        existing = getattr(self._transaction, 'connection', None)
        if existing is not None:
            yield existing
        else:
            with super().connect() as c:
                yield c

    def add_node(self, label, projects):
        if not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,47}', label) or not projects:
            raise BridgeError('node label must be a short lowercase identifier; project grants required')
        token = secrets.token_urlsafe(32)
        node_id = str(uuid.uuid4())
        with self.connect() as c:
            try:
                c.execute('INSERT INTO nodes(id,label,token_hash,projects) VALUES(?,?,?,?)',
                          (node_id, label, hashlib.sha256(token.encode()).hexdigest(), json.dumps(sorted(set(projects)))))
            except sqlite3.IntegrityError as e:
                raise BridgeError('node label already exists; revoke explicitly before replacement') from e
        return {'node_id': node_id, 'token': token, 'label': label, 'projects': projects}

    def revoke(self, node_id):
        with self.connect() as c:
            count = c.execute('UPDATE nodes SET revoked=1 WHERE id=? OR label=?', (node_id,node_id)).rowcount
            if not count:
                raise BridgeError('unknown node')
        return {'revoked': node_id}

    def invite(self,node_id,remote_ip,lifetime=3600):
        ipaddress.ip_address(remote_ip)
        code=secrets.token_urlsafe(24)
        with self.connect() as c:
            node=self._node(c,node_id)
            if node['machine_id']:
                raise BridgeError('node already enrolled; invitation cannot replace it')
            c.execute('DELETE FROM invitations WHERE node_id=?',(node_id,))
            c.execute('INSERT INTO invitations VALUES(?,?,?,?,0)',
                      (hashlib.sha256(code.encode()).hexdigest(),node_id,time.time()+lifetime,remote_ip))
        return code

    def redeem(self,code,remote_ip,machine_id,os_user_id):
        uuid.UUID(machine_id)
        if not isinstance(os_user_id,str) or not 1<=len(os_user_id)<=256:
            raise BridgeError('invalid operating system identity')
        with self.connect() as c:
            invite=c.execute('SELECT * FROM invitations WHERE code_hash=?',(hashlib.sha256(code.encode()).hexdigest(),)).fetchone()
            if not invite or invite['used'] or invite['expires']<=time.time() or invite['remote_ip']!=remote_ip:
                raise BridgeError('invitation expired, used, invalid or wrong source IP')
            node=self._node(c,invite['node_id'])
            if node['machine_id']:
                raise BridgeError('node already enrolled')
            token=secrets.token_urlsafe(32)
            c.execute('UPDATE nodes SET token_hash=?,machine_id=?,os_user_id=?,last_seen=? WHERE id=?',
                      (hashlib.sha256(token.encode()).hexdigest(),machine_id,os_user_id,time.time(),node['id']))
            c.execute('UPDATE invitations SET used=1 WHERE code_hash=?',(invite['code_hash'],))
            return {'node_id':node['id'],'label':node['label'],'token':token,'projects':json.loads(node['projects'])}

    def authenticate(self, token, machine_id, os_user_id):
        uuid.UUID(machine_id)
        if not isinstance(os_user_id, str) or not 1 <= len(os_user_id) <= 256:
            raise BridgeError('invalid operating system user identity')
        with self.connect() as c:
            n = c.execute('SELECT * FROM nodes WHERE token_hash=? AND revoked=0',
                          (hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
            if not n:
                raise BridgeError('invalid or revoked node credential')
            if n['machine_id'] and (n['machine_id'] != machine_id or n['os_user_id'] != os_user_id):
                raise BridgeError('node credential belongs to a different machine/user')
            c.execute('UPDATE nodes SET machine_id=?,os_user_id=?,last_seen=? WHERE id=?',
                      (machine_id, os_user_id, time.time(), n['id']))
            return n['id']

    def _node(self, c, node_id):
        n = c.execute('SELECT * FROM nodes WHERE id=? AND revoked=0', (node_id,)).fetchone()
        if not n:
            raise BridgeError('node revoked or missing')
        return dict(n)

    def _session(self, c, identity):
        s = super()._session(c, identity)
        self._node(c, json.loads(s['metadata'])['node_id'])
        return s

    def _owned(self, c, node, actor):
        s = self._session(c, actor)
        if json.loads(s['metadata'])['node_id'] != node['id']:
            raise BridgeError('node cannot act as another node/session')
        if s['project'] not in json.loads(node['projects']):
            raise BridgeError('project grant missing')
        return s

    def register(self, c, node, entries):
        if not isinstance(entries, list) or len(entries) > 128:
            raise BridgeError('invalid session snapshot')
        seen = set()
        for entry in entries:
            native = str(uuid.UUID(entry['native_id']))
            tool = entry['tool']
            if tool not in ('claude','codex') or entry['project'] not in json.loads(node['projects']):
                raise BridgeError('ungranted project or unsupported tool')
            alias = entry['alias']
            if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_./-]{0,127}', alias):
                raise BridgeError('invalid local alias')
            generation = entry['generation']
            if not re.fullmatch(r'g_[a-f0-9]{32}', generation):
                raise BridgeError('invalid binding generation')
            sid = node['id'] + ':' + tool + ':' + native
            if sid in seen:
                raise BridgeError('duplicate session in snapshot')
            seen.add(sid)
            c.execute('''INSERT INTO sessions VALUES(?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET alias=excluded.alias,project=excluded.project,
                generation=excluded.generation,enabled=excluded.enabled''',
                (sid,node['label']+'/'+alias,tool,native,entry['project'],generation,
                 int(bool(entry['enabled'])),json.dumps({'node_id':node['id']})))
        for s in c.execute('SELECT id,metadata FROM sessions').fetchall():
            if json.loads(s['metadata'])['node_id']==node['id'] and s['id'] not in seen:
                c.execute('UPDATE sessions SET enabled=0 WHERE id=?', (s['id'],))
        return {'registered':len(seen)}

    def _message(self, c, mid):
        m = c.execute('SELECT * FROM messages WHERE id=?', (mid,)).fetchone()
        if not m:
            raise BridgeError('unknown message')
        return dict(m)

    def _dispatch(self, c, node):
        c.execute("UPDATE messages SET delivery='expired' WHERE received_at IS NULL AND expires<=? AND delivery IN ('accepted','dispatched','submitted','delivery_uncertain')", (time.time(),))
        messages=[]
        for row in c.execute("SELECT * FROM messages WHERE delivery IN ('accepted','dispatched') AND receiver LIKE ? ORDER BY created LIMIT 4", (node['id']+':%',)).fetchall():
            m=dict(row)
            if not self._valid(c,m):
                c.execute("UPDATE messages SET delivery='blocked',error='binding revoked' WHERE id=?",(m['id'],))
                continue
            c.execute("UPDATE messages SET delivery='dispatched' WHERE id=?", (m['id'],))
            m['network']=True
            messages.append(m)
        return messages

    def _report(self, c, node, args):
        m=self._message(c,args['message_id'])
        self._owned(c,node,m['receiver'])
        version=c.execute('SELECT sequence FROM report_versions WHERE message_id=?',(m['id'],)).fetchone()
        sequence=args['sequence']
        if not isinstance(sequence,int) or sequence<1:
            raise BridgeError('invalid delivery sequence')
        if version and sequence<=version[0]:
            return {'recorded':False}
        phase=args['phase']
        if phase not in ('submitting','submitted','delivery_uncertain','blocked'):
            raise BridgeError('invalid delivery phase')
        c.execute('INSERT OR REPLACE INTO report_versions VALUES(?,?)',(m['id'],sequence))
        if m['delivery'] not in ('received','expired','failed','blocked'):
            c.execute('UPDATE messages SET delivery=?,error=? WHERE id=?',
                      (phase,args.get('error') if phase!='submitted' else None,m['id']))
        self.event(c,m['id'],'agent_'+phase,{'sequence':sequence,'node':node['id'],'detail':args.get('detail')})
        return {'recorded':True}

    def _retry_preflight(self,c,node,args):
        m=self._message(c,args['message_id'])
        target=self._owned(c,node,m['receiver'])
        version=c.execute('SELECT sequence FROM report_versions WHERE message_id=?',(m['id'],)).fetchone()
        submitted=c.execute("SELECT 1 FROM events WHERE message_id=? AND event IN ('agent_submitting','agent_submitted','agent_delivery_uncertain') LIMIT 1",(m['id'],)).fetchone()
        if (m['delivery']!='blocked' or m['received_at'] is not None or m['expires']<=time.time()
            or not self._valid(c,m) or target['generation']!=args['generation'] or submitted
            or not version or version[0]!=args['sequence']):
            raise BridgeError('only a current, never-submitted preflight block can be retried')
        c.execute("UPDATE messages SET delivery='accepted',error=NULL WHERE id=?",(m['id'],))
        self.event(c,m['id'],'preflight_retry_authorized',{'node':node['id'],'sequence':args['sequence']})
        return {'message_id':m['id'],'retry':True}

    RPC_RETENTION = 7 * 86400

    def prune(self, now=None):
        """Drop replay results well past every message TTL (max 24 h).

        A resent RPC older than this re-executes, which is safe: send is keyed by message ID,
        reply by final_reply_id, report by sequence, and an expired message cannot be claimed.
        """
        now = now or time.time()
        with self.connect() as c:
            removed = c.execute('DELETE FROM rpc_results WHERE created<?', (now - self.RPC_RETENTION,)).rowcount
        self._pruned_at = now
        return removed

    def rpc(self, node_id, request_id, op, args):
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', request_id):
            raise BridgeError('invalid RPC ID')
        if time.time() - getattr(self, '_pruned_at', 0) > 3600:
            self.prune()
        fingerprint=hashlib.sha256(json.dumps([op,args],sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        with super().connect() as c:
            self._transaction.connection=c
            try:
                node=self._node(c,node_id)
                c.execute('UPDATE nodes SET last_seen=? WHERE id=?',(time.time(),node_id))
                actor=None
                if op in ('send','ack','reply','status'):
                    actor=self._owned(c,node,args['actor'])
                    if args.get('generation')!=actor['generation']:
                        raise BridgeError('stale source binding generation')
                cached=c.execute('SELECT * FROM rpc_results WHERE node_id=? AND request_id=?',(node_id,request_id)).fetchone()
                if cached:
                    if cached['fingerprint']!=fingerprint:
                        raise BridgeError('RPC ID reused with different payload')
                    if op=='ack' and not self._valid(c,self._message(c,args['message_id'])):
                        return {'should_execute':False,'reason':'binding_revoked'}
                    return json.loads(cached['result'])
                if op=='register':
                    result=self.register(c,node,args['sessions'])
                elif op=='pull':
                    result=self._dispatch(c,node)
                elif op=='report':
                    result=self._report(c,node,args)
                elif op=='retry_preflight':
                    result=self._retry_preflight(c,node,args)
                elif op=='directory':
                    result=[]
                    for r in c.execute('SELECT * FROM sessions WHERE enabled=1').fetchall():
                        s=dict(r)
                        owner=c.execute('SELECT label,revoked,last_seen FROM nodes WHERE id=?', (json.loads(s['metadata'])['node_id'],)).fetchone()
                        if s['project'] in json.loads(node['projects']) and owner and not owner['revoked']:
                            s.pop('metadata')
                            s['status']='online' if time.time()-owner['last_seen']<60 else 'offline'
                            result.append(s)
                elif op in ('send','ack','reply','status'):
                    if op=='send':
                        target=self._session(c,args['to'])
                        is_new=c.execute('SELECT 1 FROM messages WHERE id=?',(args['message_id'],)).fetchone() is None
                        ttl=args.get('ttl',86400)
                        if not isinstance(ttl,(float,int)) or not math.isfinite(ttl) or not 0<ttl<=86400:
                            raise BridgeError('TTL must be in (0, 86400] seconds')
                        expires=args.get('expires_at',time.time()+ttl)
                        if not isinstance(expires,(float,int)) or not math.isfinite(expires):
                            raise BridgeError('invalid absolute expiration')
                        deadline=min(expires,time.time()+ttl)
                        if is_new and deadline<=time.time():
                            raise BridgeError('request expired before Hub acceptance')
                        result=self.send(actor['id'],target['id'],args['body'],args['message_id'],args.get('ttl',86400))
                        if is_new:
                            c.execute('UPDATE messages SET expires=? WHERE id=?',(deadline,result['id']))
                            result['expires']=deadline
                    else:
                        m=self._message(c,args['message_id'])
                        if op=='status':
                            if actor['id'] not in (m['sender'],m['receiver']):
                                raise BridgeError('message not visible to this session')
                            result=self.status(m['id'])
                        elif op=='ack':
                            result=self.ack(m['id'],actor['id'],args.get('context'))
                        else:
                            result=self.reply(m['id'],actor['id'],args['body'],args.get('outcome','completed'))
                else:
                    raise BridgeError('unsupported RPC operation')
                # Mutations and their replay result commit together, including claim/should_execute.
                if op not in ('pull','directory','register'):
                    c.execute('INSERT INTO rpc_results VALUES(?,?,?,?,?)',
                              (node_id,request_id,fingerprint,json.dumps(result,ensure_ascii=False),time.time()))
                return result
            finally:
                self._transaction.connection=None

    def overview(self):
        with self.connect() as c:
            nodes=[dict(n) for n in c.execute('SELECT id,label,projects,revoked,machine_id,os_user_id,last_seen FROM nodes')]
        return {'nodes':nodes,'sessions':self.sessions(),'messages':self.status()}


def create_tls(state, hosts):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes,serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID,ExtendedKeyUsageOID
    state=Path(state)
    if (state/'hub-key.pem').exists() or (state/'hub-cert.pem').exists():
        raise BridgeError('TLS material already exists; refusing to replace trust identity')
    key=rsa.generate_private_key(public_exponent=65537,key_size=3072)
    name=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'AgentBridge private Hub')])
    san=[]
    for host in hosts:
        try:
            san.append(x509.IPAddress(ipaddress.ip_address(host)))
        except ValueError:
            san.append(x509.DNSName(host))
    now=datetime.now(timezone.utc)
    cert=(x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
          .serial_number(x509.random_serial_number()).not_valid_before(now-timedelta(minutes=5))
          .not_valid_after(now+timedelta(days=90)).add_extension(x509.SubjectAlternativeName(san),False)
          .add_extension(x509.BasicConstraints(ca=False,path_length=None),True)
          .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),False).sign(key,hashes.SHA256()))
    (state/'hub-key.pem').write_bytes(key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()))
    (state/'hub-cert.pem').write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    if os.name!='nt':
        (state/'hub-key.pem').chmod(0o600)
    return cert.fingerprint(hashes.SHA256()).hex()


def serve_hub(store, host, port, cert, key, ready=None):
    from websockets.sync.server import serve
    context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version=ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(cert,key)

    def handler(ws):
        try:
            hello=json.loads(ws.recv(timeout=5))
            if hello.get('op')=='enroll':
                result=store.redeem(hello['code'],ws.remote_address[0],hello['machine_id'],hello['os_user_id'])
                ws.send(json.dumps({'ok':True,'enrollment':result}))
                return
            node_id=store.authenticate(hello['token'],hello['machine_id'],hello['os_user_id'])
            ws.send(json.dumps({'ok':True,'node_id':node_id,'protocol':'1.3'}))
            for frame in ws:
                request=json.loads(frame)
                rid=request.get('id','')
                try:
                    result=store.rpc(node_id,rid,request['op'],request.get('args',{}))
                    response={'id':rid,'ok':True,'result':result}
                except (BridgeError,ValueError,KeyError,TypeError,sqlite3.IntegrityError) as e:
                    response={'id':rid,'ok':False,'error':str(e)}
                ws.send(json.dumps(response,ensure_ascii=False))
        except Exception:
            # No credentials or message bodies in server logs.
            ws.close(code=1008,reason='authentication, protocol, or connection ended')

    with serve(handler,host,port,ssl=context,origins=[None],compression=None,max_size=1048576,
               ping_interval=15,ping_timeout=15,close_timeout=2,server_header=None) as server:
        if ready:
            ready(server)
        print(json.dumps({'ready':True,'mode':'m1-hub','host':host,'port':server.socket.getsockname()[1]}),flush=True)
        server.serve_forever()
