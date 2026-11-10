"""Python regression entry: whole test modules spread over worker processes.

Workers import modules exactly like ``unittest discover -s calibration``: the
start directory and the repository root are both importable and the working
directory stays at the repository root. ``--serial`` (and any explicit test
names) run in this process instead, in plain unittest order.
"""
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / 'local' / 'test-results'
CACHE = RESULTS / 'module-times.json'
DEFAULT_SECONDS = 2.0


def import_path(start):
    return os.pathsep.join([str(start), str(ROOT), *filter(None, [os.environ.get('PYTHONPATH')])])


def modules(start, pattern):
    return sorted(path.stem for path in Path(start).glob(pattern) if path.is_file())


def load_times(cache):
    try:
        data = json.loads(Path(cache).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: float(v) for k, v in data.items() if isinstance(v, (int, float)) and v >= 0}


def partition(names, times, workers):
    """Longest first onto the least loaded worker; history only affects order and balance."""
    known = sorted(times[n] for n in names if n in times)
    guess = known[len(known) // 2] if known else DEFAULT_SECONDS
    groups = [{'modules': [], 'seconds': 0.0} for _ in range(max(1, min(workers, len(names))))]
    for name in sorted(names, key=lambda n: -times.get(n, guess)):
        group = min(groups, key=lambda g: g['seconds'])
        group['modules'].append(name)
        group['seconds'] += times.get(name, guess)
    return groups


def module_of(suite):
    for test in suite:
        if isinstance(test, unittest.TestSuite):
            if (name := module_of(test)) is not None:
                return name
        elif isinstance(test, unittest.loader._FailedTest):
            return test._testMethodName
        else:
            return type(test).__module__
    return None


def worker(start, pattern, names, verbosity, report):
    """Run modules one after another in this process; record counts and per-module time.

    Discovery first imports every test module, as ``unittest discover`` does.
    Some modules are otherwise first imported while a test has patched the
    repository root, and would keep that temporary path for the whole process.
    """
    runner = unittest.TextTestRunner(stream=sys.stderr, verbosity=verbosity)
    totals, seconds = {'run': 0, 'failures': 0, 'errors': 0, 'skipped': 0}, {}
    discovered = unittest.TestLoader().discover(str(start), pattern=pattern, top_level_dir=str(start))
    suites = {module_of(suite): suite for suite in discovered}
    for name in names:
        began = time.monotonic()
        print(f'[worker] {name}', file=sys.stderr, flush=True)
        result = runner.run(suites.get(name) or unittest.TestSuite())
        seconds[name] = round(time.monotonic() - began, 3)
        totals['run'] += result.testsRun
        totals['failures'] += len(result.failures) + len(result.unexpectedSuccesses)
        totals['errors'] += len(result.errors)
        totals['skipped'] += len(result.skipped)
    Path(report).write_text(json.dumps({**totals, 'seconds': seconds}), encoding='utf-8')
    return 0 if not totals['failures'] and not totals['errors'] else 1


def stop(proc):
    if proc.poll() is not None:
        return
    if os.name == 'nt':  # The whole tree: browsers and ffmpeg started by fixtures go too.
        subprocess.run(['taskkill', '/T', '/F', '/PID', str(proc.pid)], capture_output=True)
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
    proc.wait()


def failure_report(log):
    text = Path(log).read_text(encoding='utf-8', errors='replace')
    marks = [i for i in (text.find('\n======'), text.find('\nFAIL:'), text.find('\nERROR:')) if i >= 0]
    return text[min(marks):].strip() if marks else text[-4000:]


def run_parallel(start, pattern, workers, verbosity, output, cache):
    names = modules(start, pattern)
    if not names:
        print(f'[tests] {start} 下没有匹配 {pattern} 的测试模块', flush=True)
        return 1
    groups = partition(names, load_times(cache), workers)
    output.mkdir(parents=True, exist_ok=True)
    for old in output.glob('worker-*'):   # Only the latest run is kept.
        old.unlink()
    env = {**os.environ, 'PYTHONPATH': import_path(start)}
    print(f'[tests] {len(names)} 个模块，{len(groups)} 个进程；日志 {output}', flush=True)
    began, procs = time.monotonic(), []
    status, totals, seconds = 0, {'run': 0, 'failures': 0, 'errors': 0, 'skipped': 0}, {}
    try:
        for index, group in enumerate(groups):
            log = (output / f'worker-{index}.log').open('w', encoding='utf-8')
            command = [sys.executable, '-X', 'utf8', '-m', 'tools.regression_runner',
                       '--worker', str(output / f'worker-{index}.json'), '--start-directory', str(start),
                       '--pattern', pattern, *(['-v'] if verbosity > 1 else []), *group['modules']]
            proc = subprocess.Popen(command, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
                                    start_new_session=os.name != 'nt')
            procs.append((index, proc, log))
        for index, proc, log in procs:
            code = proc.wait()
            log.close()
            try:
                data = json.loads((output / f'worker-{index}.json').read_text(encoding='utf-8'))
            except (OSError, ValueError):
                data = None
            if data is None or code:
                status = 1
            if data:
                for key in totals:
                    totals[key] += data[key]
                seconds.update(data['seconds'])
            ok = data is not None and not code
            print(f'[worker {index}] {"OK" if ok else f"失败（退出码 {code}）"} '
                  f'run={data["run"] if data else "?"}；日志 {log.name}', flush=True)
            if not ok:
                print(failure_report(log.name), flush=True)
    except KeyboardInterrupt:
        for _, proc, log in procs:
            stop(proc)
            log.close()
        print('[tests] 已中断', flush=True)
        return 130
    if not totals['run']:
        status = 1
    if seconds:
        try:
            Path(cache).parent.mkdir(parents=True, exist_ok=True)
            Path(cache).write_text(json.dumps({**load_times(cache), **seconds}, indent=1, sort_keys=True),
                                   encoding='utf-8')
        except OSError as error:
            print(f'[tests] 耗时缓存写入失败：{error}', flush=True)
    print(f'[tests] {"OK" if status == 0 else "FAILED"} run={totals["run"]} failures={totals["failures"]} '
          f'errors={totals["errors"]} skipped={totals["skipped"]} {time.monotonic() - began:.1f}s', flush=True)
    return status


def run_here(start, pattern, names, verbosity):
    sys.path[:0] = [str(start), str(ROOT)]
    loader = unittest.TestLoader()
    if names:
        prefix = Path(start).name + '.'
        suite = loader.loadTestsFromNames([n[len(prefix):] if n.startswith(prefix) else n for n in names])
    else:
        suite = loader.discover(str(start), pattern=pattern, top_level_dir=str(start))
    result = unittest.TextTestRunner(verbosity=verbosity).run(suite)
    return 0 if result.wasSuccessful() and result.testsRun else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description='运行 Python 回归测试（默认按模块分进程并行）')
    parser.add_argument('tests', nargs='*', help='只运行这些模块／类／方法（在当前进程里串行），可带 calibration. 前缀')
    parser.add_argument('--serial', action='store_true', help='在当前进程里按原发现顺序串行运行')
    parser.add_argument('--workers', type=int, default=4, help='并行进程数，默认 4')
    parser.add_argument('-v', '--verbose', action='store_true', help='逐个列出用例')
    parser.add_argument('--start-directory', default=str(ROOT / 'calibration'), help=argparse.SUPPRESS)
    parser.add_argument('--pattern', default='*_test.py', help=argparse.SUPPRESS)
    parser.add_argument('--output', default=str(RESULTS / 'latest'), help=argparse.SUPPRESS)
    parser.add_argument('--timings-cache', default=str(CACHE), help=argparse.SUPPRESS)
    parser.add_argument('--worker', metavar='REPORT', help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    verbosity = 2 if args.verbose else 1
    start = Path(args.start_directory).resolve()
    if args.worker:
        return worker(start, args.pattern, args.tests, verbosity, args.worker)
    if args.workers < 1:
        parser.error('--workers 至少为 1')
    if args.serial or args.tests:
        return run_here(start, args.pattern, args.tests, verbosity)
    return run_parallel(start, args.pattern, args.workers, verbosity, Path(args.output), args.timings_cache)


if __name__ == '__main__':
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='backslashreplace')
    raise SystemExit(main())
