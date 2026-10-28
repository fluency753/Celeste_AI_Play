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
from tools.menu_navigation import checked_step


LOADING_SCENES = frozenset({'LevelLoader', 'OverworldLoader', 'GameLoader'})
ENTRY_SCENES = LOADING_SCENES | {'LevelEnter'}


def _wait_for_save_ready(client, obs, validate, publish, *, max_frames=600, timeout=15):
    """只等已知加载/入场或房间转场；不发送确认，不代选剧情。

    加载器空闲时本就运行，只观测它；冻结的入场/关卡转场每次仅给一帧中性输入。
    同一预算贯穿所有场景，包含桥接报告的加载帧；RPC本身另受连接超时约束。
    """
    used = 0
    waited = False
    deadline = time.monotonic() + timeout

    def stop(reason):
        publish(phase='transition_stopped', transition_frames=used, scene=obs.get('scene'))
        raise RuntimeError(reason)

    while True:
        try:
            validate(obs)
        except RuntimeError as exc:
            stop(str(exc))
        scene, level = obs.get('scene'), obs.get('level') or {}
        if used > max_frames:
            stop(f'等待转场超过帧预算（已观测推进 {used} 帧）；未停止录制或退出游戏，可稍后重试')
        if waited and time.monotonic() >= deadline:
            stop(f'等待转场结束超时（已观测推进 {used} 帧）；未停止录制或退出游戏，可稍后重试')
        if scene == 'Overworld' or (scene == 'Level' and (level.get('canPause') is not False or level.get('paused') is True)):
            if waited:
                publish(phase='transition_ready', transition_frames=used, scene=scene)
            return obs
        if scene == 'OverworldLoader' and (obs.get('ui') or {}).get('type') == 'Postcard':
            stop('当前加载器正在呈现原生解锁提示，不能仅靠等待完成维护；请显式 watch／advance 处理后重试。'
                 '未确认提示、未停止录制或退出游戏')
        if obs.get('storyInput') or any(row.get('waitingForInput') for row in obs.get('story', [])):
            stop(f'转场等待遇到剧情输入（{obs.get("storyInput") or "story"}）；'
                 '未确认或跳过剧情，未停止录制或退出游戏')
        waiting = scene in ENTRY_SCENES or (scene == 'Level' and (
            level.get('transitioning') is True or level.get('screenWipe') is True))
        if not waiting:
            stop('当前关卡不能保存退出，且不在已知转场中；未自动推进剧情，请先完成当前操作')
        if used >= max_frames or time.monotonic() >= deadline:
            stop(f'等待转场结束超时（已观测推进 {used} 帧）；未停止录制或退出游戏，可稍后重试')
        publish(phase='waiting_transition', transition_frames=used, scene=scene)
        waited = True
        before = obs.get('frame')
        if scene in LOADING_SCENES:
            time.sleep(0.02)
            reply = client.call('observe', image=False, entities=False, tiles=False)
        else:
            reply = checked_step(client, ['1'], speed=1, stop_on=['story_input'],
                                 image=False, entities=False, tiles=False)
        obs = reply['obs']
        result = reply.get('result') or {}
        counted = result.get('consumed', 0) + result.get('loadingFrames', 0)
        after = obs.get('frame')
        elapsed = max(0, after - before) if type(before) is int and type(after) is int else 0
        used += max(counted, elapsed)


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
        if obs.get('scene') not in {'Level', 'Overworld'} | ENTRY_SCENES:
            raise RuntimeError(f"当前场景 {obs.get('scene')} 无法安全保存退出；请先完成当前操作")
        if obs.get('scene') == 'Level' and slot is None:
            raise RuntimeError('当前关卡未报告存档槽位，未执行保存或退出')

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
        if version < (1, 5, 1):
            raise RuntimeError('维护菜单宏需要 Bridge 1.5.1 或更新；未执行保存或停止录制')
        obs = client.call('activate', image=False, entities=False, tiles=False)['obs']
        validate_observation(obs)  # The world may change before activation freezes it.
        obs = _wait_for_save_ready(client, obs, validate_observation, publish)
        if obs.get('scene') == 'Level':
            publish(phase='saving', scene=obs.get('scene'))
            with controller_navigation(client, slot=save_slot):
                obs = leave_level_by_pause_menu(client, allowed_exit_ids=('menu_pause_savequit',))
            if obs.get('scene') != 'Overworld':
                raise RuntimeError('尚未完成原生保存退出，未关闭游戏')
        publish(phase='stopping_recording', scene=obs.get('scene'))

        def quit_menu():
            publish(phase='exiting_game', scene='Overworld')
            result = exit_game_by_menu(client, process)
            publish(phase='postprocessing', game_exited=True, scene=None, exit=result)
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
