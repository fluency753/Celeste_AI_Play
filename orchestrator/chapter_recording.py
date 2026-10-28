"""Independent chapter captures, sharing only a durable playlist and agent run.

The bridge excludes menu frames and stops every step at the return to the map.
Every caller, including menu macros, crosses before_step before another input.
"""
from dataclasses import asdict, replace
from copy import deepcopy
import json
from pathlib import Path

from .recording import Recorder, RecordOptions, RecordingHalt
from .session_store import write_json


class ChapterRecording:
    def __init__(self, recorder):
        self.active = recorder
        existing = getattr(recorder, 'chapter_playlist', None)
        if existing:
            self.path = Path(existing).resolve()
            self.data = json.loads(self.path.read_text(encoding='utf-8'))
            self._validate()
            pending = self.data.get('pending')
            if pending and pending['id'] == recorder.session_id:
                if recorder.started:
                    self.data['current'] = recorder.session_id
                    self.data['pending'] = None
                    self._save()
            elif self.data['current'] != recorder.session_id:
                raise RecordingHalt('分章录制索引与当前录像不一致，未推进游戏')
            if recorder.started:
                self._current()['state']='armed'
                self._save()
        else:
            if recorder.started:
                raise RecordingHalt('不能将正在采集的连续录像隐式转换为分章录像')
            root = recorder.opts.dir.resolve()
            self.path = root / 'chapters.json'
            if self.path.exists():
                raise RecordingHalt('分章目录已存在，不能覆盖其索引')
            recorder.opts = replace(recorder.opts, dir=root / 'chapter-001', chapter_only=True)
            recorder.chapter_playlist = str(self.path)
            self.data = {'version': 1, 'current': recorder.session_id,
                         'entries': [self._entry(recorder)], 'pending': {'id':recorder.session_id}}
            self._save()

    @staticmethod
    def validate(path, data):
        if not isinstance(data,dict) or data.get('version') != 1 or not isinstance(data.get('entries'), list):
            raise RecordingHalt('分章录制索引无效')
        ids = []
        for entry in data['entries']:
            if (not isinstance(entry,dict) or not isinstance(entry.get('id'),str) or not entry['id']
                    or not isinstance(entry.get('dir'),str) or entry.get('state') not in {'armed','finishing','finished','failed','empty','abandoned'}):
                raise RecordingHalt('分章片段身份或状态无效')
            try:directory = Path(entry['dir']).resolve()
            except (OSError,ValueError) as exc:raise RecordingHalt('分章目录无效') from exc
            if directory.parent != path.parent or directory.name != f'chapter-{len(ids)+1:03d}':
                raise RecordingHalt('分章采集目录与索引不匹配')
            ids.append(entry['id'])
        if len(set(ids)) != len(ids) or data['current'] not in ids:
            raise RecordingHalt('分章录制身份重复或丢失')
        if data.get('pending') and (not isinstance(data['pending'],dict) or data['pending'].get('id') not in ids):
            raise RecordingHalt('分章启动记录缺少对应片段')

    def _validate(self):
        self.validate(self.path,self.data)

    @staticmethod
    def _entry(recorder):
        return {'id': recorder.session_id, 'dir': str(recorder.opts.dir.resolve()), 'state': 'armed'}

    def _save(self):
        write_json(self.path, self.data)

    def __getattr__(self, key):
        return getattr(self.active, key)

    @property
    def call(self):
        return self.active.call

    @call.setter
    def call(self, value):
        self.active.call = value

    def start(self, *args, **kwargs):
        self._start_pending()
        return self.active._session_data()

    def _current(self):
        return next(e for e in self.data['entries'] if e['id'] == self.active.session_id)

    def _describe(self, hello):
        entry = self._current()
        if hello.get('record_chapter_active') and not self.active.opts.chapter_context:
            self.active.opts.chapter_context = True
            self.active._save_session('recording')
        if hello.get('record_chapter_sid'):
            entry.update(sid=hello['record_chapter_sid'], side=hello.get('record_chapter_side'))
        return entry

    def before_step(self):
        try:
            if self.data.get('pending'):
                self._start_pending()
            hello = self.call('hello')
            if hello.get('chapter_recording') != 1:
                raise RecordingHalt('按章录制需要桥接 1.9.0 的逐帧边界支持；未发送输入')
            self._describe(hello)
            plugin = (hello.get('clock') or {}).get('plugin') or {}
            if hello.get('scene') == 'Overworld' and not hello.get('record_chapter_active'):
                from .recording_recovery import prefix_counts
                frames = prefix_counts(self.active._session_data())['frames'] + (plugin.get('frames') or 0)
                if frames:
                    self._rotate()
        except RecordingHalt:
            raise
        except Exception as exc:
            raise RecordingHalt(f'分章录制交接未完成，游戏未继续：{exc}；原索引保留，重试认领同一片段') from exc

    def _rotate(self):
        old = self._current()
        result = self.active.stop(defer_postprocessing=True)
        if not result or not result.get('encoder_finished'):
            raise RecordingHalt('上一章编码器尚未关闭，未启动下一章录像')
        old.update(state='finishing', closed=list(self.active._closed_capture_state), result=result,
                   options={**asdict(self.active.opts), 'dir': old['dir']}, runs=[str(p) for p in self.active.run_dirs])
        self._save()  # closed accounting survives a crash before the new intent
        next_recorder = Recorder(self.call, replace(self.active.opts,
            dir=self.path.parent / f"chapter-{len(self.data['entries'])+1:03d}", chapter_context=False), self.active.log)
        entry = self._entry(next_recorder)
        self.data['entries'].append(entry)
        self.data['pending'] = {'id': entry['id']}
        self._save()  # publish unique identity before start can reach OBS
        self._start_pending()

    def _start_pending(self):
        self._save()  # a previous intent write may have failed before any start
        ident = self.data['pending']['id']
        entry = next(e for e in self.data['entries'] if e['id'] == ident)
        if self.active.session_id != ident:
            old = self.active
            recorder = Recorder(old.call, replace(old.opts, dir=Path(entry['dir'])), old.log)
            recorder.session_id = ident
            recorder.run_dirs = list(old.run_dirs)
            recorder.chapter_playlist = str(self.path)
            recorder._recovery_start = {'capture_dir': entry['dir'], 'phase': 'prepared'}
            self.active = recorder
        recorder = self.active
        if not str(recorder.opts.dir).isascii():
            raise RecordingHalt('分章录制目录只能使用英文路径')
        recorder.opts.dir.mkdir(parents=True,exist_ok=True)
        manifest=recorder.opts.dir/'session.json'
        if manifest.exists() and json.loads(manifest.read_text(encoding='utf-8'))['id'] != ident:
            raise RecordingHalt('新章目录属于其他录像，未覆盖或启动')
        if not manifest.exists():
            recorder._recovery_start = {'capture_dir':entry['dir'],'phase':'prepared'}
            recorder._save_session('recovering')
        saved = json.loads(manifest.read_text(encoding='utf-8'))
        validate_manifest(saved,entry,self.path)
        for run in recorder.run_dirs:
            Recorder.link_saved(saved,run)
        if saved.get('state') == 'recording' or (saved.get('recovery_start') or {}).get('phase') == 'requested':
            # Lost acknowledgements use the existing recovery ownership protocol.
            self.active = Recorder.recover(recorder.call, saved, recorder.run_dirs[-1], recorder.log)
        else:
            recorder._start_capture(Path(entry['dir']), 20)
        previous=self.data['current']
        self.data['current'],self.data['pending']=ident,None
        try:self._save()
        except Exception:
            self.data['current'],self.data['pending']=previous,{'id':ident}
            raise

    def retain(self):
        self._describe(self.call('hello')); self._save()
        return self.active.retain()

    def stop(self, *args, **kwargs):
        if not self.data.get('pending') and not self.active.started and all(e['state'] in {'finished','empty'} for e in self.data['entries']):
            self._notify_empty(kwargs.get('on_capture_stopped'),kwargs.get('on_encoder_finished'))
            return self._summary()
        defer = kwargs.pop('defer_postprocessing', False)
        if not self.data.get('pending') and self._current()['state']=='empty':
            self._notify_empty(kwargs.get('on_capture_stopped'),kwargs.get('on_encoder_finished'))
            return self._finish_entries() if not defer else self._summary()
        if self._cancel_prepared():
            self._notify_empty(kwargs.get('on_capture_stopped'),kwargs.get('on_encoder_finished'))
            return self._finish_entries() if not defer else self._summary()
        if self.data.get('pending'):
            self._start_pending()  # settle an unknown start before stopping its actual owner
        if not self.active._closed_capture_state:
            self._describe(self.call('hello'))
        result = self.active.stop(*args, **kwargs, defer_postprocessing=True)
        if not result or not result.get('encoder_finished'):
            return result
        entry = self._current()
        entry.update(state='finishing', closed=list(self.active._closed_capture_state), result=result,
                     options={**asdict(self.active.opts), 'dir': entry['dir']}, runs=[str(p) for p in self.active.run_dirs])
        self._save()
        if defer:
            return result
        return self._finish_entries()

    def _notify_empty(self,on_capture,on_encoder):
        if (on_capture and not self.active._stop.notified) or (on_encoder and not self.active._stop.encoder_notified):
            Recorder.validate_release(self.active._session_data(),self.call('hello'))
        self.active._stop.capture_stopped=True
        self.active._stop.notify(on_capture)
        self.active._stop.notify_encoder(on_encoder)

    def _cancel_prepared(self):
        pending=self.data.get('pending')
        if not pending:
            return False
        entry=next(e for e in self.data['entries'] if e['id']==pending['id'])
        manifest=Path(entry['dir'])/'session.json'
        saved=json.loads(manifest.read_text(encoding='utf-8')) if manifest.exists() else None
        if saved and saved.get('id')!=entry['id']:
            raise RecordingHalt('待启动分章目录属于其他录像，不能取消')
        if saved and (saved.get('recovery_start') or {}).get('phase')!='prepared' and entry['state']!='empty' and not saved.get('chapter_cancelled'):
            return False
        previous,old_active=deepcopy(self.data),self.active
        recorder=Recorder(self.active.call,replace(self.active.opts,dir=Path(entry['dir']),chapter_context=False),self.active.log)
        recorder.session_id,recorder.run_dirs=entry['id'],list(self.active.run_dirs)
        recorder.chapter_playlist=str(self.path)
        recorder.chapter_cancelled=True
        recorder.opts.dir.mkdir(parents=True,exist_ok=True)
        recorder._save_session('finished')
        entry.update(state='empty',result={'state':'empty','frames':0,'dir':entry['dir']})
        self.active=recorder
        self.data['current'],self.data['pending']=entry['id'],None
        try:self._save()
        except Exception:
            self.data,self.active=previous,old_active
            raise
        return True

    def _finish_entries(self):
        results = []
        for item in self.data['entries']:
            if item.get('state') in {'finished', 'empty'}:
                results.append(item.get('result')); continue
            if not item.get('closed'):
                raise RecordingHalt('分章片段没有已关闭的编码器状态，未离线处理')
            if not item['result'].get('frames'):
                item.update(state='empty', result={'state': 'empty', 'frames': 0, 'dir': item['dir']})
                empty = self.active if item['id'] == self.active.session_id else None
                if empty:
                    empty._postprocess_pending = False; empty._save_session('finished')
            else:
                recorder = Recorder(None, RecordOptions(**{**item['options'], 'dir': Path(item['dir'])}), self.active.log)
                recorder.session_id, recorder.run_dirs = item['id'], [Path(p) for p in item['runs']]
                recorder.chapter_playlist = str(self.path)
                recorder._publish_run_links = False
                recorder._stop.requested = True
                recorder._closed_capture_state = tuple(item['closed'])
                output = recorder.stop()
                item.update(state=output['state'], result=output)
            results.append(item['result']); self._save()
        return self._summary()

    def _summary(self):
        results=[e['result'] for e in self.data['entries'] if e.get('state')!='empty' and e.get('result')]
        pending=any(e['state'] not in {'finished','failed','empty','abandoned'} for e in self.data['entries'])
        errors=[r.get('error') for r in results if r.get('state')=='failed']
        return {'state':'finishing' if pending else 'failed' if errors else 'finished','pending':pending,
                'error':'; '.join(str(e or '章节录像失败') for e in errors), 'frames':sum(r.get('frames',0) for r in results),
                'chapters':results,'playlist':str(self.path),'dir':str(self.path.parent)}


