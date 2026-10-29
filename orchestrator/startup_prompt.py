"""Small current-task envelope; durable operating instructions live in the workspace."""
from dataclasses import asdict
import json

from .execution import workspace_capabilities
from .modes import act_frame_cap, act_frame_min, collision_image_mode, game_tools, is_explore


def current_settings(cfg):
    from .action_policy import permissions_for
    from .tuning import HOST_CPUS
    plan = getattr(cfg, 'plan', None)
    settings = {
        'task': {'mode': cfg.mode, 'save': cfg.save, 'slot': cfg.save_slot,
                 'scope': cfg.play_scope, 'completion': cfg.completion_policy,
                 'targets': [asdict(p) for p in plan] if plan else cfg.chapters or [cfg.chapter]},
        'story': cfg.cutscene_mode,
        'permissions': permissions_for(cfg).snapshot(),
        'act_frames': [act_frame_min(cfg), act_frame_cap(cfg)],
        'program': {'seconds': cfg.explore_program_s if is_explore(cfg) else cfg.bash_max_s,
                    'calls_between_act': cfg.explore_program_runs if is_explore(cfg) else None,
                    'cpus': getattr(cfg, 'agent_cpus_effective', None) or cfg.agent_cpus or HOST_CPUS},
        'prediction': {'preset_simulator': cfg.sim, 'limited_assist': cfg.sim_assist,
                       'self_written_max_frames': cfg.explore_search_frames if is_explore(cfg) else None},
        'presentation': {'live': cfg.live, 'formal_thought': cfg.formal_thought,
                         'reminder_seconds': cfg.live_remind_s, 'collision_image': collision_image_mode(cfg)},
        'recording': cfg.recording_mode if cfg.record else 'off',
        'max_minutes': cfg.max_minutes,
    }
    if cfg.play_scope == 'visited':
        settings['task']['allowed_maps'] = getattr(cfg, 'visited_maps', None)
    if cfg.sim_assist:
        from .sim_assist import AssistRules
        settings['prediction']['assist_budget'] = asdict(AssistRules.of(cfg))
    if cfg.pause_limit:
        settings['planning'] = {key: getattr(cfg, key) for key in ('rounds_per_room', 'room_scale', 'death_bonus')}
    if cfg.stiffness:
        settings['stiffness'] = {key: getattr(cfg, key) for key in ('stiff_unit_s', 'stiff_think_s', 'stiff_tool_s')}
    if getattr(cfg, 'tuning', None):
        settings['tuning'] = cfg.tuning
    return settings


def runtime_prompt(cfg, *, guide=None):
    from .prompting import play_prompt
    if not workspace_capabilities(cfg).workspace_read:
        return play_prompt(cfg)
    from .prompt_guide import guide_document
    guide = guide if guide is not None else guide_document(cfg)
    return ('你是通过工具游玩 Celeste 的玩家。当前任务与配置：\n'
            + json.dumps(current_settings(cfg), ensure_ascii=False, separators=(',', ':'))
            + '\n工作区操作指南：' + guide['index_path']
            + '\n首次正式游玩接手本会话时，先读索引及各节；仅聊天或取名时按需查阅。同会话已读且版本未变时不重复阅读。'
              '版本变化时对照索引，只补读内容hash变化的章节。压缩后或遗忘时按需补查，并在动作前查阅 /workspace/notes.md、核对最新观测。'
              '工作区的已读记录不能代替新会话自己阅读。指南是操作约定，不是知识库；准备阶段仍按必读清单执行。'
              '\n当前任务配置、用户最新指令和实际工具反馈优先于指南及旧历史；实时权限与目标以主控反馈为准。'
              '先 observe 再操作；计时和目标剩余量用 observe(detail="time") 查询。'
              '仅使用本次登记的工具；按权限操作，不绕过限制。工具故障导致无法继续时用 stop_run 报告并等待，普通死亡不算阻塞。'
              '\n本次游戏工具：' + '/'.join(game_tools(cfg)) + '。')
