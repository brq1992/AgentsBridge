"""One outbound TLS connection, durable command outbox, and local native-delivery ledger."""
import json
import math
import os
from pathlib import Path
import random
import ssl
import subprocess
import sys
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
            columns={r['name'] for r in c.execute('PRAGMA table_info(net_ops)')}
            if 'handed' not in columns:
                c.execute('ALTER TABLE net_ops ADD COLUMN handed INTEGER NOT NULL DEFAULT 0')
            if 'claim_nonce' not in columns:
                c.execute('ALTER TABLE net_ops ADD COLUMN claim_nonce TEXT')
            if 'consumed_by' not in columns:
                # who received execution permission: 'legacy', a claim nonce, or 'closed'
                c.execute('ALTER TABLE net_ops ADD COLUMN consumed_by TEXT')
            job_columns={r['name'] for r in c.execute('PRAGMA table_info(net_jobs)')}
            for name,ddl in (('notified_at','REAL'),('notify_count','INTEGER NOT NULL DEFAULT 0'),('last_receipt','TEXT'),('renotify_state','TEXT'),('auto_ack','TEXT')):
                if name not in job_columns:
                    c.execute(f'ALTER TABLE net_jobs ADD COLUMN {name} {ddl}')
            # A re-notification interrupted by a crash may or may not have been written; it was
            # already counted before the write, so it only needs its outcome marked uncertain.
            c.execute("UPDATE net_jobs SET renotify_state='uncertain' WHERE renotify_state='attempting'")
            for r in c.execute("SELECT * FROM net_jobs WHERE state='submitting'").fetchall():
                self._phase(c,r['id'],'delivery_uncertain',{'reason':'Agent restarted during native submission'})
        self._pruned_at=0.0

    def status(self):
        with self.store.connect() as c:
            pending=c.execute("SELECT count(*) FROM net_ops WHERE state='pending'").fetchone()[0]
            jobs=[dict(r) for r in c.execute('SELECT id,state,sequence,error,auto_ack FROM net_jobs')]
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
                    metadata=adapters.claude_metadata(session['native_id'],json.loads(session['metadata']))
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
                try:
                    if getattr(e,'hint',None):
                        health[session['id']]['hint']=adapters.fill_hint(e.hint,session['alias'],session['project'])
                    if health[session['id']]['reason']!=self.endpoint_health.get(session['id'],{}).get('reason'):
                        # One stderr line per change, so an operator tailing the log sees it at once.
                        print(json.dumps({'event':'endpoint_unavailable','alias':session['alias'],
                                          'reason':health[session['id']]['reason'],'hint':health[session['id']].get('hint')},
                                         ensure_ascii=False),file=sys.stderr,flush=True)
                except Exception:
                    pass  # reporting must never abort the pass for the remaining sessions
        self.endpoint_health=health

    # Longest a local ack request waits; must stay well below cli.ACK_TIMEOUT.
    ACK_WAIT=20

    def _claim_still_valid(self,c,args):
        """Execution permission is only handed out under the generation that claimed it,
        while the message has not expired."""
        local_id=args['actor'].removeprefix(self.node_id+':')
        current=c.execute('SELECT generation,enabled FROM sessions WHERE id=?',(local_id,)).fetchone()
        if not current or not current['enabled'] or current['generation']!=args.get('generation'):
            return 'binding_changed'
        job=c.execute('SELECT envelope FROM net_jobs WHERE id=?',(args['message_id'],)).fetchone()
        if job:
            expires=json.loads(job['envelope']).get('expires')
            if expires is not None and expires<=time.time():
                return 'expired'
        return None

    def _hand_out(self,c,rid,args,result,nonce):
        """Atomically give a finished ack result to exactly one caller.

        Without a nonce (a legacy caller) the result is consumed now: one winner via the
        handed=0 guard, recorded as consumed_by='legacy'. With a nonce (the CLI) an execution
        permission is only offered; a successful /ack-receipt consumes it and records that
        exact nonce in consumed_by. consumed_by is the single source of truth for who may
        execute: a receipt is replayed as success only for the nonce recorded there.
        """
        if not result.get('should_execute'):
            self._close(c,rid)
            if result.get('kind')=='reply':
                c.execute("UPDATE net_jobs SET state='received' WHERE id=?",(args['message_id'],))
            return result
        invalid=self._claim_still_valid(c,args)
        if invalid:
            self._close(c,rid)
            return {'should_execute':False,'reason':invalid,'operation_id':rid}
        if nonce:
            if not c.execute('UPDATE net_ops SET claim_nonce=? WHERE id=? AND handed=0',(nonce,rid)).rowcount:
                return {'should_execute':False,'reason':'claim_already_received','operation_id':rid}
            return dict(result,claim_receipt=rid)
        if not c.execute("UPDATE net_ops SET handed=1,consumed_by='legacy' WHERE id=? AND handed=0",(rid,)).rowcount:
            return {'should_execute':False,'reason':'claim_already_received','operation_id':rid}
        c.execute("UPDATE net_jobs SET state='received' WHERE id=?",(args['message_id'],))
        return result

    @staticmethod
    def _close(c,rid):
        """Mark an operation finished without granting execution to anyone."""
        c.execute("UPDATE net_ops SET handed=1,consumed_by=COALESCE(consumed_by,'closed') WHERE id=?",(rid,))

    def receipt(self,b):
        """Consume an offered claim for the caller holding its nonce.

        Succeeds only if the claim is still unconsumed and offered to this nonce, or if this
        exact nonce already consumed it (idempotent replay of its own success). Any other
        consumer (a legacy caller, another nonce, a closed claim) means rejection.
        """
        rid,nonce=b.get('operation_id'),b.get('claim_nonce')
        with self.store.connect() as c:
            actor=self.store._session(c,b['actor'])
            row=c.execute("SELECT * FROM net_ops WHERE id=? AND op='ack' AND state='done'",(rid,)).fetchone()
            if not row or not nonce:
                raise BridgeError('claim receipt rejected: unknown claim')
            args=json.loads(row['args'])
            if args['actor']!=self.node_id+':'+actor['id']:
                raise BridgeError('claim receipt rejected: different session')
            if row['handed']:
                if row['consumed_by']==nonce:
                    return {'receipt':True,'operation_id':rid,'replayed':True}
                raise BridgeError('claim receipt rejected: already received by another caller')
            if row['claim_nonce']!=nonce:
                raise BridgeError('claim receipt rejected: not offered to this caller')
            invalid=self._claim_still_valid(c,args)
            if invalid:
                self._close(c,rid)
                raise BridgeError('claim receipt rejected: '+invalid)
            if not c.execute('UPDATE net_ops SET handed=1,consumed_by=? WHERE id=? AND handed=0 AND claim_nonce=?',
                             (nonce,rid,nonce)).rowcount:
                raise BridgeError('claim receipt rejected: already received by another caller')
            c.execute("UPDATE net_jobs SET state='received' WHERE id=?",(args['message_id'],))
            return {'receipt':True,'operation_id':rid}

    # Claude Code may hold or drop a peer message that arrives while the session is busy
    # (its own receipts say held/expired). A notification that was written but never
    # claimed is written again once the session is idle. Execution is still gated by the
    # ack claim, so a duplicate notification cannot run a request twice.
    RENOTIFY_AFTER=(60,180,600)
    RENOTIFY_HEADER=('[AgentBridge re-notification {n}/{total}: an earlier delivery of this message may not '
                     'have been shown. If it was already handled, ack returns should_execute=false; then do nothing more.]\n')

    def renotify(self,now=None):
        now=now or time.time()
        sent=[]
        with self.store.connect() as c:
            rows=[dict(r) for r in c.execute("""SELECT id,envelope,notified_at,notify_count FROM net_jobs
                WHERE state='submitted' AND notified_at IS NOT NULL AND notify_count<?""",(len(self.RENOTIFY_AFTER),))]
        for row in rows:
            m=json.loads(row['envelope'])
            if m.get('expires',0)<=now or now-row['notified_at']<self.RENOTIFY_AFTER[row['notify_count']]:
                continue
            local_id=m['receiver'].removeprefix(self.node_id+':')
            with self.store.connect() as c:
                current=c.execute('SELECT * FROM sessions WHERE id=?',(local_id,)).fetchone()
                state=c.execute('SELECT state FROM net_jobs WHERE id=?',(row['id'],)).fetchone()
            if (not current or not current['enabled'] or current['tool']!='claude'
                    or current['generation']!=m['receiver_gen'] or not state or state[0]!='submitted'):
                continue
            session=dict(current)
            if adapters.claude_status(session)!='idle':
                continue
            n=row['notify_count']+1
            # 1. Preflight only (nothing written): a failure here is not an attempt.
            try:
                text=self.RENOTIFY_HEADER.format(n=n,total=len(self.RENOTIFY_AFTER))+adapters.envelope(
                    m,session,self.store.client_directory(local_id,self.state))
                prepared=adapters.prepare(session,text)
            except Exception:
                continue          # endpoint not ready; the normal recovery path handles it
            # 2. Record the attempt BEFORE crossing the native boundary (compare-and-set on
            #    the count), so an error or crash during the write still counts toward the
            #    back-off and the limit, and two passes cannot both claim the same attempt.
            with self.store.connect() as c:
                claimed=c.execute("""UPDATE net_jobs SET notified_at=?,notify_count=?,renotify_state='attempting'
                    WHERE id=? AND state='submitted' AND notify_count=?""",(time.time(),n,row['id'],n-1)).rowcount
            if not claimed:
                continue
            # 3. Write; the outcome only labels the attempt, it never refunds it.
            try:
                detail=adapters.deliver(prepared)
                outcome,receipt='sent',json.dumps(detail.get('receipt') if isinstance(detail,dict) else None)
            except Exception as e:
                outcome,receipt='uncertain',json.dumps({'error':type(e).__name__})
            with self.store.connect() as c:
                c.execute("UPDATE net_jobs SET renotify_state=?,last_receipt=? WHERE id=? AND notify_count=?",
                          (outcome,receipt,row['id'],n))
            sent.append(row['id'])
        return sent

    def unclaimed(self,now=None,after=60):
        """Claude notifications written but not claimed for more than `after` seconds."""
        now=now or time.time()
        with self.store.connect() as c:
            rows=c.execute("""SELECT id,notified_at,notify_count,last_receipt,renotify_state FROM net_jobs
                WHERE state='submitted' AND notified_at IS NOT NULL AND notified_at<?""",(now-after,)).fetchall()
        return [{'message_id':r['id'],'age_seconds':int(now-r['notified_at']),'renotified':r['notify_count'],'renotify_state':r['renotify_state'],
                 'last_receipt':json.loads(r['last_receipt']) if r['last_receipt'] else None} for r in rows]

    OUTBOX_RETENTION=7*86400

    def prune(self,now=None):
        """Drop finished outbox rows well past every message TTL (max 24 h).

        Unreceived ack results (handed=0) are kept only while they could still be valid:
        their message expires within 24 h, so after 2 days they are dropped too (they would
        be refused as expired anyway). Delivery jobs past expiry are dropped likewise.
        """
        now=now or time.time()
        cutoff=now-self.OUTBOX_RETENTION
        with self.store.connect() as c:
            removed=c.execute("""DELETE FROM net_ops WHERE state IN ('done','failed') AND (
                (created<? AND (op!='ack' OR handed=1)) OR (op='ack' AND handed=0 AND created<?))""",
                (cutoff,now-2*86400)).rowcount
            removed+=c.execute("""DELETE FROM net_jobs WHERE state NOT IN ('submitting','retry_pending')
                AND json_extract(envelope,'$.expires')<?""",(cutoff,)).rowcount
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
            nonce=b.get('claim_nonce') if op=='ack' else None
            if nonce is not None and (not isinstance(nonce,str) or not 8<=len(nonce)<=64 or not nonce.isalnum()):
                raise BridgeError('invalid claim nonce')
            if op=='ack':
                # Reuse an unfinished or unreceipted claim of the SAME binding generation
                # instead of asking the Hub again: a second Hub ack would answer
                # already_received and the request would never run. A claim made under an
                # older generation is never reused (rebind/revocation invalidates it).
                previous=c.execute("""SELECT id,state FROM net_ops WHERE op='ack' AND handed=0
                    AND json_extract(args,'$.message_id')=? AND json_extract(args,'$.actor')=?
                    AND json_extract(args,'$.generation')=?
                    ORDER BY seq DESC LIMIT 1""",(args['message_id'],args['actor'],args['generation'])).fetchone()
                if previous and previous['state']=='failed':
                    self._close(c,previous['id'])
                elif previous:
                    rid=previous['id']
            if rid is None:
                rid=self._enqueue(c,op,args,time.time()+10 if op in ('ack','status') else None)
        self.wake.set()
        deadline=time.monotonic()+(12 if op in ('ack','status') else 2)
        # Once transmitted ('sending'), the Hub may already have committed the claim; keep
        # waiting for its answer, but always return well before the CLI timeout
        # (cli.ACK_TIMEOUT) so a slow Hub never turns into a lost local response.
        hard_deadline=time.monotonic()+(self.ACK_WAIT if op=='ack' else 24)
        while time.monotonic()<hard_deadline:
            with self.store.connect() as c:
                row=dict(c.execute('SELECT * FROM net_ops WHERE id=?',(rid,)).fetchone())
                if row['state']=='done':
                    result=json.loads(row['result'])
                    if op=='ack':
                        return self._hand_out(c,rid,args,result,nonce)
                    self._close(c,rid)
                    if op=='status' and isinstance(result,list):
                        for item in result:
                            ids=[i for i in (item.get('id'),item.get('final_reply_id')) if i]
                            local={r['id']:r['auto_ack'] for r in c.execute(
                                'SELECT id,auto_ack FROM net_jobs WHERE id IN (%s)'%','.join('?'*len(ids)),ids)} if ids else {}
                            item['auto_ack']=local.get(item.get('id'))
                            item['reply_auto_ack']=local.get(item.get('final_reply_id'))
                    return result
                if row['state']=='failed':
                    self._close(c,rid)
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
                                if operation['op']=='ack' and json.loads(operation['args']).get('context',{}).get('auto_ack')=='agent':
                                    self._resolve_auto_ack(c,operation,response)
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

    def _resolve_auto_ack(self,c,operation,response):
        """The Hub answered (or never received) the Agent's own ack of an exact-echo reply.

        The RPC envelope 'ok' only says the Hub processed the call; the business answer is in
        result. Outcomes recorded in net_jobs.auto_ack (and visible in every status):
          agent_received          Hub recorded this ack as the receipt (kind=reply, no reason)
          already_received        someone else (the session itself) acked it first
          denied:<reason>         Hub refused (expired, binding_revoked, blocked, ...); terminal,
                                  state 'blocked' or 'expired' -- never reported as received
          expired_unsent          the reply expired before the ack could be transmitted; terminal
          failed:<error>          transport/protocol error -> state 'accepted', injected normally
        """
        mid=json.loads(operation['args'])['message_id']
        c.execute("UPDATE net_ops SET handed=1,consumed_by='agent_auto_ack' WHERE id=?",(operation['id'],))
        result=response.get('result') if isinstance(response.get('result'),dict) else {}
        reason=str(result.get('reason') or '')
        if response.get('ok') and result.get('kind')=='reply' and not reason:
            c.execute("UPDATE net_jobs SET state='received',auto_ack='agent_received' WHERE id=?",(mid,))
        elif response.get('ok') and reason=='already_received' and result.get('kind')=='reply':
            c.execute("UPDATE net_jobs SET state='received',auto_ack='already_received' WHERE id=?",(mid,))
        elif response.get('ok'):
            terminal='expired' if reason=='expired' else 'blocked'
            c.execute("UPDATE net_jobs SET state=?,auto_ack=?,error=? WHERE id=?",
                      (terminal,'denied:'+(reason or 'unknown')[:60],json.dumps({'reason':reason or 'unknown'}),mid))
        elif response.get('error')=='expired_unsent':
            c.execute("UPDATE net_jobs SET state='expired',auto_ack='expired_unsent',error=? WHERE id=?",
                      (json.dumps({'reason':'reply expired before the agent ack was sent'}),mid))
        else:
            c.execute("UPDATE net_jobs SET state='accepted',auto_ack=? WHERE id=? AND state!='received'",
                      ('failed:'+str(response.get('error') or 'unknown')[:60],mid))
            self.wake.set()

    def expire_auto_acks(self,now=None):
        """An auto ack whose deadline (= the reply's expiry) passed unsent gets a terminal,
        queryable state; it is not re-queued (delivery would only block it as expired)."""
        with self.store.connect() as c:
            for op in c.execute("""SELECT * FROM net_ops WHERE op='ack' AND state='failed' AND handed=0
                    AND json_extract(args,'$.context.auto_ack')='agent'""").fetchall():
                op=dict(op)
                unsent=(op.get('error') or '').startswith('operation expired before confirmed transmission')
                self._resolve_auto_ack(c,op,{'ok':False,'error':'expired_unsent' if unsent else op.get('error')})

    def _is_exact_echo_reply(self,c,m):
        """True only for a completed final reply whose text equals the echo text of the JSON
        echo request this same binding sent. Anything else (notice, blocked, mismatch, unknown
        or pruned parent) is injected so the session sees it."""
        # A reply row's own outcome is 'not_applicable'; a blocked notice reply never equals
        # the echo text, so body equality alone decides.
        if not m.get('parent_id'):
            return False
        row=c.execute("""SELECT args FROM net_ops WHERE op='send'
            AND json_extract(args,'$.message_id')=? AND json_extract(args,'$.actor')=?
            AND json_extract(args,'$.generation')=?
            ORDER BY seq DESC LIMIT 1""",(m['parent_id'],m['receiver'],m.get('receiver_gen'))).fetchone()
        if not row:
            return False
        from .policy import NOTICE_TEXT, echo_request_text
        expected=echo_request_text(json.loads(row['args']).get('body'))
        # A blocked reply carries NOTICE_TEXT; if the echo text is that same string the body
        # cannot tell them apart, so stay conservative and let the session see it.
        return expected is not None and expected!=NOTICE_TEXT and m.get('body')==expected

    def delivery_loop(self):
        while not self.stop.is_set():
            if time.monotonic()>=self._recovery_at:
                try:
                    self.recover_endpoints()
                except Exception:
                    # Temporary database contention must not kill delivery.
                    pass
                self._recovery_at=time.monotonic()+self.recovery_interval
                try:
                    self.renotify()
                except Exception:
                    pass
                try:
                    self.expire_auto_acks()
                except Exception:
                    pass
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
                auto_reply=(self.config.get('auto_ack_final_echo') is True
                            and not self.config.get('inject_final_replies', False)
                            and m.get('kind')=='reply' and m.get('final')
                            and row['auto_ack'] is None
                            and self._is_exact_echo_reply(c,m))
                if auto_reply:
                    # Opt-in (auto_ack_final_echo): an exact echo reply is acked by the Agent and
                    # not written into the session. The ack is queued in this same transaction,
                    # so a disconnect, crash or restart only delays it; status reports
                    # agent_received, never session_received.
                    self._phase(c,m['id'],'submitted',{'transport':'agent','meaning':'agent_received_not_injected'})
                    self._enqueue(c,'ack',{'message_id':m['id'],'actor':m['receiver'],
                                           'generation':current_session['generation'],
                                           'context':{'auto_ack':'agent'}},m['expires'])
                    c.execute("UPDATE net_jobs SET auto_ack='queued' WHERE id=?",(m['id'],))
                else:
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
            if auto_reply:
                self.wake.set()
                continue
            try:
                detail=adapters.deliver(prepared)
                phase='submitted'
            except Exception as e:
                detail={'reason':str(e)[:1000]}
                phase='delivery_uncertain'
            with self.store.connect() as c:
                self._phase(c,m['id'],phase,detail)
                if phase=='submitted':
                    c.execute('UPDATE net_jobs SET notified_at=?,last_receipt=? WHERE id=?',
                              (time.time(),json.dumps(detail.get('receipt')) if isinstance(detail,dict) else None,m['id']))
            self.wake.set()
