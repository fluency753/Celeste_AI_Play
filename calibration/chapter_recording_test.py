"""Real recorder transactions, simulated external OBS/bridge; no live sockets."""
import copy
import json
from pathlib import Path
import tempfile
import subprocess
import unittest
from unittest.mock import patch

from orchestrator.chapter_recording import ChapterRecording
from orchestrator.recording import Recorder,RecordOptions,RecordingHalt
from orchestrator.purecut import _wav_header
from orchestrator.session_store import write_json


class ChapterRecordingTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.run=Path(self.temp.name).resolve();self.starts=[];self.stops=[];self.fail_ack=False
        self.hello={'scene':'Overworld','chapter_recording':1,'record_chapter_active':False,
                    'record_audio_nrt':True,'record_audio_tracks':2,'record_audio_active':False,
                    'clock':{'connected':True,'lockstep':False,'plugin':{'state':'idle','frames':0,'audio_nrt':True,'audio_tracks':2}}}
        recorder=Recorder(self.call,RecordOptions(self.run/'recording','ffmpeg','',audio_source='game_nrt'),lambda _:None)
        recorder.bind_run(self.run)
        self.rec=ChapterRecording(recorder)
        self.visible=patch('orchestrator.recording.gamewindow.ensure_visible');self.visible.start();self.addCleanup(self.visible.stop)

    def call(self,cmd,**args):
        if getattr(self,'transport_closed',False):
            raise AssertionError('Bridge called after encoder callback closed transport')
        if cmd=='record':
            plugin=self.hello['clock']['plugin']
            if args['action']=='start':
                directory=Path(args['dir']);directory.mkdir(parents=True,exist_ok=True)
                self.starts.append(str(directory))
                plugin.update(state='recording',recording=True,dir=str(directory),frames=0)
                self.hello['clock']['lockstep']=True;self.hello['record_audio_active']=True
                self.hello.pop('record_chapter_sid',None)
                (directory/'video.mkv').write_bytes(b'fixture video')
                (directory/'frames.csv').write_text('index,game_frame,chapter_ticks,obs_ts_ns\n')
                (directory/'segments.csv').write_text('segment,first_index,start_ts_ns,measured_ms,offset_ms,method\n')
                for name in ('audio.wav','audio_play.wav'):(directory/name).write_bytes(_wav_header(0))
                if self.fail_ack:
                    self.fail_ack=False;raise OSError('ACK lost after start reached OBS')
            else:
                self.stops.append(plugin['dir']);plugin.update(state='finished',recording=False)
                self.hello['clock']['lockstep']=False;self.hello['record_audio_active']=False
        return copy.deepcopy(self.hello)

    def frames(self,n,sid='Celeste/0-Intro'):
        p=self.hello['clock']['plugin'];d=Path(p['dir']);start=p['frames']
        with (d/'frames.csv').open('a') as stream:
            for i in range(start,start+n):stream.write(f'{i},{100+i},{i},0\n')
        p['frames']+=n
        for name in ('audio.wav','audio_play.wav'):(d/name).write_bytes(_wav_header(p['frames']*6400)+b'\0'*(p['frames']*6400))
        self.hello.update(scene='Level',record_chapter_active=True,record_chapter_sid=sid,record_chapter_side='A')

    def map(self):self.hello.update(scene='Overworld',record_chapter_active=False)

    def test_first_and_second_chapter_have_distinct_ids_and_empty_menu_does_not_rotate(self):
        self.rec.start();first=self.rec.session_id
        self.rec.before_step();self.rec.before_step();self.assertEqual(len(self.starts),1)
        self.frames(3);self.rec.before_step();self.frames(2);self.map();self.rec.before_step()
        self.assertNotEqual(self.rec.session_id,first)
        self.assertEqual(len(self.starts),2);self.assertEqual(len(self.stops),1)
        self.assertEqual(self.rec.data['entries'][0]['result']['frames'],5)
        self.assertTrue(self.rec.data['entries'][0]['result']['postprocess_pending'])
        self.assertEqual(Recorder.saved_session(self.run)['id'],self.rec.session_id)
        self.rec.before_step();self.assertEqual(len(self.starts),2)

    def test_explicit_new_continuous_recording_accepts_abandoned_chapter_series(self):
        from types import SimpleNamespace
        from orchestrator import run
        from orchestrator.config import RunConfig
        self.rec.start();self.rec.stop(defer_postprocessing=True)
        saved=Recorder.saved_session(self.run)
        Recorder.abandon_saved(saved,'test abandonment')
        cfg=RunConfig(agent='test',save='test',model='test',recording_mode='continuous')
        args=SimpleNamespace(prepared=None,previous={'dir':self.run,'state':{'recording_mode':'chapter'}},
                             record=True,new_recording=True,no_agent=True)
        with patch.object(run,'parse_args',return_value=(cfg,args)), \
             patch.object(run,'port_free',return_value=True), \
             patch('orchestrator.bridge_requirements.preflight_bridge',return_value=self.hello), \
             patch.object(run,'make_run_dir',side_effect=RuntimeError('new run reached')) as create, \
             patch('tools.bridge_client.BridgeClient') as bridge:
            with self.assertRaisesRegex(RuntimeError,'new run reached'):
                run.main()
        create.assert_called_once_with('test')
        bridge.assert_not_called()

    def test_first_start_ack_loss_recognizes_same_capture_after_reconstruction(self):
        self.fail_ack=True
        with self.assertRaises(OSError):self.rec.start()
        saved=Recorder.saved_session(self.run)
        self.assertEqual(saved['recovery_start']['phase'],'requested')
        recorder=Recorder.recover(self.call,saved,self.run,lambda _:None)
        resumed=ChapterRecording(recorder);resumed.before_step()
        self.assertEqual(resumed.session_id,saved['id']);self.assertEqual(len(self.starts),1)
        self.assertIsNone(resumed.data['pending'])

    def test_rotation_ack_loss_does_not_repeat_start(self):
        self.rec.start();self.frames(2);self.rec.before_step();self.map();self.fail_ack=True
        with self.assertRaises(RecordingHalt):self.rec.before_step()
        self.assertIsNotNone(self.rec.data['pending'])
        self.rec.before_step()
        self.assertIsNone(self.rec.data['pending']);self.assertEqual(len(self.starts),2)

    def test_pending_write_failure_retries_before_start(self):
        self.rec.start();self.frames(2);self.map()
        original=self.rec._save
        def fail():
            if self.rec.data.get('pending') and len(self.rec.data['entries'])==2:raise OSError('disk unavailable')
            original()
        with patch.object(self.rec,'_save',fail),self.assertRaises(RecordingHalt):self.rec.before_step()
        self.assertEqual(len(self.starts),1)
        original_call=self.call
        def checked(cmd,**args):
            if cmd=='record' and args['action']=='start':
                data=json.loads(self.rec.path.read_text())
                self.assertEqual(data['pending']['id'],self.rec.session_id)
            return original_call(cmd,**args)
        self.rec.call=checked;self.rec.before_step()
        self.assertEqual(len(self.starts),2)

    def test_offline_old_chapters_do_not_change_current_pointer_or_touch_obs(self):
        self.rec.start();self.frames(2);self.rec.before_step();self.map();self.rec.before_step()
        current=self.rec.session_id
        with patch('orchestrator.recording.finished_movie',return_value=None):result=self.rec.stop()
        self.assertEqual(len(self.stops),2)
        self.assertEqual(Recorder.saved_session(self.run)['id'],current)
        self.assertEqual(len(result['chapters']),1)
        self.assertEqual(result['chapters'][0]['frames'],2)
        self.assertEqual(self.rec.stop(),result)
        self.assertEqual(len(self.stops),2)

    def test_continuation_keeps_episode_context_in_manifest(self):
        self.rec.start();self.frames(2);self.rec.before_step()
        self.assertTrue(Recorder.saved_session(self.run)['options']['chapter_context'])

    def test_resolve_playlist_pending_precedes_recovering_old_pointer(self):
        from orchestrator.chapter_recording import resolve_saved
        self.rec.start();self.frames(2);self.map()
        original=self.rec._start_pending
        with patch.object(self.rec,'_start_pending',side_effect=OSError('crash before manifest')),self.assertRaises(RecordingHalt):
            self.rec.before_step()
        old=Recorder.saved_session(self.run)
        selected=resolve_saved(old,self.hello)
        self.assertNotEqual(selected['id'],old['id'])
        self.assertEqual(selected['id'],self.rec.data['pending']['id'])
        self.assertEqual(len(self.starts),1)

    def test_user_stop_settles_lost_start_before_stopping_capture(self):
        self.fail_ack=True
        with self.assertRaises(OSError):self.rec.start()
        with patch('orchestrator.recording.finished_movie',return_value=None):result=self.rec.stop()
        self.assertEqual(len(self.starts),1)
        self.assertEqual(len(self.stops),1)
        self.assertFalse(self.hello['clock']['lockstep'])
        self.assertFalse(result['pending'])

    def test_unstarted_stop_does_not_start_obs(self):
        result=self.rec.stop()
        self.assertEqual(self.starts,[]);self.assertEqual(self.stops,[])
        self.assertFalse(result['pending'])
        self.assertEqual(self.rec.stop(),result)

    def test_cancellation_write_failure_is_retried_durably(self):
        original=self.rec._save
        with patch.object(self.rec,'_save',side_effect=OSError('disk full')),self.assertRaises(OSError):
            self.rec.stop()
        self.assertIsNotNone(self.rec.data['pending'])
        result=self.rec.stop()
        self.assertEqual(self.starts,[])
        self.assertIsNone(json.loads(self.rec.path.read_text())['pending'])
        self.assertFalse(result['pending'])

    def test_no_record_resolution_does_not_create_a_next_chapter(self):
        from orchestrator.chapter_recording import resolve_saved
        self.rec.start();self.frames(2);self.rec.before_step();self.map()
        with patch('orchestrator.recording.finished_movie',return_value=None):self.rec.stop()
        before=self.rec.path.read_bytes()
        saved=Recorder.saved_session(self.run)
        result=resolve_saved(saved,self.hello,prepare_next=False)
        self.assertEqual(result['state'],'finished')
        self.assertEqual(self.rec.path.read_bytes(),before)

    def test_manifest_cannot_redirect_output_away_from_playlist(self):
        from orchestrator.chapter_recording import resolve_saved
        self.rec.start();saved=Recorder.saved_session(self.run)
        saved['options']['dir']=str(self.run/'other')
        write_json(self.rec.opts.dir/'session.json',saved)
        with self.assertRaises(RecordingHalt):resolve_saved(saved,self.hello)

    def test_corrupt_playlist_cannot_report_all_recordings_finished(self):
        from orchestrator.recording_lifecycle import pending_recording
        self.rec.start();self.rec.stop(defer_postprocessing=True)
        saved=Recorder.saved_session(self.run)
        for data in ({'version':999,'entries':[]}, {'version':1,'entries':[]},
                     {'version':1,'entries':['invalid']}, []):
            with self.subTest(data=data):
                write_json(self.rec.path,data)
                self.assertTrue(pending_recording(saved))

    def test_map_resume_prepares_next_id_without_reopening_closed_chapter(self):
        from orchestrator.chapter_recording import resolve_saved
        self.rec.start();self.frames(3);self.map()
        self.rec.stop(defer_postprocessing=True)
        saved=Recorder.saved_session(self.run)
        selected=resolve_saved(saved,self.hello)
        self.assertNotEqual(selected['id'],saved['id'])
        self.assertEqual(resolve_saved(saved,self.hello)['id'],selected['id'])
        recovered=Recorder.recover(self.call,selected,self.run,lambda _:None)
        series=ChapterRecording(recovered)
        series.before_step()
        self.assertEqual(len(self.starts),2)
        self.assertNotEqual(self.starts[0],self.starts[1])
        self.assertEqual(series.data['entries'][0]['state'],'finishing')
        self.assertEqual(series.data['entries'][0]['result']['frames'],3)

    def test_level_resume_keeps_closed_chapter_id(self):
        from orchestrator.chapter_recording import resolve_saved
        self.rec.start();self.frames(3)
        self.rec.stop(defer_postprocessing=True)
        saved=Recorder.saved_session(self.run)
        self.assertEqual(resolve_saved(saved,self.hello)['id'],saved['id'])
        self.assertEqual(len(self.starts),1)

    def test_public_finish_saved_processes_all_old_chapters_after_transport_closed(self):
        from orchestrator.recording_lifecycle import pending_recording
        self.rec.start();self.frames(2);self.rec.before_step();self.map();self.rec.before_step()
        self.rec.stop(defer_postprocessing=True)
        saved=Recorder.saved_session(self.run)
        self.assertTrue(pending_recording(saved))
        def close():self.transport_closed=True
        with patch('orchestrator.recording.finished_movie',return_value=None):
            result=Recorder.finish_saved(self.call,saved,self.run,lambda _:None,on_encoder_finished=close)
        self.assertEqual(result['frames'],2)
        self.assertFalse(result['pending'])
        self.assertTrue(self.transport_closed)
        self.assertFalse(pending_recording(Recorder.saved_session(self.run)))

    def test_public_pending_includes_old_chapter_when_current_empty(self):
        from orchestrator.recording_lifecycle import pending_recording
        self.rec.start();self.frames(2);self.map()
        with patch.object(self.rec,'_start_pending',side_effect=OSError('cancel before queued')),self.assertRaises(RecordingHalt):self.rec.before_step()
        with patch.object(self.rec,'_finish_entries',side_effect=OSError('offline failure')),self.assertRaises(OSError):self.rec.stop()
        saved=Recorder.saved_session(self.run)
        self.assertEqual(saved['state'],'finished')
        self.assertTrue(pending_recording(saved))
        with patch('orchestrator.recording.finished_movie',return_value=None):
            result=Recorder.finish_saved(self.call,saved,self.run,lambda _:None)
        self.assertEqual(result['frames'],2)
        self.assertFalse(pending_recording(Recorder.saved_session(self.run)))

    def test_real_movies_are_published_separately_without_merging_chapters(self):
        from orchestrator.recording import find_ffmpeg
        from orchestrator.recording_recovery import ffprobe_path
        try:ffmpeg=find_ffmpeg();probe=ffprobe_path(ffmpeg)
        except RuntimeError as exc:self.skipTest(str(exc))
        self.rec.active.opts.ffmpeg=ffmpeg
        self.rec.active.opts.video_args='-c:v libx264 -preset ultrafast -g 1 -bf 0'
        original=self.call
        def encoded(cmd,**args):
            if cmd=='record' and args['action']=='stop':
                plugin=self.hello['clock']['plugin'];directory=Path(plugin['dir']);count=plugin['frames']
                if count:
                    color='red' if len(self.starts)==1 else 'blue'
                    for command in (
                        [ffmpeg,'-v','error','-y','-f','lavfi','-i',f'color=c={color}:s=64x48:r=60','-frames:v',str(count),'-c:v','libx264','-g','1','-bf','0',str(directory/'video.mkv')],
                        [ffmpeg,'-v','error','-y','-i',str(directory/'video.mkv'),'-i',str(directory/'audio.wav'),'-c:v','copy','-c:a','aac','-shortest',str(directory/'gameplay.mp4')]):
                        subprocess.run(command,check=True,capture_output=True,timeout=30)
            return original(cmd,**args)
        self.rec.call=encoded
        self.rec.start();self.frames(3);self.rec.before_step();self.map();self.rec.before_step()
        self.frames(5,'Celeste/1-ForsakenCity');self.rec.before_step();self.map()
        result=self.rec.stop()
        self.assertEqual([c['frames'] for c in result['chapters']],[3,5])
        for item,expected in zip(result['chapters'],(3,5)):
            directory=Path(item['dir'])
            for name in ('gameplay.mp4','gameplay_pure.mp4'):
                data=subprocess.run([probe,'-v','error','-count_frames','-select_streams','v:0','-show_entries','stream=nb_read_frames','-of','json',str(directory/name)],check=True,capture_output=True,text=True,timeout=30)
                self.assertEqual(int(json.loads(data.stdout)['streams'][0]['nb_read_frames']),expected)
        self.assertNotEqual(result['chapters'][0]['dir'],result['chapters'][1]['dir'])

    def test_public_stop_recognizes_pending_next_chapter_after_lost_ack(self):
        self.rec.start();self.frames(2);self.rec.before_step();self.map();self.fail_ack=True
        with self.assertRaises(RecordingHalt):self.rec.before_step()
        saved=Recorder.saved_session(self.run)
        with patch('orchestrator.recording.finished_movie',return_value=None):
            result=Recorder.finish_saved(self.call,saved,self.run,lambda _:None)
        self.assertEqual(len(self.starts),2)
        self.assertEqual(len(self.stops),2)
        self.assertEqual(result['frames'],2)
        self.assertFalse(result['pending'])

    def test_each_low_level_macro_step_crosses_the_rotation_guard(self):
        from tools.bridge_client import BridgeClient
        from types import SimpleNamespace
        client=BridgeClient.__new__(BridgeClient)
        client.next_id=1;client.cancel_check=None;client.step_options=None
        replies=[];steps=[]
        def send(data,**kwargs):
            request=json.loads(data);cmd=request.pop('cmd');ident=request.pop('id')
            if cmd=='step':
                steps.append(len(self.starts))
                if len(steps)==1:self.frames(2)
                elif len(steps)==2:self.map()
                else:self.frames(3,'Celeste/1-ForsakenCity')
                result={'obs':{'scene':self.hello['scene']},'result':{'consumed':1}}
            else:result=self.call(cmd,**request)
            replies.append(json.dumps({'id':ident,'ok':True,**result}).encode()+b'\n')
        client._send=send;client.file=SimpleNamespace(readline=lambda:replies.pop(0))
        self.rec.call=client.call;client.before_step=self.rec.before_step
        self.rec.start()
        # Equivalent to a menu macro's repeated client.call, with no outer act callback.
        for _ in range(3):client.call('step',inputs=['1,C'])
        self.assertEqual(steps,[1,1,2])
        self.assertEqual(len(self.stops),1)
