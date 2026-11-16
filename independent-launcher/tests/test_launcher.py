import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('independent_launcher',ROOT/'launcher.py')
launcher=importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)
AGENT=Path(os.environ.get('AGENTBRIDGE_TEST_ROOT',ROOT.parent/'agentbridge')).resolve()
sys.path.insert(0,str(AGENT))
from bridge.cli import call
from bridge.hub import create_tls


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        self.install=self.root/'install'
        self.install.mkdir()
        shutil.copytree(AGENT/'bridge',self.install/'bridge',ignore=shutil.ignore_patterns('__pycache__'))
        shutil.copy2(AGENT/'agentbridge.py',self.install/'agentbridge.py')
        self.state=self.root/'state'
        self.state.mkdir()
        create_tls(self.root,['127.0.0.1'])
        (self.state/'network.json').write_text(json.dumps({'hub_url':'wss://127.0.0.1:1','node_id':'test','label':'test',
            'token':'synthetic','projects':['test'],'hub_certificate':(self.root/'hub-cert.pem').read_text()}))
        self.config={'install':self.install,'state':self.state,'python':Path(sys.executable)}
        self.config_file=self.root/'profile.json'
        self.config_file.write_text(json.dumps({k:str(v) for k,v in self.config.items()}))

    def tearDown(self):
        try:
            call(self.state,'/stop',{})
            deadline=time.monotonic()+12
            while time.monotonic()<deadline:
                held=launcher.take_lock(self.state/'agent.lock')
                if held:
                    launcher.unlock(held)
                    break
                time.sleep(.1)
        except Exception:
            pass
        self.temp.cleanup()

    def command(self):
        return [sys.executable,str(ROOT/'launcher.py'),'--config',str(self.config_file)]

    def test_sequential_click_preserves_pid_and_logs(self):
        (self.state/'Agent.stderr.log').write_text('existing-log-marker\n')
        first=launcher.launch(self.config)
        second=launcher.launch(self.config)
        self.assertEqual(first['action'],'started')
        self.assertEqual(second['action'],'already_running')
        self.assertEqual(first['pid'],second['pid'])
        self.assertEqual(first['independent_lifetime_verified'],not first['windows_job_member'])
        self.assertIn('existing-log-marker',(self.state/'Agent.stderr.log').read_text())

    def test_parallel_clicks_and_launcher_exit_leave_one_independent_agent(self):
        processes=[subprocess.Popen(self.command(),stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,
                   creationflags=subprocess.CREATE_NO_WINDOW) for _ in range(3)]
        results=[]
        collected=[]
        for process in processes:
            output,error=process.communicate(timeout=45)
            collected.append((process.returncode,output,error))
        for code,output,error in collected:
            self.assertEqual(code,0,output+error)
            results.append(json.loads(output)['result'])
        self.assertEqual(len({r['pid'] for r in results}),1)
        self.assertEqual(sum(r['action']=='started' for r in results),1)
        time.sleep(.5)
        self.assertEqual(launcher.health(self.state)['pid'],results[0]['pid'])
        self.assertEqual(launcher.in_job(results[0]['pid']),results[0]['windows_job_member'])

    def test_status_does_not_start_anything(self):
        with self.assertRaises(launcher.LaunchError):
            launcher.launch(self.config,status_only=True)
        self.assertFalse((self.state/'Agent.stdout.log').exists())

    def test_held_agent_lock_never_starts_duplicate(self):
        held=launcher.take_lock(self.state/'agent.lock')
        try:
            with patch.object(launcher.subprocess,'Popen') as popen:
                with self.assertRaisesRegex(launcher.LaunchError,'owns its state lock'):
                    launcher.launch(self.config)
                popen.assert_not_called()
        finally:
            launcher.unlock(held)

    def test_denied_breakaway_has_no_fallback(self):
        denied=OSError('denied'); denied.winerror=5
        probe=subprocess.CompletedProcess([],0,stdout=json.dumps({'exe':sys.executable,'path':sys.path}))
        with patch.object(launcher.subprocess,'run',return_value=probe), \
             patch.object(launcher.subprocess,'Popen',side_effect=denied) as popen:
            with self.assertRaisesRegex(launcher.LaunchError,'no fallback'):
                launcher.launch(self.config)
            self.assertEqual(popen.call_count,1)
            self.assertTrue(popen.call_args.kwargs['creationflags'] & 0x01000000)

    def test_existing_job_bound_agent_is_not_restarted(self):
        with patch.object(launcher,'health',return_value={'pid':123,'hub_connected':True}), \
             patch.object(launcher,'in_job',return_value=True), \
             patch.object(launcher.subprocess,'Popen') as popen:
            result=launcher.launch(self.config)
            self.assertFalse(result['independent_lifetime_verified'])
            self.assertEqual(result['action'],'already_running')
            popen.assert_not_called()

    def test_stale_client_file_does_not_prevent_restart(self):
        (self.state/'client.json').write_text(json.dumps({'url':'http://127.0.0.1:1','token':'synthetic'}))
        result=launcher.launch(self.config)
        self.assertEqual(result['action'],'started')


if __name__=='__main__':
    unittest.main()
