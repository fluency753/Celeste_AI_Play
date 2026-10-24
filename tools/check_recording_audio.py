"""离线检查 obs_mix 或 game_nrt 录制的 audio.wav，不连接游戏或 OBS。"""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from orchestrator.audio_validation import SILENCE_THRESHOLD, wav_dropouts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('wav', type=Path, help='录制目录内的 audio.wav')
    parser.add_argument('--silence-threshold', type=float, default=SILENCE_THRESHOLD,
                        help='断点两侧的最低信号振幅，默认 1e-6（满幅为 1）')
    args = parser.parse_args()
    try:
        drops = wav_dropouts(args.wav, args.silence_threshold)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps({'path':str(args.wav), 'silence_threshold':args.silence_threshold,
                      'dropouts':len(drops), 'first_positions':drops[:20]}, ensure_ascii=False))
    return 1 if drops else 0


if __name__ == '__main__':
    raise SystemExit(main())
