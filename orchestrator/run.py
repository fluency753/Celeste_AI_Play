"""主控入口：进入存档 → 接管游戏 → 启动游戏工具服务 → 启动智能体 → 收尾。

用法（在仓库根目录）：
    python -m orchestrator.run --agent dev-opus --save test --model opus --no-record
    python -m orchestrator.run --agent dev-opus --save test --model opus --max-minutes 20 --effort medium --no-record
    python -m orchestrator.run --agent dev --save test --no-agent --no-record   # 只启动工具服务，用于手动调试
    python -m orchestrator.run --agent opus5.5 --save opus5.5 --model opus --chapters 0,1,2   # 正式挑战（速通模式，默认开预置模拟器、规划回放、游戏内 HUD 与 OBS 录屏）
    python -m orchestrator.run --agent opus5.5 --save opus5.5 --resume runs/opus5.5/<时间戳>     # 中断后续跑（沿用原来的开关）

也可以在控制台（python -m overlay.console）里设置并启动。

OBS 录屏默认开启：OBS 没在运行时主控自己启动它（专用配置文件与场景集合，最小化到托盘），结束时关掉它；
OBS 已经开着就直接用、结束时不关。--no-record 关闭录屏，不启动也不调用 OBS。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import importlib.util
import shutil
import socket
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

import uvicorn

from .prompting import current_rules_note, next_chapter_prompt, resume_prompt
from .runtimes import RUNTIMES, runtime
from .config import CHAPTER_SIDS, REPO_ROOT, RunConfig, chapter_name, chapter_name_by_id
from .container import ensure_container
from .game_session import GameSession
from .kb_study import KbStudy
from .execution import workspace_capabilities
from .recording_lifecycle import pending_recording
from .metrics import Metrics
from .recording import Recorder, RecordingError, RecordingHalt, options_from
from .pausegate import PauseGate
from .live import LiveStatus
from .modes import EXPLORE, KNOWLEDGE_OF_MODE, MODE_OF_KNOWLEDGE, SPEEDRUN, describe as describe_mode
from .playback import Playback
from .server import TRANSPORT_SECURITY, build_server, narrate_reminder


def parse_args() -> tuple[RunConfig, argparse.Namespace]:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--agent", required=True, help="智能体标签，用于日志目录与独立配置目录，如 opus5.5、dev-opus")
    ap.add_argument("--engine", choices=list(RUNTIMES), default=None,
                    help="独立执行器：claude（原有）、codex（原生 GPT，复用 Codex 登录）")
    ap.add_argument("--prepared", help="复用 preparation 完成的知识库准备与独立会话（校验知识库未变化）")
    ap.add_argument("--start-paused", action="store_true", help="原生 GPT 启动后保持暂停，可先聊天再点继续")
    ap.add_argument("--mode", choices=["speedrun", "explore"], default=None,
                    help="游玩模式（二选一）：speedrun 速通模式（Claude 执行器默认）：完整知识库，可选是否开放预置模拟器；"
                         "explore 探索模式（原生 GPT 默认）：只有基本操作与工具说明，不开放模拟器文件，可选限额短路线预测。"
                         "直播模式用 --live 叠加")
    ap.add_argument("--knowledge", choices=["explore", "full"], default=None,
                    help="旧参数，等同 --mode：full = speedrun，explore = explore")
    ap.add_argument("--save", required=True, help="游戏内存档名，如 test、opus5.5、fable5.1")
    ap.add_argument("--save-slot", type=int, help="已有存档的槽位编号；同名存档通过槽位区分")
    ap.add_argument('--cutscene-mode',choices=['auto','agent'],default=None,help='剧情：auto主控自动跳过；agent自行观看、推进或跳过')
    ap.add_argument('--allow-chapter-reset', action=argparse.BooleanOptionalAction, default=None,
                    help='允许菜单重启章节和未通关返回地图/保存退出，默认禁止')
    for action, label in (('restart-chapter','重启章节'), ('return-to-map','返回地图'), ('save-quit','保存并退出')):
        ap.add_argument('--allow-' + action, action=argparse.BooleanOptionalAction, default=None, help=label + '权限，默认禁止')
    ap.add_argument('--scope',choices=['chapter','campaign','visited'],default=None,help='单章、顺序游玩或在已到访地图内自由进出；独立于停止目标')
    ap.add_argument('--completion-policy',choices=['stop','continue'],default=None,help='计划通关后停止或继续探索；已到访地图默认继续，其他范围默认停止')
    ap.add_argument("--create-save", action="store_true", help="显式新建独立存档：放在现有最新存档的下一个槽位（不填中间的空位），只允许从序章开始；名称允许重复")
    ap.add_argument("--model", default="opus", help="Claude Code 模型别名：opus / fable 等（默认 opus）；第三方提供方写它的模型名")
    ap.add_argument("--provider", default="anthropic",
                    help="模型提供方（configs/providers.json）：anthropic（默认，Max 订阅）、deepseek 等。"
                         "第三方提供方的 API 密钥只从本机读取（configs/local.providers.json 的 key_file，或环境变量）")
    ap.add_argument("--effort", default="high", help="推理强度标识，支持的档位由所选模型决定")
    ap.add_argument("--max-minutes", type=float, default=240.0, help="现实时间上限（分钟）；目标以游戏内计时为主，这里只防止无限运行")
    ap.add_argument("--bridge-port", type=int, default=47774)
    ap.add_argument('--recording-mode', choices=('chapter','continuous'), default=None, help='新运行默认按章独立录像；continuous跨章连续录制')
    for menu in ('chapter-select','file-select','main-menu'):
        ap.add_argument('--freeze-'+menu, action=argparse.BooleanOptionalAction, default=None, help='接管时冻结此菜单的就绪界面')
    ap.add_argument("--mcp-port", type=int, default=47780)
    ap.add_argument("--play-speed", type=int, default=1, help="智能体动作段的推进速度（每次渲染推进几帧）")
    ap.add_argument("--auto-speed", type=int, default=1, help="转场、剧情、重生段的推进速度；1 与原版一致，大于 1 为快进（只用于调试，录像会失真）")
    ap.add_argument("--chapter", type=int, help="进入的章节编号：0 序章、1 被遗弃之城、2 旧址；默认进入选章页当前选中的章节")
    ap.add_argument("--chapters", help="依次通关的章节编号，逗号分隔，例如 0,1,2（正式挑战）：一章通关后主控自动走完结算画面、"
                                       "进入下一章，智能体接着玩；给了它就不用 --chapter")
    ap.add_argument("--segments", help="按段通关：逗号分隔，每项是 章节（整章）、章节.段 或 章节.起-止，例如 1.1-2,2,9.3。"
                                       "段 k 从检查点 k（0 = 章节开头）到下一个检查点或通关；同一章里相邻的段连续游玩，"
                                       "不相邻时主控直接传送到下一段的起点（需要桥接 1.0.6）。不能与 --chapter / --chapters 合用")
    ap.add_argument("--skip-enter", action="store_true", help="已在关卡内且可操作时，跳过菜单宏")
    ap.add_argument("--no-agent", action="store_true", help="不启动智能体，只运行工具服务直到 Ctrl+C")
    ap.add_argument("--autocompact", help="上下文自动压缩的窗口大小：auto，或 100000~1000000 的 token 数（默认用 Claude Code 的设置）")
    ap.add_argument("--no-study", action="store_true", help="跳过开始前的读知识库阶段（调试用）")
    ap.add_argument("--study-minutes", type=float, default=60.0,
                    help="准备阶段（读知识库、写笔记）的上限分钟数，不计入现实用时；到时自动开放游戏工具")
    ap.add_argument("--max-usage", type=float, default=None,
                    help="可选：Max 套餐 5 小时或 7 天窗口的已用比例达到该值（0~1）就提前暂停智能体、等额度重置后接着玩；"
                         "默认不设，一直用到额度真正用完（用完时同样冻结游戏、等重置后接着玩）")
    ap.add_argument("--hud-port", type=int, default=47790, help="HUD 页面与数据的端口（OBS 浏览器源与置顶窗口都连它）")
    ap.add_argument("--limit-planning", action="store_true",
                    help="限制规划轮次：按章节记账，每进入一个没来过的房间按房间大小存入轮次，死亡补 1 轮，"
                         "只有调用模拟器的消息占一轮，用完后不能再跑模拟器；自动重试关闭。默认不限制思考与调用")
    ap.add_argument("--free-planning", action="store_true", help=argparse.SUPPRESS)  # 旧开关：现在不限制就是默认
    ap.add_argument("--rounds-per-room", type=int, default=3,
                    help="（--limit-planning 时）一屏大小（320×180，1A 第一个房间）的新房间存入的规划轮次（默认 3，至少 3）；大房间按 --room-scale 放大")
    ap.add_argument("--room-scale", type=float, default=2.0,
                    help="房间的长、高每变成这么多倍，存入的轮次乘这么多（默认 2，即与面积成正比），向上取整")
    ap.add_argument("--playback", dest="playback", action="store_true", default=None,
                    help="规划回放（默认开启，写不写都一样）：把推演过的路线按轮画进游戏给观众看（开模拟器时画 /sim 的推演，"
                         "关模拟器时画智能体用 show.trajectory() 交来的轨迹）；回放不推进游戏、不计章节计时")
    ap.add_argument("--no-playback", dest="playback", action="store_false",
                    help="关闭规划回放：不把推演过的路线画进游戏")
    ap.add_argument("--no-hud", action="store_true", help="不在主控进程里启动 HUD 服务（也不打开置顶 HUD 窗口）")
    ap.add_argument("--hud-mod", dest="hud_mod", action="store_true", default=None,
                    help="HUD 以 MOD 形式显示在游戏画面右上角（不开直播时默认开启）：旧版卡片加思考语句历史，章节计时逐帧准确；"
                         "不打开置顶 HUD 窗口，OBS 浏览器源里的网页 HUD 也不显示。需要桥接 0.13.0")
    ap.add_argument("--no-hud-mod", dest="hud_mod", action="store_false",
                    help="不用游戏内 HUD，用网页 HUD（置顶窗口与 OBS 浏览器源；直播模式默认）")
    ap.add_argument("--no-hud-window", action="store_true",
                    help="不在屏幕上打开网页 HUD 窗口（用网页 HUD 时默认打开：独立窗口，可拖动、缩放、切换置顶，关闭后留在托盘；"
                         "录像里的 HUD 不受影响）")
    ap.add_argument("--record", dest="record", action="store_true", default=None,
                    help="OBS 录屏（默认开启，写不写都一样）：OBS 没在运行时主控自己启动它，用锁步插件只录游戏真正推进的帧"
                         "（和规划回放帧），成片（完整版与纯享版）在日志目录的 recording/ 下；结束时关掉主控启动的 OBS")
    ap.add_argument("--no-record", dest="record", action="store_false",
                    help="关闭 OBS 录屏：不启动、不调用 OBS，也不录制")
    ap.add_argument("--keep-obs-open", action="store_true",
                    help="结束时保留主控启动的 OBS（直播时使用）；锁步录像仍正常收尾，OBS 配置选择可在手动关闭后用 tools/obs_setup.py restore 还原")
    music = ap.add_mutually_exclusive_group()
    music.add_argument("--continuous-music", dest="continuous_music", action="store_true", default=None,
                       help="游戏冻结时 BGM 继续播放，音效暂停；录像使用校准后的固定音频偏移")
    music.add_argument("--freeze-music", dest="continuous_music", action="store_false",
                       help="游戏冻结时音乐一起暂停（录像可按静音恢复边自动校准声音）")
    ap.add_argument("--record-marker", action="store_true", help="录制时在画面左下角画帧号条码（只用于校准）")
    ap.add_argument("--ffmpeg", help="ffmpeg 路径（默认：configs/local.recording.json → PATH → winget 安装位置）")
    ap.add_argument("--record-video-args", help="ffmpeg 视频编码参数（默认见 configs/recording.json）")
    ap.add_argument("--audio-offset-ms", type=float, help="声音相对画面的偏移毫秒数（默认用校准结果，见 configs/local.recording.json）")
    ap.add_argument("--sim", dest="sim", action="store_true", default=None,
                    help="开放预置模拟器（容器运行方式下默认开启，写不写都一样）：本机 sim/dist 只读挂到容器的 /sim，"
                         "每次停下把当前帧的完整场景写到 /workspace/game/sim_state.json")
    ap.add_argument("--sim-assist", action=argparse.BooleanOptionalAction, default=None,
                    help="只用于探索模式：有额度的短路线预测（探索模式默认开启）；不向智能体开放模拟器文件")
    ap.add_argument("--stiffness", action=argparse.BooleanOptionalAction, default=None,
                    help="只用于探索模式：思考僵直（默认关；续跑沿用原运行）。一次冻结里思考、运行工具的时间各自满 2.5 分钟后，"
                         "按比例在下一次 act 前让角色保持上一帧的按键僵直（思考每 2.5 分钟 0.2 秒、工具每 2.5 分钟另加 0.4 秒，"
                         "见可调参数 stiff_unit_s、stiff_think_s、stiff_tool_s），不计入 act 的帧数上限")
    ap.add_argument("--collision-image", choices=("always", "room", "off"), default=None,
                    help="工具结果里附碰撞图：always 每次，room 只在进新房间与 observe 时，off 不附（工作区里每次照样写图）；"
                         "不写时续跑沿用原运行，新运行按执行器默认（Codex 每次，其余 room）")
    ap.add_argument("--formal-thought", action=argparse.BooleanOptionalAction, default=None,
                    help="仅正式思考（默认关；续跑沿用原运行）：冻结期间 narrate 写的临时思考只显示在控制台（Claude Code 执行器"
                         "汉字不超过 120 个，可调参数 thought_temp_max_hanzi）；act 的 thought 是正式思考，概括整个冻结期间的"
                         "思考（汉字不超过 30 个），游戏内 HUD 只显示当前这一句，字号更大")
    ap.add_argument("--save-context", action=argparse.BooleanOptionalAction, default=None,
                    help="节省上下文（新运行默认开；续跑沿用原运行，没记过的旧运行按关，免得章节中途换返回格式）："
                         "同一房间里的 act 结果省略每次都一样的样板（观测文件只给一行、不列可选推理强度、没僵直不写僵直说明、"
                         "不重复本房间范围），探索提示词轻提只打印关心的区域")
    ap.add_argument("--live", dest="live", action="store_true", default=None,
                    help="直播模式（默认关，可与速通或探索模式叠加）：HUD 默认用可交互的网页形式，解说计时 10 秒，"
                         "可按用户要求退出关卡；速通 + 直播不改变速通目标，探索 + 直播另加直播节奏")
    ap.add_argument("--no-live", dest="live", action="store_false",
                    help="不开直播模式（默认）；续跑时用来关掉原运行的直播模式")
    ap.add_argument("--no-sim", dest="sim", action="store_false",
                    help="速通模式里关模拟器的版本：不开放预置模拟器，智能体只能用自己的办法通关；提示词不教打法，只写目标、规则与工具接口"
                         "（算出的逐帧轨迹可以用工作区的 show.trajectory() 交给系统画出来）")
    ap.add_argument("--runtime", choices=["container", "host"], default="container",
                    help="container：智能体在 Docker 容器里运行（默认）；host：在本机运行且只开放游戏工具（M2 的过渡方式）")
    ap.add_argument("--no-quota-wait", action="store_true",
                    help="额度用完时直接停止（默认：游戏保持冻结，等额度重置后接着同一个会话继续）")
    ap.add_argument("--quota-wait-max-hours", type=float, default=24.0,
                    help="额度重置还要等超过这么多小时就停止本次运行（之后可用 --resume 续跑），默认 24")
    ap.add_argument("--quota-wait-cap-seconds", type=float, help=argparse.SUPPRESS)  # 只用于测试：等待额度的最长秒数
    ap.add_argument("--no-net-wait", action="store_true",
                    help="模型连不上（断网、接口不可用）时不暂停等待（默认：暂停智能体，游戏保持冻结，现实用时与思考僵直停表，"
                         "网络恢复后自动接着同一个会话继续）")
    ap.add_argument("--net-wait-max-hours", type=float, default=24.0,
                    help="模型连续这么多小时连不上就停止本次运行（之后可用 --resume 续跑），默认 24")
    ap.add_argument("--net-wait-cap-seconds", type=float, help=argparse.SUPPRESS)  # 只用于测试：每次网络等待的最长秒数
    ap.add_argument("--tune", action="append", metavar="名称=值", default=[],
                    help="修改一个可调参数（可以写多个），例如 --tune playback_speed=4 --tune explore_program_s=20；"
                         "全部参数与默认值见 docs/USAGE.md 的\"可调参数\"一节（orchestrator/tuning.py）。续跑沿用原运行改过的参数")
    ap.add_argument("--kb-deny", metavar="文件,...",
                    help="不给智能体看的知识库文件（知识库内的相对路径，逗号分隔，例如 techniques.md,reference/celeste_wiki/Tech.md）："
                         "这些文件不挂进容器，也不在准备阶段要求阅读。默认全部可读。续跑沿用")
    ap.add_argument("--kb-extra", metavar="目录",
                    help="追加本机资料：把这个目录（相对路径按仓库根目录算，例如 local/kb_extra/<系列>）里的文件加进挂给"
                         "智能体的 /kb，同名文件覆盖默认版本。用于某个系列赛自己加的资料，不入库、不影响别的运行。"
                         "续跑沿用；传空字符串取消")
    ap.add_argument("--resume", metavar="RUN_DIR",
                    help="续跑一次中断的运行（主控退出、断电以外的意外）：游戏须仍冻结在原处；接着原来的章节队列、"
                         "统计与 Claude Code 会话。模型、推理强度、章节、--sim 等沿用原运行的设置")
    ap.add_argument("--fresh-session", action="store_true",
                    help="与 --resume 一起用：不接原来的 Claude Code 会话，开一个新会话（原会话无法恢复时用）")
    ap.add_argument("--new-recording", action="store_true",
                    help="与 --resume 一起用：原录像接不上时（OBS 关了或崩溃、独立音频停了）放弃它，这次续跑另起一段录像")
    ap.add_argument("--note-file", metavar="FILE",
                    help="与 --resume 一起用：把这个文件（UTF-8 文本）的内容作为用户留言，附在续跑发给智能体的第一条消息里"
                         "（在主控的续跑说明之后、当前完整规则之前），例如章节之间的说明与新增资料的阅读要求")
    args = ap.parse_args()
    previous = args.previous = load_previous(args.resume) if args.resume else None
    args.note_text = None
    if args.note_file:
        if not args.resume:
            raise SystemExit("--note-file 只能和 --resume 一起用")
        try:
            args.note_text = Path(args.note_file).read_text(encoding="utf-8-sig").strip()
        except OSError as exc:
            raise SystemExit(f"读不了 --note-file：{exc}") from exc
        if not args.note_text:
            raise SystemExit("--note-file 是空的")
    if previous:
        state = previous["state"]
        for key in ("model", "effort", "max_minutes", "runtime", "provider"):
            flag = f"--{key.replace('_', '-')}"
            if not any(a == flag or a.startswith(flag + "=") for a in sys.argv):
                setattr(args, key, state.get(key, getattr(args, key)))
        if not any(a == "--effort" or a.startswith("--effort=") for a in sys.argv):
            # 推理强度沿用中断时生效的那个（智能体可能用 set_effort 调整过），而不是开局的值
            args.effort = previous["metrics"].get("effort") or args.effort
        if args.sim is None:
            args.sim = bool(state["sim"])
        if args.live is None:
            # 直播模式记在 live_mode 里；之前的运行（live 表示实时展示）不算直播
            args.live = bool(state.get("live_mode", False))
        if args.record is None:
            args.record = bool(state.get("record", True))
        if args.hud_mod is None and "hud_mod" in state:
            args.hud_mod = bool(state["hud_mod"])
        if args.playback is None:
            args.playback = bool(state.get("playback", True))
        args.skip_enter = True
        args.no_study = True
    cfg = RunConfig(agent=args.agent, save=args.save, model=args.model, effort=args.effort, provider=args.provider,
                    bridge_port=args.bridge_port, mcp_port=args.mcp_port, max_minutes=args.max_minutes,
                    play_speed=args.play_speed, auto_speed=args.auto_speed, skip_enter=args.skip_enter)
    cfg.chapter = args.chapter
    cfg.recording_mode = args.recording_mode or ((previous['state'].get('recording_mode') or 'continuous') if previous else 'chapter')
    for key, default in (('freeze_chapter_select',True),('freeze_file_select',False),('freeze_main_menu',False)):
        value=getattr(args,key)
        setattr(cfg,key, value if value is not None else previous['state'].get(key,False) if previous else default)
    if previous and cfg.recording_mode != previous['state'].get('recording_mode','continuous') and not args.new_recording:
        raise SystemExit('更改录像组织方式需先收尾旧录像，再显式使用 --new-recording；不会隐式拆分或合并原录像')
    from .action_policy import ActionPermissions, configure, permissions_from_state
    cfg.play_scope=args.scope or (previous['state'].get('play_scope','chapter') if previous else 'chapter')
    choosing_visited = cfg.play_scope == 'visited' and (not previous or previous['state'].get('play_scope') != 'visited')
    from .play_scope import completion_policy
    cfg.completion_policy = completion_policy(cfg.play_scope, args.completion_policy if args.completion_policy is not None else
        previous['state'].get('completion_policy') if previous and not choosing_visited else None)
    permissions = ActionPermissions()
    if previous:
        from .control import RunControl
        permissions = permissions_from_state(previous['state'], RunControl(previous['dir']).action_permissions_preference())
    if choosing_visited:
        permissions = permissions.update({'restart_chapter':True, 'return_to_map':True})
    if args.allow_chapter_reset is not None:
        permissions = ActionPermissions.legacy(args.allow_chapter_reset)
    overrides = {key: getattr(args, 'allow_' + key) for key in ('restart_chapter', 'return_to_map', 'save_quit')
                 if getattr(args, 'allow_' + key) is not None}
    if overrides:
        permissions = permissions.update(overrides)
    configure(cfg, permissions)
    cfg.create_save = args.create_save
    cfg.cutscene_mode = args.cutscene_mode or (previous['state'].get('cutscene_mode','auto') if previous else 'auto')
    if previous and args.cutscene_mode is None:
        from .control import RunControl
        cfg.cutscene_mode=RunControl(previous['dir']).cutscene_preference() or cfg.cutscene_mode
    if cfg.play_scope == 'visited' or cfg.completion_policy == 'continue':
        if args.cutscene_mode == 'auto':
            raise SystemExit('自由探索或通关后继续须使用 agent 剧情模式，由智能体操作结算与选章')
        cfg.cutscene_mode = 'agent'
    if cfg.play_scope == 'visited':
        if args.create_save:
            raise SystemExit('已到访地图自由探索需要已有存档；新存档请先正常游玩')
        if args.chapters or args.segments:
            raise SystemExit('已到访地图自由探索不使用 --chapters 或 --segments 顺序计划')
    cfg.save_slot = args.save_slot if args.save_slot is not None else previous['state'].get('save_slot') if previous else None
    if cfg.play_scope == 'visited' and cfg.save_slot is None:
        raise SystemExit('已到访地图自由探索须显式指定 --save-slot')
    if cfg.save_slot is not None and (cfg.save_slot < 0 or args.create_save):
        raise SystemExit('--save-slot 须为非负槽位编号，不能与 --create-save 合用')
    from .segments import fetch_table, load_table
    cfg.checkpoints = load_table()
    if args.segments:
        if args.chapters or args.chapter is not None:
            raise SystemExit("--segments 不能与 --chapter 或 --chapters 合用")
        from .segments import chapters_of, parse_segments, pieces_of
        cfg.checkpoints = cfg.checkpoints or fetch_table(args.bridge_port)
        if not cfg.checkpoints:
            raise SystemExit("找不到检查点表：configs/checkpoints.json 不存在，游戏里的桥接也没有提供（需要 1.0.6）")
        try:
            cfg.plan = pieces_of(parse_segments(args.segments, cfg.checkpoints), cfg.checkpoints)
        except ValueError as exc:
            raise SystemExit(str(exc)) from None
        cfg.chapters, cfg.chapter = chapters_of(cfg.plan), cfg.plan[0].chapter
    elif args.chapters:
        cfg.chapters = [int(x) for x in args.chapters.split(",") if x.strip()]
        if not cfg.chapters:
            raise SystemExit("--chapters 至少要给一个章节编号")
        cfg.chapter = cfg.chapters[0]
    elif cfg.play_scope=='campaign' and not previous:
        from .play_scope import chapter_sequence
        cfg.chapters=chapter_sequence(cfg.chapter if cfg.chapter is not None else 0,'campaign')
        cfg.chapter=cfg.chapters[0]
    if args.create_save and (args.resume or args.skip_enter or cfg.chapter != 0):
        raise SystemExit('--create-save 只可从序章新开一局，不能与续跑或跳过进档合用')
    cfg.runtime = args.runtime
    cfg.engine = args.engine or (previous["state"].get("engine", "claude") if previous else "claude")
    adapter = runtime(cfg.engine)
    if args.mode and args.knowledge and MODE_OF_KNOWLEDGE[args.knowledge] != args.mode:
        raise SystemExit("--mode 与旧参数 --knowledge 不一致，只写 --mode 即可")
    if previous:
        state = previous["state"]
        default_mode = state.get("mode") or MODE_OF_KNOWLEDGE.get(state.get("knowledge_profile", "full"), SPEEDRUN)
    else:
        default_mode = MODE_OF_KNOWLEDGE.get(adapter.default_knowledge, SPEEDRUN)
    cfg.mode = args.mode or (MODE_OF_KNOWLEDGE[args.knowledge] if args.knowledge else default_mode)
    cfg.knowledge_profile = KNOWLEDGE_OF_MODE[cfg.mode]
    if previous and cfg.mode != default_mode:
        raise SystemExit("不能通过 --resume 改变游玩模式（速通 / 探索）；请新开一次运行，避免继承另一种模式的知识")
    if not any(a == '--model' or a.startswith('--model=') for a in sys.argv) and not previous:
        cfg.model = adapter.default_model
    if not any(a == '--effort' or a.startswith('--effort=') for a in sys.argv) and not previous:
        cfg.effort = adapter.default_effort
    adapter.configure(cfg, cfg.provider)
    if args.prepared and (args.no_study or previous):
        raise SystemExit("--prepared 只用于已准备会话新开一局，不能与 --no-study 或 --resume 合用")
    if args.prepared:
        prepared_metadata = json.loads((Path(args.prepared)/'prepare_summary.json').read_text(encoding='utf-8'))
        if not any(a == '--model' or a.startswith('--model=') for a in sys.argv):
            cfg.model = prepared_metadata.get('requested_model') or prepared_metadata['metrics']['model']
        if not any(a == '--effort' or a.startswith('--effort=') for a in sys.argv):
            cfg.effort = prepared_metadata['metrics']['effort']
    cfg.autocompact = args.autocompact
    cfg.study = not args.no_study
    cfg.study_minutes = args.study_minutes
    cfg.max_usage = args.max_usage
    cfg.quota_wait = not args.no_quota_wait
    cfg.quota_wait_cap_s = args.quota_wait_cap_seconds
    cfg.quota_wait_max_h = args.quota_wait_max_hours
    cfg.net_wait = not args.no_net_wait
    cfg.net_wait_max_h = args.net_wait_max_hours
    cfg.net_wait_cap_s = args.net_wait_cap_seconds
    capabilities = workspace_capabilities(cfg)
    if args.sim is None:
        args.sim = capabilities.programs and cfg.mode == SPEEDRUN
    if cfg.mode == EXPLORE and args.sim:
        raise SystemExit("探索模式不开放模拟器文件（只能用 --sim-assist 的限额短路线预测）；要开模拟器请用速通模式 --mode speedrun")
    if cfg.mode == SPEEDRUN and args.sim_assist:
        raise SystemExit("--sim-assist 只用于探索模式；速通模式用 --sim / --no-sim 决定是否开放预置模拟器")
    cfg.sim = args.sim
    cfg.sim_assist = (bool(previous["state"].get("sim_assist", False)) if previous else
                      cfg.knowledge_profile == "explore" and capabilities.programs
                      and (cfg.sim_dir / 'celeste-sim').is_file()) if args.sim_assist is None else args.sim_assist
    if cfg.sim_assist and (cfg.sim or not capabilities.programs):
        raise SystemExit("限额模拟辅助仅支持容器模式，不能同时开放无限制 --sim")
    if cfg.sim_assist and not (cfg.sim_dir / "celeste-sim").is_file():
        raise SystemExit("限额模拟辅助需要 sim/dist/celeste-sim；可用 --no-sim-assist 关闭")
    if args.stiffness and cfg.mode != EXPLORE:
        raise SystemExit("--stiffness（思考僵直）只用于探索模式")
    cfg.stiffness = (bool(previous["state"].get("stiffness", False)) if previous and args.stiffness is None
                     else bool(args.stiffness)) and cfg.mode == EXPLORE
    cfg.formal_thought = (bool(previous["state"].get("formal_thought", False)) if previous and args.formal_thought is None
                          else bool(args.formal_thought))
    cfg.collision_image = (previous["state"].get("collision_image") if previous and args.collision_image is None
                           else args.collision_image)
    cfg.save_context = save_context_choice(previous, args.save_context)
    cfg.pause_limit = bool(args.limit_planning) and not args.free_planning
    cfg.rounds_per_room = max(1, args.rounds_per_room)
    cfg.room_scale = max(1.0, args.room_scale)
    cfg.playback = args.playback is not False   # 默认开启
    cfg.live = bool(args.live)            # 直播模式，默认关
    cfg.hud_mod = (not cfg.live) if args.hud_mod is None else bool(args.hud_mod)   # 游戏内 HUD：不开直播时默认开
    from .tuning import apply as apply_tuning, parse_pairs  # 可调参数：续跑沿用原运行改过的，再叠加这次写的 --tune
    try:
        tuning = dict(previous["state"].get("tuning") or {}) if previous else {}
        tuning.update(parse_pairs(args.tune))
        apply_tuning(cfg, tuning)
    except ValueError as exc:
        raise SystemExit(f"--tune：{exc}") from None
    cfg.live_remind_s = cfg.remind_live_s if cfg.live else cfg.remind_s   # 解说计时：默认直播 10 秒，其余半分钟
    if args.kb_deny is not None:
        cfg.kb_deny = sorted({x.strip().replace("\\", "/") for x in args.kb_deny.split(",") if x.strip()})
    elif previous:
        cfg.kb_deny = list(previous["state"].get("kb_deny") or [])
    cfg.kb_extra = kb_extra_choice(previous, args.kb_extra)
    if cfg.kb_extra and not cfg.kb_extra_dir.is_dir():
        raise SystemExit(f"--kb-extra：找不到目录 {cfg.kb_extra_dir}")
    args.record = args.record is not False  # 默认开启
    cfg.record = args.record
    from .recording import load_config
    cfg.continuous_music = bool(load_config().get("continuous_music", False) if args.continuous_music is None
                               else args.continuous_music)
    if not args.no_agent and adapter.mcp:
        check_provider(cfg.provider_info)
    if adapter.mcp and cfg.third_party and cfg.runtime != "container" and not args.no_agent:
        raise SystemExit("第三方模型提供方只支持容器运行方式")
    if previous:
        state = previous["state"]
        if (args.agent, args.save) != (state["agent"], state["save"]):
            raise SystemExit(f"--resume 的运行是 --agent {state['agent']} --save {state['save']}，与这次给的不一致")
        cfg.resume_completion_pending = bool(state.get('completion_pending'))
        if cfg.play_scope == 'visited':
            if cfg.save_slot is None or cfg.save_slot != state.get('save_slot'):
                raise SystemExit('已到访地图自由探索续跑须沿用原存档槽位')
            cfg.visited_maps = state.get('visited_maps') if state.get('play_scope') == 'visited' else None
        if cfg.play_scope == 'visited':
            cfg.plan = cfg.chapters = None
            cfg.chapter = None
            cfg.resume_pending = False
            cfg.resume_segment = None
        else:
            done = len(previous["metrics"].get("splits") or [])
            # 原计划已经全部完成时，--chapters / --segments 追加接下来要玩的部分：同一会话、设置与统计接着算
            from .segments import Piece, chapters_of
            extension = (list(cfg.plan) if args.segments else [Piece(c) for c in cfg.chapters] if args.chapters else None)
            if extension is not None:
                cfg.resume_completion_pending = False
            if extension is not None and args.chapter is not None:
                raise SystemExit("续跑追加章节用 --chapters 或 --segments，不用 --chapter")
            if state.get("plan_pieces"):
                # 按段通关（或运行中改过计划）：按已完成的截数从计划里接着走
                pieces = [Piece(int(c), int(s), None if e is None else int(e)) for c, s, e in state["plan_pieces"]]
                remaining_pieces = ([Piece(int(c),int(start),None if end is None else int(end)) for c,start,end in state['navigation_remaining']]
                                    if cfg.completion_policy == 'continue' and state.get('navigation_remaining') is not None
                                    else pieces[done:])
                if not remaining_pieces and cfg.completion_policy == 'continue' and extension is None:
                    remaining_pieces = [pieces[-1]]
                    cfg.resume_completion_pending = False
                if extension is not None:
                    if remaining_pieces:
                        raise SystemExit("原运行的计划还没有完成：续跑沿用原计划；要改范围请在运行中用控制台改")
                    pieces, remaining_pieces = pieces + extension, extension
                    cfg._extended_plan = pieces
                if not remaining_pieces:
                    raise SystemExit("这次运行的计划已经全部完成，不需要续跑（要接着玩下一章：加 --chapters 或 --segments）")
                if any(not p.whole() for p in pieces) and not cfg.checkpoints:
                    cfg.checkpoints = fetch_table(args.bridge_port)
                    if not cfg.checkpoints:
                        raise SystemExit("续跑按段通关的运行需要检查点表（configs/checkpoints.json 或桥接 1.0.6）")
                cfg.plan = remaining_pieces
                cfg.chapters, cfg.chapter = chapters_of(remaining_pieces), remaining_pieces[0].chapter
                cfg.resume_pending = bool(state.get("piece_pending")) and extension is None
                if cfg.completion_policy == 'continue' and not state.get('navigation_remaining') and done >= len(pieces):
                    cfg.resume_pending = False
                # 追加的章节从开头玩：原运行记下的段号是上一章的
                cfg.resume_segment = None if cfg.resume_pending or extension is not None else state.get("segment_index")
                remaining = cfg.chapters
            else:
                remaining = ([int(p[0]) for p in state['navigation_remaining']]
                             if cfg.completion_policy == 'continue' and state.get('navigation_remaining') is not None else
                             state["all_chapters"][done:] if state.get("all_chapters") else [])
                if not remaining and cfg.completion_policy == 'continue' and extension is None and state.get('all_chapters'):
                    remaining = [state['all_chapters'][-1]]
                    cfg.resume_completion_pending = False
                if extension is not None:
                    if remaining:
                        raise SystemExit("原运行的章节还没有全部通关：续跑沿用原计划")
                    if any(not p.whole() for p in extension):
                        raise SystemExit("这次运行没有按段的计划：续跑只能追加整章（--chapters）")
                    remaining = [p.chapter for p in extension]
                    cfg._extended_plan = [Piece(c) for c in state.get("all_chapters") or []] + extension
                if state.get("all_chapters") and not remaining:
                    raise SystemExit("这次运行的章节已经全部通关，不需要续跑（要接着玩下一章：加 --chapters）")
                cfg.chapters = remaining or None
                cfg.chapter = remaining[0] if remaining else None
    if cfg.completion_policy == 'continue' and cfg.plan:
        from .segments import plan_is_partial
        if plan_is_partial(cfg.plan):
            raise SystemExit('通关后继续目前适用于整章范围；按段计划请使用 stop 完成策略')
    if cfg.pause_limit and not cfg.sim:
        raise SystemExit("--limit-planning 只用于开模拟器的版本（关模拟器的版本不限制思考与调用）")
    if cfg.sim:
        if not capabilities.programs:
            raise SystemExit("预置模拟器只能用于容器运行方式（本机运行方式请加 --no-sim）")
        if not (cfg.sim_dir / "celeste-sim").is_file():
            raise SystemExit(f"找不到预置模拟器：{cfg.sim_dir / 'celeste-sim'}（模拟器只在本机，见使用说明的预置模拟器一节；"
                             "没有模拟器时用 --no-sim 运行关模拟器的版本）")
    return cfg, args


def load_previous(path: str) -> dict:
    """读出要续跑的那次运行：run_state.json（设置与会话）和 metrics.json（统计）。"""
    run_dir = Path(path)
    if not run_dir.is_absolute():
        run_dir = (Path.cwd() / run_dir) if (Path.cwd() / run_dir).exists() else REPO_ROOT / run_dir
    try:
        state = json.loads((run_dir / "run_state.json").read_text(encoding="utf-8"))
        metrics = json.loads((run_dir / "metrics.json").read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SystemExit(f"--resume：{run_dir} 里找不到 {Path(exc.filename).name}（只能续跑带 run_state.json 的运行）")
    return {"dir": run_dir, "state": state, "metrics": metrics}


def prompt_override_hashes() -> dict[str, str]:
    """这次用到的提示词本机修改（控制台"提示词"页保存在 local/prompts/ 的文件）：文件名 → 内容的 sha256 前 12 位。"""
    import hashlib
    from .prompting import OVERRIDES, overridden_parts
    return {name: hashlib.sha256((OVERRIDES / name).read_bytes()).hexdigest()[:12] for name in overridden_parts()}


def write_run_state(run_dir: Path, cfg: RunConfig, session_id: str | None, previous: dict | None) -> None:
    """记下续跑需要的设置与 Claude Code 会话编号。"""
    all_chapters = previous["state"].get("all_chapters") if previous else (list(cfg.chapters) if cfg.chapters else None)
    extended = getattr(cfg, "_extended_plan", None)     # 续跑时追加了章节（见 parse_args）
    if extended:
        from .segments import chapters_of
        plan_pieces = [[p.chapter, p.start, p.end] for p in extended]
        all_chapters = chapters_of(extended)
    elif previous:
        plan_pieces = previous["state"].get("plan_pieces")
    elif cfg.plan:
        plan_pieces = [[p.chapter, p.start, p.end] for p in cfg.plan]
    else:
        plan_pieces = None
    if cfg.play_scope == 'visited':
        all_chapters = plan_pieces = None
    state = {"agent": cfg.agent, "save": cfg.save, "save_slot": cfg.save_slot, "cutscene_mode":cfg.cutscene_mode, "action_permissions":cfg.action_permissions, "allow_chapter_reset":cfg.allow_chapter_reset, "play_scope":cfg.play_scope, "completion_policy":cfg.completion_policy, "visited_maps":cfg.visited_maps, "model": cfg.model, "provider": cfg.provider, "engine": cfg.engine, "requested_model":cfg.model, "mcp_port":cfg.mcp_port,
             "effort": cfg.effort, "max_minutes": cfg.max_minutes, "knowledge_profile": cfg.knowledge_profile,
             "runtime": cfg.runtime, "sim": cfg.sim, "sim_assist": cfg.sim_assist, "stiffness": cfg.stiffness, "formal_thought": cfg.formal_thought, "collision_image": cfg.collision_image, "save_context": cfg.save_context, "live": cfg.live, "live_mode": cfg.live, "mode": cfg.mode, "record": cfg.record, "playback": cfg.playback, "hud_mod": cfg.hud_mod, "all_chapters": all_chapters, "plan_pieces": plan_pieces,
             "piece_pending": False, "completion_pending":cfg.resume_completion_pending, "session_id": session_id,
             "tuning": dict(cfg.tuning), "kb_deny": list(cfg.kb_deny), "kb_extra": cfg.kb_extra, "prompt_overrides": prompt_override_hashes(),
             "resumed_from": str(previous["dir"]) if previous else None,
             "controller_host": os.environ.get("CELESTE_CONTROLLER_HOST"), "controller_pid": os.getpid(),
             "controller_parent_pid": os.getppid(),
             "bridge_port": cfg.bridge_port}
    from .segments import Piece
    current_plan = cfg.plan or [Piece(c) for c in (cfg.chapters or ([] if cfg.chapter is None else [cfg.chapter]))]
    state['navigation_remaining'] = [] if cfg.play_scope == 'visited' else [[p.chapter,p.start,p.end] for p in map(Piece.of,current_plan)]
    state.update({key:getattr(cfg,key) for key in ('recording_mode','freeze_chapter_select','freeze_file_select','freeze_main_menu')})
    (run_dir / "run_state.json").write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def check_provider(info: dict) -> None:
    """未实测的提供方给出提示；经本机网关接入的（例如 GPT）开跑前检查网关在不在运行。"""
    status = str(info.get("status") or "ready")
    if status != "ready":
        print(f"[run] 注意：模型提供方 {info.get('label') or info.get('name')} {status}", flush=True)
    url = info.get("gateway_check")
    if url:
        import urllib.request  # noqa: WPS433
        try:
            with urllib.request.urlopen(url, timeout=5):
                pass
        except Exception as exc:  # noqa: BLE001
            raise SystemExit(f"连不上 {info.get('name')} 的接口转换网关（{url}）：{exc}。先在本机启动这个网关（地址见平台配置的 gateway_check）")


def make_run_dir(agent: str) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = REPO_ROOT / "runs" / agent / stamp
    run_dir.mkdir(parents=True, exist_ok=True)
    (REPO_ROOT / "runs" / "latest.txt").write_text(str(run_dir), encoding="utf-8")
    return run_dir


def startup_step_options(client, cfg):
    """Bind visited permissions from native save facts before any entry input."""
    from .action_policy import step_options
    if getattr(cfg, 'play_scope', 'chapter') != 'visited':
        return step_options(cfg, departure=True)
    if type(cfg.save_slot) is not int or cfg.save_slot < 0:
        raise ValueError('自由探索载入须明确指定存档槽位；未发送输入')
    from .free_roam import validate_maps
    if cfg.visited_maps is None:
        obs = client.call('observe', image=False, entities=False, tiles=False)['obs']
        save = obs.get('save') or {}
        candidate = (save if save.get('slot') == cfg.save_slot and save.get('name') == cfg.save else
                     next((row for row in (obs.get('menu') or {}).get('slots', [])
                           if row.get('fileSlot') == cfg.save_slot and row.get('name') == cfg.save
                           and row.get('exists') is True), None))
        if candidate is not None and candidate.get('visited_maps') is not None:
            cfg.visited_maps = validate_maps(candidate['visited_maps'])
    maps = [] if cfg.visited_maps is None else cfg.visited_maps
    return step_options(cfg, maps=maps, slot=cfg.save_slot, departure=True)


def enter_save(cfg: RunConfig, recorder: Recorder | None = None) -> None:
    """菜单宏进入存档。录制时开局也录进去：先（不录、快进）退回选档页，从那里开始录制，
    再按正常速度选存档、进地图、选章节、进关卡、用暂停菜单跳过开场剧情。"""
    from tools.enter_save import enter_save as macro, load_save_and_freeze, to_file_select  # noqa: WPS433（延迟导入，避免 --help 时连接游戏）
    from tools.bridge_client import BridgeClient
    from .chapter_recording import ChapterRecording

    if (recorder is not None and getattr(cfg, 'recording_mode', 'continuous') == 'chapter'
            and not isinstance(recorder, ChapterRecording)):
        # Every chapter-mode caller must hand over the playlist owner; a bare
        # Recorder would bypass rotation and fail after capture already started.
        raise RecordingError('分章录制对象未接入分章清单；未连接游戏或发送菜单输入')
    client = BridgeClient(port=cfg.bridge_port, timeout=120)
    # Startup navigation is explicitly requested by the user, before the agent
    # owns this connection. Allow leaving a previously loaded map to enter the
    # selected save; the runtime session installs its own stricter policy.
    client.step_options = lambda: startup_step_options(client, cfg)
    from .chapter_recording import menu_options
    if getattr(cfg, 'play_scope', 'chapter') == 'visited' and client.call('hello').get('map_entry_set') is not True:
        client.close()
        raise RuntimeError('已到访地图自由探索需要支持地图集合门禁的新版桥接；未发送游戏输入')
    client.call('activate', **menu_options(cfg))
    def navigate(**options):
        if getattr(cfg, 'play_scope', 'chapter') == 'visited':
            return load_save_and_freeze(client, cfg.save, save_slot=cfg.save_slot, **options)
        return macro(client, cfg.save, chapter=cfg.chapter, checkpoint=first_checkpoint(cfg),
                     create_if_missing=cfg.create_save, save_slot=cfg.save_slot,
                     cutscene_mode=cfg.cutscene_mode, **options)
    try:
        t0 = time.monotonic()
        if recorder is not None and recorder.started:
            # Recovery already owns a fresh physical chunk of the original
            # logical recording. Do not navigate backwards or send start again.
            recorder.call = client.call
            if getattr(cfg,'recording_mode','continuous') == 'chapter':
                client.before_step = recorder.before_step
            obs = navigate(menu_speed=1)
        elif recorder is not None:
            to_file_select(client)
            recorder.call = client.call
            recorder.start()
            if getattr(cfg,'recording_mode','continuous') == 'chapter':
                client.before_step = recorder.before_step
            if getattr(cfg,'recording_mode','continuous') == 'chapter':
                print("[record] 分章采集已准备：排除选档与选章，从章节第一帧开始录制", flush=True)
            else:
                print("[record] 从选档页开始连续录制：选存档、进章节、跳过开场都按正常速度录进录像", flush=True)
            obs = navigate(menu_speed=1)
        else:
            # 保留的原录制已经在写；菜单宏同样按正常速度推进，不再发送 record start。
            resume_options = {'menu_speed': 1} if getattr(cfg, '_recording_resume_manifest', None) else {}
            obs = navigate(**resume_options)
        cfg.save_slot = (obs.get('save') or {}).get('slot', cfg.save_slot)
        print(f"[run] 已进入存档 {cfg.save}：{(obs.get('level') or {}).get('sid',obs.get('scene'))} 房间 {(obs.get('level') or {}).get('room','—')}"
              f"（菜单宏用时 {time.monotonic() - t0:.1f} 秒）", flush=True)
    except BaseException:
        cfg._keep_frozen = True
        try:
            if recorder is not None and recorder.started:
                recorder.stop()
        except Exception as exc:
            print(f'[record] 菜单导航失败后的录像收尾也失败：{exc}；保留游戏状态', flush=True)
        raise
    finally:
        client.close()


def add_gate_route(app, gate: PauseGate, metrics=None, live_remind_s: float | None = None, session=None) -> None:
    """容器里的 PreToolUse 钩子（agent-container/celeste-gate.py）每次调用工具前 POST 到这里。
    实时展示时（live_remind_s 不为空）兼作解说更新计时器的提醒通道：思考状态累计满 live_remind_s 秒没有 narrate，
    回复里带上 context，钩子把它作为 additionalContext 交给 Claude Code，出现在这次工具结果旁边。"""
    from starlette.requests import Request  # noqa: WPS433
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    async def endpoint(request: Request):
        try:
            data = await request.json()
        except ValueError:
            data = {}
        tool_name = str(data.get("tool_name") or "")
        guard = getattr(session, 'program_guard', getattr(session, 'control_guard', None))
        if tool_name == 'Bash' and guard and (reason := guard()):
            return JSONResponse({'decision': 'deny', 'reason': reason, 'context': ''})
        allow, reason = await gate.decide(tool_name, data.get("tool_use_id"))
        if allow and tool_name == 'Bash' and session is not None and hasattr(session, 'program_run_denied'):
            denied = session.program_run_denied()     # 探索模式：两次 act 之间的规划程序次数（explore_program_runs）
            if denied:
                return JSONResponse({'decision': 'deny', 'reason': denied, 'context': ''})
        context = ""
        if allow and live_remind_s and metrics is not None:
            if tool_name.endswith("__narrate"):
                metrics.narrated()
            elif (waited := metrics.take_live_reminder(live_remind_s)) is not None:
                context = narrate_reminder(getattr(session, "cfg", None), waited)
                print(f"[live] 提醒更新 HUD 思考（已思考 {waited:.0f} 秒，调用 {tool_name}）", flush=True)
        notes = getattr(session, 'note_reminders', None)
        offered = notes.peek() if allow and notes is not None else ()
        if offered:
            context = '\n'.join(value for value in (context, '\n'.join(text for _, text in offered)) if value)
        response = JSONResponse({"decision": "allow" if allow else "deny", "reason": reason, "context": context})
        if offered:
            notes.acknowledge(offered)
        return response

    app.router.routes.insert(0, Route("/gate", endpoint, methods=["POST"]))


def ensure_obs() -> bool:
    """OBS 录屏开着时调用：OBS 没在运行就用专用的配置文件与场景集合启动它（最小化到托盘，等锁步插件连上桥接），
    返回 True 表示是主控启动的（结束时由主控关掉）；OBS 已经在运行就直接用，返回 False。"""
    from tools import obs_setup  # noqa: WPS433（延迟导入：不录屏时不碰 OBS）
    if obs_setup.obs_running():
        print("[record] OBS 已经在运行，沿用当前场景；结束时不关闭", flush=True)
        return False
    try:
        obs_dir = obs_setup.find_obs_dir(None)
    except SystemExit as exc:
        raise SystemExit(f"{exc}。不录屏时加 --no-record") from None
    print("[record] 启动 OBS（专用配置文件与场景集合，最小化到托盘）…", flush=True)
    obs_setup.launch(obs_setup.load_config(), obs_dir, minimized=True)
    return True


def close_obs() -> None:
    """关掉主控启动的 OBS，并把 OBS 下次打开时选中的配置还原成原来的。此时录制已经收尾（成片与纯享版已写好），
    正常关闭 30 秒内没有退出（最小化到托盘的 OBS 有时不响应）就强制结束。"""
    from tools import obs_setup  # noqa: WPS433
    try:
        obs_setup.stop(wait=30.0, force=True)
    except SystemExit as exc:
        print(f"[record] {exc}", flush=True)


def start_mod_hud(cfg: RunConfig, run_dir: Path):
    """游戏内 HUD（--hud-mod）：从开局录制到录像收尾一直显示，读的是这次运行的 metrics.json 与公开解说历史。"""
    if not getattr(cfg, "hud_mod", False):
        return None
    try:
        from overlay.modhud import ModHud  # noqa: WPS433
        hud = ModHud(run_dir, bridge_port=cfg.bridge_port, log=lambda m: print(m, flush=True))
        hud.start()
    except Exception as exc:  # noqa: BLE001（HUD 起不来不影响运行）
        print(f"[run] 游戏内 HUD 启动失败：{exc}", flush=True)
        return None
    print("[run] HUD 以 MOD 形式显示在游戏画面右上角（--no-hud-mod 改用网页 HUD）", flush=True)
    return hud


def write_placeholder_metrics(run_dir: Path, cfg: RunConfig) -> None:
    """开局录制期间 HUD 显示的内容：名字、推理强度与"等待"，计时都是 0（主控接管后由 Metrics 接着写）。"""
    now = time.time()
    data = {"agent": cfg.agent, "model": cfg.model, "effort": cfg.effort, "status": "starting", "started_wall": 0.0,
            "budget_s": cfg.budget_seconds, "chapter": "", "room": "", "thought": "", "written_wall": now,
            "phase_since_wall": now, "thinking_since_wall": now, "hud_mod": bool(getattr(cfg, "hud_mod", False)),
            "formal_only": bool(getattr(cfg, "formal_thought", False))}
    (run_dir / "metrics.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def kb_extra_choice(previous: dict | None, flag: str | None) -> str:
    """本机追加资料的目录：命令行写了就按命令行（空字符串表示取消）；续跑沿用原运行记下的目录；新运行默认不追加。"""
    if flag is not None:
        return Path(flag.strip()).as_posix() if flag.strip() else ""
    if previous:
        return str(previous["state"].get("kb_extra") or "")
    return ""


def save_context_choice(previous: dict | None, flag: bool | None) -> bool:
    """节省上下文开不开：命令行写了就按命令行；新运行默认开；续跑沿用原运行记下的设置，没记过的旧运行按关
    （免得章节中途换了 act 返回的格式；追加下一章时显式加 --save-context）。"""
    if flag is not None:
        return bool(flag)
    if previous:
        return bool(previous["state"].get("save_context", False))
    return True


def check_resume_position(cfg: RunConfig) -> bool:
    """续跑前看游戏停在哪里。返回 True：游戏就停在要续跑的章节里，直接接着玩；返回 False：要用菜单宏进入
    （中断发生在两章或两截之间、游戏重启过、还没载入存档或在别的存档的菜单里），由调用方像新开一局那样进入，
    开了录屏时这段也从选档页开始录进录像。关卡里是别的存档或别的章节时报错，不替你离开那个关卡。"""
    from tools.bridge_client import BridgeClient

    client = BridgeClient(port=cfg.bridge_port, timeout=120)
    try:
        if cfg.play_scope == 'visited' and client.call('hello').get('map_entry_set') is not True:
            raise SystemExit('已到访地图自由探索需要支持地图集合门禁的新版桥接；未发送游戏输入')
        obs = client.call("observe", image=False, entities=False, tiles=False)["obs"]
        # A saved native Session may resume directly into its Level. Do not turn
        # "load this save" into "leave that Level to reach chapter select".
        if cfg.chapter is None and obs.get('scene') == 'Overworld' and (
                (obs.get('save') or {}).get('name') != cfg.save or
                (cfg.save_slot is not None and (obs.get('save') or {}).get('slot') != cfg.save_slot) or
                (obs.get('menu') or {}).get('current') not in {'OuiChapterSelect', 'OuiChapterPanel'}):
            from tools.enter_save import load_save_and_freeze
            from .action_policy import controller_navigation
            if cfg.play_scope == 'visited':
                client.step_options = lambda: startup_step_options(client, cfg)
                obs = load_save_and_freeze(client, cfg.save, save_slot=cfg.save_slot)
            else:
                with controller_navigation(client, cfg, slot=cfg.save_slot):
                    obs = load_save_and_freeze(client, cfg.save, save_slot=cfg.save_slot)
        save = (obs.get("save") or {}).get("name")
        right_save = save == cfg.save and (cfg.save_slot is None or (obs.get('save') or {}).get('slot') == cfg.save_slot)
        sid = (obs.get("level") or {}).get("sid")
        want = CHAPTER_SIDS.get(cfg.chapter) if cfg.chapter is not None else sid
        if cfg.resume_pending:
            if cfg.cutscene_mode == 'agent' and right_save:
                return True  # retain agent-owned navigation, including the previous chapter's menu
            # 上次在两截之间中断（这一截已完成、还没换过去）：先进入下一截的起点
            print(f"[run] 续跑：上次停在两截之间，进入{chapter_name_by_id(cfg.chapter)}第 {first_checkpoint(cfg)} 个检查点", flush=True)
            if obs.get("scene") == "Level" and not right_save:
                raise SystemExit(f"游戏当前在存档 {save!r} 的关卡里，不是 {cfg.save!r}：先退出那个关卡再续跑")
            return False
        if obs.get("scene") == "Level":
            if not right_save:
                raise SystemExit(f"游戏当前在存档 {save!r} 的关卡里，不是 {cfg.save!r}：先退出那个关卡再续跑")
            if sid != want and cfg.play_scope != 'visited':
                raise SystemExit(f"游戏当前在 {chapter_name(sid)}，续跑的章节是 {chapter_name(want)}：请确认 --resume 的目录")
            print(f"[run] 续跑：游戏停在 {chapter_name(sid)} 房间 {(obs.get('level') or {}).get('room')}", flush=True)
            return True
        from tools.cutscene import story_pending
        if cfg.cutscene_mode == 'agent' and right_save and story_pending(obs):
            return True
        if cfg.cutscene_mode == 'agent' and right_save and obs.get('scene') == 'Overworld' and (obs.get('menu') or {}).get('current') in {'OuiChapterSelect','OuiChapterPanel'}:
            return True
        if cfg.cutscene_mode == 'agent' and right_save and cfg.resume_completion_pending and obs.get('scene') in {'AreaComplete','LevelExit','OverworldLoader','Overworld'}:
            return True
        if cfg.chapter is None:
            raise SystemExit(f"游戏当前不在关卡里（{obs.get('scene')}），无法续跑")
        print(f"[run] 续跑：游戏在 {obs.get('scene')}（存档 {save!r}），用菜单宏进入{cfg.save} 的"
              f"{chapter_name_by_id(cfg.chapter)}", flush=True)
        return False
    except BaseException:
        # This connection either only observed an existing state, or performed
        # native loading. Neither failure authorizes unfreezing that state.
        cfg._keep_frozen = True
        raise
    finally:
        client.close()


def first_checkpoint(cfg: RunConfig) -> int:
    """计划里第一截的起点检查点（整章或没有按段通关时为 0 = 章节开头）。"""
    return cfg.plan[0].start if cfg.plan else 0


def backup_save(obs: dict, run_dir: Path) -> None:
    """开跑前把当前存档的全部文件（N.celeste 与 Everest 的 N-modsave-*、N-modsession-*、N-modsavedata）
    复制到本次运行的目录，出问题时可以手动还原。"""
    path = (obs.get("save") or {}).get("file")
    if not path:
        print("[run] 提示：桥接没有报告存档文件的位置（桥接 0.10.2 以上才有），跳过存档备份", flush=True)
        return
    src = Path(path)
    dest = run_dir / "save_backup"
    copied = []
    candidates = [src, *sorted(src.parent.glob(f"{src.stem}-*{src.suffix}"))] if src.parent.is_dir() else [src]
    for candidate in candidates:
        if candidate.is_file():
            dest.mkdir(exist_ok=True)
            shutil.copy2(candidate, dest / candidate.name)
            copied.append(candidate.name)
    print(f"[run] 已备份存档 {len(copied)} 个文件（{src.name} 等）→ {dest}" if copied
          else "[run] 提示：存档文件还不存在，跳过备份", flush=True)


def start_hud_window(port: int, run_dir: Path) -> subprocess.Popen | None:
    """在屏幕上打开网页 HUD 的独立窗口（只在 Windows；overlay/window.py）：无边框，可拖动、缩放、切换置顶，关闭后留在托盘。
    录像里的网页 HUD 来自 OBS 的浏览器源，与它无关；OBS 的游戏采集也采不到这个窗口，所以录像里不会出现两份。"""
    if sys.platform != "win32":
        return None
    import ctypes  # noqa: WPS433

    if ctypes.windll.user32.FindWindowW(None, "Celeste AI HUD"):
        print("[run] 置顶 HUD 窗口已经开着，沿用", flush=True)
        return None
    if importlib.util.find_spec("webview") is None:
        print("[run] 提示：没装 pywebview，不打开置顶 HUD 窗口（.venv 里 pip install -r requirements.txt）", flush=True)
        return None
    log = open(run_dir / "hud_window.log", "ab")
    proc = subprocess.Popen([sys.executable, "-m", "overlay.window", "--url", f"http://127.0.0.1:{port}/hud"],
                            cwd=str(REPO_ROOT), stdout=log, stderr=subprocess.STDOUT,
                            creationflags=0x08000000)  # CREATE_NO_WINDOW：不弹出控制台
    print("[run] 已打开 HUD 窗口（--no-hud-window 关闭）", flush=True)
    return proc


def stop_hud_window(proc: subprocess.Popen | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


def port_free(port: int) -> bool:
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


async def run_async(cfg: RunConfig, args: argparse.Namespace, run_dir: Path, token: str | None,
                    previous: dict | None = None, recorder: Recorder | None = None) -> dict:
    study = None
    if workspace_capabilities(cfg).needs_container and not args.no_agent:
        from .kb_access import prepare_view
        prepare_view(cfg)        # 有不给看的文件或本机追加资料时，同步合并后的副本（挂进容器 /kb）
    if cfg.study and workspace_capabilities(cfg).workspace_read and not args.no_agent:
        extra = {"/sim/README.md": cfg.sim_dir / "README.md"} if cfg.sim else None
        study = KbStudy(cfg.kb_dir, cfg.study_minutes, extra)
        if not study.has_reference and cfg.knowledge_profile == "full":
            print("[run] 提示：kb/reference/ 不存在，准备阶段只读知识库本身（见使用说明 2.5 节生成参考资料原文）", flush=True)
    prepared = None
    if args.prepared:
        from .prepared import load_preparation
        prepared = load_preparation(args.prepared, cfg, study)
    metrics = Metrics(run_dir / "metrics.json", cfg.agent, cfg.model, cfg.effort, cfg.budget_seconds,
                      study=study is not None)
    if prepared:
        metrics.kb_finished()
        metrics.update(kb_progress=1.0, study_s=prepared["metrics"].get("study_s", 0),
                       tool_calls=prepared["metrics"].get("tool_calls", 0),
                       compactions=prepared["metrics"].get("compactions", 0))
    if previous:
        metrics.restore(previous["metrics"], str(previous["dir"]))
    metrics.update(formal_only=bool(cfg.formal_thought))   # 开局录制与续跑进入关卡期间，游戏内 HUD 就按开关显示
    if not args.no_hud:
        from overlay.server import start_in_thread  # noqa: WPS433
        hud = start_in_thread(args.hud_port)
        print(f"[run] HUD：http://127.0.0.1:{args.hud_port}/hud"
              + ("" if hud else "（已在运行，沿用）"), flush=True)
    hud_window = None if (args.no_hud or args.no_hud_window or cfg.hud_mod) else start_hud_window(args.hud_port, run_dir)
    try:
        return await _run_session(cfg, args, run_dir, token, previous, study, metrics, recorder, prepared)
    finally:
        stop_hud_window(hud_window)


async def _run_session(cfg: RunConfig, args: argparse.Namespace, run_dir: Path, token: str | None,
                       previous: dict | None, study: KbStudy | None, metrics: Metrics,
                       recorder: Recorder | None = None, prepared: dict | None = None) -> dict:
    if previous and (previous["dir"] / "goal.json").exists():
        previous_goal = json.loads((previous["dir"] / "goal.json").read_text(encoding="utf-8"))
        if previous_goal.get("active") and not previous_goal.get("reached"):
            shutil.copy2(previous["dir"] / "goal.json", run_dir / "goal.json")
    if previous and (previous["dir"] / "sim_energy.json").exists():
        shutil.copy2(previous["dir"] / "sim_energy.json", run_dir / "sim_energy.json")
    session = GameSession(cfg, run_dir, metrics)
    result = None
    try:
        result = await _run_connected_session(cfg,args,run_dir,token,previous,study,metrics,recorder,prepared,session)
        return result
    finally:
        # 即使连接、后台初始化或其他清理失败，也必须通过自己的连接解除接管。
        # 例外：显式中断，以及 Claude 原有的提前退出策略保留接管，可 --resume 接着玩。
        # 由桥接按场景决定是否冻结（见 _keeps_game_frozen）。
        had_connection = getattr(session,'client',None) is not None
        keep = bool(getattr(cfg, '_keep_frozen', False)) and had_connection
        session.close(release=not keep)
        if cfg is not None:
            cfg._game_released = had_connection and not keep
        # Ctrl+C 时 _run_connected_session 在自己的 finally 里写了 summary.json 但没有返回值：从文件补上，
        # 否则续跑时以为上次交还了控制（_previous_kept_frozen）
        final = result
        if final is None and (run_dir / 'summary.json').exists():
            try:
                final = json.loads((run_dir / 'summary.json').read_text(encoding='utf-8'))
            except (OSError, ValueError):
                final = None
        if final is not None:
            final['game_released'] = not keep
            (run_dir / 'summary.json').write_text(json.dumps(final,ensure_ascii=False,indent=2),encoding='utf-8')
        if keep:
            print('[run] 游戏保持冻结在当前帧（章节计时不走）：可以用 --resume 续跑；'
                  '不续跑时在控制台交还游戏控制'
                  + ('，先收尾保留的录像' if getattr(cfg, '_recording_retained', False) else ''), flush=True)
        else:
            print('[run] 已交还游戏控制，游戏恢复正常运行',flush=True)


def _previous_kept_frozen(previous: dict) -> bool:
    """续跑的那次运行结束时有没有让游戏保持冻结（summary.json 的 game_released 为 False）。"""
    try:
        summary = json.loads((previous["dir"] / "summary.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return (previous.get("state") or {}).get("engine", "claude") == "claude"
    return summary.get("game_released") is False


def _keeps_game_frozen(cfg, args, session, *, interrupted=False) -> bool:
    """Compatibility query; game ownership and live recording retention differ."""
    return _shutdown_mode(cfg, args, session, interrupted=interrupted) != 'release'


def _shutdown_mode(cfg, args, session, *, interrupted=False):
    """An automatic goal ends capture while retaining the game's freeze gate.

    Explicit interruption/legacy Claude early exit instead retains live capture;
    ordinary user stop and natural completion still return manual control.
    """
    if args.no_agent:
        return 'release'
    goals = getattr(session, 'goals', None)
    if goals is not None and not goals.immediate and goals.snapshot().get('reached') is True:
        return 'freeze'
    if (interrupted or getattr(cfg, 'engine', 'claude') == 'claude') and not session.done:
        return 'retain'
    return 'release'


async def _run_connected_session(cfg, args, run_dir, token, previous, study, metrics, recorder, prepared, session):
    if args.prepared or previous:
        from .control import RunControl
        source_control = RunControl(Path(args.prepared) if args.prepared else previous['dir'])
        preference = source_control.fast_preference()
        if preference is not None:
            RunControl(run_dir).set_fast(preference)
        RunControl(run_dir).set_effort(source_control.effort_preference())
        RunControl(run_dir).set_cutscene_mode(cfg.cutscene_mode)
        from .action_policy import permissions_for
        RunControl(run_dir).set_action_permissions(permissions_for(cfg).snapshot())
    if cfg.play_scope == 'visited':
        # The save-bound authorization is committed by connect before any input.
        # Publish its run-state owner first; session identity is finalized below.
        write_run_state(run_dir, cfg, None, previous)
    obs = session.connect()
    print(f"[run] 已接管游戏：{(obs.get('level') or {}).get('sid',obs.get('scene'))} 房间 {(obs.get('level') or {}).get('room','—')}，存档 {cfg.save}", flush=True)
    if previous:
        session.decision_index = int(previous["metrics"].get("decisions") or 0)
        print(f"[run] 续跑 {previous['dir']}：已通关 {len(metrics.state.splits or [])} 章，现实用时已用 "
              f"{metrics.elapsed() / 60:.0f} 分钟" + (f"，剩余章节 {cfg.chapters}" if cfg.chapters else ""), flush=True)
    backup_save(obs, run_dir)
    resumed_session, session_id = choose_session(previous, args.fresh_session, prepared)
    if args.no_agent:
        metrics.update(telemetry_source="external", chat_thread_id=None)
    write_run_state(run_dir, cfg, None if args.no_agent else session_id, previous)
    if args.prepared:
        try:
            prepared_state = json.loads((Path(args.prepared) / 'run_state.json').read_text(encoding='utf-8'))
        except (OSError, ValueError):
            prepared_state = {}
        if prepared_state.get('session_name'):
            state = json.loads((run_dir / 'run_state.json').read_text(encoding='utf-8'))
            state['session_name'] = prepared_state['session_name']
            temporary = run_dir / 'run_state.tmp'
            temporary.write_text(json.dumps(state, ensure_ascii=False), encoding='utf-8')
            temporary.replace(run_dir / 'run_state.json')
    if recorder is not None:
        recorder.call = session.bridge_call  # 开局时已经开始录制（见 enter_save），之后改用主控的连接
        if getattr(cfg, '_recording_resume_manifest', None):
            cfg._recording_adopted = True
            cfg._keep_frozen = False
    elif args.record and not getattr(cfg, '_recording_resume_manifest', None):
        recorder = Recorder(session.bridge_call,
                            options_from(run_dir, args.ffmpeg, args.record_video_args, args.audio_offset_ms, args.record_marker,
                                         continuous_music=cfg.continuous_music),
                            log=lambda m: print(m, flush=True))
        recorder.bind_run(run_dir)
        from .chapter_recording import wrap
        recorder = wrap(cfg,recorder)
        try:
            recorder.start()
        except RecordingError:
            session.close()
            raise
    from .chapter_recording import wrap
    recorder = wrap(cfg,recorder)
    if recorder is not None:
        if getattr(cfg,'recording_mode','continuous') == 'chapter':
            session.client.before_step = lambda: session.before_advance(boundary_only=True)
        session.before_advance = recording_guard(recorder, run_dir)

    capabilities = workspace_capabilities(cfg)
    gate = PauseGate(metrics, enabled=cfg.pause_limit and capabilities.programs,
                     log=lambda m: print(m, flush=True), rounds_per_room=cfg.rounds_per_room,
                     death_bonus=cfg.death_bonus, started=previous is not None, sim_only=cfg.sim,
                     state_path=cfg.workspace_dir / "game" / "rounds.json" if capabilities.workspace_write else None,
                     room_scale=cfg.room_scale)
    playback = Playback(cfg, session, metrics, gate, log=lambda m: print(m, flush=True))
    if playback.collect:
        session.playback = playback
    live = None
    # narrate 与工具状态在所有模式都开（HUD 据 live 把思考放在主位）；直播模式另记 live_mode
    metrics.update(live=True, live_mode=bool(cfg.live), play_mode=describe_mode(cfg), hud_mod=bool(cfg.hud_mod),
                   formal_only=bool(cfg.formal_thought),
                   sim_stats=not (cfg.mode == "explore" and not cfg.sim_assist))
    print(f"[run] 模式：{describe_mode(cfg)}", flush=True)
    if (overrides := prompt_override_hashes()):
        print(f"[run] 提示词有本机修改（local/prompts）：{'、'.join(overrides)}", flush=True)
    if session.workspace is not None:
        live = LiveStatus(session.workspace / "game" / "hud_status.json", metrics).start()
    server = build_server(session, study, gate)
    app = server.streamable_http_app(streamable_http_path="/mcp", session_idle_timeout=None, host="127.0.0.1",
                                     transport_security=TRANSPORT_SECURITY)
    add_gate_route(app, gate, metrics, cfg.live_remind_s if capabilities.workspace_write else None,
                   session=session)
    web = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=cfg.mcp_port, log_level="warning"))
    web_task = asyncio.create_task(web.serve())
    while not web.started:
        if web_task.done():
            raise RuntimeError(f"游戏工具服务启动失败（端口 {cfg.mcp_port}）")
        await asyncio.sleep(0.05)
    print(f"[run] 游戏工具服务：http://127.0.0.1:{cfg.mcp_port}/mcp", flush=True)

    reason = "中断"
    runner = None
    # 等工具服务就绪才认领原录制；之前任何初始化失败均保留原运行供续跑。
    saved_recording = getattr(cfg, '_recording_resume_manifest', None)
    if recorder is None and args.record and saved_recording:
        recorder = Recorder.adopt(session.bridge_call, saved_recording, run_dir,
                                  log=lambda m: print(m, flush=True))
        recorder = wrap(cfg,recorder)
        if getattr(cfg,'recording_mode','continuous') == 'chapter':
            session.client.before_step = lambda: session.before_advance(boundary_only=True)
        cfg._recording_adopted = True
        cfg._keep_frozen = False
        session.before_advance = recording_guard(recorder, run_dir)
    try:
        if args.no_agent:
            print("[run] --no-agent：工具服务已就绪，按 Ctrl+C 结束", flush=True)
            await web_task
            reason = "手动结束"
        else:
            container = None
            if capabilities.needs_container:
                container = await asyncio.to_thread(ensure_container, cfg, lambda m: print(m, flush=True))
                print(f"[run] 智能体容器：{container}（工作区 {cfg.workspace_dir}）", flush=True)
                session.container = container
            note = opening_note(cfg, previous, resumed_session, getattr(args, "note_text", None))
            publish_user_note(run_dir, getattr(args, "note_text", None))
            from .runtimes import runtime
            runner = runtime(cfg.engine).runner(cfg, run_dir, session, metrics, container, study,
                game_server=server, token=token, session_id=session_id,
                resume=resumed_session is not None, resume_note=note,
                gate=gate, playback=playback if playback.collect else None, start_paused=args.start_paused)
            print(f"[run] 启动智能体：model={cfg.model} effort={cfg.effort} 运行方式 {cfg.runtime} "
                  f"预算 {cfg.max_minutes:g} 分钟", flush=True)
            reason = await runner.run()
    except Exception as exc:
        print(f'[run] 智能体异常，开始收尾：{type(exc).__name__}: {exc}', flush=True)
        raise
    finally:
        # Commit the ownership decision before cleanup that may itself fail.
        # The outer socket/error cleanup must never undo a reached goal's hold.
        shutdown = _shutdown_mode(cfg, args, session,
                                  interrupted=bool(runner and runner.interrupted_for_resume))
        cfg._keep_frozen = shutdown != 'release'
        finish_error = None
        if session.done and session.done_reason in ("章节完成", "分段完成"):
            try:
                await asyncio.to_thread(session.finish_at_results if session.done_reason == "章节完成"
                                        else session.finish_at_checkpoint)
            except Exception as exc:
                finish_error = str(exc)
                print(f"[run] 结算收尾未完成：{exc}", flush=True)
        metrics.update(status="done" if session.done else "stopped")
        web.should_exit = True
        if not web_task.done():
            try:
                await asyncio.wait_for(web_task, timeout=10)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                web_task.cancel()
        playback.close()
        if live is not None:
            live.close()
        recording = None
        if recorder:
            try:
                def release_after_capture():
                    session.bridge_call('deactivate')
                    cfg._game_released=True
                    print('[run] 锁步录制已停止，已交还游戏控制；视频后处理继续进行',flush=True)
                if shutdown == 'retain':
                    try:
                        recording = recorder.retain()
                        cfg._recording_retained = True
                    except Exception as exc:
                        print(f'[record] 保留录制失败，改为收尾本段：{exc}', flush=True)
                        recording = recorder.stop(defer_postprocessing=bool(runner and runner.interrupted_for_resume))
                elif shutdown == 'freeze':
                    recording = recorder.stop()
                else:
                    recording = recorder.stop(on_capture_stopped=release_after_capture)
            except Exception as exc:  # noqa: BLE001（录制收尾失败不影响交还游戏控制与写总结）
                print(f"[record] 停止录制失败：{exc}", flush=True)
        # 锁步结束后先释放，外层finally仍负责关闭连接及异常兜底；暂停不走结束流程。
        summary = metrics.snapshot()
        summary.update(stop_reason=reason, completed=session.done and session.done_reason in ("章节完成", "分段完成"),
                       user_stopped=session.user_stopped, agent_stop=getattr(session, "agent_stop", None), goal=session.goals.snapshot(),
                       chapters=cfg.chapters, splits=metrics.state.splits or [],
                       plan=[[p.chapter, p.start, p.end] for p in cfg.plan] if cfg.plan else None,
                       total_time_ticks=metrics.state.completed_ticks + (0 if metrics.state.piece_done or session.chapter_finished
                                                                         or session.done_reason in ("章节完成", "分段完成")
                                                                         else metrics.state.chapter_time_ticks),
                       start_sid=session.start_sid, end_scene=session.end_scene, finish_error=finish_error, turns=runner.turns if runner else 0,
                       session_id=runner.session_id if runner else None, run_dir=str(run_dir),
                       resumed_from=str(previous["dir"]) if previous else None,
                       kb_study=study.summary() if study else None, recording=recording)
        (run_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


def choose_session(previous: dict | None, fresh: bool, prepared: dict | None = None) -> tuple[str | None, str]:
    """(接续的会话编号或 None, 这次用的会话编号)。接班会话的目录（session_pending，见 chat.successor_run）里
    新会话还没开始过：用它记下的编号新开一个会话；--fresh-session 总是另起新编号。"""
    if prepared:
        return prepared["session_id"], prepared["session_id"]
    state = previous["state"] if previous else {}
    if previous and not fresh and state.get("session_id"):
        if state.get("session_pending"):
            return None, state["session_id"]
        return state["session_id"], state["session_id"]
    return None, str(uuid.uuid4())


def recording_guard(recorder: Recorder, run_dir: Path):
    """Before input, recover failed capture into the same logical recording or pause."""
    def check(boundary_only=False):
        reason = '录制安全检查未通过'
        try:
            from .chapter_recording import ChapterRecording
            if isinstance(recorder,ChapterRecording):
                recorder.before_step()
            if boundary_only:
                return
            recorder.check_window()
            try:
                reason = recorder.segment_failed()
                if not reason:
                    return
                recorder.roll(reason)
            except RecordingError:
                raise
            except Exception as exc:  # noqa: BLE001
                # A failed hello/file read is also an unverified capture, not
                # permission to advance without recording. Ordinary window
                # errors keep the generic callback behavior outside this block.
                raise RecordingHalt(f'无法确认录制安全状态：{exc}') from exc
        except RecordingError as exc:
            # GameSession intentionally ignores ordinary window-check errors.
            # Recording ownership/ledger failures must cross that boundary as
            # RecordingHalt, including failures before roll is reached.
            halt = exc if isinstance(exc, RecordingHalt) else RecordingHalt(f'录制安全检查失败：{exc}')
            from .control import RunControl
            try:
                RunControl(run_dir).enqueue('pause')
            except Exception as again:  # noqa: BLE001
                print(f'[record] 暂停试玩失败：{again}', flush=True)
            print(f'[record] {exc}：试玩已暂停', flush=True)
            try:
                from .telemetry import publish
                publish(run_dir, f'system:recording-halt:{int(time.time())}',
                        f'录像中断（{reason}），录制检查或恢复失败：{exc}。试玩已暂停，游戏保持冻结；'
                        '处理好 OBS 或录制记录后重试，以原会话恢复；仅明确放弃旧录像时使用 --new-recording。', role='system', kind='系统')
            except Exception as again:  # noqa: BLE001
                print(f'[record] 发布暂停通知失败：{again}', flush=True)
            if halt is exc:
                raise
            raise halt from exc
        from .telemetry import publish
        publish(run_dir, f'system:recording-roll:{len(recorder.segments)}',
                f'录像捕获中断（{reason}）：原内容已保留，已恢复同一录制会话（{recorder.opts.dir.name}）。'
                '故障中未录下的帧不会补造，恢复边界写入录制记录。', role='system', kind='系统')
    return check


def publish_user_note(run_dir: Path, text: str | None) -> None:
    """--note-file 的留言附在开场消息里发给智能体；同时记进公开事件，控制台的对话栏里能看到发了什么。"""
    if text:
        from .telemetry import publish
        publish(run_dir, "user:note-file", text, role="user", kind="你 · 局间留言（附在开场消息里）")


def opening_note(cfg, previous, resumed_session, user_note: str | None = None) -> str | None:
    """续跑时代替开局的第一条消息：追加了新章节（原计划已全部通关）时说开始新的一章，否则说中途被打断后接着玩。
    user_note（--note-file）是用户的留言，放在主控的说明之后、当前完整规则之前。"""
    if not previous:
        return None
    extra = f"\n\n（用户留言）\n{user_note}" if user_note else ""
    if getattr(cfg, "_extended_plan", None):
        note = next_chapter_prompt(cfg, fresh_session=not resumed_session) + extra
        return note + current_rules_note(cfg) if resumed_session else note
    gap = max(0.0, time.time() - float(previous["metrics"].get("written_wall") or time.time())) / 60
    if resumed_session:
        return resume_prompt("主控中断后续跑", gap) + extra + current_rules_note(cfg)
    frozen = _previous_kept_frozen(previous)
    history = ("之前的对话不在了，但工作区里的笔记、程序与 game/ 文件都在。先查阅 /workspace/notes.md。"
               if workspace_capabilities(cfg).workspace_read else "之前的对话不在了；当前没有工作区文件工具，以最新观测为准。")
    state = ("游戏一直冻结在中断时的那一帧，章节计时没有走。" if frozen else
             "上次退出后已交还游戏控制，游戏状态可能发生变化。")
    return "（系统）主控中断后续跑，这是一个新的会话：" + history + state + "调用 observe 核对当前状态后继续游玩。" + extra


def abandon_recording(bridge_port: int, saved: dict | None, previous_dir, problem) -> dict:
    """--new-recording：原录像接不上时放弃它，这次续跑另起一段录像；返回最新的 hello。
    OBS 还开着同一目录的录像（例如独立音频停了）就先收尾它；OBS 已经停了就把会话标成 abandoned；
    OBS 正在录别的会话时不动它，照常报错（不能另开录像）。"""
    from tools.bridge_client import BridgeClient  # noqa: WPS433
    client = BridgeClient(port=bridge_port, timeout=900)
    try:
        hello = client.call('hello')
        if pending_recording(saved):
            clock = hello.get('clock') or {}
            plugin = clock.get('plugin') or {}
            state = plugin.get('state')
            recording = state == 'recording' or plugin.get('recording') is True
            same = bool(plugin.get('dir')) and Path(plugin['dir']).resolve() == Recorder.capture_directory(saved)
            # OBS 断开后桥接仍报告断开前的插件状态：还在"录制/收尾中"的收不了尾，只能放弃；
            # 已经编码完（finished/failed）的同目录录像照常收尾，生成成片与纯享版，不标成放弃
            connected = clock.get('connected') is not False
            if same and ((connected and (recording or state == 'finishing')) or state in {'finished', 'failed'}):
                print(f"[record] 原录像接不上（{problem}）：先收尾它", flush=True)
                Recorder.finish_saved(client.call, saved, previous_dir, log=lambda m: print(m, flush=True))
            else:
                Recorder.abandon_saved(saved, problem)
                try:
                    # Chapters closed before the failure keep their footage; publish
                    # their derivatives offline before the new recording starts.
                    Recorder.finish_closed_offline(saved, log=lambda m: print(m, flush=True))
                except Exception as exc:  # noqa: BLE001（离线收尾失败不阻止另起录像，章节保持待收尾）
                    print(f'[record] 已关闭章节的离线收尾未完成，保持待收尾：{exc}', flush=True)
            hello = client.call('hello')
        Recorder.validate_resume(None, hello)          # OBS 还在录别的会话：不另开录像
        print(f"[record] 已放弃原录像（{problem}）：这次续跑另起一段录像", flush=True)
        return hello
    finally:
        client.close()


def resume_recording_mode(cfg, args, saved, hello) -> str:
    """续跑时这次的录制方式：'live' 接续仍在录的原编码器，'recover' 恢复到原录制会话，'new' 在新运行目录另起录像（或不录）。

    追加章节（--chapters / --segments）且原录像已正常收尾（finished）时另起新录像：上一章的录像目录与成片原样保留，
    不把下一章拼进去。只有明确 --new-recording 才放弃尚未收尾的原录像。"""
    state = (saved or {}).get('state')
    if getattr(args, 'new_recording', False):
        return 'live' if Recorder.validate_resume(saved, hello) else 'new'
    if saved and not args.record and state in {'finished', 'failed', 'abandoned'}:
        print(f"[record] 原录像已结束（{state}）：这次 --no-record 续跑不录制，原录像文件保持不变", flush=True)
        return 'new'
    if saved and state == 'finished' and getattr(cfg, '_extended_plan', None):
        print(f"[record] 上一章的录像已正常收尾（{saved['options']['dir']}）：这次追加章节另起新录像，"
              '原录像目录与成片保持不变', flush=True)
        return 'new'
    if saved and state == 'abandoned':
        reason = saved.get('abandoned_reason')
        raise RecordingError('原录制会话已被放弃' + (f'（{reason}）' if reason else '') + '，不能再恢复：'
                             '这次续跑要录制请加 --new-recording 另起新录像（原录像文件保持不变），不录制请加 --no-record')
    return Recorder.resume_mode(saved, hello)


def main() -> int:
    cfg, args = parse_args()
    if cfg.create_save:
        from .saves import validate_selection
        validate_selection(cfg.save, True, cfg.chapter)
    if args.prepared:
        # 在选档、录制或连接游戏前验证，错误的准备目录不能导致游戏动作。
        from .prepared import load_preparation
        preparation_study = KbStudy(cfg.kb_dir, cfg.study_minutes,
                                    {"/sim/README.md": cfg.sim_dir / "README.md"} if cfg.sim else None)
        load_preparation(args.prepared, cfg, preparation_study)
    previous = args.previous
    if not port_free(cfg.mcp_port):
        raise SystemExit(f"端口 {cfg.mcp_port} 已被占用：可能有另一个主控在运行，或用 --mcp-port 换一个端口")
    from .bridge_requirements import preflight_bridge
    hello = preflight_bridge(cfg.bridge_port)
    if getattr(cfg, 'play_scope', 'chapter') == 'visited' and hello.get('map_entry_set') is not True:
        raise RuntimeError('自由探索需要桥接 1.10.0 的地图集合门禁；未开始录制或进档')
    if (args.record and getattr(cfg,'recording_mode','continuous') == 'chapter' or any(getattr(cfg,key,False) for key in
            ('freeze_chapter_select','freeze_file_select','freeze_main_menu'))) and hello.get('chapter_recording') != 1:
        raise SystemExit('按章录制与菜单冻结设置需要桥接 1.9.0；请先更新 Bridge，未启动录制或游戏输入')
    saved_recording = None
    try:
        saved_recording = Recorder.saved_session(args.previous['dir']) if args.previous else None
        if (args.previous and getattr(cfg,'recording_mode','continuous') != (args.previous.get('state') or {}).get('recording_mode','continuous') and saved_recording):
            clock=hello.get('clock') or {};plugin=clock.get('plugin') or {}
            if saved_recording.get('state') not in {'finished','failed','abandoned'} or clock.get('lockstep') or plugin.get('state') in {'recording','finishing'}:
                raise SystemExit('切换按章／连续录像前，请先停止并完整保存原录像；不会把在录的原片隐式转换')
            if saved_recording.get('chapter_playlist'):
                if pending_recording(saved_recording):
                    raise SystemExit('分章清单仍有待收尾的片段，先完整保存后再切换录制方式')
        if getattr(cfg,'recording_mode','continuous') == 'chapter' and not args.new_recording:
            from .chapter_recording import resolve_saved
            saved_recording = resolve_saved(saved_recording,hello,prepare_next=args.record)
        recording_mode = resume_recording_mode(cfg, args, saved_recording, hello)
        if recording_mode == 'new':
            saved_recording = None
        resume_recording = recording_mode != 'new'
    except RecordingError as exc:
        if not (args.previous and args.new_recording):
            print(f"[record] {exc}", flush=True)
            if args.previous and (saved_recording or {}).get('state') != 'abandoned':
                print("[record] 原录像保持，处理故障后正常 --resume 会重试恢复；仅明确放弃它时加 --new-recording"
                      "（控制台里用“另起录像续跑”）；不续跑时在控制台交还游戏控制", flush=True)
            return 2
        try:
            hello = abandon_recording(cfg.bridge_port, saved_recording, args.previous['dir'], exc)
        except RecordingError as again:
            print(f"[record] {again}", flush=True)
            return 2
        saved_recording, resume_recording, recording_mode = None, False, 'new'
    if resume_recording:
        if not args.record:
            if recording_mode == 'live':
                print('[record] 原录像仍在继续，不能用 --no-record 续跑；请先收尾录像（控制台交还游戏控制时会收尾）', flush=True)
            else:
                print(f"[record] 原录像尚未收尾（{saved_recording.get('state')}），不能用 --no-record 续跑：请不加 --no-record 正常 --resume，"
                      '以原会话恢复并收尾；确定放弃原录像时加 --new-recording', flush=True)
            return 2
        cfg._recording_resume_manifest = saved_recording
        cfg._keep_frozen = True  # 新主控完成接管前，启动失败不得破坏原冻结和录制。
        cfg.continuous_music = saved_recording['options']['continuous_music']
    if cfg.hud_mod and not hello.get("mod_hud"):
        print("[run] 桥接还不支持游戏内 HUD（需要 0.13.0）：这次改用网页 HUD", flush=True)
        cfg.hud_mod = False
    from .runtimes import runtime
    token = None if args.no_agent else runtime(cfg.engine).token(cfg)
    run_dir = make_run_dir(cfg.agent)
    print(f"[run] 日志目录：{run_dir}", flush=True)
    mod_hud = None
    recorder = None
    obs_launched = False
    handoff_needed = False
    try:
        if getattr(cfg, '_recording_resume_manifest', None):
            Recorder.link_saved(cfg._recording_resume_manifest, run_dir)
        mod_hud = start_mod_hud(cfg, run_dir)
        if args.record:
            obs_launched = ensure_obs()
        if recording_mode == 'recover':
            from tools.bridge_client import BridgeClient
            recovery_client = BridgeClient(port=cfg.bridge_port, timeout=120)
            try:
                Recorder.validate_recovery(saved_recording, recovery_client.call('hello'))
                recovery_client.call('activate', entities=False, tiles=False, continuous_music=cfg.continuous_music)
                recovered = Recorder.recover(recovery_client.call, saved_recording, run_dir,
                                             log=lambda m: print(m, flush=True))
                # Chapter mode: the playlist owner must wrap the recovered chunk
                # before enter_save or the session can advance the game. A
                # rejected wrap keeps the live chunk for the next resume.
                from .chapter_recording import wrap
                recorder = wrap(cfg, recovered)
                handoff_needed = True
            except BaseException:
                # An asynchronous start may have succeeded despite a lost ACK.
                # Keep this owner and OBS available for the next recovery try.
                cfg._keep_frozen = True
                cfg._recording_retained = True
                raise
            finally:
                recovery_client.close()
        if previous and check_resume_position(cfg):
            handoff_needed = True
        elif previous or not cfg.skip_enter:
            # 新开一局，或续跑时游戏不在要续跑的关卡里（两章之间中断、游戏重启过）：用菜单宏进入
            if args.record and not getattr(cfg, '_recording_resume_manifest', None):
                # 开局（选存档、进章节）也录进录像：录制从选档页开始。HUD 服务先起来，显示这一局的名字
                if not args.no_hud:
                    from overlay.server import start_in_thread  # noqa: WPS433
                    start_in_thread(args.hud_port)
                    write_placeholder_metrics(run_dir, cfg)
                recorder = Recorder(None, options_from(run_dir, args.ffmpeg, args.record_video_args,
                                                       args.audio_offset_ms, args.record_marker,
                                                       continuous_music=cfg.continuous_music),
                                    log=lambda m: print(m, flush=True))
                recorder.bind_run(run_dir)
                from .chapter_recording import wrap
                recorder = wrap(cfg,recorder)
            enter_save(cfg, recorder)
            handoff_needed = True
        summary = asyncio.run(run_async(cfg, args, run_dir, token, previous, recorder))
    except KeyboardInterrupt:
        print("[run] 已中断", flush=True)
        return 130
    except RecordingError as exc:
        print(f"[record] {exc}", flush=True)
        return 2
    finally:
        try:
            keep_frozen = bool(getattr(cfg, '_keep_frozen', False))
            if (recorder is not None and recorder.started) or (handoff_needed and not getattr(cfg,'_game_released',False) and not keep_frozen):
                # 进入存档后、正式会话创建前也可能失败；此时原宏连接已经关闭，在这里收尾并恢复游戏。
                from tools.bridge_client import BridgeClient  # noqa: WPS433
                client = BridgeClient(port=cfg.bridge_port, timeout=120)
                try:
                    if recorder is not None and recorder.started:
                        recorder.call = client.call
                        try:
                            # 已经停过一次（运行里收尾失败或超时）时只短等一下；失败只记下，不让收尾报堆栈退出
                            recorder.stop(timeout=60.0 if recorder._stop.requested else 900.0)
                        except Exception as exc:  # noqa: BLE001
                            print(f"[record] 收尾时停止录制失败：{exc}", flush=True)
                finally:
                    try:
                        if not keep_frozen:
                            client.call('deactivate')
                    finally:
                        client.close()
        finally:
            if recorder is not None and getattr(cfg,'recording_mode','continuous') == 'chapter' and recorder._recovery_start is not None:
                cfg._recording_retained = True
            if getattr(cfg, '_recording_resume_manifest', None) and not getattr(cfg, '_recording_adopted', False):
                latest = REPO_ROOT / 'runs/latest.txt'
                if latest.exists() and latest.read_text(encoding='utf-8').strip() == str(run_dir):
                    latest.write_text(str(previous['dir']), encoding='utf-8')
            if mod_hud is not None:
                mod_hud.close()
            if obs_launched and not args.keep_obs_open and not getattr(cfg, '_recording_retained', False):
                close_obs()
            elif obs_launched:
                print("[record] 保留 OBS：录像等待续跑，或已启用 --keep-obs-open；最终收尾前不要关闭 OBS", flush=True)
    print(f"[run] 结束：{summary['stop_reason']}；决策 {summary['decisions']} 次，死亡 {summary['deaths']} 次，"
          f"用时 {summary['elapsed_s']:.0f} 秒（思考 {summary['thinking_s']:.0f}，工具 {summary['tool_s']:.0f}，"
          f"游戏推进 {summary['play_game_s'] + summary['auto_game_s']:.0f}）"
          + (f"；准备阶段 {summary['study_s']:.0f} 秒，知识库已读 {summary['kb_study']['progress']:.0%}"
             if summary.get("kb_study") else ""), flush=True)
    return 0 if summary["completed"] or summary.get("user_stopped") else 1


def claim_controller_lease():
    """命令行启动时持有主控互斥锁（后台宿主已代为持有时跳过），供维护与重启工具核验归属。"""
    from .game_process import hold_cli_controller_lease
    try:
        return hold_cli_controller_lease()
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from None


if __name__ == "__main__":
    sys.path.insert(0, str(REPO_ROOT))
    _controller_lease = claim_controller_lease()  # held until this process exits
    sys.exit(main())
