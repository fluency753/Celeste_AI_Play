import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from orchestrator import session_environment as env


def state(**values):
    return dict(agent='test', save='test', save_slot=10, engine='codex', provider='codex',
                model='gpt-test', effort='high', session_id='old', runtime='host',
                knowledge_profile='explore', mode='explore', live_mode=True,
                sim=False, sim_assist=False, cutscene_mode='agent', **values)


def rollout(tools, ident='old'):
    return (json.dumps({'type':'session_meta','payload':{'id':ident,'dynamic_tools':tools,'extra':'keep'}}).encode()
            + b'\r\n' + b'{"type":"response_item","payload":{"text":"history"}}\r\n')


class SessionEnvironmentTests(unittest.TestCase):
    def test_patch_preserves_history_and_identity_and_is_idempotent(self):
        old=rollout([{'name':'celeste_act','description':'old'}])
        tools=[{'name':'celeste_act','description':'new'},{'name':'celeste_stop_run'}]
        new=env.patched_rollout(old,'old',tools)
        self.assertEqual(old.partition(b'\n')[2],new.partition(b'\n')[2])
        self.assertEqual(json.loads(new.split(b'\n')[0])['payload']['extra'],'keep')
        self.assertEqual(env.patched_rollout(new,'old',tools),new)

    def test_foreign_identity_tools_and_malformed_metadata_are_rejected(self):
        for data in [rollout([],ident='other'),rollout([{'name':'foreign_tool'}]),b'{}']:
            with self.assertRaises(ValueError):env.patched_rollout(data,'old',[])

    def test_atomic_replace_refuses_concurrent_history_changes(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'session.jsonl';path.write_bytes(b'changed')
            with self.assertRaisesRegex(ValueError,'发生变化'):env.replace_checked(path,b'old',b'new')
            self.assertEqual(path.read_bytes(),b'changed')
            env.replace_checked(path,b'changed',b'new')
            self.assertEqual(path.read_bytes(),b'new')
            self.assertEqual(list(Path(folder).iterdir()),[path])

    def test_config_preserves_user_choices_and_tuning(self):
        s=state(tuning={'explore_act_frames':120})
        cfg=env.configuration(s)
        self.assertEqual((cfg.save_slot,cfg.engine,cfg.model,cfg.effort),(10,'codex','gpt-test','high'))
        self.assertEqual((cfg.mode,cfg.live,cfg.cutscene_mode,cfg.explore_act_frames),('explore',True,'agent',120))
        self.assertFalse(cfg.sim_assist)

    def test_targets_deduplicate_and_reject_outside_repository(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            for name in ['20260101-010000','20260102-010000']:
                d=root/'runs'/'test'/name;d.mkdir(parents=True);(d/'run_state.json').write_text(json.dumps(state()))
            found=env.targets(root=root)
            self.assertEqual(len(found),1);self.assertEqual(found[0][0].name,'20260102-010000')
            with self.assertRaises(ValueError):env.targets([root/'outside'],root=root)

    def test_busy_workspace_is_skipped_without_accessing_session_or_container(self):
        with patch.object(env,'active_agents',return_value={'test'}),patch.object(env,'thread_metadata',new_callable=AsyncMock) as rpc:
            result=asyncio.run(env.synchronize(Path('unused'),state(),Path('unused')))
            self.assertEqual(result['status'],'busy');rpc.assert_not_awaited()

    def test_apply_rolls_back_when_resume_verification_fails(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);home=root/'home';d=home/'sessions';d.mkdir(parents=True)
            path=d/'session.jsonl';original=rollout([{'name':'celeste_act','description':'old'}]);path.write_bytes(original)
            tools=[{'name':'celeste_act','description':'new'}]
            rpc=AsyncMock(side_effect=[{'id':'old','path':str(path)},{'id':'old','path':str(path)},RuntimeError('verify failed')])
            with patch.dict('os.environ',{'CODEX_HOME':str(home)}),patch.object(env,'active_agents',return_value=set()), \
                 patch.object(env,'thread_metadata',rpc),patch.object(env,'tool_specs',AsyncMock(return_value=tools)):
                with self.assertRaisesRegex(RuntimeError,'verify failed'):
                    asyncio.run(env.synchronize(root,state(),root/'backup',apply=True,root=root))
            self.assertEqual(path.read_bytes(),original)
            self.assertEqual((root/'backup'/'old'/'session.jsonl').read_bytes(),original)

    def test_check_never_changes_tools_or_starts_container(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);home=root/'home';d=home/'sessions';d.mkdir(parents=True)
            path=d/'session.jsonl';original=rollout([]);path.write_bytes(original)
            with patch.dict('os.environ',{'CODEX_HOME':str(home)}),patch.object(env,'active_agents',return_value=set()), \
                 patch.object(env,'thread_metadata',AsyncMock(return_value={'id':'old','path':str(path)})), \
                 patch.object(env,'tool_specs',AsyncMock(return_value=[{'name':'celeste_act'}])), \
                 patch('orchestrator.container.ensure_container') as container:
                result=asyncio.run(env.synchronize(root,state(),root/'backup',root=root))
            self.assertEqual(result['status'],'outdated');self.assertEqual(path.read_bytes(),original);container.assert_not_called()

    def test_successful_resume_preserves_protocol_audit_records(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);home=root/'home';d=home/'sessions';d.mkdir(parents=True)
            path=d/'session.jsonl';original=rollout([]);path.write_bytes(original)
            tools=[{'name':'celeste_act'}]
            audit=b'{"type":"event_msg","payload":{"type":"thread_settings_applied"}}\n'
            async def rpc(*args,**kwargs):
                if kwargs.get('resume'):
                    with path.open('ab') as stream:stream.write(audit)
                return {'id':'old','path':str(path)}
            with patch.dict('os.environ',{'CODEX_HOME':str(home)}),patch.object(env,'active_agents',return_value=set()), \
                 patch.object(env,'thread_metadata',side_effect=rpc),patch.object(env,'tool_specs',AsyncMock(return_value=tools)):
                result=asyncio.run(env.synchronize(root,state(),root/'backup',apply=True,root=root))
            self.assertEqual(result['status'],'updated')
            self.assertEqual(path.read_bytes().partition(b'\n')[2],original.partition(b'\n')[2]+audit+audit)

    def test_archived_session_is_not_resumed_or_changed(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);home=root/'home';d=home/'archived_sessions';d.mkdir(parents=True)
            path=d/'session.jsonl';original=rollout([]);path.write_bytes(original)
            rpc=AsyncMock(return_value={'id':'old','path':str(path)})
            with patch.dict('os.environ',{'CODEX_HOME':str(home)}),patch.object(env,'active_agents',return_value=set()), \
                 patch.object(env,'thread_metadata',rpc),patch.object(env,'tool_specs',AsyncMock(return_value=[])):
                result=asyncio.run(env.synchronize(root,state(),root/'backup',apply=True,root=root))
            self.assertEqual(result['status'],'archived');self.assertEqual(rpc.await_count,1);self.assertEqual(path.read_bytes(),original)


if __name__=='__main__':unittest.main()
