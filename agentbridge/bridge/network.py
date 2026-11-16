"""One outbound TLS connection, durable command outbox, and local native-delivery ledger."""
import json
import math
import os
from pathlib import Path
import random
import ssl
import subprocess
import threading
import time
import uuid

from . import adapters
from .store import BridgeError, uid


def local_identity(state):
    path=Path(state)/'node-identity.json'
    if path.exists():
        return json.loads(path.read_text(encoding='utf8'))
    if os.name=='nt':
        import csv
        rows=list(csv.reader(subprocess.check_output(['whoami','/user','/fo','csv'],text=True).splitlines()))
        user=rows[-1][1]
    else:
        user=str(os.getuid())
    identity={'machine_id':str(uuid.uuid4()),'os_user_id':user}
    path.write_text(json.dumps(identity),encoding='utf8')
    return identity


class RemoteAgent:
    def __init__(self, store, state, config_path, stop, identity=None):
        self.store=store
        self.state=Path(state)
        self.config=json.loads(Path(config_path).read_text(encoding='utf8'))
        if not self.config['hub_url'].startswith('wss://'):
            raise BridgeError('network Agent requires wss:// with certificate verification')
        self.identity=identity or local_identity(state)
        self.stop=stop
        self.wake=threading.Event()
        self.connected=False
        self.last_error=None
        self.last_seen=None
        self.directory=[]
        self.endpoint_health={}
        self.recovery_interval=5.0
        self._recovery_at=0.0
        self.node_id=self.config['node_id']
        self.label=self.config['label']
        with store.connect() as c:
            c.executescript('''
                CREATE TABLE IF NOT EXISTS net_ops (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,id TEXT UNIQUE NOT NULL,op TEXT NOT NULL,
                    args TEXT NOT NULL,state TEXT NOT NULL,result TEXT,error TEXT,created REAL NOT NULL,deadline REAL);
                CREATE TABLE IF NOT EXISTS net_jobs (
                    id TEXT PRIMARY KEY,envelope TEXT NOT NULL,state TEXT NOT NULL,
                    sequence INTEGER NOT NULL DEFAULT 0,error TEXT);
            ''')
            # handed=1 once an operation result has been returned to its caller. An ack
            # whose caller gave up before the Hub answered keeps handed=0, so the next
            # ack for the same message and actor receives that stored claim exactly once.
            if 'handed' not in {r['name'] for r in c.execute('PRAGMA table_info(net_ops)')}:
                c.execute('ALTER TABLE net_ops ADD COLUMN handed INTEGER NOT NULL DEFAULT 0')
            for r in c.execute("SELECT * FROM net_jobs WHERE state='submitting'").fetchall():
                self._phase(c,r['id'],'delivery_uncertain',{'reason':'Agent restarted during native submission'})
        self._pruned_at=0.0

    def status(self):
        with self.store.connect() as c:
            pending=c.execute("SELECT count(*) FROM net_ops WHERE state='pending'").fetchone()[0]
            jobs=[dict(r) for r in c.execute('SELECT id,state,sequence,error FROM net_jobs')]
        return {'node_id':self.node_id,'label':self.label,'connected':self.connected,'last_seen':self.last_seen,
                'last_error':self.last_error,'pending_operations':pending,'jobs':jobs,
                'endpoint_health':dict(self.endpoint_health)}

    def recover_endpoints(self):
        """Renew exact-UUID endpoints; retry only proven pre-submission failures.

        Runs in the delivery worker, including after an Agent restart. A successful
        native submission is never repeated merely because its ack is delayed.
        """
        aliases=self.config.get('local_aliases')
        health={}
        for session in self.store.sessions():
            if (not session['enabled'] or session['project'] not in self.config['projects']
                    or (aliases and session['alias'] not in aliases)):
                continue
            try:
                if session['tool']=='claude':
                    metadata=adapters.claude_metadata(session['native_id'])
                    candidate=dict(session,metadata=json.dumps(metadata))
                    adapters.claude_preflight(candidate)
                    if metadata!=json.loads(session['metadata']):
                        session=self.store.refresh_endpoint(session['id'],session['generation'],metadata)
                else:
                    adapters.codex_args(session,'')
                health[session['id']]={'state':'ready','checked_at':time.time()}
                # A full dry preflight also checks the actual message size and
                # current endpoint. No pipe write or queue command occurs here.
                self.retry_preflight(session['id'],probe=True)
            except Exception as e:
                # No exception text: registry/key parse failures can contain data. Known
                # endpoint conditions carry a stable code and a safe hint instead.
                health[session['id']]={'state':'unavailable','reason':getattr(e,'code',type(e).__name__),
                                       'checked_at':time.time()}
                if getattr(e,'hint',None):
                    health[session['id']]['hint']=e.hint
        self.endpoint_health=health

    OUTBOX_RETENTION=7*86400

    def prune(self,now=None):
        """Drop finished outbox rows well past every message TTL (max 24 h).

        Unreturned ack results (handed=0) are kept so a late caller can still get them.
        """
        cutoff=(now or time.time())-self.OUTBOX_RETENTION
        with self.store.connect() as c:
            removed=c.execute("""DELETE FROM net_ops WHERE state IN ('done','failed') AND created<?
                AND (op!='ack' OR handed=1)""",(cutoff,)).rowcount
        return removed

    def _enqueue(self,c,op,args,deadline=None):
        rid=uid('rpc')
        c.execute("INSERT INTO net_ops(id,op,args,state,created,deadline) VALUES(?,?,?,'pending',?,?)",
                  (rid,op,json.dumps(args,ensure_ascii=False),time.time(),deadline))
        return rid

    def retry_preflight(self,local_id,probe=False):
        queued=[]
        client_state=self.store.client_directory(local_id,self.state) if probe else self.state
        with self.store.connect() as c:
            session=self.store._session(c,local_id)
            history={}
            for op in c.execute("SELECT args FROM net_ops WHERE op='report'"):
                report=json.loads(op['args'])
                history.setdefault(report['message_id'],[]).append(report)
            for row in c.execute("SELECT * FROM net_jobs WHERE state='blocked'").fetchall():
                m=json.loads(row['envelope'])
                reports=history.get(m['id'],[])
                if (m['receiver']!=self.node_id+':'+local_id or m['receiver_gen']!=session['generation']
                    or m['project']!=session['project'] or m['expires']<=time.time()
                    or sorted(r['sequence'] for r in reports)!=list(range(1,row['sequence']+1))
                    or not reports or any(r['phase']!='blocked' for r in reports)):
                    continue
                if probe:
                    try:
                        adapters.prepare(session,adapters.envelope(m,session,client_state))
                    except Exception:
                        continue
                self._enqueue(c,'retry_preflight',{'message_id':m['id'],'generation':session['generation'],
                                                  'sequence':row['sequence']})
                c.execute("UPDATE net_jobs SET state='retry_pending' WHERE id=?",(m['id'],))
                queued.append(m['id'])
        self.wake.set()
        return queued

    def forward(self,path,b,principal=None):
        if path=='/sessions':
            return [s for s in self.directory if not principal or s['project']==principal['project']]
        if path=='/status' and not b.get('message_id'):
            return self.status()
        op=path.removeprefix('/')
        if op not in ('send','ack','reply','status'):
            raise BridgeError('unsupported network operation')
        if op=='ack' and not self.connected:
            return {'should_execute':False,'reason':'hub_unavailable_no_claim'}
        with self.store.connect() as c:
            actor=self.store._session(c,b['actor'])
            if self.config.get('local_aliases') and actor['alias'] not in self.config['local_aliases']:
                raise BridgeError('binding not enrolled for network use')
            args={k:v for k,v in b.items() if k in ('body','message_id','ttl','outcome','context','to')}
            args['actor']=self.node_id+':'+actor['id']
            args['generation']=actor['generation']
            if op=='send':
                ttl=args.get('ttl',86400)
                if not isinstance(ttl,(float,int)) or not math.isfinite(ttl) or not 0<ttl<=86400:
                    raise BridgeError('TTL must be in (0, 86400] seconds')
                if not isinstance(args.get('body'),str) or len(args['body'].encode('utf8'))>32768:
                    raise BridgeError('body exceeds 32 KiB UTF-8')
                args['message_id']=args.get('message_id') or uid()
                args['expires_at']=time.time()+ttl
                # Existing local aliases remain usable; remote aliases explicitly include a node label.
                local=c.execute('SELECT alias FROM sessions WHERE alias=? AND enabled=1',(args['to'],)).fetchone()
                if local:
                    args['to']=self.label+'/'+args['to']
            rid=None
            if op=='ack':
                # Reuse an unfinished or unreturned claim instead of asking the Hub again:
                # a second Hub ack would answer already_received and the request would
                # never run (claimed at the Hub but abandoned by the timed-out caller).
                previous=c.execute("""SELECT id,state FROM net_ops WHERE op='ack' AND handed=0
                    AND json_extract(args,'$.message_id')=? AND json_extract(args,'$.actor')=?
                    ORDER BY seq DESC LIMIT 1""",(args['message_id'],args['actor'])).fetchone()
                if previous and previous['state']=='failed':
                    c.execute('UPDATE net_ops SET handed=1 WHERE id=?',(previous['id'],))
                elif previous:
                    rid=previous['id']
            if rid is None:
                rid=self._enqueue(c,op,args,time.time()+10 if op in ('ack','status') else None)
        self.wake.set()
        deadline=time.monotonic()+(12 if op in ('ack','status') else 2)
        # Once transmitted ('sending'), the Hub may already have committed the claim;
        # keep waiting for its answer rather than reporting failure early.
        hard_deadline=deadline+12
        while time.monotonic()<hard_deadline:
            with self.store.connect() as c:
                row=dict(c.execute('SELECT * FROM net_ops WHERE id=?',(rid,)).fetchone())
                if row['state']=='done':
                    result=json.loads(row['result'])
                    c.execute('UPDATE net_ops SET handed=1 WHERE id=?',(rid,))
                    if op=='ack' and (result.get('should_execute') or result.get('kind')=='reply'):
                        c.execute("UPDATE net_jobs SET state='received' WHERE id=?",(args['message_id'],))
                    return result
                if row['state']=='failed':
                    c.execute('UPDATE net_ops SET handed=1 WHERE id=?',(rid,))
                    raise BridgeError(row['error'])
            if time.monotonic()>=deadline and row['state']!='sending':
                break
            if self.stop.wait(0.05):
                break
        if op=='ack':
            return {'should_execute':False,'reason':'claim_unconfirmed_do_not_execute','operation_id':rid,
                    'retry':'run the same ack again later; a claim that reaches the Hub is returned once'}
        return {'id':args.get('message_id'),'operation_id':rid,'delivery':'local_pending',
                'note':'durable on this Agent; not yet confirmed by Hub'}

    def _snapshot(self):
        aliases=self.config.get('local_aliases')
        projects=set(self.config['projects'])
        return [{k:s[k] for k in ('tool','native_id','alias','project','generation','enabled')}
                for s in self.store.sessions() if s['project'] in projects and (not aliases or s['alias'] in aliases)]

    def _exchange(self,ws,op,args,rid=None):
        rid=rid or uid('rpc')
        ws.send(json.dumps({'id':rid,'op':op,'args':args},ensure_ascii=False))
        response=json.loads(ws.recv(timeout=8))
        if response.get('id')!=rid:
            raise BridgeError('Hub response correlation mismatch')
        return response

    def _success(self,ws,op,args):
        response=self._exchange(ws,op,args)
        if not response['ok']:
            raise BridgeError(response['error'])
        return response['result']

    def connection_loop(self):
        from websockets.sync.client import connect
        context=ssl.create_default_context(cadata=self.config['hub_certificate'])
        context.minimum_version=ssl.TLSVersion.TLSv1_2
        backoff=1
        while not self.stop.is_set():
            try:
                with connect(self.config['hub_url'],ssl=context,proxy=None,open_timeout=5,close_timeout=2,
                             max_size=1048576,compression=None,ping_interval=15,ping_timeout=15) as ws:
                    ws.send(json.dumps({'token':self.config['token'],**self.identity}))
                    hello=json.loads(ws.recv(timeout=5))
                    if not hello.get('ok') or hello.get('node_id')!=self.node_id:
                        raise BridgeError('Hub/node identity mismatch')
                    self.connected=True
                    self.last_error=None
                    backoff=1
                    previous=None
                    directory_at=0
                    while not self.stop.is_set():
                        snapshot=self._snapshot()
                        encoded=json.dumps(snapshot,sort_keys=True)
                        if encoded!=previous:
                            self._success(ws,'register',{'sessions':snapshot})
                            previous=encoded
                        with self.store.connect() as c:
                            operations=[dict(r) for r in c.execute("SELECT * FROM net_ops WHERE state IN ('pending','sending') ORDER BY seq LIMIT 32")]
                        for operation in operations:
                            if operation['state']=='pending' and operation['deadline'] and operation['deadline']<time.time():
                                with self.store.connect() as c:
                                    c.execute("UPDATE net_ops SET state='failed',error='operation expired before confirmed transmission; do not execute' WHERE id=? AND state='pending'",(operation['id'],))
                                continue
                            # Mark before transmitting: a 'sending' operation may have been
                            # applied by the Hub, so it is never expired, only resent with
                            # the same RPC ID (the Hub replays its cached result).
                            with self.store.connect() as c:
                                c.execute("UPDATE net_ops SET state='sending' WHERE id=? AND state IN ('pending','sending')",(operation['id'],))
                            response=self._exchange(ws,operation['op'],json.loads(operation['args']),operation['id'])
                            with self.store.connect() as c:
                                c.execute('UPDATE net_ops SET state=?,result=?,error=? WHERE id=?',
                                    ('done' if response['ok'] else 'failed',json.dumps(response.get('result'),ensure_ascii=False),response.get('error'),operation['id']))
                                if operation['op']=='retry_preflight':
                                    mid=json.loads(operation['args'])['message_id']
                                    c.execute("UPDATE net_jobs SET state=?,error=? WHERE id=? AND state='retry_pending'",
                                              ('accepted' if response['ok'] else 'blocked',response.get('error'),mid))
                        messages=self._success(ws,'pull',{})
                        with self.store.connect() as c:
                            for message in messages:
                                c.execute("INSERT OR IGNORE INTO net_jobs(id,envelope,state) VALUES(?,?,'accepted')",
                                          (message['id'],json.dumps(message,ensure_ascii=False)))
                        if time.monotonic()-directory_at>5:
                            self.directory=self._success(ws,'directory',{})
                            directory_at=time.monotonic()
                        self.last_seen=time.time()
                        self.wake.wait(0.25)
                        self.wake.clear()
            except Exception as e:
                self.last_error=type(e).__name__
            finally:
                self.connected=False
            self.stop.wait(backoff+random.random()*0.25)
            backoff=min(backoff*2,60)

    def _phase(self,c,mid,phase,detail=None):
        row=c.execute('SELECT state,sequence FROM net_jobs WHERE id=?',(mid,)).fetchone()
        sequence=row['sequence']+1
        c.execute("UPDATE net_jobs SET state=CASE WHEN state='received' THEN state ELSE ? END,sequence=?,error=? WHERE id=?",
                  (phase,sequence,json.dumps(detail) if detail and phase!='submitted' else None,mid))
        self._enqueue(c,'report',{'message_id':mid,'sequence':sequence,'phase':phase,
                                 'error':str(detail)[:1000] if phase!='submitted' and detail else None,'detail':detail})

    def delivery_loop(self):
        while not self.stop.is_set():
            if time.monotonic()>=self._recovery_at:
                try:
                    self.recover_endpoints()
                except Exception:
                    # Temporary database contention must not kill delivery.
                    pass
                self._recovery_at=time.monotonic()+self.recovery_interval
                if time.monotonic()>=self._pruned_at:
                    try:
                        self.prune()
                    except Exception:
                        pass
                    self._pruned_at=time.monotonic()+3600
            with self.store.connect() as c:
                row=c.execute("SELECT * FROM net_jobs WHERE state='accepted' ORDER BY rowid LIMIT 1").fetchone()
            if not row:
                self.stop.wait(0.1)
                continue
            m=json.loads(row['envelope'])
            local_id=m['receiver'].removeprefix(self.node_id+':')
            client_state=self.store.client_directory(local_id,self.state)
            with self.store.connect() as c:
                current=c.execute('SELECT state FROM net_jobs WHERE id=?',(m['id'],)).fetchone()[0]
                if current!='accepted':
                    continue
                current_session=c.execute('SELECT * FROM sessions WHERE id=?',(local_id,)).fetchone()
                if (not current_session or not current_session['enabled'] or
                    current_session['generation']!=m['receiver_gen'] or current_session['project']!=m['project']
                    or m['expires']<=time.time()):
                    self._phase(c,m['id'],'blocked',{'reason':'binding changed or expired before submission'})
                    self.wake.set()
                    continue
                # Preflight against the endpoint as stored in this same transaction, so a
                # concurrent endpoint refresh cannot leave a stale pipe/CLI in 'prepared'.
                try:
                    session=dict(current_session)
                    prepared=adapters.prepare(session,adapters.envelope(m,session,client_state))
                except Exception as e:
                    self._phase(c,m['id'],'blocked',{'reason':str(e)[:1000]})
                    self.wake.set()
                    continue
                self._phase(c,m['id'],'submitting')
            try:
                detail=adapters.deliver(prepared)
                phase='submitted'
            except Exception as e:
                detail={'reason':str(e)[:1000]}
                phase='delivery_uncertain'
            with self.store.connect() as c:
                self._phase(c,m['id'],phase,detail)
            self.wake.set()
