"""Advisory note reminders; no file edits, tool denial, or model turns."""
import hashlib
import math
import threading
from pathlib import Path


USER_NOTE_RESEND = ('（系统·留言重发）上下文刚完成压缩，开场消息里附的用户留言可能只剩摘要。下面重发原文，'
                    '其中的要求和规则继续有效；按当前进度继续，不要因此重新开始章节或重做已经完成的开局步骤，'
                    '也不因此自行恢复游戏。\n（用户留言）\n')

# 操作指南是智能体开局时自己读进上下文的工作区文件：压缩后正文只剩摘要，规则跟着丢。压缩完成后提醒重读一次。
GUIDE_REREAD = ('（系统·指南提醒）上下文刚完成压缩，之前读过的操作指南正文可能只剩摘要。'
                '继续正式试玩前，请重读 {path}（只查某一节时见同目录的 index.md）；仅聊天、准备资料或取名时按需查阅。'
                '遵守当前暂停或仅聊天状态，不因此自行恢复游戏。')


class NoteReminders:
    def __init__(self, workspace, threshold, user_note=None):
        self.path = Path(workspace) / 'notes.md' if workspace is not None else None
        self.threshold = threshold
        # --note-file 的原文：开场消息压缩后只剩摘要，每次压缩完成后随笔记提醒重发一次。
        self.user_note = (user_note or '').strip() or None
        self.lock = threading.RLock()
        self.cycle = 0
        self.seen = set()
        self.pending = {}
        self.warned = False
        self.percent = None
        self.updated = False
        self.baseline = self._fingerprint()
        self.last_hash = self.baseline
        self.observable = self.baseline is not None

    def _fingerprint(self):
        if self.path is None:
            return None
        try:
            with self.path.open('rb') as stream:
                raw = stream.read(256 * 1024 + 1)
            return hashlib.sha256(raw).hexdigest() if len(raw) <= 256 * 1024 else None
        except FileNotFoundError:
            return 'missing'
        except OSError:
            return None

    def observe_file(self):
        with self.lock:
            value = self._fingerprint()
            if value is None:
                self.observable = False
                if 'before' in self.pending:
                    self.warned = False
                self.pending.pop('before', None)
                return
            if self.baseline is None:
                # Do not infer what happened while the file was unobservable.
                self.baseline = value
            elif value != self.last_hash:
                # Deletion is not an update; creation/content changes are observable.
                if value != 'missing':
                    self.updated = True
                    self.pending.pop('before', None)
            self.last_hash = value
            self.observable = True

    def compacted(self, ident, guide=None):
        """压缩完成：排队笔记提醒（有工作区时）、操作指南重读提醒（guide 是本会话的 GuideReading，指南发布在
        工作区时才有）与留言原文重发（有 --note-file 时），同一次压缩只排一次。返回这次是否排了留言重发。"""
        guide_path = guide.guide.get('path') if guide is not None else None
        if self.path is None and self.user_note is None and not guide_path:
            return False
        with self.lock:
            key = str(ident)
            if key in self.seen:
                return False
            self.seen.add(key)
            self.cycle += 1
            if self.path is not None:
                self.baseline = self.last_hash = self._fingerprint()
                self.observable = self.baseline is not None
                self.updated = self.warned = False
                self.percent = None
                self.pending.pop('before', None)
                self.pending['after'] = (self.cycle,
                    '（系统·笔记提醒）上下文刚完成压缩。请读取 /workspace/notes.md，'
                    '与最新观测、最近动作结果核对；笔记缺失则建立简短索引，过期或矛盾则更新整理。'
                    '遵守当前暂停或仅聊天状态，不因此自行恢复游戏。')
            if guide_path:
                guide.compacted()
                self.pending.pop('guide', None)
                self.pending['guide'] = (self.cycle, GUIDE_REREAD.format(path=guide_path))
            if self.user_note is not None:
                self.pending.pop('note', None)   # 重新插入，排在笔记提醒之后
                self.pending['note'] = (self.cycle, USER_NOTE_RESEND + self.user_note)
            return self.user_note is not None

    def usage(self, used, window):
        """Only call for a fresh usage event, not restored or pre-compaction metrics."""
        if self.path is None:
            return
        with self.lock:
            self.observe_file()
            threshold = self.threshold()
            valid = all(type(v) in (int, float) and math.isfinite(v) for v in (used, window, threshold))
            if not valid or window <= 0 or used < 0 or not 0 < threshold <= 100:
                self.percent = None
                if 'before' in self.pending:
                    self.warned = False
                    self.pending.pop('before')
                return
            percent = used / window * 100
            self.percent = percent
            if self.updated or self.warned or not self.observable:
                return
            if percent > threshold:
                self.pending['before'] = (self.cycle,
                    f'（系统·笔记提醒）当前上下文占用约 {percent:.0f}%（提醒阈值 {threshold:g}%），'
                    '本监测周期尚未检测到 /workspace/notes.md 内容更新。'
                    '请检查并及时整理当前进度、已验证事实、失败假设和待办，避免压缩后遗漏；'
                    '只记录有用变化，不机械重写，也不需要暂停试玩。')
                self.warned = True

    def peek(self):
        with self.lock:
            self.observe_file()
            value = self.threshold()
            if (type(value) not in (int, float) or not math.isfinite(value) or value <= 0
                    or (self.percent is not None and self.percent <= value)):
                if 'before' in self.pending:
                    self.warned = False
                self.pending.pop('before', None)
            return tuple(((kind, cycle), text) for kind, (cycle, text) in self.pending.items())

    def acknowledge(self, offered):
        with self.lock:
            for (kind, cycle), value in offered:
                if self.pending.get(kind) == (cycle, value):
                    self.pending.pop(kind, None)

    def take(self):
        offered = self.peek()
        self.acknowledge(offered)
        return '\n'.join(text for _, text in offered)
