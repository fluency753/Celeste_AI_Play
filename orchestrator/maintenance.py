"""显式维护退出：保存关卡、停止录制、正常菜单退出；之后继续离线后处理。

python -m orchestrator.maintenance --save-slot 10 --run runs/<agent>/<time>
先中断并等待原主控结束。本命令不重启、不强杀、不修改安装；完成后再部署和启动。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import time

from .action_policy import controller_navigation
from .game_process import GameProcess, controller_lease
from .recording import Recorder
from .recording_lifecycle import pending_recording
from tools.cutscene import leave_level_by_pause_menu
from tools.game_exit import exit_game_by_menu


def save_and_exit(client, *, save_slot: int, run_dir: Path | None, progress=lambda data: None):
    """使用调用方独占的桥接连接；不打开第二条连接，不将进程断线当作退出成功。"""
    if type(save_slot) is not int or save_slot < 0:
        raise ValueError('维护退出必须显式指定非负存档槽位')
    report = {'save_slot': save_slot, 'phase': 'checking', 'game_exited': False}
    process = None

    def publish(**data):
        report.update(data, written_wall=time.time())
        progress(dict(report))

    def release_summary(recording=None):
        if run_dir is None:
            return
        path = run_dir/'summary.json'
        if not path.exists():
            return
        summary = json.loads(path.read_text(encoding='utf-8'))
        summary.update(game_released=True, user_stopped=True, end_scene='Overworld',
                       stop_reason='用户维护：保存退出并通过原生菜单关闭游戏', written_wall=time.time())
        if recording is not None:
            summary['recording'] = recording
        temporary = path.with_suffix('.maintenance.tmp')
        temporary.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(path)

    def validate_observation(obs):
        slot = (obs.get('save') or {}).get('slot')
        if slot is not None and slot != save_slot:
            raise RuntimeError(f'当前存档槽位 {slot} 与指定 {save_slot} 不符；未执行保存或退出')
        if obs.get('scene') not in {'Level', 'Overworld'}:
            raise RuntimeError(f"当前场景 {obs.get('scene')} 无法安全保存退出；请先完成当前操作")
        if obs.get('scene') == 'Level' and (slot is None or (obs.get('level') or {}).get('canPause') is False):
            raise RuntimeError('当前关卡尚不能保存退出；请先结束剧情输入或等待转场，不自动跳过剧情')

    try:
        hello = client.call('hello')
        process = GameProcess(hello.get('process_id'))
        saved = Recorder.saved_session(run_dir) if run_dir is not None else None
        Recorder.validate_release(saved, hello)  # Refuse other recordings before saving or navigating.
        if saved is None and hello.get('record_audio_active'):
            raise RuntimeError('仍有不属于本次维护的游戏录音；未保存退出或关闭游戏')
        obs = client.call('observe', image=False, entities=False, tiles=False)['obs']
        validate_observation(obs)
        # Main-menu graph support must be present before any save/stop side effect.
        version = tuple(int(n) for n in hello.get('bridge', '').split('.'))
        if version < (1, 5, 0):
            raise RuntimeError('维护菜单宏需要 Bridge 1.5.0 或更新；未执行保存或停止录制')
        obs = client.call('activate', image=False, entities=False, tiles=False)['obs']
        validate_observation(obs)  # The world may change before activation freezes it.
        if obs.get('scene') == 'Level':
            publish(phase='saving')
            with controller_navigation(client, slot=save_slot):
                obs = leave_level_by_pause_menu(client, allowed_exit_ids=('menu_pause_savequit',))
            if obs.get('scene') != 'Overworld':
                raise RuntimeError('尚未完成原生保存退出，未关闭游戏')
        publish(phase='stopping_recording')

        def quit_menu():
            publish(phase='exiting_game')
            result = exit_game_by_menu(client, process)
            publish(phase='postprocessing', game_exited=True, exit=result)
            # This remains truthful even if offline media processing later fails.
            release_summary()

        if pending_recording(saved):
            recording = Recorder.finish_saved(client.call, saved, run_dir, on_encoder_finished=quit_menu)
            if not report['game_exited']:
                raise RuntimeError('录制编码器尚未确认结束；游戏保持连接，未执行退出，可重试收尾')
            publish(recording=recording)
        else:
            quit_menu()
            recording = None
        release_summary(recording)
        publish(phase='finished')
        return report
    except Exception as exc:
        publish(phase='failed', error=str(exc))
        raise
    finally:
        if process is not None:
            process.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--save-slot', type=int, required=True)
    parser.add_argument('--run', type=Path, help='原试玩目录，用于核对及收尾同一录制')
    parser.add_argument('--bridge-port', type=int, default=47774)
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    from overlay.console_api import orchestrator_status
    if orchestrator_status()['running']:
        parser.error('主控仍在运行，请先中断并等待退出；未连接桥接')
    from tools.bridge_client import BridgeClient
    report_path = args.report or Path('local')/f'maintenance-exit-{time.strftime("%Y%m%d-%H%M%S")}.json'

    def progress(data):
        report_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = report_path.with_suffix('.tmp')
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(report_path)
        print(json.dumps(data, ensure_ascii=False), flush=True)

    with controller_lease():
        client = BridgeClient(port=args.bridge_port, timeout=60)
        try:
            save_and_exit(client, save_slot=args.save_slot, run_dir=args.run, progress=progress)
        finally:
            client.close()


if __name__ == '__main__':
    main()
