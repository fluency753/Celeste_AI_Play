"""Retry explicit model-service refusals without replaying game operations."""
from __future__ import annotations

import asyncio
import json
import re
import time


def is_model_unavailable(error) -> bool:
    """Only model availability failures, never authentication or tool-policy errors."""
    value = getattr(error, 'error', error)
    if isinstance(value, dict) and value.get('code') in {-32600, -32601, -32602}:
        return False
    text = json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value)
    if re.search(r'\b(?:401|403|400|404|422|-32600|-32601|-32602)\b|unauthori[sz]ed|authentication|invalid.api.key|'
                 r'permission.denied|invalid.(?:argument|param)|usage.limit|quota.exceeded|insufficient.quota', text, re.I):
        return False
    capacity = bool(re.search(r'at capacity|overloaded|modelCapacity', text, re.I))
    statuses = {int(code) for code in re.findall(
        r'\b(?:http(?:/\d(?:\.\d)?)?(?:status(?:code)?)?|status(?:_?code)?|error_status)'
        r'[\s\W_]{0,8}([45]\d\d)\b', text, re.I)}
    if statuses:
        # An explicit permanent HTTP refusal outranks a generic connection label.
        return statuses <= ({408, 500, 502, 503, 504, 529} | ({429} if capacity else set()))
    if re.search(r'\b429\b|rate.limit', text, re.I) and not capacity:
        return False
    return bool(re.search(r'(?:model|server).{0,60}at capacity|'
                          r'overloaded|server.{0,20}busy|temporarily unavailable|service unavailable|'
                          r'modelCapacity|serverOverloaded|httpConnectionFailed|responseStreamDisconnected|'
                          r'connection (?:error|reset)|network error', text, re.I))


class ModelRecovery:
    def _hold_model_failure(self):
        """An unavailable model must not release a live game on error cleanup."""
        if self.stop_requested or (self.session is not None and self.session.done):
            return
        self.retain_on_model_failure = True
        self.cfg._keep_frozen = True
        self.turn_active = False
        self.turn_done.set()

    def _model_recovered(self):
        if getattr(self, '_model_retry_since', None) is None:
            return
        from .telemetry import publish
        publish(self.run_dir, 'system:codex-model-back:' + str(self.turns),
                '模型服务已恢复，继续原会话；不重放已执行动作。', role='system', kind='系统')
        self._model_retry_since = None
        self._model_retry_attempts = 0
        self.retain_on_model_failure = False

    async def _wait_for_model(self, error):
        """Retry by making a real model request after backoff, not by HTTP probing."""
        from .telemetry import publish
        now = time.monotonic()
        if getattr(self, '_model_retry_since', None) is None:
            self._model_retry_since = now
            self._model_retry_attempts = 0
        self._hold_model_failure()
        deadline = self._model_retry_since + self.cfg.net_wait_max_h * 3600
        if not self.cfg.net_wait:
            self._hold_model_failure()
            raise RuntimeError(f'模型服务暂不可用，自动等待已关闭；已保留接管，可 --resume 续跑：{error}')
        if now >= deadline:
            self._hold_model_failure()
            raise RuntimeError('模型服务等待已达上限；已保留接管，可 --resume 续跑')
        self._model_retry_attempts += 1
        delay = min(600.0, 30.0 * 2 ** min(self._model_retry_attempts - 1, 5))
        if self.cfg.net_wait_cap_s is not None:  # Isolated test delay; not the outage deadline.
            delay = min(delay, self.cfg.net_wait_cap_s)
        self.turn_active, self.turn_id = False, None
        self._end_model_turn()
        self.model_waiting = True
        async with self.action_lock:
            pass  # Let the already submitted action finish; deny queued old actions.
        began = time.monotonic()
        if self.pause_at is None:
            self.pause_at = began
        self.metrics.pause(status='net_wait')
        publish(self.run_dir, 'system:codex-model-wait:' + str(self.turns),
                f'模型服务暂不可用：{str(error)[:200]}。约 {delay:g} 秒后重试原会话；'
                '保留游戏接管和录制，等待不计现实预算与思考僵直，可暂停、中断或停止。',
                role='system', kind='系统')
        print(f'[codex] 模型暂不可用，等待 {delay:g} 秒后重试原会话', flush=True)
        try:
            until = min(began + delay, deadline)
            while time.monotonic() < until:
                if self.stop_requested or (self.session is not None and self.session.done):
                    return
                if self.control_task is not None and self.control_task.done():
                    self.control_task.result()
                    raise RuntimeError('模型等待期间用户控制通道已停止')
                if self.rpc.reader.done():
                    raise ConnectionError('模型等待期间 Codex app-server 已断开；请 --resume 续跑')
                await asyncio.sleep(min(.1, max(.001, until - time.monotonic())))
            if time.monotonic() >= deadline:
                raise RuntimeError('模型服务等待已达上限；请 --resume 续跑')
        except (RuntimeError, OSError):
            self._hold_model_failure()
            raise
        finally:
            waited = time.monotonic() - began
            self.model_waiting = False
            self.metrics.add(net_wait_s=round(waited, 3), net_waits=1)
            if self.study is not None and not self.metrics.playing:
                self.study.started += waited
            if not self.paused:
                self.paused_seconds += time.monotonic() - self.pause_at
                self.pause_at = None
                self.metrics.resume(quota=False)
            else:
                self.metrics.update(status='chat_idle' if self.chat_only else 'paused')

    def _model_recovery_prompt(self):
        from .button_prompts import notification
        facts = ('模型服务暂时不可用导致上一轮失败，现在重试同一个会话。'
                 '已完成的工具或动作仍然有效，不要重放原动作指令；最后一次执行结果可能未送达。')
        if self.naming_only:
            return facts + '请继续原来的存档命名请求，不操作游戏。'
        if self.chat_only:
            return notification('wake_chat', facts=facts + '请继续处理原用户任务，不操作游戏。')
        if self.study and not self.study.complete():
            return notification('resume_prepare', facts=facts + '准备阶段仍未完成，请继续核对必读资料。')
        return notification('reconnect', facts=facts + '先用 observe 核对当前帧、最近动作及结果，再决定下一步。')
