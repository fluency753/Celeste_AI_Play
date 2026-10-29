"""Exploration feedback can come from modelling without changing its cadence or budgets."""
import unittest

from orchestrator.config import RunConfig
from orchestrator.prompting import play_prompt, prompt_parts
from orchestrator.sim_assist import AssistRules


class ExploreIntentPromptTests(unittest.TestCase):
    def config(self, **values):
        cfg = RunConfig(agent='test', save='test', model='test', engine='codex',
                        mode='explore', runtime='container', live=True, sim_assist=True)
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
        self.assertIn('不让默认的收集或通关倾向取代当前任务', shared)
        self.assertNotIn('必须建立', shared)
        text = play_prompt(self.config())
        self.assertIn('持续积累可复用的模型与方法', text)
        self.assertNotIn('先评估是否比直接验证更省事', text)

    def test_program_constraints_and_current_frame_limits_still_apply(self):
        cfg = self.config(explore_act_frames=300, explore_search_frames=240,
                          explore_program_s=20, agent_cpus_effective=2)
        text = play_prompt(cfg)
        for expected in ('最多 300 帧', '每次推演最多 240 帧', '最多 20 秒',
                         '本次最多可以用 2 个核', '需要较多计算时用 C++ 写、不要用 Python'):
            self.assertIn(expected, text)

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
