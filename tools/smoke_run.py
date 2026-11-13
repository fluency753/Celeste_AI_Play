"""发布前的实机冒烟：用便宜的模型在测试存档上真实跑一小段，再核对进档、动作、观测、死亡重生、录像与收尾。

回归测试里的游戏、模型与 OBS 都是替身；这条命令把真实的游戏、桥接、容器、模型与录像接在一起跑一遍，
默认从第一章开头玩到进入第 3 个房间（过了前两面）为止，探索模式、分章录制、跳过准备阶段。
转正式版之前跑一次；它要花模型的钱、占用游戏十几分钟，不在回归和持续集成里。

用法（Windows，仓库根目录；控制台的游戏位置是 AI 专用副本，Docker Desktop 已启动，没有别的运行）：
    .\\.venv\\Scripts\\python.exe -X utf8 tools\\smoke_run.py --save-slot 7
    .\\.venv\\Scripts\\python.exe -X utf8 tools\\smoke_run.py --check runs\\smoke\\<时间>     只核对一次已经结束的运行

启动方式与控制台相同（独立宿主），用内置的“默认”配置，不改本机选中的配置。模型打不过去不算失败：
核对的是主控这一侧（有没有异常、记录与录像是否完整、游戏有没有按约定交还或冻结），结果逐项列出。
到达目标后主控按约定把游戏冻结在原地；核对完由这条命令交还控制（--keep-frozen 时不交还）。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import socket
import subprocess
import sys
import time
from urllib.error import URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None


def read_rows(path: Path) -> list[dict]:
    rows = []
    try:
        with path.open(encoding='utf-8') as stream:
            for line in stream:
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    pass          # 最后一行可能没写完
    except OSError:
        pass
    return rows


def port_open(port: int) -> bool:
    with socket.socket() as probe:
        probe.settimeout(1)
        return probe.connect_ex(('127.0.0.1', port)) == 0


def parse_segments(text: str) -> list[list[int]]:
    """0.0,1.0 或 1.0-1 → [[章节, 段], ...]（与 --segments 的写法相同，只支持写明段号）。"""
    out = []
    for item in text.split(','):
        chapter, _, part = item.strip().partition('.')
        first, _, last = part.partition('-')
        if not chapter.isdigit() or not first.isdigit() or (last and not last.isdigit()):
            raise ValueError(f'段的写法无效：{item}（例如 0.0,1.0 或 1.0-1）')
        out += [[int(chapter), k] for k in range(int(first), int(last or first) + 1)]
    return out


# ---- 启动与等待 ----

def prepare(args) -> None:
    from overlay import console_api
    if console_api.orchestrator_status()['running']:
        raise SystemExit('已有主控在运行；等它结束再跑冒烟')
    from orchestrator.agent import find_docker_exe
    try:
        docker = subprocess.run([find_docker_exe(), 'info', '--format', '{{.ServerVersion}}'],
                                capture_output=True, timeout=60, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SystemExit(f'Docker 不可用（{exc}）；先启动 Docker Desktop')
    if docker.returncode:
        raise SystemExit('Docker 没有在运行；先启动 Docker Desktop')
    if not port_open(args.bridge_port):
        print('[smoke] 游戏没开，启动 AI 专用副本…', flush=True)
        try:
            # 让控制台服务去启动（与在控制台里点“启动”相同）：游戏不成为这条命令所在程序的子进程。
            request = Request(f'http://127.0.0.1:{args.hud_port}/api/console/instance/launch', data=b'{}',
                              headers={'Content-Type': 'application/json'})
            with urlopen(request, timeout=30) as response:
                json.load(response)
        except (URLError, OSError, ValueError):
            from orchestrator import game_instance
            game_instance.launch(log=lambda line: print(line, flush=True))
        deadline = time.monotonic() + 180
        while not port_open(args.bridge_port):
            if time.monotonic() > deadline:
                raise SystemExit('游戏启动后 3 分钟内桥接端口没有打开；看游戏目录的 log.txt')
            time.sleep(2)
        time.sleep(5)          # 标题画面出来


def launch(args) -> Path:
    from overlay import console_api
    options = {'engine': 'claude', 'provider': args.provider, 'model': args.model, 'effort': args.effort,
               'agent': args.agent, 'mode': 'explore', 'chapters': sorted({c for c, _ in args.segment_list}),
               'segments': args.segment_list, 'save': {'name': args.save, 'slot': args.save_slot, 'create': False},
               'max_minutes': args.max_minutes, 'study': False, 'record': True, 'recording_mode': 'chapter',
               'cutscene_mode': 'auto', 'bridge_port': args.bridge_port, 'hud_port': args.hud_port}
    extra = []
    if args.until_room:
        goal = ROOT / 'local' / 'smoke' / 'goal.json'
        goal.parent.mkdir(parents=True, exist_ok=True)
        goal.write_text(json.dumps({'text': f'进入房间 {args.until_room} 就算完成，不用再往前。', 'match': 'all',
                                    'conditions': [{'field': 'room', 'op': 'eq', 'value': args.until_room}]},
                                   ensure_ascii=False), encoding='utf-8')
        extra = ['--goal-file', str(goal)]
    before = console_api.latest_run()
    started = console_api.start(options, profile=args.profile, extra=extra)
    print(f"[smoke] 已启动：{started['command']}", flush=True)
    deadline = time.monotonic() + 300
    while True:
        run = console_api.latest_run()
        if run is not None and run != before:
            return run
        status = console_api.orchestrator_status()
        if status['failed'] or time.monotonic() > deadline:
            raise SystemExit('主控没有建立运行目录就结束了，或 5 分钟内没有开始：\n' + (status.get('log_tail') or
                             f"看 {started['host']} 里的 output.log"))
        time.sleep(2)


def wait(run: Path, args) -> None:
    from overlay import console_api
    deadline = time.monotonic() + args.max_minutes * 60 + args.grace_minutes * 60
    shown = 0.0
    while True:
        finished = (run / 'summary.json').exists() and not console_api.orchestrator_status()['running']
        if finished:
            return
        if time.monotonic() > deadline:
            raise SystemExit(f'超过 {args.max_minutes + args.grace_minutes:g} 分钟还没有结束；运行目录 {run}。'
                             '主控可能还在运行，在控制台里查看或停止')
        if time.monotonic() - shown >= 30:
            shown = time.monotonic()
            m = read_json(run / 'metrics.json') or {}
            print(f"[smoke] {m.get('status')}｜房间 {m.get('room')}｜决策 {m.get('decisions')}｜死亡 {m.get('deaths')}｜"
                  f"现实用时 {m.get('elapsed_s') or 0:.0f} 秒", flush=True)
        time.sleep(3)


# ---- 核对 ----

def recordings(node) -> list[dict]:
    """总结里所有带目录与文件清单的录像条目（连续录制是一条，分章录制每章一条）。"""
    found = []
    if isinstance(node, dict):
        if isinstance(node.get('files'), dict) and node.get('dir'):
            found.append(node)
        for value in node.values():
            found += recordings(value)
    elif isinstance(node, list):
        for value in node:
            found += recordings(value)
    return found


def check(run: Path, bridge_port: int = 47774) -> int:
    summary = read_json(run / 'summary.json')
    state = read_json(run / 'run_state.json') or {}
    rows: list[tuple[str, str, str]] = []

    def row(mark, name, detail):
        rows.append((mark, name, str(detail)))

    if summary is None:
        print(f'[smoke] 失败：{run} 里没有 summary.json（主控没有正常收尾）')
        return 1
    log = ''
    try:
        log = (run / 'controller.log').read_text(encoding='utf-8', errors='replace')
    except OSError:
        pass
    error = summary.get('error')
    row('失败' if error else '通过', '主控没有因异常结束',
        f"{error.get('type')}: {error.get('message')}" if isinstance(error, dict) else f"结束原因：{summary.get('stop_reason')}")
    row('失败' if 'Traceback (most recent call last)' in log else '通过', '运行记录里没有异常堆栈',
        'controller.log' if log else '没有 controller.log')
    if summary.get('cleanup_errors'):
        row('失败', '收尾清理', '；'.join(summary['cleanup_errors']))

    decisions = read_rows(run / 'decisions.jsonl')
    frames = len(read_rows(run / 'frames.jsonl'))
    row('通过' if summary.get('start_sid') else '失败', '进入存档并接管',
        f"存档 {state.get('save')}（槽位 {state.get('save_slot')}），起点 {summary.get('start_sid')}")
    row('通过' if len(decisions) >= 3 and frames > 0 else '失败', '动作与逐帧记录',
        f'{len(decisions)} 次动作，{frames} 帧记录，游玩推进 {summary.get("frames_played")} 帧')

    workspace = ROOT / 'workspaces' / f"{state.get('agent')}-explore"
    guides = sorted((workspace / 'game' / 'instructions').glob('*/guide.md')) if workspace.is_dir() else []
    events = ''
    try:
        events = (run / 'agent_events.jsonl').read_text(encoding='utf-8', errors='replace')
    except OSError:
        pass
    read_guide = '/workspace/game/instructions/' in events and 'guide.md' in events
    row('通过' if guides and read_guide else '失败', '操作指南发布到工作区并被读取',
        f"{len(guides)} 个版本；智能体{'读过' if read_guide else '没有读'} guide.md")
    row('通过' if (workspace / 'game' / 'latest_obs.json').exists() else '失败', '观测文件', workspace / 'game' / 'latest_obs.json')

    deaths = [i for i, d in enumerate(decisions) if d.get('stop') == 'death']
    if deaths:
        after = len(decisions) - 1 - deaths[0]
        row('通过' if after > 0 or summary.get('completed') else '失败', '死亡后重生并继续',
            f'死亡 {summary.get("deaths")} 次；第一次死亡之后又执行了 {after} 次动作')
    else:
        row('未覆盖', '死亡后重生并继续', '这次没有死亡')

    splits = summary.get('splits') or []
    goal = summary.get('goal') or {}
    if goal.get('conditions'):
        row('通过' if goal.get('reached') else '未覆盖', '到达停止条件',
            (goal.get('text') or '') + ('' if goal.get('reached') else '（没有到达：模型没打过去，或预算先用完）'))
    else:
        row('通过' if splits else '未覆盖', '完成的段',
            '；'.join(f"{s.get('chapter')} {s.get('ticks', 0) / 1e7:.3f} 秒（死亡 {s.get('deaths')}）" for s in splits)
            or '一段都没有完成')

    if state.get('record') is False:
        row('未覆盖', '录像', '这次没有录像')
    else:
        entries = recordings(summary.get('recording'))
        if not entries:
            row('失败', '录像', f"总结里没有录像条目：{json.dumps(summary.get('recording'), ensure_ascii=False)[:200]}")
        for entry in entries:
            folder = Path(entry['dir'])
            missing = [name for name, size in entry['files'].items()
                       if not (folder / name).is_file() or (folder / name).stat().st_size != size]
            videos = [name for name in entry['files'] if name.endswith(('.mp4', '.mkv'))]
            checked = entry.get('check') or {}
            good = (entry.get('state') == 'finished' and not entry.get('error') and not missing and videos
                    and (entry.get('frames') or 0) > 0 and checked.get('rows') == entry.get('frames'))
            row('通过' if good else '失败', f'录像 {folder.name}',
                f"{entry.get('state')}，{entry.get('frames')} 帧，缺帧 {checked.get('missing_frames')}，"
                f"重复 {checked.get('repeats')}，视频 {'、'.join(videos) or '无'}"
                + (f"；文件缺失或大小不符：{'、'.join(missing)}" if missing else '')
                + (f"；{entry.get('error')}" if entry.get('error') else ''))

    exits = summary.get('game_exits')
    row('失败' if exits else '通过', '游戏进程没有中途退出', f'{len(exits)} 次退出，见 game_exits.jsonl' if exits else '—')
    finished_cleanly = bool(summary.get('completed') or summary.get('user_stopped'))
    released = summary.get('game_released')
    row('通过' if (released if finished_cleanly else True) else '失败', '收尾后的游戏',
        ('已交还控制' if released else '保持冻结（可续跑）') + f"，停在 {summary.get('end_scene')}")
    row('通过' if port_open(bridge_port) else '失败', '游戏还开着', f'桥接端口 {bridge_port}')

    width = max(len(name) for _, name, _ in rows)
    print(f'\n[smoke] {run}')
    for mark, name, detail in rows:
        print(f'  [{mark}] {name}{" " * (width - len(name))}  {detail}')
    failed = [name for mark, name, _ in rows if mark == '失败']
    skipped = [name for mark, name, _ in rows if mark == '未覆盖']
    print(f"[smoke] {'失败：' + '、'.join(failed) if failed else '通过'}"
          + (f"；未覆盖：{'、'.join(skipped)}" if skipped else ''))
    return 1 if failed else 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--check', type=Path, metavar='运行目录', help='只核对一次已经结束的运行，不启动')
    parser.add_argument('--save', default='test', help='测试存档的名字（默认 test）')
    parser.add_argument('--save-slot', type=int, help='测试存档的槽位（启动时必填，避免进错存档）')
    parser.add_argument('--provider', default='deepseek', help='模型提供方（默认 deepseek）')
    parser.add_argument('--model', default='deepseek-flash', help='模型（默认 deepseek-flash）')
    parser.add_argument('--effort', default='high')
    parser.add_argument('--agent', default='smoke', help='智能体标签：容器、工作区与运行目录都用它（默认 smoke）')
    parser.add_argument('--segments', default='1.0', help='要玩的段（默认第一章从开头起的第一段；序章加第一章写 0.0,1.0）')
    parser.add_argument('--until-room', default='3', metavar='房间',
                        help='进入这个房间就停止（默认 3：过了第一章前两面）；写空字符串则玩完所选的段')
    parser.add_argument('--max-minutes', type=float, default=25, help='现实时间预算（默认 25 分钟）')
    parser.add_argument('--grace-minutes', type=float, default=20, help='预算之外留给启动、收尾与录像后处理的时间')
    parser.add_argument('--profile', default='default', help='使用的配置（默认内置的“默认”）')
    parser.add_argument('--keep-frozen', action='store_true', help='结束后不交还游戏控制（留着冻结的现场）')
    parser.add_argument('--bridge-port', type=int, default=47774)
    parser.add_argument('--hud-port', type=int, default=47790)
    args = parser.parse_args(argv)
    if args.check:
        return check(args.check.resolve(), args.bridge_port)
    if sys.platform != 'win32':
        parser.error('实机冒烟需要 Windows 上的游戏；别的系统只能用 --check 核对已有的运行')
    if args.save_slot is None:
        parser.error('启动冒烟要写明 --save-slot（测试存档的槽位）')
    args.segment_list = parse_segments(args.segments)
    prepare(args)
    run = launch(args)
    print(f'[smoke] 运行目录 {run}', flush=True)
    wait(run, args)
    code = check(run, args.bridge_port)
    summary = read_json(run / 'summary.json') or {}
    if not args.keep_frozen and not summary.get('game_released') and port_open(args.bridge_port):
        from overlay import console_api
        try:
            released = console_api.release(args.bridge_port)
            print(f"[smoke] 已交还游戏控制：{released.get('note') or released}", flush=True)
        except Exception as exc:  # noqa: BLE001 - 核对结果已经给出，交还失败只提示
            print(f'[smoke] 没能交还游戏控制（{type(exc).__name__}: {exc}）；在控制台里点“交还控制”', flush=True)
    return code


if __name__ == '__main__':
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='backslashreplace')
    raise SystemExit(main())
