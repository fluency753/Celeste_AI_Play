"""Exercise the composed knowledge view, compatibility and its real consumers."""
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from orchestrator import kb_access, kb_layout, profiles
from orchestrator.config import RunConfig
from orchestrator.kb_study import KbStudy, required_files
from orchestrator.prompt_environment import reference_path, render_tool_references


class SharedDocsTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.kb = self.root/'kb'
        for name, text in {
            'shared/tools.md': '使用 {tool:act}，JSON值 {"tool":"act"}。\n```python\nobserve()\n```\n',
            'shared/data.md': '坐标说明\n', 'shared/tools/act.md': '完整 {tool:act} 计划格式\n',
            'explore/README.md': '探索索引\n', 'explore/basics.md': '基础\n',
            'speedrun/README.md': '速通索引\n', 'speedrun/techniques.md': '技巧\n',
        }.items():
            path = self.kb/name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding='utf-8')
        self.enterContext(patch('orchestrator.config.REPO_ROOT', self.root))
        self.enterContext(patch.object(kb_access, 'KB_ROOT', self.kb))
        self.enterContext(profiles.using(profiles.LEGACY))
        self.cfg = RunConfig(agent='shared-test', save='none', model='test', engine='codex',
                             mode='explore', knowledge_profile='explore')

    def test_shared_view_study_and_explicit_speedrun_permission(self):
        rows = {row['path']: row for row in kb_access.documents('explore')}
        self.assertIn('shared/tools.md', rows)
        self.assertTrue(rows['speedrun/techniques.md']['opt_in'])
        view = kb_access.prepare_view(self.cfg)
        self.assertEqual(required_files(view), ['README.md', 'basics.md', 'shared/data.md', 'shared/tools.md'])
        self.assertFalse((view/'speedrun').exists())
        self.assertFalse((view/'techniques.md').exists())
        self.assertEqual((view/'shared/tools.md').read_bytes(), (view/'tools.md').read_bytes())
        study = KbStudy(view, 1)
        for rel in study.files:
            study.note_lines(rel, range(1, study.total[rel]+1))
        self.assertTrue(study.complete())
        allowed = replace(self.cfg, kb_allow=['speedrun/techniques.md'])
        selected = kb_access.prepare_view(allowed)
        self.assertTrue((selected/'speedrun/techniques.md').is_file())
        self.assertNotIn('speedrun/techniques.md', required_files(selected))
        self.assertNotEqual(selected, view)

    def test_local_source_is_one_readonly_optional_speedrun_document(self):
        source = kb_layout.local_source(self.kb)
        (source/'Celeste').mkdir(parents=True)
        code = 'public class Player {}\n'
        (source/'Celeste/Player.cs').write_text(code, encoding='utf-8')
        (source/'project.csproj').write_text('<Project/>\n', encoding='utf-8')
        (source/'example.ico').write_bytes(b'original non-text file')
        path = kb_layout.CELESTE_SOURCE
        (self.kb/'.collections.json').write_text(json.dumps([{
            'path': path, 'source': 'local/kb/celeste-source', 'modes': ['speedrun'],
        }]), encoding='utf-8')
        fname = path + '/Celeste/Player.cs'
        rows = [row for row in kb_access.documents('speedrun') if row['local_only']]
        self.assertEqual([row['path'] for row in rows], [path])
        self.assertTrue(rows[0]['directory'])
        self.assertEqual(rows[0]['file_count'], 3)
        self.assertFalse(rows[0]['required'])
        self.assertFalse(rows[0]['editable'])
        self.assertNotIn('speedrun/' + path, [row['path'] for row in kb_access.documents('explore')])
        with self.assertRaises(ValueError):
            kb_access.normalize_allow('explore', ['speedrun/' + path])
        with self.assertRaises(ValueError):
            kb_access.write_document('speedrun', path, 'edited')
        cfg = replace(self.cfg, mode='speedrun', knowledge_profile='full')
        from orchestrator.prompting import kb_reference_rule
        self.assertIn('/kb/' + path, kb_reference_rule(cfg))
        self.assertNotIn(path, kb_reference_rule(self.cfg))
        self.assertEqual((kb_access.prepare_view(cfg) / fname).read_text(encoding='utf-8'), code)
        self.assertEqual((cfg.kb_dir / path / 'example.ico').read_bytes(), b'original non-text file')
        self.assertTrue(kb_access.read_document('speedrun', path)['directory'])
        self.assertNotIn(fname, required_files(cfg.kb_dir))
        required = replace(cfg, kb_required={kb_layout.LEGACY_CELESTE_SOURCE: True, 'README.md': False})
        view = kb_access.prepare_view(required)
        self.assertIn(fname, required_files(view))
        self.assertIn(path + '/project.csproj', required_files(view))
        self.assertNotIn(path + '/example.ico', required_files(view))
        self.assertNotIn('README.md', required_files(view))
        study = KbStudy(view, 1)
        self.assertIn('/kb/' + path + '/', study.display_paths())
        self.assertNotIn('/kb/' + fname, study.display_paths())
        self.assertIn('/kb/' + fname, study.container_paths())
        self.assertNotEqual(view, cfg.kb_dir)
        denied = replace(required, kb_deny=[kb_layout.LEGACY_CELESTE_SOURCE])
        view = kb_access.prepare_view(denied)
        self.assertFalse((view / path).exists())
        self.assertNotIn(fname, required_files(view))
        self.assertNotIn(path, kb_reference_rule(denied))
        self.assertEqual(kb_access.normalize_deny('speedrun', [kb_layout.LEGACY_CELESTE_SOURCE]), [path])

    def test_user_config_can_group_builtin_directory_without_reopening_denied_child(self):
        group, child = 'shared/tools', 'shared/tools/act.md'
        config = self.root/'local/kb/collections.json'
        config.parent.mkdir(parents=True)
        config.write_text(json.dumps([{'path': group, 'source': 'kb/shared/tools',
                                      'modes': ['explore', 'speedrun'], 'required': True}]), encoding='utf-8')
        entries = {row['path']: row for row in kb_access.documents('explore')}
        self.assertTrue(entries[group]['directory'])
        self.assertNotIn(child, entries)
        self.assertEqual(kb_access.normalize_deny('explore', ['help/actions.md']), [child])
        view = kb_access.prepare_view(self.cfg)
        self.assertIn(child, required_files(view))
        self.assertEqual((view/child).read_bytes(), (view/'help/actions.md').read_bytes())
        edits = self.root/'local/kb_edits'
        (edits/child).parent.mkdir(parents=True)
        (edits/child).write_text('改写 {tool:act}\n', encoding='utf-8')
        edited = kb_access.prepare_view(replace(self.cfg, kb_overlay=str(edits)))
        self.assertEqual((edited/child).read_text(encoding='utf-8'), '改写 celeste_act\n')
        view = kb_access.prepare_view(replace(self.cfg, kb_deny=[child]))
        self.assertFalse((view/child).exists())
        self.assertFalse((view/'help/actions.md').exists())
        view = kb_access.prepare_view(replace(self.cfg, kb_required={group: False}))
        self.assertNotIn(child, required_files(view))
        self.assertTrue((view/child).is_file())

    def test_collection_rejects_ambiguous_mount_and_path_traversal(self):
        config = self.kb/'.collections.json'
        base = {'path': 'shared/tools', 'source': 'kb/shared/tools', 'modes': ['explore']}
        config.write_text(json.dumps([base, {**base, 'path': 'Shared/Tools/Nested'}]), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, '路径重叠'):
            kb_access.documents('explore')
        config.write_text(json.dumps([{**base, 'source': 'local/kb/.. /outside'}]), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, '路径组件'):
            kb_access.documents('explore')
        config.write_text(json.dumps([{**base, 'path': 'speedrun/collision'}]), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, '路径冲突'):
            kb_access.documents('explore')

    def test_grouping_speedrun_files_keeps_an_existing_explore_grant(self):
        child = 'route/physics.md'
        source = self.kb/'speedrun'/child
        source.parent.mkdir()
        source.write_text('旧资料\n', encoding='utf-8')
        (self.kb/'.collections.json').write_text(json.dumps([{
            'path': 'route', 'source': 'kb/speedrun/route', 'modes': ['speedrun'],
        }]), encoding='utf-8')
        path = 'speedrun/' + child
        self.assertEqual(kb_access.normalize_allow('explore', [path]), [path])
        view = kb_access.prepare_view(replace(self.cfg, kb_allow=[path]))
        self.assertEqual((view/path).read_text(encoding='utf-8'), '旧资料\n')
        self.assertNotIn(path, [row['path'] for row in kb_access.documents('explore')])

    def test_pdf_uses_paired_text_for_an_explicit_read_requirement(self):
        original = 'reference/community/pdf/example.pdf'
        transcript = 'reference/community/example.md'
        (self.kb / original).parent.mkdir(parents=True)
        (self.kb / original).write_bytes(b'%PDF-1.5 fixture original')
        (self.kb / transcript).write_text('text matching original\n', encoding='utf-8')
        rows = {row['path']: row for row in kb_access.documents('speedrun')}
        self.assertEqual(rows[original]['study_target'], transcript)
        self.assertEqual(kb_access.normalize_required('speedrun', {original: True}), {original: True})
        cfg = replace(self.cfg, mode='speedrun', knowledge_profile='full',
                      kb_required={original: True, transcript: False})
        view = kb_access.prepare_view(cfg)
        self.assertTrue((view / original).is_file())
        study = KbStudy(view, 1)
        self.assertNotIn(original, study.files)
        self.assertIn(transcript, study.files)
        self.assertEqual(study.total[transcript], 1)
        denied = replace(cfg, kb_deny=[transcript])
        self.assertNotIn(transcript, required_files(kb_access.prepare_view(denied)))

    def test_engine_rendering_preserves_protocol_values_and_fenced_programs(self):
        codex = kb_access.prepare_view(self.cfg)
        claude = kb_access.prepare_view(replace(self.cfg, engine='claude'))
        text = (codex/'shared/tools.md').read_text(encoding='utf-8')
        self.assertIn('celeste_act', text)
        self.assertIn('{"tool":"act"}', text)
        self.assertIn('observe()', text)
        self.assertNotIn('celeste_act', (claude/'shared/tools.md').read_text(encoding='utf-8'))
        self.assertNotEqual(codex, claude)
        source = 'act {"tool":"act"} /workspace/act.py\n```python\nobserve()\n```'
        self.assertEqual(render_tool_references(source, self.cfg), source.replace('act ', 'celeste_act ', 1))

    def test_deny_applies_to_all_old_aliases_and_reference_links(self):
        for alias in ('shared/tools/act.md', 'help/actions.md', 'explore/help/actions.md'):
            cfg = replace(self.cfg, kb_deny=[alias])
            view = kb_access.prepare_view(cfg)
            for name in ('shared/tools/act.md', 'help/actions.md', 'explore/help/actions.md'):
                self.assertFalse((view/name).exists(), name)
            self.assertIsNone(reference_path(cfg, 'actions.md'))
            self.assertEqual(kb_access.normalize_deny('explore', [alias]), ['shared/tools/act.md'])

    def test_console_shows_old_denials_on_canonical_checkboxes_without_rewriting_profile(self):
        from overlay import console_api
        prefs = self.root/'prefs.json'
        prefs.write_text(json.dumps({'kb_deny': {'explore': ['help/actions.md']}}), encoding='utf-8')
        original = prefs.read_bytes()
        with patch.object(console_api, 'PREFS', prefs), patch.object(console_api, 'REPO_ROOT', self.root), \
             patch.object(console_api, '_profile', return_value=profiles.LEGACY), \
             patch.object(profiles, 'selected', return_value=profiles.LEGACY):
            listed = console_api.kb_list('explore')
        self.assertEqual(listed['deny'], ['shared/tools/act.md'])
        self.assertEqual(prefs.read_bytes(), original)

    def test_old_edits_are_effective_and_duplicate_paths_fail_before_writing(self):
        edits = self.root/'local/kb_edits'
        old = edits/'explore/tools.md'
        old.parent.mkdir(parents=True)
        old.write_text('旧路径个人修改\n', encoding='utf-8')
        cfg = replace(self.cfg, kb_overlay=str(edits))
        view = kb_access.prepare_view(cfg)
        self.assertEqual((view/'shared/tools.md').read_text(encoding='utf-8'), '旧路径个人修改\n')
        self.assertEqual(kb_access.read_document('explore', 'tools.md')['text'], '旧路径个人修改\n')
        kb_access.reset_document('explore', 'tools.md')
        self.assertFalse(old.exists())
        old.parent.mkdir(parents=True, exist_ok=True)
        old.write_text('相同\n', encoding='utf-8')
        new = edits/'shared/tools.md'
        new.parent.mkdir(parents=True)
        new.write_text('相同\n', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, '多份修改'):
            kb_access.write_document('explore', 'tools.md', '新内容')
        self.assertEqual(old.read_bytes(), new.read_bytes())

    def test_view_is_pinned_between_study_and_container_consumers(self):
        from orchestrator.container import _labels, LABEL
        view = kb_access.prepare_view(self.cfg)
        study = KbStudy(view, 1)
        (self.kb/'shared/data.md').write_text('变化后的内容\n', encoding='utf-8')
        self.assertEqual(kb_access.prepare_view(self.cfg), view)
        self.assertEqual(self.cfg.kb_dir, study.kb_dir)
        self.assertEqual(_labels(self.cfg)[LABEL+'.kb_layout'], str(view))
        self.assertEqual((view/'shared/data.md').read_text(encoding='utf-8'), '坐标说明\n')
        (self.kb/'shared/tools/programs.md').write_text('新增的选读文档\n', encoding='utf-8')
        self.assertIsNone(reference_path(self.cfg, 'programs.md'))
        self.assertEqual(reference_path(self.cfg, 'actions.md'), '/kb/shared/tools/act.md')
        fresh = replace(self.cfg)
        self.assertNotEqual(kb_access.prepare_view(fresh), view)
        (self.kb/'shared/tools.md').unlink()
        self.assertEqual(kb_access.prepare_view(self.cfg), view)
        self.assertEqual(reference_path(self.cfg, 'actions.md'), '/kb/shared/tools/act.md')
        self.assertTrue(self.cfg.kb_view)
        self.assertTrue((view/'shared/data.md').is_file())
        with self.assertRaisesRegex(ValueError, '源码不完整'):
            kb_access.prepare_view(replace(self.cfg))
        self.cfg.kb_deny = ['shared/tools.md']
        with self.assertRaisesRegex(ValueError, '快照已固定'):
            _ = self.cfg.kb_dir

    def test_prepared_reuse_checks_complete_view_and_missing_manifest_fails_closed(self):
        from orchestrator.prepared import load_preparation
        view = kb_access.prepare_view(self.cfg)
        study = KbStudy(view, 1)
        for rel in study.files:
            study.note_lines(rel, range(1, study.total[rel]+1))
        import hashlib
        data = dict(engine='codex', provider='codex', runtime='container', knowledge_profile='explore',
                    success=True, game_tools_registered=True, metrics={'agent': self.cfg.agent}, session_id='test',
                    kb_study=study.summary(), kb_identity=kb_layout.study_identity(view),
                    kb_hashes={rel: hashlib.sha256((view/rel).read_bytes()).hexdigest() for rel in study.files})
        run = self.root/'prepared'
        run.mkdir()
        (run/'prepare_summary.json').write_text(json.dumps(data), encoding='utf-8')
        load_preparation(str(run), self.cfg, study)
        narrowed = replace(self.cfg, kb_deny=['shared/tools/act.md'])
        narrowed_study = KbStudy(kb_access.prepare_view(narrowed), 1)
        with self.assertRaisesRegex(ValueError, '授权已变化'):
            load_preparation(str(run), narrowed, narrowed_study)
        (view/kb_layout.MANIFEST).unlink()
        with self.assertRaisesRegex(ValueError, '清单缺失'):
            load_preparation(str(run), self.cfg, study)
        with self.assertRaisesRegex(ValueError, '清单缺失'):
            KbStudy(view, 1)

    def test_cached_snapshot_refuses_modified_or_unregistered_files(self):
        view = kb_access.prepare_view(self.cfg)
        data = view/'shared/data.md'
        original = data.read_bytes()
        data.write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, '内容与生成清单不一致'):
            kb_access.prepare_view(replace(self.cfg))
        data.write_bytes(original)
        (view/'unregistered.md').write_bytes(b'extra')
        with self.assertRaisesRegex(ValueError, '文件集合'):
            kb_access.prepare_view(self.cfg)
        (view/'unregistered.md').unlink()
        (view/'nested').mkdir()
        (view/'nested/.knowledge.json').write_bytes(b'extra')
        with self.assertRaisesRegex(ValueError, '文件集合'):
            kb_access.prepare_view(self.cfg)

    def test_extra_documents_preserve_hidden_file_and_directory_exclusion(self):
        extra = self.root/'extra'
        (extra/'.private').mkdir(parents=True)
        (extra/'.env').write_bytes(b'hidden')
        (extra/'.private/data.md').write_bytes(b'hidden')
        (extra/'series.md').write_bytes(b'visible')
        view = kb_access.prepare_view(replace(self.cfg, kb_extra=str(extra)))
        self.assertFalse((view/'.env').exists())
        self.assertFalse((view/'.private').exists())
        self.assertTrue((view/'series.md').is_file())

    def test_old_preparation_cannot_pass_using_an_unmaterialized_empty_study(self):
        from orchestrator.prepared import load_preparation
        self.assertFalse(self.cfg.kb_dir.exists())
        study = KbStudy(self.cfg.kb_dir, 1)
        self.assertTrue(study.complete())
        run = self.root/'old-preparation'
        run.mkdir()
        data = dict(engine='codex', provider='codex', runtime='container', knowledge_profile='explore',
                    success=True, game_tools_registered=True, metrics={'agent': self.cfg.agent}, session_id='old',
                    kb_study={'complete': True, 'files': {}}, kb_hashes={})
        (run/'prepare_summary.json').write_text(json.dumps(data), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, '快照清单'):
            load_preparation(str(run), self.cfg, study)


