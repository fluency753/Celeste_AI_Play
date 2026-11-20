"""numpy 的 OpenBLAS 线程数：控制台服务与主控都限成 1 个线程（24 核的电脑上服务因此少提交约 750 MB 内存）。"""
import os
from pathlib import Path
import subprocess
import sys
import unittest

REPO = Path(__file__).resolve().parent.parent


def _env_in_child(module: str, preset: str | None) -> str:
    env = {k: v for k, v in os.environ.items() if k != 'OPENBLAS_NUM_THREADS'}
    if preset is not None:
        env['OPENBLAS_NUM_THREADS'] = preset
    code = (f'import sys, os, {module}\n'
            'print(os.environ.get("OPENBLAS_NUM_THREADS"), "numpy" in sys.modules)')
    out = subprocess.run([sys.executable, '-X', 'utf8', '-c', code], cwd=REPO, env=env, capture_output=True,
                         text=True, timeout=120)
    if out.returncode != 0:
        raise AssertionError(out.stderr[-2000:])
    return out.stdout.strip()


class BlasThreadsTests(unittest.TestCase):
    def test_service_and_controller_limit_openblas_before_numpy_loads(self):
        # 控制台服务（overlay.server）启动时还没导入 numpy，之后按需导入时 OpenBLAS 读到的已经是 1
        self.assertEqual(_env_in_child('overlay.server', None), '1 False')
        # 主控的规划回放一导入就带上 numpy：包的 __init__ 先执行，环境变量在 numpy 之前设好
        self.assertEqual(_env_in_child('orchestrator.playback', None), '1 True')

    def test_a_value_the_user_set_is_kept(self):
        self.assertEqual(_env_in_child('overlay.server', '4'), '4 False')


if __name__ == '__main__':
    unittest.main()
