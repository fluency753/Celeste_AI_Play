"""unittest module sharding, bounded heavy workers, and auditable collection/results.

Only standard-library facilities are required, including on Python 3.11.
"""
import argparse
from collections import Counter
from datetime import datetime
import importlib
import json
import math
import os
from pathlib import Path
import re
import signal
import sys
import time
import traceback
import unittest
import uuid

from tools.regression_process import OwnedWorker

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_START = ROOT / 'calibration'
DEFAULT_CACHE = ROOT / 'local/test-results/module-times.json'
CLOCK, SLEEP, STAT = time.perf_counter, time.sleep, os.stat
OPEN, REPLACE, UNLINK = open, os.replace, os.unlink
HEAVY = re.compile(r'playwright|ffmpeg|ffprobe|webview_host_fixture|make_atlas', re.I)


def write_json(path, data):
    tmp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with OPEN(str(tmp), 'x', encoding='utf-8') as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
        REPLACE(str(tmp), str(path))
    finally:
        try:
            UNLINK(str(tmp))
        except FileNotFoundError:
            pass


def exists(path):
    try:
        STAT(str(path))
        return True
    except FileNotFoundError:
        return False


def test_ids(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from test_ids(item)
        else:
            yield item.id()


def heavy_suite(module, suite):
    names = {module}
    def visit(node):
        for item in node:
            if isinstance(item, unittest.TestSuite):
                visit(item)
            else:
                names.add(type(item).__module__)
    visit(suite)
    for name in names:
        path = getattr(sys.modules.get(name), '__file__', None)
        if path:
            try:
                if HEAVY.search(Path(path).read_text(encoding='utf-8')):
                    return True
            except (OSError, UnicodeError):
                return True  # Unknown resources use a heavy slot conservatively.
    return False


def load_entry(loader, entry):
    if entry['kind'] in ('module', 'discovery_path'):
        # Reproduce discover's context when a package load_tests calls discover
        # again. Without it, nested module IDs change or the package recurses.
        old_top, old_packages = loader._top_level_dir, set(loader._loading_packages)
        loader._top_level_dir = entry.get('top_level_dir', old_top)
        loader._loading_packages.update(entry.get('loading_packages', []))
        try:
            if entry['kind'] == 'discovery_path':
                suite, _ = loader._find_test_path(entry['path'], entry['pattern'])
                return suite or loader.suiteClass()
            return loader.loadTestsFromModule(importlib.import_module(entry['name']), pattern=entry['pattern'])
        finally:
            loader._top_level_dir, loader._loading_packages = old_top, old_packages
    return loader.loadTestsFromName(entry['name'])


def collect(start, pattern, selectors):
    sys.path.insert(0, str(start))
    entries = []
    class Loader(unittest.TestLoader):
        depth = 0

        def _find_test_path(self, full_path, pattern, *args, **kwargs):
            count, top, packages = len(entries), self._top_level_dir, sorted(self._loading_packages)
            suite, recurse = super()._find_test_path(full_path, pattern, *args, **kwargs)
            if suite is not None and not self.depth and len(entries) == count:
                # Import-level SkipTest is a real unittest result, even though
                # loadTestsFromModule was never reached. Replay the same discovery.
                name = self._get_name_from_path(full_path)
                entries.append({'kind':'discovery_path', 'name':name, 'module':name,
                                'path':str(full_path), 'pattern':pattern, 'ids':list(test_ids(suite)),
                                'top_level_dir':top, 'loading_packages':packages, 'heavy':True})
            return suite, recurse

        def loadTestsFromModule(self, module, *args, **kwargs):
            top, packages = self._top_level_dir, sorted(self._loading_packages)
            self.depth += 1
            try:
                suite = super().loadTestsFromModule(module, *args, **kwargs)
            finally:
                self.depth -= 1
            if not self.depth:
                entries.append({'kind':'module', 'name':module.__name__, 'module':module.__name__,
                                'pattern':pattern, 'ids':list(test_ids(suite)),
                                'top_level_dir':top, 'loading_packages':packages,
                                'heavy':heavy_suite(module.__name__, suite)})
            return suite

    loader = Loader()
    if selectors:
        loader = unittest.TestLoader()
        for name in selectors:
            if start == DEFAULT_START and name.startswith('calibration.'):
                name = name[len('calibration.'):]
            entry = {'kind':'name', 'name':name}
            suite = load_entry(loader, entry)
            entry['ids'] = list(test_ids(suite))
            entry['module'] = next((name.rsplit('.', i)[0] if i else name
                                    for i in range(name.count('.')+1)
                                    if (name.rsplit('.', i)[0] if i else name) in sys.modules), name)
            entry['heavy'] = heavy_suite(entry['module'], suite)
            entries.append(entry)
    else:
        discovered = list(test_ids(loader.discover(str(start), pattern=pattern)))
        if Counter(discovered) != Counter(ident for entry in entries for ident in entry['ids']):
            loader.errors.append('发现的用例未完整归入模块')
    return entries, loader.errors


def load_weights(cache, start):
    weights = {}
    paths = ([ROOT/'configs/test_timings.json'] if start == DEFAULT_START else []) + [cache]
    for path in paths:
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
            if path == cache and data.get('start_directory') != str(start):
                continue
            for name, value in data['modules'].items():
                if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0:
                    weights[name] = value
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
            print(f'[tests] 忽略无效耗时提示 {path}: {exc}', flush=True)
    return weights


def partition(entries, weights, workers, heavy_workers):
    groups = [{'entries':[], 'predicted_s':0.0} for _ in range(workers)]
    # A module's classes/methods stay together even when selected separately.
    bundles = {}
    for entry in entries:
        name = entry['module']
        bundle = bundles.setdefault(name, {'entries':[], 'heavy':False, 'seconds':weights.get(name, 1.0)})
        bundle['entries'].append(entry)
        bundle['heavy'] |= entry['heavy']
    ordered = sorted(bundles.items(), key=lambda item:(-item[1]['seconds'], item[0]))
    for heavy_phase in (True, False):
        for _, bundle in ordered:
            if bundle['heavy'] != heavy_phase:
                continue
            candidates = groups[:heavy_workers] if bundle['heavy'] else groups
            group = min(candidates, key=lambda row:row['predicted_s'])
            group['entries'].extend(bundle['entries'])
            group['predicted_s'] += bundle['seconds']
    return [group for group in groups if group['entries']]


class Result(unittest.TextTestResult):
    def __init__(self, *args, cancel, progress, **kwargs):
        super().__init__(*args, **kwargs)
        self.cancel, self.progress = cancel, progress
        self.started_ids, self.samples, self.fixture_blocks = [], [], []
        self.last_progress = 0.0
        self.current = ''
        self.signalled = False

    @property
    def shouldStop(self):
        return getattr(self, '_stopped', False) or self.signalled or exists(self.cancel)

    @shouldStop.setter
    def shouldStop(self, value):
        self._stopped = value

    def publish(self, force=False):
        now = CLOCK()
        if force or now-self.last_progress >= 2:
            self.last_progress = now
            try:
                write_json(self.progress, {'tests_run':self.testsRun, 'current':self.current})
            except OSError:
                pass  # Progress is best effort; the final report is mandatory.

    def startTest(self, test):
        super().startTest(test)
        self.current = test.id()
        self.started_ids.append(test.id())
        self.case_started = CLOCK()
        self.publish()

    def stopTest(self, test):
        self.samples.append({'id':test.id(), 'seconds':CLOCK()-self.case_started})
        super().stopTest(test)
        self.publish()

    def fixture(self, test, kind):
        match = re.fullmatch(r'(setUpClass|setUpModule) \((.+)\)', test.id())
        if match:
            self.fixture_blocks.append({'fixture':test.id(), 'prefix':match[2]+'.', 'kind':kind})

    def addSkip(self, test, reason):
        self.fixture(test, 'skip')
        super().addSkip(test, reason)

    def addError(self, test, err):
        self.fixture(test, 'error')
        super().addError(test, err)


def execute(entries, cancel, progress, report, verbosity=1):
    began = CLOCK()
    loader = unittest.TestLoader()
    suites = [load_entry(loader, entry) for entry in entries]
    collected = [ident for suite in suites for ident in test_ids(suite)]
    expected = [ident for entry in entries for ident in entry['ids']]
    if Counter(collected) != Counter(expected) or loader.errors:
        data = {'success':False, 'complete':False, 'collection_match':False,
                'collected_ids':collected, 'errors':[{'id':'collection', 'traceback':error}
                    for error in (loader.errors or ['worker 用例收集与主进程不一致'])]}
        write_json(report, data)
        return data
    runner = unittest.TextTestRunner(verbosity=verbosity, resultclass=lambda *a, **kw:Result(
        *a, cancel=cancel, progress=progress, **kw))
    # No exception is raised by the first signal: unittest must run its cleanups.
    result = runner._makeResult()
    runner._makeResult = lambda:result
    old_signals = {}
    for signum in (signal.SIGINT, signal.SIGTERM, *([signal.SIGBREAK] if hasattr(signal, 'SIGBREAK') else [])):
        old_signals[signum] = signal.signal(signum, lambda *_:setattr(result, 'signalled', True))
    try:
        runner.run(unittest.TestSuite(suites))
    finally:
        for signum, handler in old_signals.items():
            signal.signal(signum, handler)
    missing = Counter(expected)-Counter(result.started_ids)
    blocked = [ident for ident in missing.elements()
               if any(ident.startswith(row['prefix']) for row in result.fixture_blocks)]
    coverage = Counter(result.started_ids)+Counter(blocked) == Counter(expected)
    interrupted = result.shouldStop
    data = {'success':result.wasSuccessful() and coverage and not interrupted,
            'complete':coverage and not interrupted, 'collection_match':True,
            'collected_ids':collected, 'started_ids':result.started_ids, 'blocked_ids':blocked,
            'fixture_blocks':result.fixture_blocks, 'tests_run':result.testsRun,
            'skipped':len(result.skipped), 'skip_ids':[test.id() for test, _ in result.skipped],
            'skips':[{'id':test.id(), 'reason':reason} for test, reason in result.skipped],
            'failures':[{'id':test.id(), 'traceback':text} for test, text in result.failures],
            'errors':[{'id':test.id(), 'traceback':text} for test, text in result.errors],
            'unexpected_successes':[test.id() for test in result.unexpectedSuccesses],
            'interrupted':interrupted, 'wall_s':CLOCK()-began, 'cases':result.samples}
    if not coverage and not interrupted:
        data['errors'].append({'id':'coverage', 'traceback':'存在未执行且无fixture说明的用例，或重复／额外执行'})
    result.publish(force=True)
    write_json(report, data)
    return data


def worker_main(manifest):
    data = json.loads(manifest.read_text(encoding='utf-8'))
    ready, cancel = Path(data['ready']), Path(data['cancel'])
    until = CLOCK()+15
    while not exists(ready):
        if exists(cancel) or CLOCK() >= until:
            return 130
        SLEEP(.01)
    sys.path.insert(0, data['start_directory'])
    report = execute(data['entries'], cancel, Path(data['progress']), Path(data['report']), data['verbosity'])
    return 130 if report.get('interrupted') else 0 if report['success'] else 1


def run_parallel(groups, start, output, cancel, grace, verbosity):
    workers, reports = [], []
    cancelled_at = None
    last_progress = CLOCK()
    try:
        for i, group in enumerate(groups):
            manifest = output/f'worker-{i}.manifest.json'
            paths = {key:str(output/f'worker-{i}.{suffix}') for key, suffix in (
                ('ready','ready'), ('progress','progress.json'), ('report','json'))}
            write_json(manifest, {**paths, 'cancel':str(cancel), 'start_directory':str(start),
                                  'entries':group['entries'], 'verbosity':verbosity})
            log = (output/f'worker-{i}.log').open('w', encoding='utf-8')
            try:
                owner = OwnedWorker([sys.executable, str(ROOT/'tools/run_tests.py'), '--worker', str(manifest)],
                                    log, Path(paths['ready']))
            except BaseException:
                log.close()
                raise
            workers.append({'owner':owner, 'report':Path(paths['report']), 'done':False, 'index':i})
        while any(not row['done'] for row in workers):
            try:
                if exists(cancel) and cancelled_at is None:
                    cancelled_at = CLOCK()
                    print('[tests] 正在中断：等待当前用例及fixture清理；再次Ctrl+C强制回收本次worker。', flush=True)
                for row in workers:
                    proc = row['owner'].proc
                    if not row['done'] and proc.poll() is not None:
                        row['done'] = True
                        row['owner'].close()
                        try:
                            data = json.loads(row['report'].read_text(encoding='utf-8'))
                        except (OSError, ValueError):
                            data = {'success':False, 'complete':False,
                                    'errors':[{'id':f"worker-{row['index']}", 'traceback':'worker 未产生有效结果'}]}
                        data['exit_code'] = proc.returncode
                        data['index'] = row['index']
                        if data.get('success') and not (data.get('complete') and data.get('collection_match')):
                            data.setdefault('errors', []).append({'id':'protocol', 'traceback':'worker声称成功但未完成收尾／收集核对'})
                        data['success'] = (data.get('success') is True and data.get('complete') is True
                                           and data.get('collection_match') is True and proc.returncode == 0)
                        reports.append(data)
                        print(f"[worker {row['index']}] {'OK' if data['success'] else 'FAILED'} "
                              f"run={data.get('tests_run', 0)} skip={data.get('skipped', 0)} "
                              f"{data.get('wall_s', 0):.1f}s；日志 worker-{row['index']}.log", flush=True)
                if cancelled_at is not None and CLOCK()-cancelled_at >= grace:
                    for row in workers:
                        if not row['done']:
                            row['owner'].force_stop()
                if CLOCK()-last_progress >= 15:
                    last_progress = CLOCK()
                    progress = []
                    for row in workers:
                        if row['done']:
                            continue
                        try:
                            current = json.loads((output/f"worker-{row['index']}.progress.json").read_text(encoding='utf-8'))
                            progress.append(f"{row['index']}: {current['tests_run']} {current['current']}")
                        except (OSError, ValueError, KeyError):
                            progress.append(f"{row['index']}: 收集中")
                    print('[tests] '+ ' | '.join(progress), flush=True)
                SLEEP(.1)
            except KeyboardInterrupt:
                if cancelled_at is not None:
                    for row in workers:
                        if not row['done']:
                            row['owner'].force_stop()
                else:
                    cancel.write_text('cancel', encoding='ascii')
                    cancelled_at = CLOCK()
                    print('[tests] 收到中断，等待当前用例清理。', flush=True)
    finally:
        forced = []
        handler = signal.signal(signal.SIGINT, lambda *_:forced.append(True))
        try:
            if any(row['owner'].proc.poll() is None for row in workers):
                cancel.write_text('cancel', encoding='ascii')
                deadline = CLOCK()+grace
                while (not forced and any(row['owner'].proc.poll() is None for row in workers)
                       and CLOCK()<deadline):
                    SLEEP(.05)
            for row in workers:
                if row['owner'].proc.poll() is None:
                    row['owner'].force_stop()
                row['owner'].close()
        finally:
            signal.signal(signal.SIGINT, handler)
    return reports, cancelled_at is not None


def main(argv=None):
    parser = argparse.ArgumentParser(description='Python回归：默认按模块并行，--serial保留串行。')
    parser.add_argument('tests', nargs='*', help='可选模块／类／方法，例如 calibration.codex_agent_test')
    parser.add_argument('--serial', action='store_true', help='单worker按原收集顺序串行运行')
    parser.add_argument('--workers', type=int, default=4, help='独立进程数，默认4')
    parser.add_argument('--heavy-workers', type=int, default=2, help='浏览器／视频模块最多占用的进程数，默认2')
    parser.add_argument('--start-directory', type=Path, default=DEFAULT_START)
    parser.add_argument('--pattern', default='*_test.py')
    parser.add_argument('--output', type=Path, help='新建结果目录，默认local/test-results/<时间-随机号>')
    parser.add_argument('--timings-cache', type=Path, default=DEFAULT_CACHE)
    parser.add_argument('--cancel-grace-seconds', type=float, default=60, help='中断后允许当前用例收尾的秒数，默认60')
    parser.add_argument('-v', '--verbose', action='store_true', help='worker日志逐条显示用例名')
    parser.add_argument('--worker', type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.worker:
        return worker_main(args.worker)
    if args.workers < 1 or args.heavy_workers < 1 or not math.isfinite(args.cancel_grace_seconds) or args.cancel_grace_seconds < 0:
        parser.error('进程数须为正整数，中断宽限须为有限非负秒数')
    start = args.start_directory.resolve()
    if not start.is_dir():
        parser.error('测试目录不存在')
    output = (args.output or ROOT/'local/test-results'/
              (datetime.now().strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:8])).resolve()
    try:
        output.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        parser.error('结果目录已经存在，请使用新目录，避免覆盖旧证据')
    began = CLOCK()
    entries, errors = collect(start, args.pattern, args.tests)
    expected = [ident for entry in entries for ident in entry['ids']]
    if errors or not expected:
        write_json(output/'summary.json', {'success':False, 'phase':'collection', 'errors':errors or ['没有收集到测试']})
        print(f'[tests] 收集失败：{errors or "没有测试"}；{output}', file=sys.stderr)
        return 1
    cancel = output/'cancel'
    weights = load_weights(args.timings_cache.resolve(), start)
    groups = ([{'entries':entries, 'predicted_s':sum(weights.get(entry['name'], 1) for entry in entries)}]
              if args.serial else partition(entries, weights, args.workers, min(args.workers, args.heavy_workers)))
    write_json(output/'plan.json', {'start_directory':str(start), 'collected_ids':expected,
               'mode':'serial' if args.serial else 'parallel', 'groups':groups})
    print(f'[tests] 收集{len(expected)}项，{len(groups)}组，重型最多{min(len(groups), args.heavy_workers)}组；结果 {output}', flush=True)
    try:
        reports, interrupted = run_parallel(groups, start, output, cancel, args.cancel_grace_seconds,
                                           2 if args.verbose else 1)
    except KeyboardInterrupt:
        write_json(output/'summary.json', {'success':False, 'interrupted':True, 'phase':'worker_startup'})
        return 130
    except Exception as exc:
        write_json(output/'summary.json', {'success':False, 'phase':'worker_startup',
                   'errors':[{'type':type(exc).__name__, 'message':str(exc), 'traceback':traceback.format_exc()}]})
        print(f'[tests] worker启动／协议失败：{type(exc).__name__}: {exc}；{output}', file=sys.stderr)
        return 1
    actual = [ident for report in reports for ident in report.get('collected_ids', [])]
    success = (not interrupted and len(reports)==len(groups) and all(report['success'] for report in reports)
               and Counter(actual)==Counter(expected))
    summary = {'success':success, 'interrupted':interrupted, 'collected_tests':len(expected),
               'collection_match':Counter(actual)==Counter(expected), 'wall_s':CLOCK()-began,
               'tests_run':sum(report.get('tests_run', 0) for report in reports),
               'skipped':sum(report.get('skipped', 0) for report in reports),
               'skips':[row for report in reports for row in report.get('skips', [])],
               'failures':[row for report in reports for row in report.get('failures', [])],
               'errors':[row for report in reports for row in report.get('errors', [])],
               'unexpected_successes':[ident for report in reports for ident in report.get('unexpected_successes', [])],
               'blocked_ids':[ident for report in reports for ident in report.get('blocked_ids', [])],
               'workers':[{'index':report['index'], 'success':report['success'], 'complete':report.get('complete', False),
                           'wall_s':report.get('wall_s'), 'exit_code':report.get('exit_code')}
                          for report in reports]}
    write_json(output/'summary.json', summary)
    if success and not args.tests:
        measured = {row['id']:row['seconds'] for report in reports for row in report.get('cases', [])}
        for entry in entries:
            weights[entry['module']] = max(.01, sum(measured.get(ident, 0) for ident in entry['ids']))
        try:
            args.timings_cache.parent.mkdir(parents=True, exist_ok=True)
            write_json(args.timings_cache, {'start_directory':str(start), 'modules':weights})
        except OSError as exc:
            print(f'[tests] 耗时缓存写入失败（不影响测试结果）：{exc}', file=sys.stderr)
    print(f"[tests] {'OK' if success else 'INTERRUPTED' if interrupted else 'FAILED'} "
          f"run={summary['tests_run']} skip={summary['skipped']} {summary['wall_s']:.1f}s；{output/'summary.json'}", flush=True)
    if not success:
        for row in [*summary['failures'], *summary['errors']]:
            print(json.dumps(row, ensure_ascii=False), file=sys.stderr)
        for ident in summary['unexpected_successes']:
            print(f'UNEXPECTED SUCCESS: {ident}', file=sys.stderr)
    return 130 if interrupted else 0 if success else 1
