"""Menu macros must stop at a denied action instead of waiting for an impossible transition."""
from __future__ import annotations

from orchestrator.action_policy import BLOCK_STOPS


class MenuActionBlocked(RuntimeError):
    def __init__(self, reply: dict):
        self.obs = reply.get('obs') or {}
        self.result = reply.get('result') or {}
        self.block = self.result.get('block') or {}
        labels = {'restart_chapter': '重启章节', 'return_to_map': '返回地图', 'save_quit': '保存并退出',
                  'enter_map': '进入地图', 'menu_item': '菜单确认', 'enter_pico8': 'PICO-8 小游戏入口'}
        action = labels.get(self.block.get('action'), self.block.get('action') or '菜单操作')
        rule = self.block.get('rule_id') or '未提供规则'
        if rule.startswith('permission.'):
            reason = f'本次不允许{action}'
        elif rule in {'target.map', 'target.save_slot'}:
            target = '地图' if rule == 'target.map' else '存档槽位'
            reason = f'{target}与任务不符（要求 {self.block.get("expected")}，实际 {self.block.get("actual")}）'
        elif rule == 'menu.unknown_item':
            reason = '未通关时只允许继续、重试、跳过剧情或取消；当前选项不在允许范围'
        else:
            reason = self.block.get('reason') or self.result.get('stop') or '权限不足'
        super().__init__(f'菜单宏已停止：主控拦截「{action}」（{rule}）：{reason}。'
                         f'已推进输入 {self.result.get("consumed", 0)}/{self.result.get("planned", "?")} 帧'
                         f'（另有加载 {self.result.get("loadingFrames", 0)} 帧）；'
                         '本次被拒动作及其后的输入未执行，保留当前观测，不再等待或重试退出。')


def checked_step(client, inputs: list[str], **options) -> dict:
    reply = client.call('step', inputs=inputs, **options)
    result = reply.get('result') or {}
    if result.get('block') or result.get('stop') in BLOCK_STOPS:
        raise MenuActionBlocked(reply)
    return reply
