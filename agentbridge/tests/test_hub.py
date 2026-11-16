import json
from pathlib import Path
import tempfile
import time
import unittest
import uuid

from bridge.hub import HubStore
from bridge.store import BridgeError, uid


class HubTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.path=Path(self.temp.name)/'hub.sqlite'
        self.hub=HubStore(self.path)
        self.a=self.hub.add_node('win-a',['echo'])
        self.b=self.hub.add_node('win-b',['echo'])
        self.c=self.hub.add_node('isolated',['secret'])
        self.native=str(uuid.uuid4())
        self.sa=self.register(self.a,'echo')
        self.sb=self.register(self.b,'echo')
        self.sc=self.register(self.c,'secret')

    def tearDown(self):
        self.temp.cleanup()

    def register(self,node,project):
        s={'tool':'codex','native_id':self.native,'alias':'same-name','project':project,
           'generation':uid('g'),'enabled':True}
        self.hub.rpc(node['node_id'],uid('rpc'),'register',{'sessions':[s]})
        return s|{'id':node['node_id']+':codex:'+self.native}

    def rpc(self,node,session,op,**args):
        return self.hub.rpc(node['node_id'],uid('rpc'),op,
                            {'actor':session['id'],'generation':session['generation'],**args})

    def send(self,**extra):
        return self.rpc(self.a,self.sa,'send',to='win-b/same-name',body='跨机 echo',message_id=uid(),**extra)

    def test_same_native_uuid_and_alias_are_namespaced(self):
        self.assertNotEqual(self.sa['id'],self.sb['id'])
        directory=self.hub.rpc(self.a['node_id'],uid('rpc'),'directory',{})
        self.assertEqual({s['alias'] for s in directory},{'win-a/same-name','win-b/same-name'})
        self.assertTrue(all('metadata' not in s for s in directory))

    def test_bidirectional_final_receipts_and_no_reply_loop(self):
        for node,session,target_node,target in ((self.a,self.sa,self.b,self.sb),(self.b,self.sb,self.a,self.sa)):
            m=self.rpc(node,session,'send',to=target['id'],body='echo',message_id=uid())
            self.assertTrue(self.rpc(target_node,target,'ack',message_id=m['id'])['should_execute'])
            reply=self.rpc(target_node,target,'reply',message_id=m['id'],body='echo')
            claim=self.rpc(node,session,'ack',message_id=reply['id'])
            self.assertFalse(claim['should_execute'])
            self.assertEqual(self.hub.status(m['id'])[0]['reply_delivery'],'received')
            with self.assertRaises(BridgeError):
                self.rpc(node,session,'reply',message_id=reply['id'],body='loop')

    def test_lost_ack_response_replays_atomically_after_hub_restart(self):
        m=self.send()
        rid=uid('rpc')
        args={'actor':self.sb['id'],'generation':self.sb['generation'],'message_id':m['id']}
        result=self.hub.rpc(self.b['node_id'],rid,'ack',args)
        self.hub=HubStore(self.path)
        self.assertEqual(result,self.hub.rpc(self.b['node_id'],rid,'ack',args))
        self.assertTrue(result['should_execute'])
        self.assertFalse(self.rpc(self.b,self.sb,'ack',message_id=m['id'])['should_execute'])
        self.assertEqual(len([e for e in self.hub.status(m['id'])[0]['events'] if e['event']=='received']),1)

    def test_rpc_id_payload_conflict(self):
        rid=uid('rpc')
        args={'actor':self.sa['id'],'generation':self.sa['generation'],'to':self.sb['id'],'body':'one','message_id':uid()}
        self.hub.rpc(self.a['node_id'],rid,'send',args)
        with self.assertRaises(BridgeError):
            self.hub.rpc(self.a['node_id'],rid,'send',args|{'body':'two'})

    def test_node_cannot_impersonate_other_node(self):
        with self.assertRaises(BridgeError):
            self.rpc(self.a,self.sb,'send',to=self.sa['id'],body='fake',message_id=uid())

    def test_project_isolation_and_status_isolation(self):
        with self.assertRaises(BridgeError):
            self.rpc(self.a,self.sa,'send',to=self.sc['id'],body='leak',message_id=uid())
        m=self.send()
        with self.assertRaises(BridgeError):
            self.rpc(self.c,self.sc,'status',message_id=m['id'])

    def test_snapshot_cannot_publish_ungranted_project(self):
        with self.assertRaises(BridgeError):
            self.hub.rpc(self.a['node_id'],uid('rpc'),'register',{'sessions':[self.sa|{'project':'secret'}]})

    def test_revocation_applies_to_open_authenticated_connection(self):
        machine=str(uuid.uuid4())
        self.hub.authenticate(self.a['token'],machine,'test-user')
        self.hub.revoke(self.a['node_id'])
        with self.assertRaises(BridgeError):
            self.hub.rpc(self.a['node_id'],uid('rpc'),'directory',{})
        with self.assertRaises(BridgeError):
            self.hub.authenticate(self.a['token'],machine,'test-user')

    def test_node_credential_is_bound_to_first_machine_user(self):
        machine=str(uuid.uuid4())
        self.hub.authenticate(self.a['token'],machine,'one-user')
        with self.assertRaises(BridgeError):
            self.hub.authenticate(self.a['token'],str(uuid.uuid4()),'one-user')
        with self.assertRaises(BridgeError):
            self.hub.authenticate(self.a['token'],machine,'other-user')

    def test_source_revoke_invalidates_cached_claim(self):
        m=self.send()
        rid=uid('rpc')
        args={'actor':self.sb['id'],'generation':self.sb['generation'],'message_id':m['id']}
        self.assertTrue(self.hub.rpc(self.b['node_id'],rid,'ack',args)['should_execute'])
        self.hub.revoke(self.a['node_id'])
        self.assertFalse(self.hub.rpc(self.b['node_id'],rid,'ack',args)['should_execute'])

    def test_stale_binding_does_not_execute(self):
        m=self.send()
        self.register(self.b,'echo')
        with self.assertRaises(BridgeError):
            self.rpc(self.b,self.sb,'ack',message_id=m['id'])

    def test_expired_offline_send_does_not_get_fresh_ttl(self):
        with self.assertRaises(BridgeError):
            self.send(ttl=60,expires_at=time.time()-1)
        for invalid in ('bad',float('nan'),float('inf'),-1):
            with self.assertRaises(BridgeError):
                self.send(ttl=invalid)

    def test_delivery_reports_are_monotonic_and_do_not_overwrite_ack(self):
        m=self.send()
        self.rpc(self.b,self.sb,'ack',message_id=m['id'])
        self.hub.rpc(self.b['node_id'],uid('rpc'),'report',{'message_id':m['id'],'sequence':2,'phase':'submitted'})
        old=self.hub.rpc(self.b['node_id'],uid('rpc'),'report',{'message_id':m['id'],'sequence':1,'phase':'submitting'})
        self.assertFalse(old['recorded'])
        self.assertEqual(self.hub.status(m['id'])[0]['delivery'],'received')

    def test_failed_mutation_rolls_back_without_cache(self):
        rid=uid('rpc')
        with self.assertRaises(BridgeError):
            self.hub.rpc(self.a['node_id'],rid,'send',{'actor':self.sa['id'],'generation':self.sa['generation'],
                'to':self.sb['id'],'message_id':uid(),'body':'x','ttl':0})
        with self.hub.connect() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM messages').fetchone()[0],0)
            self.assertEqual(c.execute('SELECT count(*) FROM rpc_results WHERE request_id=?',(rid,)).fetchone()[0],0)

    def test_pull_survives_restart_and_limits_frame_size(self):
        for _ in range(6):
            self.rpc(self.a,self.sa,'send',to=self.sb['id'],body='\x00'*32768,message_id=uid())
        first=self.hub.rpc(self.b['node_id'],uid('rpc'),'pull',{})
        self.assertEqual(len(first),4)
        self.assertLess(len(json.dumps(first).encode()),1048576)
        self.hub=HubStore(self.path)
        second=self.hub.rpc(self.b['node_id'],uid('rpc'),'pull',{})
        self.assertEqual([m['id'] for m in first],[m['id'] for m in second])

    def test_invitation_single_use_and_source_ip_bound(self):
        code=self.hub.invite(self.a['node_id'],'192.0.2.10')
        machine=str(uuid.uuid4())
        with self.assertRaises(BridgeError):
            self.hub.redeem(code,'192.0.2.11',machine,'user')
        enrollment=self.hub.redeem(code,'192.0.2.10',machine,'user')
        self.assertEqual(enrollment['node_id'],self.a['node_id'])
        self.assertEqual(self.hub.authenticate(enrollment['token'],machine,'user'),self.a['node_id'])
        with self.assertRaises(BridgeError):
            self.hub.redeem(code,'192.0.2.10',machine,'user')
        with self.assertRaises(BridgeError):
            self.hub.authenticate(self.a['token'],machine,'user')
        with self.assertRaises(BridgeError):
            self.hub.invite(self.a['node_id'],'192.0.2.10')

    def test_expired_invitation_denied(self):
        code=self.hub.invite(self.a['node_id'],'127.0.0.1',lifetime=-1)
        with self.assertRaises(BridgeError):
            self.hub.redeem(code,'127.0.0.1',str(uuid.uuid4()),'user')

    def test_preflight_retry_only_before_any_native_submission(self):
        m=self.send()
        self.hub.rpc(self.b['node_id'],uid('rpc'),'report',{'message_id':m['id'],'sequence':1,'phase':'blocked'})
        args={'message_id':m['id'],'sequence':1,'generation':self.sb['generation']}
        with self.assertRaises(BridgeError):
            self.hub.rpc(self.a['node_id'],uid('rpc'),'retry_preflight',args)
        rid=uid('rpc')
        self.assertTrue(self.hub.rpc(self.b['node_id'],rid,'retry_preflight',args)['retry'])
        self.assertTrue(self.hub.rpc(self.b['node_id'],rid,'retry_preflight',args)['retry'])
        self.hub.rpc(self.b['node_id'],uid('rpc'),'report',{'message_id':m['id'],'sequence':2,'phase':'submitting'})
        self.hub.rpc(self.b['node_id'],uid('rpc'),'report',{'message_id':m['id'],'sequence':3,'phase':'blocked'})
        with self.assertRaises(BridgeError):
            self.hub.rpc(self.b['node_id'],uid('rpc'),'retry_preflight',args|{'sequence':3})

    def test_preflight_retry_cannot_revive_expired_message(self):
        m=self.send()
        self.hub.rpc(self.b['node_id'],uid('rpc'),'report',{'message_id':m['id'],'sequence':1,'phase':'blocked'})
        with self.hub.connect() as c:
            c.execute('UPDATE messages SET expires=0 WHERE id=?',(m['id'],))
        with self.assertRaises(BridgeError):
            self.hub.rpc(self.b['node_id'],uid('rpc'),'retry_preflight',
                         {'message_id':m['id'],'sequence':1,'generation':self.sb['generation']})


if __name__=='__main__':
    unittest.main()
