"""Small current-task envelope; durable operating instructions live in the workspace."""
from dataclasses import asdict
import json
import re

from .execution import workspace_capabilities
from .modes import act_frame_cap, act_frame_min, collision_image_mode, game_tools, is_explore


def current_settings(cfg, *, include_task=True):
    from .action_policy import permissions_for
    from .tuning import HOST_CPUS
    plan = getattr(cfg, 'plan', None)
    settings = {
        'task': {'mode': cfg.mode, 'save': cfg.save, 'slot': cfg.save_slot,
                 'scope': cfg.play_scope, 'completion': cfg.completion_policy,
                 # A 面的一截仍是 {chapter, start, end}（与原来相同），B／C 面另加 side
                 'targets': [{k: v for k, v in asdict(p).items() if k != 'side' or v != 'A'} for p in plan]
                            if plan else cfg.chapters or [cfg.chapter]},
        'story': cfg.cutscene_mode,
        'permissions': permissions_for(cfg).snapshot(),
        'act_frames': [act_frame_min(cfg), act_frame_cap(cfg)],
        'program': {'seconds': cfg.explore_program_s if is_explore(cfg) else cfg.bash_max_s,
                    'calls_between_act': None,
                    'cpus': getattr(cfg, 'agent_cpus_effective', None) or cfg.agent_cpus or HOST_CPUS},
        'prediction': {'preset_simulator': cfg.sim and cfg.pause_limit, 'limited_assist': cfg.sim_assist,
                       'self_written_max_frames': cfg.explore_search_frames if is_explore(cfg) else None},
        'presentation': {'live': cfg.live, 'formal_thought': cfg.formal_thought,
                         'reminder_seconds': cfg.live_remind_s, 'collision_image': collision_image_mode(cfg)},
        'recording': cfg.recording_mode if cfg.record else 'off',
        'max_minutes': cfg.max_minutes,
    }
    if cfg.play_scope == 'visited':
        settings['task']['allowed_maps'] = getattr(cfg, 'visited_maps', None)
    from .detour import enabled as detour_enabled
    if detour_enabled(cfg):
        settings['task']['detour'] = 'official_unlocked'   # 允许绕行计划外的已解锁官方地图，计划不变
    if cfg.sim_assist:
        from .sim_assist import AssistRules
        settings['prediction']['assist_budget'] = asdict(AssistRules.of(cfg))
    if cfg.pause_limit:
        settings['planning'] = {key: getattr(cfg, key) for key in ('rounds_per_room', 'room_scale', 'death_bonus')}
    if cfg.stiffness:
        settings['stiffness'] = {key: getattr(cfg, key) for key in ('stiff_unit_s', 'stiff_think_s', 'stiff_tool_s')}
        if getattr(cfg, 'stiff_max_s', None) is not None:      # 没设上限时与原来的设置快照相同
            settings['stiffness']['stiff_max_s'] = cfg.stiff_max_s
    if getattr(cfg, 'tuning', None):
        settings['tuning'] = cfg.tuning
    if not include_task:
        settings['task'].pop('targets', None)
    return settings


def task_brief(cfg, *, include_task=True):
    """Only task identity and boundaries; interface budgets live in tool feedback."""
    from .modes import MODE_NAMES, mode_of
    from .segments import Piece, describe_piece
    scope = getattr(cfg, 'play_scope', 'chapter')
    scope_name = {'chapter': '指定章节或分段', 'campaign': '按计划连续游玩',
                  'visited': '已到访地图内自由探索'}.get(scope, scope)
    slot = f'，槽位 {cfg.save_slot}' if cfg.save_slot is not None else ''
    lines = [f'模式：{MODE_NAMES[mode_of(cfg)]}；存档：{cfg.save}{slot}；范围：{scope_name}。']
    if not include_task:
        lines.append('从已有会话和可用的工作区记录自行恢复任务目标。')
    elif scope == 'visited':
        lines.append('允许地图以主控保存的许可集合及最新观测为准，区分 A/B/C 面；通关不扩大范围。')
    else:
        plan = getattr(cfg, 'plan', None) or [Piece(chapter) for chapter in (cfg.chapters or [cfg.chapter])
                                             if chapter is not None]
        table = getattr(cfg, 'checkpoints', None) or {}
        if plan:
            lines.append('计划目标（依次）：' + '；'.join(describe_piece(piece, table, explicit_side=True) for piece in plan) + '。')
        from .detour import enabled
        if enabled(cfg):
            lines.append('计划外地图仅作获准的绕行，不改变计划目标。')
    if getattr(cfg, 'completion_policy', 'stop') == 'continue':
        lines.append('计划完成后继续在许可范围内活动，直到用户停止、指定停止条件触发或真正阻塞。')
    else:
        lines.append('以主控核验本次目标完成或用户指定停止条件为结束依据。')
    if getattr(cfg, 'max_minutes', None) is not None:
        lines.append(f'本次现实时间上限：{cfg.max_minutes:g} 分钟。')
    return '\n'.join(lines)


def validate_runtime_template(text):
    if '{agent_environment}' not in text:
        raise ValueError('runtime_zh.md 缺少 {agent_environment}：请在控制台“提示词”中保留个人修改并补入该槽位，'
                         '以接收当前执行器的工具与环境约定；原模板未覆盖。迁移说明见 docs/USAGE.md 4.13。')


def runtime_prompt_parts(cfg, *, guide=None, include_task=True):
    """Render the editable startup template and keep local additions separate."""
    from .prompting import PromptPart, prompt_parts, read_part
    from .personal_prompts import snapshot
    if guide is not None:
        if not include_task and guide.get('include_task', True):
            raise ValueError('恢复说明须使用 include_task=False 生成的指南，不能重新引用含任务目标的开局指南')
        include_task = include_task and guide.get('include_task', True)
    personal = guide['personal'] if guide is not None and 'personal' in guide else snapshot(cfg)
    if not workspace_capabilities(cfg).workspace_read:
        parts = [part for part in prompt_parts(cfg) if include_task or not part.is_task]
    else:
        from .prompt_guide import GuideReading, guide_document
        from .prompt_environment import environment_instructions, render_tool_references, tool_name
        guide = guide if guide is not None else guide_document(cfg, include_task=include_task)
        read_plan = guide.get('read_plan') or GuideReading(cfg, guide).read_plan()
        values = {
            '{task_brief}': task_brief(cfg, include_task=include_task),
            '{current_settings}': json.dumps(current_settings(cfg, include_task=include_task), ensure_ascii=False, separators=(',', ':')),
            '{guide_index_path}': guide['index_path'],
            '{guide_read_plan}': read_plan,
            '{game_tools}': '/'.join(tool_name(cfg, name) for name in game_tools(cfg)),
            '{agent_environment}': environment_instructions(cfg),
        }
        template = read_part('runtime_zh.md').removesuffix('\n')
        validate_runtime_template(template)
        # Replace only template tokens, never tokens inside inserted configuration/user text.
        text = re.sub('|'.join(re.escape(key) for key in values), lambda match: values[match.group()],
                      render_tool_references(template, cfg))
        if '{guide_read_plan}' not in template:
            text += '\n\n本次阅读范围以主控以下安排为准。\n' + read_plan
        parts = [PromptPart('当前任务与配置', text, 'runtime_zh.md')]
    if personal['text']:
        parts.append(PromptPart('本机个人追加要求', personal['text'], None, '\n\n'))
    return parts


def runtime_prompt(cfg, *, guide=None, include_task=True):
    return ''.join(part.sep + part.text for part in runtime_prompt_parts(cfg, guide=guide, include_task=include_task))
