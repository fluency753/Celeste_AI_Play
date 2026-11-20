import os as _os

# 与 orchestrator/__init__.py 相同：numpy 的 OpenBLAS 限成 1 个线程（第一次导入 numpy 之前设置，用户自己设了时不改）
_os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
