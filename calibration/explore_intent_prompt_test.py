"""Exploration feedback can come from modelling without changing its cadence or budgets."""
import unittest

from orchestrator.config import RunConfig
from orchestrator.prompting import PROMPTS, play_prompt, prompt_parts
from orchestrator.sim_assist import AssistRules


class ExploreIntentPromptTests(unittest.TestCase):
    def config(self, **values):
        cfg = RunConfig(agent='test', save='test', model='test', engine='codex',
                        mode='explore', knowledge_profile='explore', runtime='container', live=True, sim_assist=True)
        for key, value in values.items():
            setattr(cfg, key, value)
        return cfg

    def test_live_feedback_keeps_cadence_and_includes_real_program_progress(self):
        for engine in ('codex', 'claude'):
            text = play_prompt(self.config(engine=engine))
            self.assertIn('10–20 秒', text)
            self.assertIn('5–10 秒', text)
            self.assertIn('反馈可以来自游戏操作、程序实验或真实运行进展', text)
            self.assertIn('研究建模同样持续反馈', text)
            self.assertIn('未有新发现时不编造', text)

    def test_shared_default_defers_research_priority_to_current_task(self):
        parts = prompt_parts(self.config())
        shared = ''.join(p.text for p in parts if p.source == 'explore_zh.md')
        self.assertIn('以用户当前任务为准', shared)
        self.assertIn('不让默认的通关倾向取代当前任务', shared)
        self.assertIn('没有指定时以完成本次章节为主', shared)
        self.assertNotIn('必须建立', shared)
        text = play_prompt(self.config())
        self.assertIn('持续积累可复用的模型与方法', text)
        self.assertNotIn('先评估是否比直接验证更省事', text)

    def test_program_constraints_and_current_frame_limits_still_apply(self):
        cfg = self.config(explore_act_frames=300, explore_search_frames=240,
                          explore_program_s=20, agent_cpus_effective=2)
        text = play_prompt(cfg)
        for expected in ('最多 300 帧', '每次推演最多 240 帧', '往后总共最多推演 240 帧', '不能把模拟出来的中间状态',
                         '240 帧内走不完的，先执行推演好的这一段', '最多 20 秒',
                         '本次最多可以用 2 个核', '建议用编译型语言（如 C++）'):
            self.assertIn(expected, text)

    def test_program_limits_are_initial_and_optimization_is_discoverable_on_demand(self):
        sections = []
        for engine in ('codex', 'claude'):
            for runtime in ('container', 'host'):
                cfg = self.config(engine=engine, runtime=runtime, agent_cpus_effective=1)
                parts = prompt_parts(cfg)
                program = ''.join(p.text for p in parts if p.source == 'explore_programs_zh.md')
                text = play_prompt(cfg)
                if engine == 'claude' and runtime == 'host':
                    self.assertEqual(program, '')
                    self.assertNotIn('你可以自行编写', text)
                    continue
                sections.append(program)
                self.assertIn('你可以自行编写模拟器、搜索或其他辅助程序，自由发挥', text)
                for expected in ('本次最多可以用 1 个核', '多线程完成',
                                 '/kb/help/programs.md', '/kb/help/workspace.md'):
                    self.assertIn(expected, program)
                reference = (PROMPTS.parent.parent / 'kb/explore/help/programs.md').read_text(encoding='utf-8')
                for detail in ('去重、择优和汇总', '避免逐个启动进程', '循环中不反复读写工作区'):
                    self.assertNotIn(detail, program)
                    self.assertIn(detail, reference)
                self.assertNotIn('各线程把候选', program)
                self.assertNotIn('先评估是否比直接验证更省事', text)
        self.assertTrue(all(section == sections[0] for section in sections))

    def test_fixed_program_guidance_lives_in_editable_templates(self):
        from overlay.console_api import PLACEHOLDERS
        intro = (PROMPTS / 'explore_zh.md').read_text(encoding='utf-8')
        programs = (PROMPTS / 'explore_programs_zh.md').read_text(encoding='utf-8')
        for marker in ('{program_freedom}', '{program_lead}', '{parallel_rule}'):
            self.assertNotIn(marker, intro + programs)
            self.assertNotIn(marker, PLACEHOLDERS)
        for expected in ('你可以自行编写', '建议用编译型语言（如 C++）', '{program_reference_rule}'):
            self.assertIn(expected, programs)
        self.assertIn('{cpu_rule}', programs)
        self.assertIn('{explore_program_rule}', programs)

    def test_limited_assist_supports_model_comparison_with_unchanged_budget_and_origin(self):
        rules = AssistRules(capacity=75, initial=33, max_candidates=4, max_frames=120, max_total=240)
        text = rules.instructions()
        self.assertIn('路线决策或自建模型提供对照', text)
        self.assertIn(rules.text(), text)
        self.assertIn('只能从游戏当前帧预测', text)
        self.assertIn('不保证未知机制完全准确', text)
        self.assertIn('不寻找模拟器文件绕过额度', text)
        self.assertNotIn('体力不足时继续真实尝试', text)


if __name__ == '__main__':
    unittest.main()
