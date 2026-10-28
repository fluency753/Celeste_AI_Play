"""纯享版多区间筛帧及 FFmpeg 参数兼容；真实编码器只在可用时运行。"""
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestrator import purecut


FFMPEG = os.environ.get('FFMPEG') or shutil.which('ffmpeg')


class PureCutFilterTests(unittest.TestCase):
    def test_only_unsupported_file_option_retries_legacy_syntax(self):
        for output, count in [("Unrecognized option '/filter:v'.", 2),
                              ("Unrecognized option 'filter:v'.", 2),
                              ('Error while parsing expression', 1),
                              ('Unknown encoder', 1)]:
            with self.subTest(output=output), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                (root/'frames.csv').write_text('index,chapter_ticks\n0,0\n1,-1\n')
                old = root/'gameplay_pure.mp4'
                old.write_bytes(b'previous finished movie')
                with patch.object(purecut, '_run', side_effect=purecut.FFmpegError(1, root/'log', output)) as run:
                    result = purecut.make_pure(root, 'ffmpeg', '', log=lambda _: None)
                self.assertEqual(result['state'], 'failed')
                self.assertEqual(run.call_count, count)
                if count == 2:
                    self.assertIn('-filter_script:v', run.call_args.args[0])
                self.assertEqual(old.read_bytes(), b'previous finished movie')

    @unittest.skipUnless(FFMPEG, '需要 FFmpeg（或设置 FFMPEG）')
    def test_real_ffmpeg_many_ranges_preserve_frame_identity_and_timestamps(self):
        for segments in (116, 1024):
            with self.subTest(segments=segments), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                frames = segments * 2
                # 两个色彩分量编码源帧号；解码后逐像素检查实际保留的是哪些帧。
                source = b''.join(bytes((i % 256, i // 256, 0)) * 16 * 16 for i in range(frames))
                # NUT 装原始 RGB，避免源编码器影响逐像素筛帧断言；输入按容器内容识别。
                subprocess.run([FFMPEG, '-v', 'error', '-f', 'rawvideo', '-pixel_format', 'rgb24',
                                '-video_size', '16x16', '-framerate', '60', '-i', 'pipe:0',
                                '-c:v', 'rawvideo', '-f', 'nut', str(root/'video.mkv')], input=source, check=True)
                (root/'frames.csv').write_text('index,chapter_ticks\n'+''.join(
                    f'{i},{i if i % 2 == 0 else -1}\n' for i in range(frames)))
                pcm = bytes(segments * purecut.SAMPLES_PER_FRAME * 8)
                (root/'audio_play.wav').write_bytes(purecut._wav_header(len(pcm)) + pcm)
                result = purecut.make_pure(root, FFMPEG, '-c:v libx264rgb -crf 0 -preset ultrafast -threads 1',
                                           log=lambda _: None)
                self.assertEqual(result['state'], 'finished', result)
                self.assertEqual(result['frames'], segments)
                output = root/'gameplay_pure.mp4'
                decoded = subprocess.run([FFMPEG, '-v', 'error', '-i', str(output),
                                          '-map', '0:v', '-pix_fmt', 'rgb24', '-f', 'rawvideo', '-'],
                                         capture_output=True, check=True).stdout
                expected = b''.join(source[i*768:(i+1)*768] for i in range(0, frames, 2))
                self.assertEqual(decoded, expected)
                timing = subprocess.run([FFMPEG, '-v', 'info', '-i', str(output), '-map', '0:v',
                                         '-vf', 'showinfo', '-f', 'null', '-'],
                                        capture_output=True, text=True, check=True).stderr
                pts = [float(t) for t in re.findall(r'\bpts_time:([\d.]+)', timing)]
                self.assertEqual(len(pts), segments)
                for i, t in enumerate(pts):
                    self.assertAlmostEqual(t, i/60, delta=0.001)

    @unittest.skipUnless(FFMPEG, '需要 FFmpeg（或设置 FFMPEG）')
    def test_real_encoder_error_does_not_retry_or_reuse_old_log_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            log = root/'ffmpeg.log'
            log.write_text("Unrecognized option '/filter:v'.\n")
            with self.assertRaises(purecut.FFmpegError) as caught:
                purecut._run([FFMPEG, '-v', 'error', '-f', 'lavfi', '-i', 'color=s=16x16',
                              '-frames:v', '1', '-c:v', 'nonexistent_purecut_encoder',
                              str(root/'out.mkv')], log)
            self.assertIn('nonexistent_purecut_encoder', caught.exception.output)
            self.assertNotIn('Unrecognized option', caught.exception.output)


if __name__ == '__main__':
    unittest.main()
