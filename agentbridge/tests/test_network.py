import json
from pathlib import Path
import queue
import ssl
import tempfile
import threading
import time
import unittest
import uuid
from unittest.mock import patch

from websockets.sync.client import connect
from bridge.hub import HubStore, create_tls, serve_hub
from bridge.network import RemoteAgent
from bridge.store import Store, uid


def until(predicate, seconds=12):
    end=time.monotonic()+seconds
    while time.monotonic()<end:
        if predicate():
            return
        time.sleep(0.03)
    raise AssertionError('condition not reached before timeout')


class NetworkTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        create_tls(self.root,['127.0.0.1'])
        self.hub=HubStore(self.root/'hub.sqlite')
        self.server=None
        self.server_ready=threading.Event()
        def ready(server):
            self.server=server
            self.server_ready.set()
        self.hub_thread=threading.Thread(target=serve_hub,args=(self.hub,'127.0.0.1',0,
            self.root/'hub-cert.pem',self.root/'hub-key.pem',ready),daemon=True)
        self.hub_thread.start()
        self.assertTrue(self.server_ready.wait(5))
        self.url=f'wss://127.0.0.1:{self.server.socket.getsockname()[1]}'
        self.agents=[]
        self.threads=[]
        self.stops=[]
        self.delivered=queue.Queue()
        self.patch_prepare=patch('bridge.network.adapters.prepare',side_effect=lambda s,t:(s,t))
        self.patch_deliver=patch('bridge.network.adapters.deliver',side_effect=self.fake_native)
        self.patch_prepare.start()
        self.patch_deliver.start()

    def tearDown(self):
        for stop in self.stops:
            stop.set()
        for thread in self.threads:
            thread.join(12)
            self.assertFalse(thread.is_alive())
        self.server.shutdown()
        self.hub_thread.join(5)
        self.patch_prepare.stop()
        self.patch_deliver.stop()
        self.temp.cleanup()

    def fake_native(self,prepared):
        self.delivered.put(prepared)
        return {'transport':'test_double','meaning':'simulated_native_queue'}

    def agent(self,label,start=True):
        node=self.hub.add_node(label,['echo'])
        state=self.root/label
        state.mkdir()
        config=state/'network.json'
        config.write_text(json.dumps(node|{'hub_url':self.url,'hub_certificate':(self.root/'hub-cert.pem').read_text()}))
        store=Store(state/'ledger.sqlite')
        session=store.bind('codex',str(uuid.uuid4()),'echo','echo',{})
        stop=threading.Event()
        agent=RemoteAgent(store,state,config,stop,{'machine_id':str(uuid.uuid4()),'os_user_id':'test-user'})
        self.agents.append(agent)
        self.stops.append(stop)
        if start:
            self.start(agent)
        return agent,session

    def start(self,agent):
        for target in (agent.connection_loop,agent.delivery_loop):
            thread=threading.Thread(target=target,daemon=True)
            self.threads.append(thread)
            thread.start()

    def wait_directory(self,agent,count):
        until(lambda:agent.connected and len(agent.directory)==count)

    def test_two_agents_tls_roundtrip_both_directions(self):
        a,sa=self.agent('win-a')
        b,sb=self.agent('win-b')
        self.wait_directory(a,2)
        for source,s,receiver,r in ((a,sa,b,sb),(b,sb,a,sa)):
            m=source.forward('/send',{'actor':s['id'],'to':receiver.label+'/echo','body':'中文跨节点 echo','message_id':uid()})
            self.assertEqual(m['delivery'],'accepted')
            delivered=self.delivered.get(timeout=6)
            self.assertEqual(delivered[0]['id'],r['id'])
            claim=receiver.forward('/ack',{'actor':r['id'],'message_id':m['id']})
            self.assertTrue(claim['should_execute'])
            reply=receiver.forward('/reply',{'actor':r['id'],'message_id':m['id'],'body':claim['body']})
            delivered=self.delivered.get(timeout=6)
            self.assertEqual(delivered[0]['id'],s['id'])
            received=source.forward('/ack',{'actor':s['id'],'message_id':reply['id']})
            self.assertFalse(received['should_execute'])
            self.assertEqual(self.hub.status(m['id'])[0]['reply_delivery'],'received')
        self.assertTrue(self.delivered.empty())

    def test_offline_outbox_is_persistent_and_reconnects(self):
        a,sa=self.agent('win-a',start=False)
        b,sb=self.agent('win-b')
        self.wait_directory(b,1)
        mid=uid()
        pending=a.forward('/send',{'actor':sa['id'],'to':'win-b/echo','body':'buffered','message_id':mid})
        self.assertEqual(pending['delivery'],'local_pending')
        self.assertEqual(self.hub.status(mid),[])
        self.assertFalse(a.forward('/ack',{'actor':sa['id'],'message_id':mid})['should_execute'])
        recovered=RemoteAgent(a.store,a.state,a.state/'network.json',a.stop,a.identity)
        self.start(recovered)
        self.delivered.get(timeout=10)
        self.assertEqual(self.hub.status(mid)[0]['body'],'buffered')
        until(lambda:recovered.status()['pending_operations']==0)

    def test_retry_same_rpc_after_lost_ack_response(self):
        a,sa=self.agent('win-a')
        b,sb=self.agent('win-b')
        self.wait_directory(a,2)
        m=a.forward('/send',{'actor':sa['id'],'to':'win-b/echo','body':'once','message_id':uid()})
        self.delivered.get(timeout=6)
        original=b._exchange
        dropped=[False]
        def drop_once(ws,op,args,rid=None):
            result=original(ws,op,args,rid)
            if op=='ack' and not dropped[0]:
                dropped[0]=True
                raise OSError('test: response lost after Hub commit')
            return result
        b._exchange=drop_once
        result=b.forward('/ack',{'actor':sb['id'],'message_id':m['id']})
        self.assertTrue(result['should_execute'])
        self.assertTrue(dropped[0])
        self.assertFalse(b.forward('/ack',{'actor':sb['id'],'message_id':m['id']})['should_execute'])
        self.assertTrue(self.delivered.empty())

    def test_restart_submission_intent_never_reinjects(self):
        a,sa=self.agent('win-a',start=False)
        mid=uid()
        with a.store.connect() as c:
            c.execute("INSERT INTO net_jobs(id,envelope,state) VALUES(?,?,'submitting')",(mid,'{}'))
        recovered=RemoteAgent(a.store,a.state,a.state/'network.json',a.stop,a.identity)
        self.assertEqual(recovered.status()['jobs'][0]['state'],'delivery_uncertain')
        self.assertEqual(recovered.status()['pending_operations'],1)

    def test_tls_rejects_untrusted_certificate(self):
        with self.assertRaises(ssl.SSLCertVerificationError):
            connect(self.url,ssl=ssl.create_default_context(),proxy=None,open_timeout=3)

    def test_revocation_rejects_existing_websocket(self):
        node=self.hub.add_node('revoked',['echo'])
        context=ssl.create_default_context(cafile=str(self.root/'hub-cert.pem'))
        with connect(self.url,ssl=context,proxy=None) as ws:
            ws.send(json.dumps({'token':node['token'],'machine_id':str(uuid.uuid4()),'os_user_id':'test-user'}))
            self.assertTrue(json.loads(ws.recv())['ok'])
            self.hub.revoke(node['node_id'])
            ws.send(json.dumps({'id':uid('rpc'),'op':'directory','args':{}}))
            self.assertFalse(json.loads(ws.recv())['ok'])

    def test_enrollment_over_tls_without_node_secret_in_bundle(self):
        node=self.hub.add_node('joining',['echo'])
        code=self.hub.invite(node['node_id'],'127.0.0.1')
        context=ssl.create_default_context(cafile=str(self.root/'hub-cert.pem'))
        with connect(self.url,ssl=context,proxy=None) as ws:
            ws.send(json.dumps({'op':'enroll','code':code,'machine_id':str(uuid.uuid4()),'os_user_id':'user'}))
            response=json.loads(ws.recv())
            self.assertTrue(response['ok'])
            self.assertNotEqual(response['enrollment']['token'],node['token'])

    def test_hub_restart_reconnects_without_duplicate_native_delivery(self):
        a,sa=self.agent('win-a')
        b,sb=self.agent('win-b')
        self.wait_directory(a,2)
        m=a.forward('/send',{'actor':sa['id'],'to':'win-b/echo','body':'persisted','message_id':uid()})
        self.delivered.get(timeout=6)
        until(lambda:self.hub.status(m['id'])[0]['delivery']=='submitted')
        port=self.server.socket.getsockname()[1]
        self.server.shutdown()
        self.hub_thread.join(5)
        until(lambda:not a.connected and not b.connected)
        self.hub=HubStore(self.root/'hub.sqlite')
        self.server_ready.clear()
        def ready(server):
            self.server=server
            self.server_ready.set()
        self.hub_thread=threading.Thread(target=serve_hub,args=(self.hub,'127.0.0.1',port,
            self.root/'hub-cert.pem',self.root/'hub-key.pem',ready),daemon=True)
        self.hub_thread.start()
        self.assertTrue(self.server_ready.wait(5))
        until(lambda:a.connected and b.connected)
        claim=b.forward('/ack',{'actor':sb['id'],'message_id':m['id']})
        self.assertTrue(claim['should_execute'])
        self.assertTrue(self.delivered.empty())

    def test_explicit_retry_of_failed_preflight_delivers_exactly_once(self):
        a,sa=self.agent('win-a')
        b,sb=self.agent('win-b')
        self.wait_directory(a,2)
        unavailable=[True]
        def prepare(session,text):
            if session['id']==sb['id'] and unavailable[0]:
                raise FileNotFoundError('test: previous runtime registry disappeared')
            return session,text
        with patch('bridge.network.adapters.prepare',side_effect=prepare):
            m=a.forward('/send',{'actor':sa['id'],'to':'win-b/echo','body':'same original session','message_id':uid()})
            until(lambda:self.hub.status(m['id'])[0]['delivery']=='blocked')
            self.assertTrue(self.delivered.empty())

            unavailable[0]=False
            self.assertEqual(b.retry_preflight(sb['id']),[m['id']])
            self.assertEqual(b.retry_preflight(sb['id']),[])
            self.assertEqual(self.delivered.get(timeout=6)[0]['id'],sb['id'])
            claim=b.forward('/ack',{'actor':sb['id'],'message_id':m['id']})
            self.assertTrue(claim['should_execute'])
            self.assertEqual(b.retry_preflight(sb['id']),[])
            self.assertTrue(self.delivered.empty())

    def test_automatic_recovery_after_manual_reopen(self):
        a,sa=self.agent('win-a')
        b,sb=self.agent('win-b')
        b.recovery_interval=0.1
        b._recovery_at=0
        self.wait_directory(a,2)
        unavailable=[True]
        def prepare(session,text):
            if session['id']==sb['id'] and unavailable[0]:
                raise FileNotFoundError('app closed')
            return session,text
        with patch('bridge.network.adapters.prepare',side_effect=prepare), patch('bridge.network.adapters.codex_args',return_value=[]):
            m=a.forward('/send',{'actor':sa['id'],'to':'win-b/echo','body':'reopen','message_id':uid()})
            until(lambda:self.hub.status(m['id'])[0]['delivery']=='blocked')
            self.assertTrue(self.delivered.empty())
            unavailable[0]=False
            self.assertEqual(self.delivered.get(timeout=6)[0]['id'],sb['id'])
            until(lambda:self.hub.status(m['id'])[0]['delivery']=='submitted')
            # Repeated reconciliation cannot replay a submitted but unacked job.
            for _ in range(3):
                b.recover_endpoints()
            self.assertTrue(self.delivered.empty())
            self.assertTrue(b.forward('/ack',{'actor':sb['id'],'message_id':m['id']})['should_execute'])
            reply=b.forward('/reply',{'actor':sb['id'],'message_id':m['id'],'body':'reopen'})
            self.delivered.get(timeout=6)
            self.assertFalse(a.forward('/ack',{'actor':sa['id'],'message_id':reply['id']})['should_execute'])
            self.assertEqual(self.hub.status(m['id'])[0]['reply_delivery'],'received')


if __name__=='__main__':
    unittest.main()
