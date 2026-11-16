"""Execute the real PowerShell updater against an isolated legacy installation."""
import ast
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time
import unittest

from bridge.cli import call
from bridge.hub import HubStore, create_tls, serve_hub


@unittest.skipUnless(os.name=='nt','Windows installer')
class UpgradeTests(unittest.TestCase):
    def test_legacy_install_check_upgrade_and_real_endpoint_refresh(self):
        source=Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            install=root/'legacy'
            install.mkdir()
            # Same layout as v3, with its missing Store API explicitly reproduced.
            for name in ('bridge','runtime'):
                shutil.copytree(source/name,install/name,ignore=shutil.ignore_patterns('__pycache__'))
            for name in ('agentbridge.py','start-m1.ps1'):
                shutil.copy2(source/name,install/name)
            store_file=install/'bridge/store.py'
            old=store_file.read_text()
            tree=ast.parse(old)
            cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='Store')
            method=next(n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name=='refresh_endpoint')
            lines=old.splitlines(keepends=True)
            del lines[method.lineno-1:method.end_lineno]
            store_file.write_text(''.join(lines))
            state=root/'state'
            state.mkdir()
            create_tls(root,['127.0.0.1'])
            hub=HubStore(root/'hub.sqlite')
            ready=threading.Event()
            servers=[]
            def on_ready(server):
                servers.append(server)
                ready.set()
            thread=threading.Thread(target=serve_hub,args=(hub,'127.0.0.1',0,root/'hub-cert.pem',root/'hub-key.pem',on_ready),daemon=True)
            thread.start()
            self.assertTrue(ready.wait(5))
            node=hub.add_node('upgrade-test',['echo'])
            config=node|{'hub_url':f'wss://127.0.0.1:{servers[0].socket.getsockname()[1]}',
                         'hub_certificate':(root/'hub-cert.pem').read_text()}
            (state/'network.json').write_text(json.dumps(config))
            python=install/'runtime/python.exe'
            flags=subprocess.CREATE_NO_WINDOW
            log=(root/'first.stderr').open('w')
            process=subprocess.Popen([str(python),str(install/'agentbridge.py'),'--state',str(state),'agent','--hub-config',str(state/'network.json')],stdout=subprocess.DEVNULL,stderr=log,creationflags=flags)
            try:
                deadline=time.monotonic()+12
                while True:
                    try:
                        if call(state,'/health',{}).get('hub_connected'):
                            break
                    except Exception:
                        pass
                    if time.monotonic()>deadline:
                        self.fail('isolated legacy Agent failed to start')
                    time.sleep(.1)
                command=['powershell.exe','-NoProfile','-ExecutionPolicy','Bypass','-File',str(source/'upgrade-recovery.ps1'),
                         '-InstallDirectory',str(install),'-State',str(state)]
                def run_updater(arguments,label):
                    # Start-Process can pass pipe handles to the background Agent;
                    # use files so communicate() does not await the long-lived child.
                    output=root/(label+'.txt')
                    with output.open('w') as stream:
                        result=subprocess.run(arguments,stdout=stream,stderr=stream,timeout=80,creationflags=flags)
                    return result.returncode,output.read_text()
                before=store_file.read_bytes()
                checked,check_output=run_updater(command+['-CheckOnly'],'check')
                self.assertEqual(checked,0,check_output)
                self.assertIn('refresh_endpoint',check_output)
                self.assertEqual(store_file.read_bytes(),before)
                self.assertIsNone(process.poll())
                updated,update_output=run_updater(command,'upgrade')
                self.assertEqual(updated,0,update_output)
                self.assertEqual(store_file.read_bytes(),(source/'bridge/store.py').read_bytes())
                self.assertEqual(call(state,'/health',{})['recovery_interval_seconds'],5)
                # Use the installed modules and an actual live process identity,
                # synthetic registry/key only; never write to a real native pipe.
                probe=r'''
import json, os, sys, threading, uuid
from pathlib import Path
from bridge import adapters
from bridge.network import RemoteAgent
from bridge.store import Store
root=Path(sys.argv[1]); registry=root/'registry'; registry.mkdir()
native=str(uuid.uuid4()); pid=os.getpid(); start=adapters.process_start(pid)
entry={'sessionId':native,'hostSessionId':'local_'+str(uuid.uuid4()),'pid':pid,'procStart':start,'version':'2.1.325','pidDomain':'test','messagingSocketPath':r'\\.\pipe\LOCAL\cc-msg-test'}
(registry/f'{pid}.json').write_text(json.dumps(entry))
(registry/f'{pid}.test.key').write_text(json.dumps({'procStartFt':start,'pidDomain':'test','peerToken':'synthetic'}))
store=Store(root/'probe.sqlite'); s=store.bind('claude',native,'claude2','echo',{'pid':0,'registry':'missing'})
original=adapters.discover_claude; adapters.discover_claude=lambda:original(registry)
agent=RemoteAgent(store,root,root/'state/network.json',threading.Event(),{})
agent.recover_endpoints()
current=store.sessions()[0]
assert json.loads(current['metadata'])['pid']==pid
assert current['generation']==s['generation']
assert agent.endpoint_health[s['id']]['state']=='ready'
print('installed Store endpoint refresh passed')
'''
                result=subprocess.run([str(python),'-c',probe,str(root)],capture_output=True,text=True,timeout=15,creationflags=flags)
                self.assertEqual(result.returncode,0,result.stdout+result.stderr)
                call(state,'/stop',{})
                deadline=time.monotonic()+12
                while time.monotonic()<deadline:
                    stderr=(state/'Agent.stderr.log').read_text() if (state/'Agent.stderr.log').exists() else ''
                    if 'agent_stopped' in stderr:
                        break
                    time.sleep(.1)
                for event in ('agent_started','agent_heartbeat','stop_requested','agent_stopped'):
                    self.assertIn(event,stderr)
            finally:
                try:
                    call(state,'/stop',{})
                except Exception:
                    pass
                if process.poll() is None:
                    process.terminate()
                process.wait(timeout=10)
                log.close()
                servers[0].shutdown()
                thread.join(5)


if __name__=='__main__':
    unittest.main()
