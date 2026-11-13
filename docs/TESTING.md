# Python 回归测试

在仓库根目录用 `.venv` 的 Python 运行，只用标准库 `unittest`，不需要额外安装测试框架。

```powershell
# 默认：按模块分到 4 个 Python 进程并行
.\.venv\Scripts\python.exe tools/run_tests.py

# 串行：当前进程里按原发现顺序运行，等同 python -m unittest discover -s calibration -p '*_test.py'
.\.venv\Scripts\python.exe tools/run_tests.py --serial

# 只运行指定模块、类或方法（当前进程里串行）；可带 calibration. 前缀
.\.venv\Scripts\python.exe tools/run_tests.py calibration.codex_agent_test calibration.net_wait_test -v

# 调整并行进程数
.\.venv\Scripts\python.exe tools/run_tests.py --workers 2
```

仓库约定、发布前验证及开发文档默认使用此入口。原生 C# 校准、可选的真实模型／游戏验收仍按各自文档显式运行，不包含在此命令中。不要把 `*_test.py` 逐个当脚本执行，其中含手工实机验收入口。

## 并行方式

主进程用 `unittest discover` 收集 `calibration/` 及其子包中的 `*_test.py`，整个模块分给同一个进程（模块内的类和方法不拆开，避免全局替换互相干扰）。分组按本机上次记录的各模块耗时均衡，没有记录的模块按中位数估计；耗时缓存在 `local/test-results/module-times.json`，只影响分组，不影响收集或结果。

每个进程先像 `unittest discover` 一样导入全部测试模块，再只运行分到的模块。这样模块的导入时机与串行运行相同：有的模块会在测试临时替换仓库路径期间才第一次被导入，提前导入可避免它在整个进程里沿用临时路径。

自动化测试必须隔离本机配置、工作区、端口及进程归属，不连接在用主控或修改用户配置。新增测试依赖进程外的共享资源时应先隔离；暂时不能隔离时用 `--serial` 诊断。用到智能体工作区或知识库视图的测试模块在开头加 `setUpModule, tearDownModule = generated_dirs_fixture()`（`calibration/isolation.py`），把 `workspaces/` 与 `local/kb_views/` 换到临时目录。读取本机设置的代码（控制台选中的配置、个人追加要求、AI 副本标记等）在测试里要换成固定值，不能让结果随本机设置变化。在临时目录里建符号链接或 Windows 目录联接的测试，在建临时目录之后紧接着注册 `self.addCleanup(unlink_links, 根目录)`（同一文件），让链接在目录删除前先移除：Windows 上的 Python 3.14 删除目录时，若同一次删除中链接的目标先被删掉，链接会留下、目录删不掉。Windows 上关文件要早于删临时目录：临时目录的清理在 `setUp` 里最先注册（清理按注册的倒序执行），不要放在 `tearDown` 里。

## 输出与失败

各进程的完整输出写在 `local/test-results/latest/worker-N.log`，每次运行覆盖上一次。某个进程失败时，控制台直接打印它的失败详情；最后一行汇总运行、失败、错误与跳过数和总耗时。

退出码：`0` 全部通过；`1` 有失败、错误、导入错误、进程异常退出或一个用例都没运行；`130` 被 Ctrl+C 中断，此时结束全部测试进程及其子进程（Windows 用 `taskkill /T`）。

## 持续集成

`.github/workflows/tests.yml` 在每次推送和拉取请求时运行：

- Python 全量回归：Windows（Python 3.14）与 Linux（Python 3.13）各一次，带 `--rerun-failed`：并行一轮后，失败的模块在一个新进程里单独重跑一次，通过则算通过，最后一行列出“首轮失败、单独重跑通过（不稳定）”的模块；单独重跑仍失败、或有测试进程异常退出，照旧算失败。本机运行默认不重跑。失败时各进程的日志作为附件上传。Linux 上另装 ffmpeg 与 Playwright 的 Chromium，页面测试和用真实 ffmpeg 的录制测试在那里运行；Windows 上没有装，这些用例自行跳过。
- 静态检查 `python tools/static_check.py`：只拦会在运行时出错的写法（未定义的名字、语法错误），未使用的导入等只报数量。本机运行需要先装 `pyflakes`。
- 端口检查 `dotnet run --project calibration/ipc_auth`：Windows 与 Linux 各一次，不需要游戏文件。

组件构建（桥接、常驻核心、OBS 插件、启动器）在 `release-components.yml`，两者互不依赖。

测试不能依赖这台机器装了什么：需要 ffmpeg、ziglang、git 历史或真实桌面环境的地方用替身，或者按环境跳过并写明原因。持续集成的机器比本机慢，Windows 运行器的计时粒度约 16 毫秒；用真实时间做判断的测试要留足余量。目前按环境跳过的只有 `background_test`（GitHub 的 Windows 运行器上后台宿主 20 秒内没有启动，原因未查）。`codex_capacity_test` 用几十毫秒的真实时间窗口判断重试与等待，在 Windows 运行器上每次有不同的用例失败，靠单独重跑通过；改成不依赖真实时间之前，它会经常出现在“不稳定”里。
