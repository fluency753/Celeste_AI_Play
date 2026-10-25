import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from orchestrator.config import RunConfig
from orchestrator.metrics import Metrics
from orchestrator.playback import Playback, MAGIC, HEADER
from orchestrator.rollout_tail import RecordTail


def record(index):
    return HEADER.pack(float(index),0,0,2,index,0,0,index*8,0,*([0]*16)) + b'\x08\0\0\0'


class StreamTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.root=Path(self.tmp.name)
        cfg=RunConfig(agent='test',save='test',model='test')
        cfg.playback=True
        cfg.playback_stream_interval_s=0  # poll deterministically; rendering still uses the real worker
        self.metrics=Metrics(self.root/'metrics.json','test','test','high',60)
        session=SimpleNamespace(workspace=self.root/'workspace',run_dir=self.root/'run',
                                last_obs={'level':{'bounds':[0,-100,10000,200]}},decision_index=0,room_entry_frame=0)
        self.gate=SimpleNamespace(started=True,charge=Mock())
        self.playback=Playback(cfg,session,self.metrics,self.gate,log=lambda _:None)
        cfg.playback_stream_interval_s=1
        self.playback._call=Mock(return_value={'render_avg_ms':1})
        self.file=self.playback.dir/'writer.bin'

    def tearDown(self):
        self.playback.close()
        self.tmp.cleanup()

    def wait(self):
        with self.playback._cv:
            self.assertTrue(self.playback._cv.wait_for(lambda:self.playback._backlog==0,timeout=3))

    def poll(self):
        with self.playback._submit_lock:
            active=[(k,m) for k,m in self.playback._msgs.items() if m.program and m.pending]
            self.playback._queue_preview(*active[0])

    def start(self, mid='message', call='call'):
        self.playback.note_tool_use(mid,call,'Bash',{'command':'python search.py','description':'search'})

    def test_tail_waits_for_complete_record_and_does_not_touch_writer(self):
        tail=RecordTail(MAGIC,HEADER)
        data=MAGIC+record(1)+record(2)[:10]
        self.file.write_bytes(data)
        self.assertEqual(tail.read([self.file]),[MAGIC+record(1)])
        self.assertEqual(tail.read([self.file]),[])
        self.assertEqual(self.file.read_bytes(),data)
        with self.file.open('ab') as f:f.write(record(2)[10:])
        self.assertEqual(tail.read([self.file]),[MAGIC+record(2)])
        self.assertEqual(tail.read([self.file]),[])
        self.file.write_bytes(MAGIC+record(3))
        self.assertEqual(tail.read([self.file]),[MAGIC+record(3)])

    def test_long_complete_record_exceeds_fair_share_but_does_not_stall(self):
        tail = RecordTail(MAGIC, HEADER)
        points = 16366  # 65537 bytes: one byte above the 128-file window.
        long_record = HEADER.pack(1.,0,0,points,1,0,0,8,0,*([0]*16)) + b'\0'*(4*(points-1))
        paths = [self.playback.dir/f'writer-{i}.bin' for i in range(128)]
        for path in paths:
            path.write_bytes(MAGIC + record(2))
        paths[0].write_bytes(MAGIC + long_record[:65536])
        self.assertEqual(len(tail.read(paths)),127)
        self.assertEqual(tail.read(paths),[])
        with paths[0].open('ab') as stream:
            stream.write(long_record[65536:] + record(3))
        self.assertEqual(tail.read(paths),[MAGIC + long_record])
        self.assertEqual(tail.read(paths),[MAGIC + record(3)])
        self.assertEqual(tail.read(paths),[])
        self.assertEqual(paths[0].read_bytes(),MAGIC+long_record+record(3))

    def test_oversize_record_is_reported_and_does_not_block_following_record(self):
        tail = RecordTail(MAGIC, HEADER)
        skipped = []
        points = 16366
        long_record = HEADER.pack(1.,0,0,points,1,0,0,8,0,*([0]*16)) + b'\0'*(4*(points-1))
        self.file.write_bytes(MAGIC + long_record + record(3))
        self.assertEqual(tail.read([self.file],budget=65536,on_skip=skipped.append),[])
        self.assertEqual(len(skipped),1)
        self.assertIn('65537',skipped[0])
        self.assertEqual(tail.read([self.file],budget=65536,on_skip=skipped.append),[MAGIC+record(3)])
        self.assertEqual(len(skipped),1)

    def test_total_read_budget_is_shared_across_large_records_and_polls(self):
        tail = RecordTail(MAGIC,HEADER)
        points = 262144
        paths = [self.playback.dir/f'large-{i}.bin' for i in range(16)]
        for i,path in enumerate(paths):
            path.write_bytes(MAGIC + HEADER.pack(float(i),0,0,points,i,0,0,8,0,*([0]*16))
                             + bytes(4*(points-1)))
        seen = []
        for _ in range(20):
            chunks = tail.read(paths)
            self.assertLessEqual(sum(map(len,chunks)),8*1024*1024)
            seen.extend(int(HEADER.unpack_from(chunk,len(MAGIC))[0]) for chunk in chunks)
            if len(seen) == len(paths): break
        self.assertEqual(sorted(seen),list(range(16)))
        self.assertEqual(tail.read(paths),[])

    def test_deferred_file_gets_budget_before_an_earlier_writer_grows(self):
        tail = RecordTail(MAGIC,HEADER)
        paths = [self.playback.dir/f'fair-{i}.bin' for i in range(3)]
        for i,path in enumerate(paths): path.write_bytes(MAGIC+record(i+1))
        first = tail.read(paths,budget=200)
        self.assertEqual(first,[MAGIC+record(1),MAGIC+record(2)])
        with paths[0].open('ab') as stream: stream.write(record(4))
        second = tail.read(paths,budget=200)
        self.assertEqual(second[0],MAGIC+record(3))
        self.assertLessEqual(sum(map(len,second)),200)

    def test_deferred_file_priority_survives_new_log_files(self):
        tail = RecordTail(MAGIC,HEADER)
        paths = [self.playback.dir/f'fair-{i}.bin' for i in range(3)]
        for i,path in enumerate(paths): path.write_bytes(MAGIC+record(i+1))
        self.assertEqual(len(tail.read(paths,budget=200)),2)
        added = self.playback.dir/'fair--new.bin'
        added.write_bytes(MAGIC+record(99))
        with paths[1].open('ab') as stream: stream.write(record(4)+record(5))
        second = tail.read(sorted([added,*paths]),budget=200)
        self.assertEqual(second[0],MAGIC+record(3))

    def test_previews_arrive_before_result_and_account_only_once(self):
        self.start()
        self.file.write_bytes(MAGIC+record(1))
        self.poll();self.wait()
        self.assertEqual(self.playback._call.call_count,1)
        self.assertTrue(self.file.exists())
        self.assertEqual(self.metrics.state.sim_calls,0)
        self.assertEqual(self.metrics.state.sim_rollouts,0)
        with self.file.open('ab') as f:f.write(record(2)+record(3))
        self.poll();self.wait()
        self.assertEqual(self.playback._call.call_count,2)
        self.playback.note_tool_result('call');self.wait()
        self.assertEqual(self.playback._call.call_count,2)  # no full duplicate replay at the end
        self.assertEqual(self.metrics.state.sim_calls,1)
        self.assertEqual(self.metrics.state.sim_rollouts,3)
        self.assertEqual(len(self.playback.rounds),1)
        self.gate.charge.assert_called_once()

    def test_stream_default_draws_every_complete_route(self):
        self.start();self.file.write_bytes(MAGIC+b''.join(record(i) for i in range(1,101)))
        self.poll();self.wait()
        segment=self.playback._call.call_args.args[0]
        self.assertEqual(len(segment['tracks']),100)
        self.assertLessEqual(segment['frames'],90)

    def test_slow_preview_coalesces_updates_instead_of_unbounded_queue(self):
        entered,release=threading.Event(),threading.Event()
        def render(*args,**kwargs):
            entered.set();release.wait(3);return {'render_avg_ms':1}
        self.playback._call.side_effect=render
        self.start();self.file.write_bytes(MAGIC+record(1));self.poll()
        self.assertTrue(entered.wait(2))
        try:
            for index in range(2,12):
                with self.file.open('ab') as f:f.write(record(index))
                self.poll()
            self.assertEqual(len(self.playback._live_pending),1)
            self.assertEqual(self.playback._backlog,2)
        finally:release.set()
        self.wait()
        self.assertEqual(self.playback._call.call_count,2)
        self.assertEqual(len(self.playback._call.call_args.args[0]['tracks']),10)

    def test_read_result_does_not_move_a_running_programs_log(self):
        self.start();self.file.write_bytes(MAGIC+record(1))
        self.playback.note_tool_use('read','read-call','Read',{})
        self.playback.note_tool_result('read-call')
        self.assertTrue(self.file.exists())
        self.assertEqual(self.metrics.state.sim_calls,0)
        with self.assertRaises(TimeoutError):self.playback.drain(timeout=0)
        self.playback.note_tool_result('call');self.wait()
        self.assertEqual(self.metrics.state.sim_rollouts,1)

    def test_renderer_pressure_enables_sampling_only_after_measurement(self):
        self.start();self.file.write_bytes(MAGIC+b''.join(record(i) for i in range(1,101)))
        self.playback._call.return_value={'render_avg_ms':20}
        self.poll();self.wait()
        self.assertEqual(len(self.playback._call.call_args.args[0]['tracks']),100)
        self.assertTrue(self.playback._stream_sample)
        with self.file.open('ab') as f:f.write(b''.join(record(i) for i in range(101,201)))
        self.poll();self.wait()
        self.assertLess(len(self.playback._call.call_args.args[0]['tracks']),100)

    def test_parallel_tools_of_same_search_do_not_consume_active_log(self):
        self.start(call='one');self.start(call='two')
        self.file.write_bytes(MAGIC+record(1))
        self.playback.note_tool_result('one')
        self.assertTrue(self.file.exists())
        self.assertEqual(self.metrics.state.sim_calls,0)
        with self.file.open('ab') as f:f.write(record(2))
        self.playback.note_tool_result('two');self.wait()
        self.assertEqual(self.metrics.state.sim_rollouts,2)
        self.assertEqual(self.metrics.state.sim_calls,1)
        self.gate.charge.assert_called_once()

    def test_failed_preview_does_not_suppress_the_final_replay(self):
        self.start();self.file.write_bytes(MAGIC+record(1))
        self.playback._call.return_value=None
        self.poll();self.wait()
        self.playback._call.return_value={}
        self.playback.note_tool_result('call');self.wait()
        self.assertEqual(self.playback._call.call_count,2)
        self.assertEqual(self.metrics.state.sim_rollouts,1)
