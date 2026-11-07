"""AI 专用游戏副本（独立的存档、设置、Mod 环境与桥接连接；个人的《蔚蓝》可以同时游玩），见 orchestrator/game_instance.py。

    python tools/game_instance.py create [--name main] [--source <个人安装>] [--dir <副本目录>] [--no-select]
    python tools/game_instance.py launch            # 启动现在选中的副本（Steam 没开时先静默启动 Steam）
    python tools/game_instance.py status
    python tools/game_instance.py update            # 用个人安装刷新副本的游戏本体（副本的 Mods、Saves 不动）
    python tools/game_instance.py copy-saves 7 8    # 把个人安装的这些存档槽复制进副本（之后只写副本）
    python tools/game_instance.py select <副本目录>  # 把控制台的游戏位置设为这个副本
    python tools/game_instance.py select --personal <个人安装目录>   # 换回个人安装

更新副本里的桥接：.\\tools\\deploy_bridge.ps1 -CelesteDir <副本目录>
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from orchestrator import game_instance  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='command', required=True)
    create = sub.add_parser('create', help='从个人安装复制出 AI 专用副本')
    create.add_argument('--name', default='main')
    create.add_argument('--source', help='个人的《蔚蓝》安装目录（默认现在用的游戏位置）')
    create.add_argument('--dir', help=r'副本目录（默认 local\instances\<名字>）')
    create.add_argument('--title', default=game_instance.DEFAULT_TITLE, help='副本的窗口标题（OBS 按它采集）')
    create.add_argument('--steam-appid', default=game_instance.DEFAULT_APPID,
                        help='写进 steam_appid.txt（默认 480；写 504230 时 Steam 认为《蔚蓝》在运行，就不能从 Steam 再启动个人的游戏）')
    create.add_argument('--no-select', action='store_true', help='不把游戏位置改成新副本')
    sub.add_parser('launch', help='启动现在选中的副本')
    sub.add_parser('status', help='显示副本与运行状态')
    update = sub.add_parser('update', help='用个人安装刷新副本的游戏本体')
    update.add_argument('--source', help='个人安装目录（默认副本标记里记下的来源）')
    saves = sub.add_parser('copy-saves', help='把个人安装的存档槽复制进副本')
    saves.add_argument('slots', nargs='+', type=int, help='存档槽号（游戏里第一个存档是 0）')
    saves.add_argument('--dir', help='副本目录（默认现在的游戏位置）')
    saves.add_argument('--source', help='个人安装目录（默认副本标记里记下的来源）')
    select = sub.add_parser('select', help='把控制台的游戏位置设为某个目录')
    select.add_argument('directory')
    select.add_argument('--personal', action='store_true', help='允许选不是副本的目录（换回个人安装）')
    args = ap.parse_args()
    try:
        if args.command == 'create':
            marker = game_instance.create(args.name, args.source, args.dir, title=args.title,
                                          steam_appid=args.steam_appid)
            if not args.no_select:
                game_instance.select(marker['dir'])
                print(f'游戏位置已设为副本：{marker["dir"]}')
        elif args.command == 'launch':
            print(json.dumps(game_instance.launch(), ensure_ascii=False))
        elif args.command == 'status':
            marker = game_instance.current()
            if marker is None:
                print('现在的游戏位置不是 AI 专用副本')
            else:
                running = game_instance.game_processes(marker['dir'])
                print(json.dumps({**marker, 'running': running, 'steam_ready': game_instance.steam_ready()},
                                 ensure_ascii=False, indent=2))
        elif args.command == 'update':
            print(json.dumps(game_instance.update(source=args.source), ensure_ascii=False, indent=2))
        elif args.command == 'copy-saves':
            print(json.dumps(game_instance.copy_saves(args.slots, args.dir, args.source), ensure_ascii=False, indent=2))
        elif args.command == 'select':
            directory = Path(args.directory).resolve()
            if not (directory / 'Celeste.exe').is_file():
                raise ValueError(f'{directory} 里没有 Celeste.exe')
            if game_instance.info(directory) is None and not args.personal:
                raise ValueError(f'{directory} 不是 AI 专用副本；换回个人安装请加 --personal')
            game_instance.select(directory)
            print(f'游戏位置已设为 {directory}')
    except (ValueError, RuntimeError, OSError) as exc:
        print(f'未完成：{exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
