import json
import tempfile
import threading
import unittest
from pathlib import Path
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock, patch

from orchestrator.config import RunConfig
from orchestrator.metrics import Metrics
from orchestrator.playback import Playback, MAGIC, HEADER, read_logs
from orchestrator.rollout_tail import RecordTail


def record(index):
    return HEADER.pack(float(index),0,0,2,index,index*8,0,index*8+8,0,*([0]*16)) + b'\x08\0\0\0'


@contextmanager
def small_preview_batches():
    read, batches = RecordTail.read, Playback._remaining_batches
    def bounded_read(tail, paths, **kwargs):
        kwargs['budget'] = min(kwargs.get('budget', 200), 200)
        return read(tail, paths, **kwargs)
    def bounded_batches(playback, log, indexes):
        return batches(playback, log, indexes, budget=200)
    with patch.object(RecordTail,'read',bounded_read), patch.object(Playback,'_remaining_batches',bounded_batches):
        yield


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

    def drawn_starts(self):
        return [int(track['p'][1]) for call in self.playback._call.call_args_list for track in call.args[0]['tracks']]

    def test_finish_renders_every_unread_batch_without_repeating_successful_prefix(self):
        self.start()
        self.file.write_bytes(MAGIC+b''.join(record(i) for i in range(1,7)))
        with small_preview_batches():
            self.poll();self.wait()
            self.assertEqual(self.drawn_starts(),[1,2])
            self.playback.note_tool_result('call');self.wait()
        self.assertEqual(self.drawn_starts(),list(range(1,7)))
        self.assertTrue(all(len(call.args[0]['tracks'])<=2 for call in self.playback._call.call_args_list))
        self.assertEqual(self.metrics.state.sim_calls,1)
        self.assertEqual(self.metrics.state.sim_rollouts,6)
        self.gate.charge.assert_called_once()
        self.assertEqual(len(self.playback.rounds),1)
        self.assertFalse(self.file.exists())
        self.assertEqual(list(self.playback.pending.glob('*.bin')),[])

    def test_tool_finish_without_prior_poll_still_renders_the_entire_log(self):
        self.start()
        self.file.write_bytes(MAGIC+b''.join(record(i) for i in range(1,7)))
        with small_preview_batches():
            self.playback.note_tool_result('call');self.wait()
        self.assertEqual(self.drawn_starts(),list(range(1,7)))
        self.assertEqual(self.metrics.state.sim_rollouts,6)

    def test_default_budget_finishes_a_log_larger_than_eight_mib(self):
        self.start()
        points = 200000
        with self.file.open('wb') as stream:
            stream.write(MAGIC)
            for index in range(1,13):
                stream.write(HEADER.pack(float(index),0,0,points,index,index*8,0,index*8,0,*([0]*16)))
                stream.write(bytes(4*(points-1)))
        self.assertGreater(self.file.stat().st_size,8*1024*1024)
        self.playback.note_tool_result('call');self.wait()
        self.assertEqual(self.drawn_starts(),list(range(1,13)))
        self.assertGreater(self.playback._call.call_count,1)
        self.assertEqual(self.metrics.state.sim_rollouts,12)
        self.gate.charge.assert_called_once()

    def test_finish_handles_stream_copy_fallback_and_invalid_sources(self):
        self.start()
        self.file.write_bytes(MAGIC+b''.join(record(i) for i in range(1,7)))
        (self.playback.dir/'bad.bin').write_bytes(b'not a rollout log')
        with small_preview_batches(), patch('orchestrator.playback.os.replace',side_effect=OSError('locked')):
            self.playback.note_tool_result('call');self.wait()
        self.assertEqual(self.drawn_starts(),list(range(1,7)))
        self.assertEqual(self.metrics.state.sim_rollouts,6)

    def test_finish_preserves_offsets_for_moved_and_copied_sources(self):
        import os
        self.start()
        first = self.playback.dir/'a.bin'
        first.write_bytes(MAGIC+b''.join(record(i) for i in range(1,4)))
        self.file.write_bytes(MAGIC+b''.join(record(i) for i in range(4,7)))
        replace = os.replace
        def locked_first(source,dest):
            if source == first:
                raise OSError('locked')
            return replace(source,dest)
        with small_preview_batches(), patch('orchestrator.playback.os.replace',side_effect=locked_first):
            self.poll();self.wait()
            self.playback.note_tool_result('call');self.wait()
        self.assertEqual(self.drawn_starts(),list(range(1,7)))
        self.assertEqual(self.metrics.state.sim_rollouts,6)
        self.assertEqual(list(self.playback.pending.glob('*.bin')),[])

    def test_room_filtered_batch_does_not_skip_later_eligible_routes(self):
        self.start()
        with self.file.open('wb') as stream:
            stream.write(MAGIC)
            for index in range(1,61):
                if index in (3,4):
                    stream.write(HEADER.pack(float(index),0,0,2,index,20000*8,0,20001*8,0,*([0]*16))
                                 + b'\x08\0\0\0')
                else:
                    stream.write(record(index))
        with small_preview_batches():
            self.playback.note_tool_result('call');self.wait()
        self.assertEqual(self.drawn_starts(),[i for i in range(1,61) if i not in (3,4)])
        self.assertEqual(self.metrics.state.sim_rollouts,58)
        self.gate.charge.assert_called_once()

    def test_finish_ignores_incomplete_record_after_all_complete_routes(self):
        self.start()
        self.file.write_bytes(MAGIC+b''.join(record(i) for i in range(1,6))+record(6)[:10])
        with small_preview_batches():
            self.playback.note_tool_result('call');self.wait()
        self.assertEqual(self.drawn_starts(),list(range(1,6)))
        self.assertEqual(self.metrics.state.sim_rollouts,5)

    def test_failed_earlier_preview_is_not_hidden_by_a_later_success(self):
        self.start();self.file.write_bytes(MAGIC+record(1))
        self.playback._call.return_value = None
        self.poll();self.wait()
        self.playback._call.return_value = {}
        with self.file.open('ab') as stream: stream.write(record(2))
        self.poll();self.wait()
        self.playback.note_tool_result('call');self.wait()
        self.assertEqual(self.drawn_starts(),[1,2,1,2])
        self.assertEqual(self.metrics.state.sim_rollouts,2)

    def test_drain_fallback_also_completes_unread_tail(self):
        self.start();self.file.write_bytes(MAGIC+b''.join(record(i) for i in range(1,7)))
        with small_preview_batches():
            self.poll();self.wait()
            with self.playback._submit_lock:
                self.playback._msgs['message'].pending.clear()  # tool ended, result notification not yet collected
            self.playback.drain(timeout=3)
        self.assertEqual(self.drawn_starts(),list(range(1,7)))
        self.assertEqual(self.metrics.state.sim_rollouts,6)

    def test_close_timeout_preserves_worker_sources_until_final_accounting(self):
        entered, release = threading.Event(),threading.Event()
        self.start();self.file.write_bytes(MAGIC+b''.join(record(i) for i in range(1,7)))
        worker = self.playback._worker
        def render(segment,**kwargs):
            if int(segment['tracks'][0]['p'][1]) == 5:
                entered.set();release.wait(3)
            return {}
        self.playback._call.side_effect = render
        try:
            with small_preview_batches():
                self.poll();self.wait()
                self.playback.note_tool_result('call')
                self.assertTrue(entered.wait(2))
                with patch.object(worker,'join',return_value=None):
                    self.playback.close()
                self.assertIs(self.playback._worker,worker)
                self.assertTrue(list(self.playback.pending.glob('*.bin')))
                release.set();worker.join(3)
            self.assertFalse(worker.is_alive())
            self.assertEqual(self.metrics.state.sim_rollouts,6)
            self.assertEqual(list(self.playback.pending.glob('*.bin')),[])
        finally:
            release.set();worker.join(3)

    def test_preview_budget_includes_active_and_queued_blocks_without_losing_routes(self):
        entered, release = threading.Event(), threading.Event()
        def render(*args, **kwargs):
            entered.set()
            release.wait(3)
            return {}
        self.playback._call.side_effect = render
        self.start()
        self.file.write_bytes(MAGIC + record(1))
        with patch('orchestrator.playback.PREVIEW_BYTES', 300), small_preview_batches():
            self.poll()
            self.assertTrue(entered.wait(2))
            try:
                for indexes in ((2, 3), (4,), (5, 6)):
                    with self.file.open('ab') as stream:
                        stream.write(b''.join(record(index) for index in indexes))
                    self.poll()
                    self.assertLessEqual(self.playback._preview_bytes, 300)
                self.assertEqual(self.playback._backlog, 2)
                self.playback.note_tool_result('call')
            finally:
                release.set()
            self.wait()
        self.assertEqual(self.drawn_starts(), list(range(1, 7)))
        self.assertEqual(self.playback._preview_bytes, 0)
        self.assertEqual(self.metrics.state.sim_rollouts, 6)

    def test_capacity_deferral_does_not_skip_a_record_within_the_record_limit(self):
        tail = RecordTail(MAGIC, HEADER)
        self.file.write_bytes(MAGIC + record(1) + record(2))
        self.assertEqual(tail.read([self.file], budget=100, max_record_bytes=300), [MAGIC + record(1)])
        self.assertEqual(tail.read([self.file], budget=100, max_record_bytes=300), [MAGIC + record(2)])

    def test_close_serializes_with_transfer_and_drains_the_accepted_job(self):
        entered, release = threading.Event(), threading.Event()
        transferred = self.playback._take
        def take(*args, **kwargs):
            entered.set()
            release.wait(3)
            return transferred(*args, **kwargs)
        self.start()
        self.file.write_bytes(MAGIC + record(1))
        producer = threading.Thread(target=self.playback.note_tool_result, args=('call',))
        closer = threading.Thread(target=self.playback.close)
        with patch.object(self.playback, '_take', side_effect=take):
            producer.start()
            self.assertTrue(entered.wait(2))
            closer.start()
            try:
                self.assertFalse(self.playback._worker_stopped.wait(.1))
            finally:
                release.set()
                producer.join(3)
                closer.join(3)
        self.assertFalse(producer.is_alive())
        self.assertFalse(closer.is_alive())
        self.assertTrue(self.playback._worker_stopped.is_set())
        self.assertEqual(self.metrics.state.sim_rollouts, 1)
        self.assertEqual(self.playback._backlog, 0)
        self.assertEqual(list(self.playback.pending.glob('*.bin')), [])

    def test_closed_playback_rejects_late_results_without_claiming_their_files(self):
        self.start()
        self.file.write_bytes(MAGIC + record(1))
        self.playback.close()
        self.playback.note_tool_result('call')
        self.playback.note_tool_use('late', 'late-call', 'Bash', {})
        self.playback._submit('late', True)
        self.assertTrue(self.file.exists())
        self.assertEqual(self.playback._backlog, 0)
        self.assertEqual(self.metrics.state.sim_calls, 0)
        self.assertEqual(self.playback._msgs, {})

    def test_failed_source_transfer_preserves_original_and_can_be_retried(self):
        self.start()
        self.file.write_bytes(MAGIC + record(1))
        with patch('orchestrator.playback.os.replace', side_effect=OSError('locked')), \
                patch('orchestrator.playback.shutil.copyfile', side_effect=OSError('unreadable')):
            self.playback.note_tool_result('call')
        self.assertEqual(self.file.read_bytes(), MAGIC + record(1))
        self.assertEqual(self.metrics.state.sim_calls, 0)
        self.playback.drain(timeout=3)
        self.assertEqual(self.metrics.state.sim_calls, 1)
        self.assertEqual(self.metrics.state.sim_rollouts, 1)
        self.gate.charge.assert_called_once()

    def test_worker_owns_cleanup_even_when_digest_raises(self):
        self.start()
        self.file.write_bytes(MAGIC + record(1))
        with patch.object(self.playback, '_digest', side_effect=ValueError('invalid log')):
            self.playback.note_tool_result('call')
            self.wait()
        self.assertEqual(list(self.playback.pending.glob('*.bin')), [])

    def test_partial_transfer_and_retry_share_the_same_search_bill(self):
        self.start()
        first = self.playback.dir / 'a.bin'
        first.write_bytes(MAGIC + record(1))
        self.file.write_bytes(MAGIC + record(2))
        import os
        replace = os.replace
        def locked_last(source, dest):
            if source == self.file:
                raise OSError('locked')
            return replace(source, dest)
        with patch('orchestrator.playback.os.replace', side_effect=locked_last), \
                patch('orchestrator.playback.shutil.copyfile', side_effect=OSError('unreadable')):
            self.playback.note_tool_result('call')
            self.wait()
        self.assertEqual(self.metrics.state.sim_calls, 1)
        self.assertEqual(self.metrics.state.sim_rollouts, 1)
        self.playback.drain(timeout=3)
        self.assertEqual(self.metrics.state.sim_calls, 1)
        self.assertEqual(self.metrics.state.sim_rollouts, 2)
        self.gate.charge.assert_called_once()
        # A failed transfer may replay a successful preview conservatively; it
        # must not lose a route or charge the search again.
        self.assertEqual(set(self.drawn_starts()), {1, 2})

    def test_native_prediction_retry_does_not_charge_a_bash_search(self):
        self.metrics.add(sim_calls=1, sim_rollouts=1)  # GameSession.simulate already accounted this result.
        self.file.write_bytes(MAGIC + record(1))
        with patch('orchestrator.sim_assist.write_replay', return_value=[self.file]), \
                patch('orchestrator.playback.os.replace', side_effect=OSError('locked')), \
                patch('orchestrator.playback.shutil.copyfile', side_effect=OSError('unreadable')):
            self.playback.collect_prediction([{}], 0)
            self.wait()
        self.playback.drain(timeout=3)
        self.assertEqual(self.metrics.state.sim_calls, 1)
        self.gate.charge.assert_not_called()
        self.assertEqual(self.metrics.state.sim_rollouts, 1)

    def test_mixed_native_and_bash_retry_keep_independent_rollout_ownership(self):
        self.metrics.add(sim_calls=1, sim_rollouts=1)
        self.file.write_bytes(MAGIC + record(1))
        with patch('orchestrator.sim_assist.write_replay', return_value=[self.file]), \
                patch('orchestrator.playback.os.replace', side_effect=OSError('locked')), \
                patch('orchestrator.playback.shutil.copyfile', side_effect=OSError('unreadable')):
            self.playback.collect_prediction([{}], 0)
            self.wait()
        self.start()
        (self.playback.dir / 'bash.bin').write_bytes(MAGIC + record(2))
        self.playback.note_tool_result('call')
        self.wait()
        self.assertEqual(self.metrics.state.sim_calls, 2)
        self.assertEqual(self.metrics.state.sim_rollouts, 2)
        self.gate.charge.assert_called_once()
        self.assertEqual(set(self.drawn_starts()), {1, 2})

    def test_file_index_reads_only_headers_and_seeks_over_large_bodies(self):
        points = 8 * 1024 * 1024
        header = HEADER.pack(1., 0, 0, points, 1, 0, 0, 8, 0, *([0] * 16))
        with self.file.open('wb') as stream:
            stream.write(MAGIC + header)
            stream.seek(4 * (points - 1) - 1, 1)
            stream.write(b'\0')
        original_open = Path.open
        reads = []
        class HeaderReader:
            def __init__(reader, stream): reader.stream = stream
            def __enter__(reader): return reader
            def __exit__(reader, *args): reader.stream.close()
            def seek(reader, *args): return reader.stream.seek(*args)
            def read(reader, size=-1):
                self.assertGreaterEqual(size, 0)
                self.assertLessEqual(size, HEADER.size)
                reads.append(size)
                return reader.stream.read(size)
        def header_open(path, *args, **kwargs):
            return HeaderReader(original_open(path, *args, **kwargs))
        with patch.object(Path, 'open', header_open), \
                patch.object(Path, 'read_bytes', side_effect=AssertionError('whole file read')):
            log = read_logs([self.file])
        self.assertEqual(len(log), 1)
        self.assertEqual(int(log.npts[0]), points)
        self.assertEqual(reads, [len(MAGIC), HEADER.size])

    def test_final_collection_checks_oversize_paths_before_decoding(self):
        self.playback.cfg.playback_stream_interval_s = 0
        self.playback.log = Mock()
        self.start()
        points = 8 * 1024 * 1024
        with self.file.open('wb') as stream:
            stream.write(MAGIC + HEADER.pack(1., 0, 0, points, 1, 0, 0, 8, 0, *([0] * 16)))
            stream.seek(4 * (points - 1) - 1, 1)
            stream.write(b'\0')
        with patch('orchestrator.playback.LogIndex.path', side_effect=AssertionError('oversize decode')):
            self.playback.note_tool_result('call')
            self.wait()
        self.assertEqual(self.metrics.state.sim_rollouts, 1)
        self.playback._call.assert_not_called()
        self.assertTrue(any('轨迹解码略过' in call.args[0] for call in self.playback.log.call_args_list))

    def test_final_selection_applies_batch_budget_before_decode_and_keeps_best(self):
        self.playback.cfg.playback_stream_interval_s = 0
        self.playback.log = Mock()
        self.start()
        # Every route fits individually, while all six together exceed 300 B.
        records = [record(i) for i in range(1, 6)]
        records.append(HEADER.pack(6., 0, 2, 2, 6, 0, 0, 100 * 8, 0, *([0] * 16)) + b'\x08\0\0\0')
        self.file.write_bytes(MAGIC + b''.join(records))
        with patch('orchestrator.playback.PREVIEW_BYTES', 300):
            self.playback.note_tool_result('call')
            self.wait()
        tracks = self.playback._call.call_args.args[0]['tracks']
        self.assertLessEqual(len(tracks) * (len(MAGIC) + HEADER.size + 4), 300)
        self.assertTrue(any(track['k'] == 3 for track in tracks))
        self.assertEqual(self.metrics.state.sim_rollouts, 6)
        self.assertTrue(any('整批解码上限' in call.args[0] for call in self.playback.log.call_args_list))
        self.assertEqual(self.playback.rounds[0].tracks, [])
