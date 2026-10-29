"""Lifecycle integration over a real stdio child, without a model or game."""
import asyncio
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from orchestrator.agent import ClaudeRunner
from orchestrator.chat import chat, can_wake
from orchestrator.config import RunConfig
from orchestrator.control import RunControl
from orchestrator.kb_study import KbStudy
from orchestrator.preparation import prepare
from orchestrator.prepared import load_preparation
from orchestrator.runtimes import runtime
from orchestrator import kb_access


STUB = '''import sys,json,uuid
def emit(v): print(json.dumps(v,ensure_ascii=False),flush=True)
for line in sys.stdin:
 p=json.loads(line)
 if p['type']=='control_request':
  emit({'type':'control_response','response':{'request_id':p['request_id'],'subtype':'success'}})
  continue
 text=p['message']['content']
 emit({'type':'system','subtype':'init','model':'claude-model-stub','tools':['Read'],'mcp_servers':[]})
 if '开始游玩之前' in text:
  for i,name in enumerate(['README.md','tools.md']):
   emit({'type':'assistant','message':{'id':str(uuid.uuid4()),'content':[{'type':'tool_use','id':str(i),'name':'Read','input':{'file_path':'/kb/'+name}}]}})
   emit({'type':'user','message':{'content':[{'type':'tool_result','tool_use_id':str(i)}]},'tool_use_result':{'file':{'filePath':'/kb/'+name,'startLine':1,'numLines':1,'totalLines':1}}})
  reply='准备完成'
 elif '请为玩家角色' in text: reply='North Star'
 else: reply='已收到提示，仍只聊天。'
 emit({'type':'assistant','message':{'id':str(uuid.uuid4()),'content':[{'type':'text','text':reply}]}})
 emit({'type':'result','subtype':'success','is_error':False,'result':reply,'usage':{'output_tokens':5}})
'''


class LifecycleIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_claude_preparation_chat_detach_wake_and_name_keep_same_session(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            kb = root / 'kb/explore'
            kb.mkdir(parents=True)
            for name in ('README.md','tools.md'):
                (kb/name).write_text('one\n', encoding='utf-8')
            helper = root/'orchestrator/workspace_files/show.py'
            helper.parent.mkdir(parents=True)
            helper.write_text('# test helper\n', encoding='utf-8')
            stub = root / 'agent_stub.py'
            stub.write_text(STUB, encoding='utf-8')
            directory = root / 'runs/claude-test/prepared'
            args = SimpleNamespace(engine='claude',provider='anthropic',agent='claude-test',model='opus',
                effort='high',knowledge='explore',max_minutes=1,output=str(directory),mcp_port=0,
                hud_managed=True,hud_port=0,quick_check=False,fast=None)
            commands=[]
            original_command = ClaudeRunner._command
            def command(runner):
                original_command(runner)  # Keep guide publication and prompt preparation; replace only the child executable.
                commands.append(runner._common_args())
                return [sys.executable,'-X','utf8','-u',str(stub)]
            with patch('orchestrator.config.REPO_ROOT',root), patch('orchestrator.preparation.REPO_ROOT',root), \
                 patch.object(kb_access, 'KB_ROOT', root/'kb'), \
                 patch('orchestrator.runtimes.Runtime.token',return_value='stub'), \
                 patch('orchestrator.preparation.ensure_container',return_value=None), \
                 patch('orchestrator.chat.ensure_container',return_value=None), \
                 patch.object(ClaudeRunner,'_command',command):
                self.assertEqual(await asyncio.wait_for(prepare(args),10),0)
                for name in ("show.py", "tas.py", "collision_matrix.py", "collision_matrix.md"):
                    self.assertTrue((root / "workspaces/claude-test-explore/game" / name).is_file(), name)
                self.assertFalse((root / "workspaces/claude-test-explore/game/collision_matrix.json").exists())
                self.assertTrue(can_wake(directory))
                proof = json.loads((directory/'prepare_summary.json').read_text(encoding='utf-8'))
                self.assertEqual(proof['engine'],'claude')
                self.assertTrue(proof['kb_study']['complete'])
                cfg = runtime('claude').configure(RunConfig(agent='claude-test',save='',model='opus',knowledge_profile='explore'), 'anthropic')
                load_preparation(str(directory),cfg,KbStudy(kb,1))
                other = runtime('codex').configure(RunConfig(agent='claude-test',save='',model='test',knowledge_profile='explore'))
                with self.assertRaisesRegex(ValueError,'执行器'):
                    load_preparation(str(directory),other,KbStudy(kb,1))
                other = runtime('claude').configure(RunConfig(agent='claude-test',save='',model='test',knowledge_profile='explore'),'deepseek')
                with self.assertRaisesRegex(ValueError,'提供方'):
                    load_preparation(str(directory),other,KbStudy(kb,1))

                state = {'engine':'claude','provider':'anthropic','agent':'claude-test','model':proof['metrics']['model'],
                         'requested_model':'opus','effort':'high','save':'','runtime':'container','mcp_port':0,
                         'knowledge_profile':'explore','session_kind':'ready','session_id':proof['session_id']}
                (directory/'run_state.json').write_text(json.dumps(state),encoding='utf-8')
                self.assertTrue(can_wake(directory))
                control = RunControl(directory)
                async with asyncio.timeout(10):
                    task = asyncio.create_task(chat(directory))
                    try:
                        while not control.snapshot()['connected']:
                            await asyncio.sleep(.02)
                        control.enqueue('message','解释一下工具')
                        while not any(m['role']=='assistant' for m in control.snapshot()['messages']):
                            await asyncio.sleep(.02)
                        control.enqueue('detach')
                        await task
                    finally:
                        if not task.done():
                            task.cancel()
                            await asyncio.gather(task,return_exceptions=True)
                self.assertFalse(control.snapshot()['connected'])
                await asyncio.wait_for(chat(directory,name_request='name-1'),10)
                result = json.loads((directory/'save_name_result.json').read_text(encoding='utf-8'))
                self.assertEqual(result,{'request_id':'name-1','name':'North Star'})
                self.assertEqual(json.loads((directory/'run_state.json').read_text())['session_id'],proof['session_id'])
                self.assertIn('--session-id', commands[0])
                for command_args in commands[1:]:
                    self.assertEqual(command_args[command_args.index('--resume')+1],proof['session_id'])
                    self.assertEqual(command_args[command_args.index('--model')+1],'opus')
                self.assertEqual(len(commands),3)
                self.assertFalse((directory/'recording').exists())
                self.assertFalse((root/'runs/latest.txt').exists())
                args.output = str(root/'runs/claude-test/timeout')
                args.max_minutes = 0
                self.assertEqual(await asyncio.wait_for(prepare(args),10),1)
                failed = json.loads((Path(args.output)/'prepare_summary.json').read_text(encoding='utf-8'))
                self.assertFalse(failed['success'])
                self.assertFalse(can_wake(Path(args.output)))
                with self.assertRaisesRegex(ValueError,'准备'):
                    load_preparation(args.output,cfg,KbStudy(kb,1))



class RuntimeCatalogTests(unittest.TestCase):
    def test_provider_efforts_are_preserved_without_inventing_missing_capabilities(self):
        provider = {'models':[{'id':'future', 'efforts':['minimal','ultra','adaptive-high','ultra'],
                               'default_effort':'adaptive-high'}, {'id':'unknown'}]}
        with patch('orchestrator.runtimes.load_provider',return_value=provider):
            data = runtime('claude').models('custom')
            self.assertEqual(data['models'][0]['efforts'], ['minimal','ultra','adaptive-high'])
            self.assertEqual(data['models'][0]['default_effort'], 'adaptive-high')
            self.assertEqual([m['id'] for m in data['models']], ['future'])
            self.assertEqual(runtime('claude').resolve('future','ultra','custom'), ('future','ultra'))
            with self.assertRaises(ValueError): runtime('claude').resolve('future','high','custom')

    def test_model_choices_are_per_provider_and_do_not_expose_credentials(self):
        provider = {'models':[{'id':'custom/model','efforts':['medium'],'token':'SECRET'}],
                    'api_key':'SECRET', 'key_file':'private-path'}
        with patch('orchestrator.runtimes.load_provider',return_value=provider), \
             patch('orchestrator.model_catalog.load_models') as codex:
            data = runtime('claude').models('custom')
            self.assertEqual(runtime('claude').resolve('custom/model','medium','custom'),('custom/model','medium'))
            with self.assertRaises(ValueError):
                runtime('claude').resolve('custom/model','high','custom')
        codex.assert_not_called()
        self.assertNotIn('SECRET',json.dumps(data))
        self.assertNotIn('private-path',json.dumps(data))

    def test_hud_model_endpoint_uses_selected_runtime(self):
        from http.server import ThreadingHTTPServer
        import threading
        import urllib.request
        from overlay.server import make_handler
        server = ThreadingHTTPServer(('127.0.0.1',0),make_handler(None))
        thread = threading.Thread(target=server.serve_forever,daemon=True)
        thread.start()
        try:
            with patch('orchestrator.model_catalog.load_models') as codex:
                url = f'http://127.0.0.1:{server.server_port}/api/models?engine=claude&provider=anthropic'
                with urllib.request.urlopen(url) as response:
                    data = json.load(response)
                self.assertIn('opus',[m['id'] for m in data['models']])
                self.assertEqual(data['source'],'provider_config')
                codex.assert_not_called()
        finally:
            server.shutdown()
            thread.join()
            server.server_close()


if __name__ == '__main__':
    unittest.main()
