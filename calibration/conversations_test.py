import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from orchestrator.control import RunControl
from orchestrator.conversations import catalog, prepare_target, release_chat, selected_run, switch_session


class ConversationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.anchor = self.make_run("current", "active", room="end", decisions=289)
        self.source = self.make_run("old", "older", room="11", decisions=100)

    def tearDown(self):
        self.temporary.cleanup()

    def make_run(self, name, ident, **metrics):
        directory = self.root / "runs" / "gpt-native" / name
        directory.mkdir(parents=True)
        (directory / "run_state.json").write_text(json.dumps({"engine":"codex", "agent":name, "session_id":ident,
            "model":"model", "effort":"high", "knowledge_profile":"explore"}))
        (directory / "summary.json").write_text(json.dumps({"completed":True}))
        (directory / "metrics.json").write_text(json.dumps({"context_tokens":1000, **metrics}))
        return directory

    def test_catalog_excludes_archived_and_deduplicates(self):
        archived = self.root / "archive"
        archived.mkdir()
        (archived / "rollout-older.jsonl").touch()
        self.make_run("duplicate", "active")
        result = catalog(self.anchor, self.root / "runs", archived)
        self.assertEqual([record["id"] for record in result], ["active"])

    def test_same_id_across_engines_does_not_collide_or_share_chat_history(self):
        from orchestrator.conversations import session_key
        state = json.loads((self.source/'run_state.json').read_text())
        state.update(engine='claude',provider='anthropic',session_id='active')
        (self.source/'run_state.json').write_text(json.dumps(state))
        records = catalog(self.anchor,self.root/'runs',self.root/'archive')
        self.assertEqual(len(records),2)
        self.assertEqual(len({r['key'] for r in records}),2)
        target = prepare_target(self.anchor,next(r for r in records if r['engine']=='claude'))
        selected = json.loads((target/'run_state.json').read_text())
        self.assertEqual(session_key(selected),'claude:anthropic:active')
        self.assertEqual(selected['provider'],'anthropic')
        self.assertNotEqual(target,self.anchor)

    def test_selection_cannot_escape_chat_directory(self):
        (self.anchor / "hud_session.json").write_text(json.dumps({"relative":"../old"}))
        self.assertEqual(selected_run(self.anchor), self.anchor)

    def test_switch_preserves_save_name_and_slot_as_one_identity(self):
        for folder,name,slot in [(self.anchor,'live',12),(self.source,'older',3)]:
            path=folder/'run_state.json';state=json.loads(path.read_text());state.update(save=name,save_slot=slot);path.write_text(json.dumps(state))
        target=prepare_target(self.anchor,{'directory':str(self.source),'id':'older'})
        state=json.loads((target/'run_state.json').read_text())
        self.assertEqual((state['save'],state['save_slot']),('older',3))

    def test_missing_source_slot_never_inherits_another_saves_slot(self):
        path=self.anchor/'run_state.json';state=json.loads(path.read_text());state['save_slot']=12;path.write_text(json.dumps(state))
        target=prepare_target(self.anchor,{'directory':str(self.source),'id':'older'})
        self.assertNotIn('save_slot',json.loads((target/'run_state.json').read_text()))

    def test_selected_configuration_is_authoritative_including_missing_fields(self):
        settings = {'sim_assist': True, 'sim': True, 'cutscene_mode': 'agent',
                    'action_permissions': {'restart_chapter': True}, 'tuning': {'min_frames_per_act': 80},
                    'kb_extra': 'old-only', 'kb_deny': ['old.md'], 'live_mode': True,
                    'stiffness': True, 'formal_thought': True, 'save_context': True,
                    'future_session_setting': 'must not leak'}
        anchor_path = self.anchor/'run_state.json'
        anchor_state = json.loads(anchor_path.read_text())
        anchor_state.update(settings)
        anchor_path.write_text(json.dumps(anchor_state))
        source_path = self.source/'run_state.json'
        source_state = json.loads(source_path.read_text())
        source_state.update(sim_assist=False, sim=False, cutscene_mode='auto',
                            session_kind='ready', prepared_dir=str(self.source),
                            action_permissions={'restart_chapter': False})
        source_path.write_text(json.dumps(source_state))
        target = prepare_target(self.anchor, {'directory':str(self.source), 'id':'older'})
        selected = json.loads((target/'run_state.json').read_text())
        for key, value in source_state.items():
            self.assertEqual(selected[key], value, key)
        for key in settings.keys() - source_state.keys():
            self.assertNotIn(key, selected, key)
        self.assertEqual(json.loads(anchor_path.read_text()), anchor_state)
        self.assertEqual(json.loads(source_path.read_text()), source_state)

    def test_selected_enabled_settings_are_not_replaced_by_anchor_defaults(self):
        path = self.source/'run_state.json'
        state = json.loads(path.read_text())
        state.update(sim_assist=True, cutscene_mode='agent', kb_deny=['source.md'],
                     tuning={'min_frames_per_act': 80})
        path.write_text(json.dumps(state))
        target = prepare_target(self.anchor, {'directory':str(self.source), 'id':'older'})
        selected = json.loads((target/'run_state.json').read_text())
        for key in ('sim_assist', 'cutscene_mode', 'kb_deny', 'tuning'):
            self.assertEqual(selected[key], state[key])

    def test_recovered_conversation_replaces_occupied_source_in_picker(self):
        target = prepare_target(self.anchor, {"directory":str(self.source), "id":"older"})
        state = json.loads((target / "run_state.json").read_text())
        state["session_id"] = "recovered"
        (target / "run_state.json").write_text(json.dumps(state))
        records = catalog(self.anchor, self.root / "runs", self.root / "archive")
        self.assertEqual({record["id"] for record in records}, {"active", "recovered"})

    def test_chat_history_and_preferences_are_isolated_while_scores_stay_current(self):
        old = RunControl(self.source)
        old.enqueue("message", "历史上尚未完成投递的提示")
        old.set_fast(False)
        old.set_effort('low')
        (self.source / "codex_events.jsonl").write_text("public history\n")
        target = prepare_target(self.anchor, {"directory":str(self.source), "id":"older"})
        metrics = json.loads((target / "metrics.json").read_text())
        self.assertEqual(metrics["room"], "end")
        self.assertEqual(metrics["decisions"], 289)
        copied = RunControl(target)
        self.assertFalse(copied.fast_preference())
        self.assertEqual(copied.effort_preference(),'low')
        self.assertFalse(copied.pending())
        self.assertEqual(copied.snapshot()["messages"][0]["status"], "error")
        self.assertEqual(len(old.pending()), 1)

    def test_failed_start_keeps_selection_and_restores_original_host(self):
        record = {"id":"older", "directory":str(self.source)}
        with patch("orchestrator.conversations.catalog", return_value=[record]), \
             patch("orchestrator.conversations.release_chat"), \
             patch("orchestrator.conversations.start_chat", side_effect=[RuntimeError("failed"), None]) as start:
            with self.assertRaisesRegex(RuntimeError, "failed"):
                switch_session(self.anchor, self.anchor, "older")
        self.assertFalse((self.anchor / "hud_session.json").exists())
        self.assertEqual(start.call_args.args, (self.anchor,))

    def test_active_game_cannot_be_detached(self):
        control = RunControl(self.anchor)
        control.heartbeat(chat_only=False, phase="idle")
        with self.assertRaisesRegex(ValueError, "试玩尚在进行"):
            release_chat(self.anchor)
        self.assertEqual(control.pending(), [])


if __name__ == "__main__":
    unittest.main()
