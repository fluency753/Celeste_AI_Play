import copy
from contextlib import nullcontext, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from orchestrator import controller_restart as restart
from orchestrator.control import RunControl
from orchestrator.session_store import write_json

HTTP_CONTROL = restart.control

class RestartTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.old = self.root/'runs/agent/old'
        self.host = self.root/'local/background/old'
        self.journal = self.root/'local/controller-restart.json'
        self.state = {'agent':'agent','save':'save','save_slot':3,'session_id':'same-thread',
                      'engine':'codex','provider':'codex','model':'model','runtime':'host',
                      'record':True,'bridge_port':48984,'mcp_port':48980,
                      'controller_host':str(self.host),'controller_pid':100}
        write_json(self.old/'run_state.json', self.state)
        write_json(self.host/'status.json', {'state':'running','child_pid':100,'host_pid':101})
        write_json(self.host/'launch.json', {'arguments':['--agent','agent','--save','save']})
        self.manifest = self.old/'recording/session.json'
        write_json(self.manifest, {'version':1,'id':'recording-id','state':'recording',
                                   'options':{'audio_source':'game_nrt','dir':str(self.manifest.parent)}})
        self.link = {'id':'recording-id','manifest':str(self.manifest)}
        write_json(self.old/'recording_session.json', self.link)
        RunControl(self.old)
        self.snapshot = {'run':str(self.old),'control':{'connected':True,'paused':True,'phase':'idle',
                         'interrupt_control':True,'session_id':'same-thread'}}
        self.bridge = {'running':True,'active':True,'frozen':True,'frame':500,
                       'obs_plugin':True,'lockstep':True,'recording_audio':True,'version':'1.8.1'}
        self.calls = []
        self.launches = []
        for target, value in [('orchestrator.controller_restart.REPO_ROOT',self.root),
                              ('orchestrator.launch_options.REPO_ROOT',self.root)]:
            patcher=patch(target,value);patcher.start();self.addCleanup(patcher.stop)
        for target, value in [('orchestrator.controller_restart.control',self.control),
                              ('orchestrator.controller_restart.launch',self.launch),
                              ('overlay.modhud.bridge_status',lambda port:copy.deepcopy(self.bridge)),
                              ('orchestrator.controller_restart.controller_lease',lambda:nullcontext()),
                              ('orchestrator.controller_restart.GameProcess',self.process)]:
            patcher=patch(target,value);patcher.start();self.addCleanup(patcher.stop)

    def process(self, pid):
        error=OSError('process gone');error.winerror=87
        raise error

    def control(self, port, payload=None):
        if payload is not None:
            self.calls.append(payload)
            self.assertEqual(payload['run'],str(self.old))
            ident=RunControl(self.old).enqueue(payload['kind'],payload['text'])
            RunControl(self.old).mark(ident,'submitted','已中断')
            write_json(self.old/'summary.json',{'game_released':False,
                       'recording':{'state':'retained','id':'recording-id'}})
            write_json(self.host/'status.json',{'state':'exited','exit_code':1})
            self.snapshot['control']['connected']=False
            return {'id':ident}
        return copy.deepcopy(self.snapshot)

    def launch(self, args, *, directory):
        self.launches.append(args)
        write_json(directory/'status.json',{'state':'running','child_pid':200,'host_pid':201})
        self.new=self.root/'runs/agent/new'
        write_json(self.new/'run_state.json',{**self.state,'controller_host':str(directory),
                   'controller_pid':200,'resumed_from':str(self.old),
                   'bridge_port':int(args[args.index('--bridge-port')+1])})
        if self.state['record']:
            write_json(self.new/'recording_session.json',self.link)
        self.snapshot={'run':str(self.new),'control':{'connected':True,'paused':True,'phase':'idle',
                       'interrupt_control':True,'session_id':'same-thread'}}
        return directory, {'state':'running','child_pid':200,'host_pid':201}

    def execute(self, **kwargs):
        return restart.restart(port=48992,timeout=1,record_path=self.journal,**kwargs)

    def test_complete_chain_preserves_session_recording_ports_and_pause(self):
        result=self.execute()
        self.assertEqual(result['phase'],'finished')
        self.assertEqual(result['after']['recording']['id'],'recording-id')
        self.assertTrue(result['after']['paused'])
        self.assertEqual(len(self.calls),1)
        self.assertEqual(len(self.launches),1)
        args=self.launches[0]
        for flag in ('--start-paused','--keep-obs-open','--bridge-port','--mcp-port'):
            self.assertIn(flag,args)
        self.assertNotIn('--new-recording',args)
        self.assertNotIn('--fresh-session',args)
        self.assertEqual(args[args.index('--bridge-port')+1],'48984')
        self.assertEqual(args[args.index('--mcp-port')+1],'48980')

    def test_check_only_reads_and_does_not_create_journal_or_dispatch(self):
        before=(self.old/'control.sqlite3').read_bytes()
        result=restart.inspect(48992)
        self.assertEqual(result['bridge']['frame'],500)
        self.assertEqual((self.old/'control.sqlite3').read_bytes(),before)
        self.assertFalse(self.journal.exists())
        self.assertEqual(self.calls+self.launches,[])

    def test_preflight_refuses_running_menu_stale_audio_or_identity_change(self):
        for field,value in [('paused',False),('phase','executing'),('interrupt_control',False)]:
            original=copy.deepcopy(self.snapshot)
            self.snapshot['control'][field]=value
            with self.assertRaises(ValueError):self.execute()
            self.snapshot=original
        for field in ('running','frozen','active','obs_plugin','recording_audio'):
            self.bridge[field]=False
            with self.assertRaises(ValueError):self.execute()
            self.bridge[field]=True
        self.snapshot['control']['session_id']='other'
        with self.assertRaisesRegex(ValueError,'身份不一致'):self.execute()
        self.assertEqual(self.calls+self.launches,[])

    def test_launch_ack_loss_retry_observes_same_host_without_relaunch(self):
        original=self.launch
        def lost(args,**kwargs):
            original(args,**kwargs)
            raise OSError('WMI ACK lost')
        with patch.object(restart,'launch',lost),self.assertRaises(OSError):self.execute()
        self.assertEqual(restart.read(self.journal)['phase'],'launching')
        result=self.execute(retry=True)
        self.assertEqual(result['phase'],'finished')
        self.assertEqual(len(self.calls),1)
        self.assertEqual(len(self.launches),1)

    def test_repeat_finished_retry_refreshes_current_state_without_restart(self):
        self.execute()
        self.snapshot['control']['paused']=False
        result=self.execute(retry=True)
        self.assertFalse(result['after']['paused'])
        self.assertEqual(len(self.launches),1)

    def test_new_recording_identity_or_frame_change_is_not_success(self):
        original=self.launch
        def changed(args,**kwargs):
            result=original(args,**kwargs)
            write_json(self.new/'recording_session.json',{'id':'wrong','manifest':str(self.manifest)})
            return result
        with patch.object(restart,'launch',changed),self.assertRaisesRegex(RuntimeError,'录制会话身份'):
            self.execute()
        self.assertEqual(restart.read(self.journal)['phase'],'failed')

    def test_actual_interrupt_error_is_reported_not_hidden_as_timeout(self):
        original=self.control
        def reject(port,payload=None):
            result=original(port,payload)
            if payload:
                RunControl(self.old).mark(result['id'],'error','执行器拒绝中断')
            return result
        with patch.object(restart,'control',reject),self.assertRaisesRegex(ValueError,'执行器拒绝中断'):
            self.execute()
        self.assertEqual(self.launches,[])

    def test_same_session_wrong_host_is_not_accepted(self):
        original=self.launch
        def wrong(args,**kwargs):
            result=original(args,**kwargs)
            state=restart.read(self.new/'run_state.json');state['controller_pid']=999
            write_json(self.new/'run_state.json',state)
            return result
        with patch.object(restart,'launch',wrong),self.assertRaisesRegex(ValueError,'其他宿主'):
            self.execute()

    def test_old_schema_needs_explicit_host_and_port_and_no_recording_is_supported(self):
        self.state.pop('controller_host');self.state.pop('controller_pid');self.state.pop('bridge_port')
        self.state['record']=False
        write_json(self.old/'run_state.json',self.state)
        (self.old/'recording_session.json').unlink()
        with self.assertRaises(ValueError):self.execute()
        # A preflight refusal has not written or interrupted anything.
        self.assertFalse(self.journal.exists())
        result=self.execute(host=str(self.host),bridge_port=48984)
        self.assertIsNone(result['after']['recording'])

    def test_existing_unfinished_operation_does_not_dispatch_again(self):
        write_json(self.journal,{'phase':'waiting_ready'})
        with self.assertRaisesRegex(ValueError,'--retry'):self.execute()
        self.assertEqual(self.calls+self.launches,[])

    def test_dead_host_finishes_failed_attempt_without_blocking_future_update(self):
        original=self.launch
        def failed(args,**kwargs):
            result=original(args,**kwargs)
            write_json(kwargs['directory']/'status.json',{'state':'failed','exit_code':1})
            return result
        with patch.object(restart,'launch',failed),self.assertRaisesRegex(RuntimeError,'新主控已退出'):
            self.execute()
        self.assertEqual(restart.read(self.journal)['phase'],'failed')
        with self.assertRaisesRegex(RuntimeError,'本次交接已结束'):self.execute(retry=True)
        # A future invocation gets fresh preflight, rather than an eternal retry lock.
        self.snapshot['control']['paused']=False
        with self.assertRaisesRegex(ValueError,'先在 HUD 暂停'):self.execute()
        self.assertEqual(len(self.launches),1)

    def test_frame_change_is_reported_and_not_marked_finished(self):
        original=self.launch
        def moved(args,**kwargs):
            result=original(args,**kwargs);self.bridge['frame']+=1
            return result
        with patch.object(restart,'launch',moved),self.assertRaisesRegex(ValueError,'帧号发生变化'):
            self.execute()
        self.assertEqual(restart.read(self.journal)['phase'],'failed')

    def test_interrupt_ack_loss_uses_recorded_action_on_retry(self):
        original=self.control
        def lost(port,payload=None):
            result=original(port,payload)
            if payload:raise OSError('HTTP ACK lost')
            return result
        with patch.object(restart,'control',lost),self.assertRaises(OSError):self.execute()
        self.assertEqual(self.execute(retry=True)['phase'],'finished')
        self.assertEqual(len(self.calls),1)

    def test_partial_run_state_and_mutex_release_are_waited_for(self):
        original=self.control
        pending=[]
        original_launch=self.launch
        def starting(args,**kwargs):
            result=original_launch(args,**kwargs)
            pending.append((self.new/'run_state.json').read_text())
            (self.new/'run_state.json').write_text('{')
            return result
        def update(port,payload=None):
            result=original(port,payload)
            if pending:
                text=pending.pop()
                timer=threading.Timer(.05,lambda:(self.new/'run_state.json').write_text(text))
                timer.start();self.addCleanup(timer.join)
            return result
        checks=[]
        def lease():
            checks.append(1)
            if len(checks)==1:raise RuntimeError('busy')
            return nullcontext()
        with patch.object(restart,'control',update),patch.object(restart,'launch',starting),patch.object(restart,'controller_lease',lease):
            self.assertEqual(self.execute()['phase'],'finished')
        self.assertGreaterEqual(len(checks),2)

    def test_cli_restart_over_real_http_with_isolated_external_processes(self):
        from tools.restart_controller import main
        fixture=self
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):self.respond(None)
            def do_POST(self):self.respond(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
            def respond(self,payload):
                result=fixture.control(self.server.server_port,payload)
                body=json.dumps(result).encode()
                self.send_response(200);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
            def log_message(self,*args):pass
        with ThreadingHTTPServer(('127.0.0.1',0),Handler) as server:
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            try:
                output=io.StringIO()
                with patch.object(restart,'control',HTTP_CONTROL),patch('sys.argv',['restart_controller.py','restart','--hud-port',str(server.server_port),'--json']),redirect_stdout(output):
                    self.assertEqual(main(),0)
                received=json.loads(output.getvalue())
                self.assertEqual(received['session']['session_id'],'same-thread')
                self.assertEqual(received['recording']['id'],'recording-id')
                self.assertTrue(received['paused'] and received['bridge']['frozen'])
                self.assertEqual(len(self.calls),1)
                self.assertEqual(len(self.launches),1)
            finally:
                server.shutdown();thread.join()
