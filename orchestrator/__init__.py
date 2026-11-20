"""Celeste_AI_Play 主控：连接桥接 Mod、提供游戏工具（MCP）、驱动 Claude Code 智能体、记录日志与统计。"""
import os as _os

# numpy 自带的 OpenBLAS 在第一次导入时按 CPU 核数各开一个线程、各预留一块缓冲区：24 核的电脑上控制台服务因此多提交约
# 750 MB 内存。本项目只用 numpy 做逐元素运算（不做矩阵乘法），多线程用不上，限成 1 个线程。必须在 numpy 第一次导入
# 之前设置；本包与 overlay 包被导入时最先执行这里。用户自己设了这个环境变量时不改。
_os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
