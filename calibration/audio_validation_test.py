"""Audio checks use PCM fixtures and never connect to the game or OBS."""
import json
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest

from orchestrator.audio_validation import wav_dropouts


def write_wav(path, frames, *, extra=b'', format_code=3):
    pcm = b''.join(struct.pack('<ff', *frame) for frame in frames)
    fmt = struct.pack('<HHIIHH', format_code, 2, 48000, 384000, 8, 32)
    chunks = b'fmt ' + struct.pack('<I', len(fmt)) + fmt
    if extra:
        chunks += b'JUNK' + struct.pack('<I', len(extra)) + extra + (b'\0' if len(extra) % 2 else b'')
    chunks += b'data' + struct.pack('<I', len(pcm)) + pcm
    path.write_bytes(b'RIFF' + struct.pack('<I', len(chunks)+4) + b'WAVE' + chunks)


class AudioValidationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)/'audio.wav'

    def tearDown(self):
        self.temp.cleanup()

    def test_silent_residue_is_not_a_dropout(self):
        tiny = 1.2924697071141057e-26
        write_wav(self.path, [(tiny,0), (0,0), (tiny,0), (0,0), (tiny,0)])
        self.assertEqual(wav_dropouts(self.path), [])
        self.assertEqual(wav_dropouts(self.path, 0), [1,3])

    def test_audible_gap_is_detected_in_either_channel(self):
        for signal in [(0.05,0), (0,0.05), (0.05,0.05)]:
            with self.subTest(signal=signal):
                write_wav(self.path, [signal,(0,0),signal])
                self.assertEqual(wav_dropouts(self.path), [1])

    def test_periodic_1024_sample_gaps_are_not_hidden_by_the_threshold(self):
        frames = [(0.03,0.02)]*4096
        for index in (1024,2048,3072):
            frames[index] = (0,0)
        write_wav(self.path, frames)
        self.assertEqual(wav_dropouts(self.path), [1024,2048,3072])

    def test_zero_in_only_one_channel_and_silence_edges_are_not_gaps(self):
        write_wav(self.path, [(0,0),(0.05,0.05),(0,0.05),(0.05,0.05),(0,0),(0,0)])
        self.assertEqual(wav_dropouts(self.path), [])

    def test_signal_must_continue_in_the_same_channel(self):
        write_wav(self.path, [(0.05,0),(0,0),(0,0.05)])
        self.assertEqual(wav_dropouts(self.path), [])

    def test_quiet_signal_can_be_checked_with_an_explicit_lower_threshold(self):
        write_wav(self.path, [(1e-8,1e-8),(0,0),(1e-8,1e-8)])
        self.assertEqual(wav_dropouts(self.path), [])
        self.assertEqual(wav_dropouts(self.path, 1e-9), [1])

    def test_stream_chunk_boundaries_preserve_both_neighbors(self):
        for hole in (65535,65536):
            with self.subTest(hole=hole):
                frames = [(0.02,0.02)]*65539
                frames[hole] = (0,0)
                write_wav(self.path, frames)
                self.assertEqual(wav_dropouts(self.path), [hole])

    def test_metadata_containing_data_is_not_mistaken_for_audio(self):
        write_wav(self.path, [(0.02,0.02),(0,0),(0.02,0.02)], extra=b'data!')
        self.assertEqual(wav_dropouts(self.path), [1])

    def test_invalid_or_truncated_wav_is_rejected(self):
        self.path.write_bytes(b'not a wav')
        with self.assertRaises(ValueError):
            wav_dropouts(self.path)
        write_wav(self.path, [(0.02,0.02)], format_code=1)
        with self.assertRaisesRegex(ValueError, '浮点立体声'):
            wav_dropouts(self.path)
        write_wav(self.path, [(0.02,0.02)])
        self.path.write_bytes(self.path.read_bytes()[:-4])
        with self.assertRaisesRegex(ValueError, '截断'):
            wav_dropouts(self.path)

    def test_invalid_threshold_is_rejected(self):
        for value in (-1, float('nan'), float('inf')):
            with self.subTest(value=value), self.assertRaises(ValueError):
                wav_dropouts(self.path, value)

    def test_offline_command_reports_success_or_signal_gaps(self):
        tool = Path(__file__).resolve().parent.parent/'tools/check_recording_audio.py'
        for frames, code in [([(1e-26,0),(0,0),(1e-26,0)],0),
                             ([(0.05,0),(0,0),(0.05,0)],1)]:
            write_wav(self.path, frames)
            result = subprocess.run([sys.executable,'-X','utf8',str(tool),str(self.path)],
                                    capture_output=True,text=True,encoding='utf-8')
            self.assertEqual(result.returncode,code,result.stderr)
            self.assertEqual(json.loads(result.stdout)['dropouts'],code)


if __name__ == '__main__':
    unittest.main()