def wrap(cfg, recorder):
    return (ChapterRecording(recorder) if recorder is not None and getattr(cfg,'recording_mode','continuous') == 'chapter'
            and not isinstance(recorder, ChapterRecording) else recorder)


def menu_options(cfg):
    return {key: getattr(cfg, key, False) for key in ('freeze_chapter_select', 'freeze_file_select', 'freeze_main_menu')}


def validate_manifest(saved,entry,path):
    if (saved.get('id')!=entry['id'] or saved.get('chapter_playlist')!=str(path)
            or Path((saved.get('options') or {}).get('dir','')).resolve()!=Path(entry['dir']).resolve()
            or (saved.get('options') or {}).get('chapter_only') is not True):
        raise RecordingHalt('分章清单指向的录像身份不一致')


def resolve_saved(saved, hello, *, prepare_next=True):
    """Resolve playlist intent before the generic recorder can adopt an old ID."""
    if not saved or not saved.get('chapter_playlist'):
        return saved
    path=Path(saved['chapter_playlist']).resolve()
    data=json.loads(path.read_text(encoding='utf-8'));ChapterRecording.validate(path,data)
    ident=(data.get('pending') or {}).get('id') or data['current']
    entry=next(e for e in data['entries'] if e['id']==ident)
    plugin=(hello.get('clock') or {}).get('plugin') or {}
    closed = entry.get('state')=='finished' or entry.get('state')=='finishing' and bool(entry.get('closed'))
    if prepare_next and not data.get('pending') and closed and entry.get('result',{}).get('frames') and hello.get('scene')=='Overworld' and plugin.get('state') in {'finished','failed','idle'}:
        recorder=Recorder(None,replace(RecordOptions(**{**saved['options'],'dir':Path(saved['options']['dir'])}),
            dir=path.parent/f"chapter-{len(data['entries'])+1:03d}",chapter_context=False))
        entry=ChapterRecording._entry(recorder);ident=entry['id']
        data['entries'].append(entry);data['pending']={'id':ident};write_json(path,data)
    manifest=Path(entry['dir'])/'session.json'
    if manifest.exists():
        selected=json.loads(manifest.read_text(encoding='utf-8'))
        validate_manifest(selected,entry,path)
        if not prepare_next and any(e['state']=='finishing' for e in data['entries']):
            selected={**selected,'state':'finishing','postprocess_pending':True}
        return selected
    recorder=Recorder(None,RecordOptions(**{**saved['options'],'dir':Path(entry['dir']),'chapter_context':False}))
    recorder.session_id,recorder.run_dirs=ident,[Path(p) for p in saved.get('runs',[])]
    recorder.chapter_playlist=str(path)
    recorder._recovery_start={'capture_dir':entry['dir'],'phase':'prepared'}
    if not prepare_next:
        return recorder._session_data('recovering')
    recorder.opts.dir.mkdir(parents=True,exist_ok=True)
    recorder._save_session('recovering')
    return recorder._session_data('recovering')


def finish_saved(call,saved,run_dir,log=print,on_capture=None,on_encoder=None):
    hello=call('hello')
    selected=resolve_saved(saved,hello,prepare_next=False)
    Recorder.validate_release(selected,hello)
    recorder=Recorder(call,RecordOptions(**{**selected['options'],'dir':Path(selected['options']['dir'])}),log)
    recorder.session_id=selected['id']
    recorder.run_dirs=[Path(p) for p in selected.get('runs',[])];recorder.bind_run(Path(run_dir))
    recorder.chapter_playlist=selected['chapter_playlist']
    recorder._recovery_start=selected.get('recovery_start')
    path=Path(recorder.chapter_playlist)
    data=json.loads(path.read_text(encoding='utf-8'));ChapterRecording.validate(path,data)
    entry=next(e for e in data['entries'] if e['id']==recorder.session_id)
    if entry.get('closed'):
        recorder._closed_capture_state=tuple(entry['closed']);recorder._stop.requested=True
    elif not data.get('pending') and entry['state']!='empty':
        recorder.started=True
    series=ChapterRecording(recorder)
    return series.stop(on_capture_stopped=on_capture,on_encoder_finished=on_encoder)
