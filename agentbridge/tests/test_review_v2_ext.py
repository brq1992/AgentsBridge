import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from bridge.network import RemoteAgent
from bridge.store import Store
from bridge import cli
from bridge.policy import NOTICE_TEXT

class Boundaries(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / 'ledger.sqlite')
        self.s = self.store.bind('codex', '11111111-1111-4111-8111-111111111111', 'echo', 'echo', {})
        config = self.root / 'network.json'
        config.write_text(json.dumps(dict(hub_url='wss://localhost',node_id='node',label='test')))
        self.a = RemoteAgent(self.store,self.root,config,threading.Event(),{})
        self.a._recovery_at = float('inf')
        self.args = dict(message_id='parent',actor='node:'+self.s['id'],generation=self.s['generation'],
                         to='remote/echo',body=json.dumps(dict(op='echo',text='ok')))
        self.m = dict(id='reply',parent_id='parent',kind='reply',final=True,body='ok',sender='remote:codex:peer',hop=1,
                      receiver=self.args['actor'],receiver_gen=self.s['generation'],project='echo',expires=time.time()+60)
        self.save_parent()

    def save_parent(self):
        with self.store.connect() as c:
            c.execute('DELETE FROM net_ops')
            self.a._enqueue(c,'send',self.args)

    def matches(self):
        with self.store.connect() as c:
            return self.a._is_exact_echo_reply(c,self.m)

    def test_unknown_parent_injected(self):
        self.m['parent_id']='missing'
        self.assertFalse(self.matches())

    def test_mismatch_injected(self):
        self.m['body']='different'
        self.assertFalse(self.matches())

    def test_old_generation_parent_not_autoacked(self):
        self.args['generation']='old-generation'
        self.save_parent()
        self.assertFalse(self.matches())

    def test_blocked_notice_text_collision_not_autoacked(self):
        self.args['body']=json.dumps(dict(op='echo',text=NOTICE_TEXT))
        self.m['body']=NOTICE_TEXT
        self.save_parent()
        # The reply body cannot distinguish a blocked notice from an exact echo.
        self.assertFalse(self.matches())

    def run_job(self, fail_ack=False):
        with self.store.connect() as c:
            c.execute("INSERT INTO net_jobs(id,envelope,state) VALUES(?,?,'accepted')",('reply',json.dumps(self.m)))
        calls=[]
        def ack(*args,**kwargs):
            calls.append(1)
            if fail_ack:
                self.a.stop.set()
                return {'should_execute':False,'reason':'hub_unavailable_no_claim'}
            self.a.stop.set()
            return {'should_execute':False,'kind':'reply'}
        guard=threading.Timer(1.5,self.a.stop.set)   # auto-ack no longer calls forward()
        guard.start()
        with patch.object(self.a,'forward',side_effect=ack), patch('bridge.network.adapters.prepare',return_value=('fake',)), patch('bridge.network.adapters.deliver',side_effect=lambda p:self.a.stop.set()) as deliver:
            self.a.delivery_loop()
        guard.cancel()
        with self.store.connect() as c:
            state=c.execute('SELECT state FROM net_jobs WHERE id=?',('reply',)).fetchone()[0]
            acks=c.execute("SELECT count(*) FROM net_ops WHERE op='ack'").fetchone()[0]
        return calls, deliver.call_count, state, acks

    def test_disconnected_autoack_has_durable_retry(self):
        self.a.config['auto_ack_final_echo']=True   # opt-in (default off)
        _,_,state,acks=self.run_job(fail_ack=True)
        self.assertTrue(state != 'submitted' or acks > 0, 'hidden reply has no durable acknowledgement retry')

    def test_expired_reply_does_not_autoack(self):
        self.m['expires']=time.time()-1
        # Stop after the blocked phase so the worker does not idle indefinitely.
        original=self.a._phase
        def phase(*args,**kwargs):
            original(*args,**kwargs)
            self.a.stop.set()
        with patch.object(self.a,'_phase',side_effect=phase):
            calls,delivered,state,_=self.run_job()
        self.assertEqual((calls,delivered,state),([],0,'blocked'))

    def test_opt_out_injects_reply(self):
        self.a.config['inject_final_replies']=True
        calls,delivered,_,_=self.run_job()
        self.assertEqual((calls,delivered),([],1))

    def test_default_off_injects(self):
        calls,delivered,_,acks=self.run_job()
        self.assertEqual((calls,delivered,acks),([],1,0))

    def test_queued_ack_survives_restart(self):
        self.a.config['auto_ack_final_echo']=True
        self.run_job()
        config=self.root/'network.json'
        again=RemoteAgent(self.store,self.root,config,threading.Event(),{})
        with self.store.connect() as c:
            op=c.execute("SELECT state,deadline FROM net_ops WHERE op='ack'").fetchone()
            job=c.execute("SELECT state,auto_ack FROM net_jobs WHERE id='reply'").fetchone()
        self.assertEqual((op['state'],tuple(job)),('pending',('submitted','queued')))
        self.assertGreater(op['deadline'],time.time())

    def test_failed_autoack_is_injected_later(self):
        self.a.config['auto_ack_final_echo']=True
        self.run_job()
        with self.store.connect() as c:
            op=dict(c.execute("SELECT * FROM net_ops WHERE op='ack'").fetchone())
            self.a._resolve_auto_ack(c,op,{'ok':False,'error':'x'})
            job=c.execute("SELECT state,auto_ack FROM net_jobs WHERE id='reply'").fetchone()
        self.assertEqual(tuple(job),('accepted','failed:x'))
        calls,delivered,_,_=self.run_job_existing()

    def run_job_existing(self):
        with patch('bridge.network.adapters.prepare',return_value=('fake',)), patch('bridge.network.adapters.deliver',side_effect=lambda p:self.a.stop.set()) as deliver:
            self.a.stop.clear()
            guard=threading.Timer(1.5,self.a.stop.set); guard.start()
            self.a.delivery_loop(); guard.cancel()
        self.assertEqual(deliver.call_count,1)
        return None,deliver.call_count,None,None


class HandleFailure(unittest.TestCase):
    def test_claim_failure_not_reported_as_acked(self):
        with patch.object(cli,'claim',return_value={'should_execute':False,'reason':'hub_unavailable_no_claim'}):
            r=cli.handle_message('/unused',{})
        self.assertEqual(r['action'],'ack_failed_retry')

    def test_expired_is_not_authorised(self):
        with patch.object(cli,'claim',return_value={'should_execute':False,'reason':'message_expired'}):
            self.assertEqual(cli.handle_message('/unused',{})['action'],'not_authorised')

if __name__=='__main__': unittest.main()