class InterfaceDocsTests(unittest.TestCase):
    def test_mode_protocol_values_and_technique_names_remain_literal(self):
        from orchestrator.config import REPO_ROOT
        cfg = RunConfig(agent='docs-check', save='none', model='test', engine='codex')
        for rel, literals in [('entities.md', ('以 `cutscene` 停下',)),
                              ('techniques.md', ('cutscene ultra, cutscene warp',))]:
            source = (REPO_ROOT/'kb/speedrun'/rel).read_text(encoding='utf-8')
            rendered = render_tool_references(source, cfg, legacy=False)
            for literal in literals:
                self.assertIn(literal, rendered)
            self.assertIn('celeste_cutscene', rendered)  # 同页真实调用仍按执行器命名

    def test_sections_match_real_registered_tools_for_both_executors_and_modes(self):
        import asyncio
        from types import SimpleNamespace
        from orchestrator.server import build_server
        from orchestrator.prompting import play_prompt, prompt_parts
        from orchestrator.prompt_environment import tool_name
        with profiles.using(profiles.DEFAULT):
            for engine in ('codex', 'claude'):
                for mode in ('explore', 'speedrun'):
                    cfg = RunConfig(agent='docs-check', save='none', model='test', engine=engine, mode=mode,
                                    knowledge_profile='explore' if mode == 'explore' else 'full',
                                    sim=False, sim_assist=mode == 'explore', live=True, cutscene_mode='agent')
                    registered = asyncio.run(build_server(SimpleNamespace(cfg=cfg, metrics=None, workspace=None)).list_tools())
                    text = play_prompt(cfg)
                    sections = {p.source for p in prompt_parts(cfg)}
                    for tool in registered:
                        self.assertIn('## '+tool_name(cfg, tool.name)+'：', text)
                        self.assertIn('tool_'+tool.name+'_zh.md', sections)
                    self.assertNotIn('{tool:', text)
                    self.assertNotIn('tool_travel_zh.md', sections)
                    self.assertIn('/kb/shared/tools/act.md', text)

    def test_host_without_files_gets_geometry_inline_and_no_file_sections(self):
        from orchestrator.prompting import play_prompt, prompt_parts
        cfg = RunConfig(agent='docs-host', save='none', model='test', engine='claude', runtime='host', sim=False)
        with profiles.using(profiles.DEFAULT):
            text = play_prompt(cfg)
            self.assertIn('base_snapshot_id', text)
            self.assertIn('不要逐层重复累加父锚点', text)
            sources = {p.source for p in prompt_parts(cfg)}
            self.assertNotIn('tool_files_zh.md', sources)
            self.assertNotIn('tool_programs_zh.md', sources)


if __name__ == '__main__':
    unittest.main()
