"""Configurable button message bodies with controller-owned provenance and facts."""


_LABELS = {
    'start_play': '开始试玩',
    'start_explore': '开始探索',
    'prepare': '准备资料',
    'prepare_sim': '准备模拟器核验',
    'continue_play': '自动续轮',
    'continue_prepare': '自动续轮',
    'resume': '恢复试玩',
    'reconnect': '主控中断后恢复',
    'resume_prepare': '恢复准备',
    'next_chapter': '开始后续章节',
    'wake_chat': '唤起聊天',
    'name_save': '存档取名',
    'goal': '更新目标',
    'goal_clear': '清除目标',
    'chapters': '更新章节范围',
    'action_permissions': '更新操作权限',
    'sim_update': '更新推演设置',
    'cutscene_mode': '切换剧情模式',
    'effort': '调整推理强度',
    'fast': '切换 Fast',
    'bridge_reload': '桥接重载完成',
    'act_limit': '更新动作帧数上限',
}

_CONTINUATIONS = {'continue_play', 'continue_prepare'}
_CONTINUATION_NOTE = '这不是用户新消息，不改变任务范围、权限或停止条件。'


def catalog():
    """Return editable message bodies independently of startup guide templates."""
    return [{'key': key, 'name': f'button_{key}_zh.md', 'label': label}
            for key, label in _LABELS.items()]


def notification(key, facts=''):
    """Render an event body without letting editable prose replace controller facts."""
    if not isinstance(key, str) or key not in _LABELS:
        raise ValueError('未知按钮提示词')
    if not isinstance(facts, str):
        raise ValueError('主控通知事实须为文字')
    from .prompting import PROMPTS, read_part
    name = f'button_{key}_zh.md'
    failed = False
    try:
        body = read_part(name).strip()
    except (OSError, UnicodeError):
        failed = True
        try:
            body = (PROMPTS / name).read_text(encoding='utf-8').strip()
        except (OSError, UnicodeError):
            body = '请依据当前状态与配置继续当前任务。'
    parts = [f'【主控系统消息｜{_LABELS[key]}】']
    if facts.strip():
        parts.append(facts.strip())
    if body:
        parts.append(body)
    if failed:
        parts.append('本机消息模板读取失败，已用默认说明。')
    if key in _CONTINUATIONS:
        parts.append(_CONTINUATION_NOTE)
    return '\n'.join(parts)
