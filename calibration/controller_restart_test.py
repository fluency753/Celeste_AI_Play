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

    def test_map_handoff_requires_closed_prefix_accounting_and_allows_menu_frames(self):
        self.bridge.update(frozen=False,scene='Overworld')
        original_control=self.control;original_launch=self.launch
        def closed(port,payload=None):
            result=original_control(port,payload)
            if payload:
                write_json(self.old/'summary.json',{'game_released':False,'recording':{
                    'state':'finishing','id':'recording-id','postprocess_pending':True,
                    'encoder_finished':True,'capture_state':'finished','frames':800}})
            return result
        def recovered(args,**kwargs):
            result=original_launch(args,**kwargs)
            self.bridge['frame']+=300
            return result
        with patch.object(restart,'control',closed),patch.object(restart,'launch',recovered),patch('orchestrator.recording_recovery.prefix_counts',return_value={'frames':800,'replays':0}):
            result=self.execute()
        self.assertEqual(result['phase'],'finished')
        self.assertEqual(result['closed_frames'],800)
        self.assertTrue(result['after']['paused'])

    def test_map_prefix_mismatch_does_not_claim_success(self):
        self.bridge.update(frozen=False,scene='Overworld')
        original=self.control
        def closed(port,payload=None):
            result=original(port,payload)
            if payload:
                write_json(self.old/'summary.json',{'game_released':False,'recording':{
                    'state':'finishing','id':'recording-id','postprocess_pending':True,
                    'encoder_finished':True,'capture_state':'finished','frames':800}})
            return result
        with patch.object(restart,'control',closed),self.assertRaisesRegex(ValueError,'恢复前缀'):
            self.execute()

    def test_venv_launcher_parent_is_verified_and_actual_process_is_waited(self):
        self.state['controller_pid']=102;self.state['controller_parent_pid']=100
        write_json(self.old/'run_state.json',self.state)
        original=self.launch
        def wrapped(args,**kwargs):
            result=original(args,**kwargs)
            state=restart.read(self.new/'run_state.json')
            state.update(controller_pid=202,controller_parent_pid=200)
            write_json(self.new/'run_state.json',state)
            return result
        with patch.object(restart,'launch',wrapped):
            result=self.execute()
        self.assertIn(102,result['old_pids'])
        self.assertEqual(result['after']['pid'],202)

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

    def test_frozen_level_cannot_finish_on_unfrozen_map_at_same_frame(self):
        original=self.launch
        def changed(args,**kwargs):
            result=original(args,**kwargs)
            self.bridge.update(frozen=False,scene='Overworld')
            return result
        with patch.object(restart,'launch',changed),self.assertRaisesRegex(ValueError,'未声称零推进'):
            self.execute()

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

    # ---- review fixes: recoverable launch failures, CLI controllers, read-only check ----

    def test_rejected_launch_retries_in_fresh_host_without_second_interrupt(self):
        from orchestrator.background import LaunchRejected
        original=self.launch
        attempts=[]
        def flaky(args,*,directory):
            attempts.append(directory)
            if len(attempts)==1:
                # launch() creates the host directory before Windows rejects the process.
                write_json(directory/'launch.json',{'arguments':args})
                raise LaunchRejected('ReturnValue 9')
            return original(args,directory=directory)
        with patch.object(restart,'launch',flaky):
            with self.assertRaisesRegex(RuntimeError,'--retry'):self.execute()
            self.assertEqual(restart.read(self.journal)['phase'],'launching')
            result=self.execute(retry=True)
        self.assertEqual(result['phase'],'finished')
        self.assertEqual(len(self.calls),1)
        self.assertEqual(len(self.launches),1)
        self.assertNotEqual(attempts[0],attempts[1])
        self.assertEqual(result['failed_hosts'][0]['host'],str(attempts[0].resolve()))

    def test_unconfirmed_launch_relaunches_only_without_host_evidence(self):
        import os
        import time
        original=self.launch
        attempts=[]
        def lost(args,*,directory):
            attempts.append(directory)
            if len(attempts)==1:
                write_json(directory/'launch.json',{'arguments':args})
                raise TimeoutError('powershell timed out')
            return original(args,directory=directory)
        with patch.object(restart,'launch',lost):
            with self.assertRaises(TimeoutError):self.execute()
            # Within the grace period the worker may still be starting.
            with self.assertRaises(TimeoutError):self.execute(retry=True)
            self.assertEqual(len(attempts),1)
            stale=time.time()-restart.LAUNCH_GRACE_S-5
            os.utime(attempts[0]/'launch.json',(stale,stale))
            def held():
                raise RuntimeError('主控仍在运行')
            # A held lease means a worker or controller is alive: never launch a second one.
            with patch.object(restart,'controller_lease',held),self.assertRaises(TimeoutError):
                self.execute(retry=True)
            self.assertEqual(len(attempts),1)
            result=self.execute(retry=True)
        self.assertEqual(result['phase'],'finished')
        self.assertEqual(len(attempts),2)
        self.assertEqual(len(self.calls),1)

    def test_superseded_unfinished_update_does_not_block_a_fresh_restart(self):
        stale=self.root/'local/background/stale'
        stale.mkdir(parents=True)
        write_json(self.journal,{'phase':'waiting_ready','token':'old','port':48992,'host':str(stale),
                                 'before':{'run':str(self.root/'runs/agent/older')}})
        result=self.execute()
        self.assertEqual(result['phase'],'finished')
        self.assertEqual(len(self.calls),1)
        self.assertEqual(len(self.launches),1)

    def test_unfinished_update_whose_host_runs_still_requires_retry(self):
        running=self.root/'local/background/running'
        write_json(running/'status.json',{'state':'running','child_pid':300,'host_pid':301})
        write_json(self.journal,{'phase':'waiting_ready','token':'old','port':48992,'host':str(running),
                                 'before':{'run':str(self.root/'runs/agent/older')}})
        with self.assertRaisesRegex(ValueError,'--retry'):self.execute()
        self.assertEqual(self.calls+self.launches,[])

    def test_cli_controller_is_refused_before_interrupt_even_with_host(self):
        write_json(self.old/'run_state.json',{**self.state,'controller_host':None})
        self.assertEqual(restart.inspect(48992)['controller'],'cli')
        with self.assertRaisesRegex(ValueError,'命令行'):self.execute(host=str(self.host))
        self.assertEqual(self.calls+self.launches,[])
        self.assertFalse(self.journal.exists())

    def test_check_reports_non_recording_states_without_raising(self):
        from tools.restart_controller import main
        write_json(self.manifest,{'version':1,'id':'recording-id','state':'finished',
                                  'options':{'audio_source':'game_nrt','dir':str(self.manifest.parent)}})
        status=restart.inspect(48992)
        self.assertEqual((status['recording']['id'],status['recording']['state']),('recording-id','finished'))
        output=io.StringIO()
        with patch('sys.argv',['restart_controller.py','check','--json']),redirect_stdout(output):
            self.assertEqual(main(),0)
        self.assertEqual(json.loads(output.getvalue())['recording']['state'],'finished')
        with self.assertRaisesRegex(ValueError,'不是 recording'):self.execute()
        self.assertEqual(self.calls+self.launches,[])
        self.assertFalse(self.journal.exists())
        write_json(self.old/'recording_session.json',{'id':'other','manifest':str(self.manifest)})
        self.assertEqual(restart.inspect(48992)['recording']['state'],'unreadable')
        (self.old/'recording_session.json').unlink()
        self.assertEqual(restart.inspect(48992)['recording'],{'state':'missing'})
