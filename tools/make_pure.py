"""给已有的录像补生成纯享版（只有真实游玩的帧，去掉规划回放）：gameplay_pure.mp4。

用法（仓库根目录）：
    python tools/make_pure.py runs/<智能体>/<时间>/recording
    python tools/make_pure.py <recording 目录> --video-args "-c:v h264_nvenc -preset p5 -rc constqp -qp 16 -g 120"
编码参数默认用这次录制的设置（recording.json），没有时用 configs 里的 video_args。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from orchestrator.purecut import make_pure  # noqa: E402
from orchestrator.recording import find_ffmpeg, load_config  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dir", help="录像目录（含 video.mkv、audio.wav、frames.csv）")
    ap.add_argument("--ffmpeg")
    ap.add_argument("--video-args")
    args = ap.parse_args()
    directory = Path(args.dir)
    meta_path = directory / "recording.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    video_args = args.video_args or (meta.get("options") or {}).get("video_args") or load_config().get("video_args") or ""
    result = make_pure(directory, find_ffmpeg(args.ffmpeg), video_args)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if meta and result.get("state") == "finished":
        meta["pure"] = result
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if result.get("state") == "finished" else 1


if __name__ == "__main__":
    sys.exit(main())
