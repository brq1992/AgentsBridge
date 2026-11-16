import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
import uuid
from unittest.mock import patch

from bridge import adapters
from bridge.network import RemoteAgent
from bridge.store import Store, BridgeError


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.registry=self.root/'sessions'
        self.registry.mkdir()
        self.native=str(uuid.uuid4())
        self.store=Store(self.root/'ledger.sqlite')
        self.session=self.store.bind('claude',self.native,'claude2','echo',{'registry':'old.json','pid':1})
        self.store.add_client(self.session['id'],str(self.root/'client'),'test-token')
        config=self.root/'network.json'
        config.write_text(json.dumps({'hub_url':'wss://localhost','node_id':'node','label':'test','projects':['echo']}))
        self.agent=RemoteAgent(self.store,self.root,config,threading.Event(),{})
        original=adapters.discover_claude
        self.discovery=patch('bridge.adapters.discover_claude',side_effect=lambda:original(self.registry))
        self.discovery.start()
        self.addCleanup(self.discovery.stop)
        self.process=patch('bridge.adapters.process_start',return_value='123')
        self.process.start()
        self.addCleanup(self.process.stop)

    def registry_entry(self,pid=2,native=None,version='2.1.325',host=True):
        entry={'sessionId':str(uuid.uuid4()) if host else (native or self.native),
               'pid':pid,'procStart':'123','version':version,'pidDomain':'test',
               'messagingSocketPath':r'\\.\pipe\LOCAL\cc-msg-test'}
        if host:
            entry['hostSessionId']=native or self.native
        path=self.registry/f'{pid}.json'
        path.write_text(json.dumps(entry))
        (self.registry/f'{pid}.test.key').write_text(json.dumps({'procStartFt':'123','pidDomain':'test','peerToken':'fake-test-token'}))
        return path

    def current(self):
        return self.store.sessions()[0]

    def test_local_host_id_falls_back_through_discovery_refresh_and_preflight(self):
        path=self.registry_entry(host=False)
        registry=json.loads(path.read_text())
        registry['hostSessionId']='local_17b203ea-fcf1-4a09-8527-2d5000fe3b66'
        path.write_text(json.dumps(registry))
        self.assertEqual(adapters.discover_claude()[0]['sessionId'],self.native)
        self.agent.recover_endpoints()
        current=self.current()
        self.assertEqual(current['generation'],self.session['generation'])
        self.assertEqual(self.agent.endpoint_health[current['id']]['state'],'ready')
        self.assertIsNotNone(self.store.authenticate_client('test-token'))
        self.assertEqual(adapters.claude_preflight(current)[0],registry['messagingSocketPath'])

    def test_invalid_host_values_fall_back_and_valid_host_is_canonical(self):
        for value in (None,'',123,{},'local_'+self.native,'invalid'):
            with self.subTest(value=value):
                self.assertEqual(adapters.claude_session_id({'hostSessionId':value,'sessionId':self.native}),self.native)
        self.assertEqual(adapters.claude_session_id({'hostSessionId':self.native.upper(),'sessionId':str(uuid.uuid4())}),self.native)

    def test_no_valid_uuid_is_not_discovered(self):
        path=self.registry_entry(host=False)
        registry=json.loads(path.read_text())
        registry.update(hostSessionId='local_'+self.native,sessionId='invalid')
        path.write_text(json.dumps(registry))
        self.assertEqual(adapters.discover_claude(),[])
        self.agent.recover_endpoints()
        self.assertEqual(self.current()['metadata'],self.session['metadata'])

    def test_closed_then_reopened_preserves_identity_and_client(self):
        self.agent.recover_endpoints()
        self.assertEqual(self.agent.endpoint_health[self.session['id']]['state'],'unavailable')
        self.registry_entry()
        self.agent.recover_endpoints()
        current=self.current()
        self.assertEqual(json.loads(current['metadata'])['pid'],2)
        for key in ('id','native_id','generation','alias','project'):
            self.assertEqual(current[key],self.session[key])
        self.assertIsNotNone(self.store.authenticate_client('test-token'))
        self.assertEqual(self.agent.endpoint_health[self.session['id']]['state'],'ready')
        # A second close/reopen changes the PID, not the conversation binding.
        (self.registry/'2.json').unlink()
        self.registry_entry(pid=3)
        self.agent.recover_endpoints()
        self.assertEqual(json.loads(self.current()['metadata'])['pid'],3)

    def test_other_uuid_never_replaces_binding(self):
        self.registry_entry(native=str(uuid.uuid4()))
        self.agent.recover_endpoints()
        self.assertEqual(self.current()['metadata'],self.session['metadata'])

    def test_duplicate_live_uuid_does_not_guess_latest(self):
        self.registry_entry(pid=2)
        self.registry_entry(pid=3)
        self.agent.recover_endpoints()
        self.assertEqual(self.current()['metadata'],self.session['metadata'])

    def test_unqualified_version_and_reused_pid_are_rejected(self):
        p=self.registry_entry(version='99.0.0')
        self.agent.recover_endpoints()
        self.assertEqual(self.current()['metadata'],self.session['metadata'])
        p.unlink()
        self.registry_entry()
        self.process.return_value='999'
        with patch('bridge.adapters.process_start',return_value='999'):
            self.agent.recover_endpoints()
        self.assertEqual(self.current()['metadata'],self.session['metadata'])

    def test_legacy_registry_and_bad_key(self):
        self.registry_entry(version='2.1.321',host=False)
        (self.registry/'2.test.key').unlink()
        self.agent.recover_endpoints()
        self.assertEqual(self.current()['metadata'],self.session['metadata'])
        self.registry_entry(version='2.1.321',host=False)
        self.agent.recover_endpoints()
        self.assertEqual(self.agent.endpoint_health[self.session['id']]['state'],'ready')

    def test_disabled_binding_not_refreshed(self):
        self.registry_entry()
        self.store.unbind(self.session['id'])
        self.agent.recover_endpoints()
        self.assertEqual(self.agent.endpoint_health,{})
        self.assertEqual(self.current()['metadata'],self.session['metadata'])

    def test_unbind_removes_capability_and_rebind_does_not_restore_it(self):
        self.store.unbind(self.session['id'])
        self.assertIsNone(self.store.authenticate_client('test-token'))
        with self.store.connect() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM clients').fetchone()[0],0)
        self.store.bind('claude',self.native,'claude2','echo',{'registry':'old.json','pid':1})
        self.assertIsNone(self.store.authenticate_client('test-token'))

    def test_retry_excludes_expired_stale_and_uncertain(self):
        self.registry_entry()
        for name,expiry,generation,state in (
            ('valid',time.time()+100,self.session['generation'],'blocked'),
            ('expired',time.time()-1,self.session['generation'],'blocked'),
            ('stale',time.time()+100,'other','blocked'),
            ('uncertain',time.time()+100,self.session['generation'],'delivery_uncertain'),
            ('submitted',time.time()+100,self.session['generation'],'submitted')):
            m={'id':name,'receiver':'node:'+self.session['id'],'receiver_gen':generation,
               'project':'echo','expires':expiry,'kind':'request','sender':'other',
               'parent_id':None,'hop':0,'body':'echo','network':True}
            with self.store.connect() as c:
                c.execute('INSERT INTO net_jobs(id,envelope,state) VALUES(?,?,?)',(name,json.dumps(m),state))
                self.agent._phase(c,name,state,{'reason':'test'})
        self.agent.recover_endpoints()
        jobs={j['id']:j['state'] for j in self.agent.status()['jobs']}
        self.assertEqual(jobs['valid'],'retry_pending')
        self.assertEqual(jobs['expired'],'blocked')
        self.assertEqual(jobs['stale'],'blocked')
        self.assertEqual(jobs['uncertain'],'delivery_uncertain')
        self.assertEqual(jobs['submitted'],'submitted')
        self.agent.recover_endpoints()
        with self.store.connect() as c:
            self.assertEqual(c.execute("SELECT count(*) FROM net_ops WHERE op='retry_preflight'").fetchone()[0],1)


if __name__=='__main__':
    unittest.main()
