"""MP4 finished movies, legacy lookup and atomic pure-cut publication."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from orchestrator import purecut
from orchestrator.recording import Recorder, RecordOptions
from orchestrator.recording_files import finished_movie


def ticks(directory, values):
    (directory/'frames.csv').write_text('index,chapter_ticks\n'+''.join(
        f'{i},{value}\n' for i,value in enumerate(values)),encoding='utf-8')


class RecordingMovieTests(unittest.TestCase):
    def test_lookup_prefers_mp4_and_accepts_legacy_mkv(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for pure in (False,True):
                stem = 'gameplay_pure' if pure else 'gameplay'
                old,new = root/(stem+'.mkv'),root/(stem+'.mp4')
                old.touch()
                self.assertEqual(finished_movie(root,pure),old)
                new.touch()
                self.assertEqual(finished_movie(root,pure),new)

    def test_no_replay_mp4_is_reused_without_running_an_encoder(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp);ticks(root,[0,1,2])
            (root/'gameplay.mp4').write_bytes(b'completed mp4 fixture')
            with patch.object(purecut,'_run') as run:
                result = purecut.make_pure(root,'ffmpeg','',log=lambda _:None)
            self.assertEqual(result['state'],'finished')
            self.assertTrue(result['same_as_full'])
            self.assertEqual((root/'gameplay_pure.mp4').read_bytes(),(root/'gameplay.mp4').read_bytes())
            run.assert_not_called()

    def test_no_replay_legacy_mkv_is_remuxed_instead_of_renamed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp);ticks(root,[0,1,2])
            old = root/'gameplay.mkv';old.write_bytes(b'legacy fixture')
            def remux(command,log):
                self.assertEqual(command[command.index('-i')+1],str(old))
                self.assertEqual(command[command.index('-c')+1],'copy')
                self.assertIn('+faststart',command)
                Path(command[-1]).write_bytes(b'mp4 fixture')
            with patch.object(purecut,'_run',side_effect=remux):
                result = purecut.make_pure(root,'ffmpeg','',log=lambda _:None)
            self.assertEqual(result['state'],'finished')
            self.assertEqual((root/'gameplay_pure.mp4').read_bytes(),b'mp4 fixture')
            self.assertEqual(old.read_bytes(),b'legacy fixture')

    def test_failed_mux_keeps_previous_finished_pure_movie(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp);ticks(root,[0,-1,1])
            out = root/'gameplay_pure.mp4';out.write_bytes(b'previous good movie')
            with patch.object(purecut,'play_track',return_value=root/'audio_play.wav'), \
                 patch.object(purecut,'_run',side_effect=[None,RuntimeError('mux failed')]):
                result = purecut.make_pure(root,'ffmpeg','',log=lambda _:None)
            self.assertEqual(result['state'],'failed')
            self.assertEqual(out.read_bytes(),b'previous good movie')
            self.assertFalse((root/'gameplay_pure.tmp.mp4').exists())

    def test_recorder_stop_recognizes_mp4_and_legacy_then_records_new_pure_file(self):
        for suffix in ('.mp4','.mkv'):
            with self.subTest(suffix=suffix),tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp);(root/('gameplay'+suffix)).touch()
                recorder = Recorder(Mock(),RecordOptions(root,'ffmpeg',''),log=lambda _:None)
                recorder.started = True
                def pure(*args):
                    (root/'gameplay_pure.mp4').write_bytes(b'pure')
                    return {'state':'finished','file':str(root/'gameplay_pure.mp4'),
                            'frames':1,'seconds':1/60,'removed':0}
                with patch.object(recorder,'_plugin',return_value=({'lockstep':False},{'state':'finished','frames':1})), \
                     patch('orchestrator.recording.make_pure',side_effect=pure):
                    result = recorder.stop()
                self.assertIn('gameplay_pure.mp4',result['files'])
                saved = json.loads((root/'recording.json').read_text(encoding='utf-8'))
                self.assertEqual(saved['pure']['file'],str(root/'gameplay_pure.mp4'))

    def test_console_run_list_recognizes_both_finished_formats(self):
        from overlay import console_api
        for suffix in ('.mp4','.mkv'):
            with self.subTest(suffix=suffix),tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp);run = root/'runs/test/20261026-000000';recording = run/'recording'
                recording.mkdir(parents=True)
                (run/'run_state.json').write_text('{"agent":"test"}')
                for stem in ('gameplay','gameplay_pure'):
                    (recording/(stem+suffix)).touch()
                with patch.object(console_api,'REPO_ROOT',root), \
                     patch.object(console_api,'latest_run',return_value=None), \
                     patch.object(console_api,'game_status',return_value={}):
                    rows = console_api.runs()
                self.assertEqual(len(rows),1)
                self.assertTrue(rows[0]['recording'])
                self.assertTrue(rows[0]['pure'])
