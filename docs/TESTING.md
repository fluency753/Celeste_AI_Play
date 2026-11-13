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

## 发布前的实机冒烟

回归测试里的游戏、模型与 OBS 都是替身。转正式版之前，用 `tools/smoke_run.py` 把真实的游戏、桥接、容器、模型与录像接在一起跑一遍：

```powershell
.\.venv\Scripts\python.exe -X utf8 tools\smoke_run.py --save-slot <测试存档的槽位>
```

默认用 DeepSeek V4.1 Flash 在名为 `test` 的存档上从第一章开头玩到进入第 3 个房间为止（过了前两面；探索模式、分章录制、跳过准备阶段，现实预算 25 分钟），启动方式与控制台相同，用内置的“默认”配置。需要 Windows、Docker Desktop 已启动、控制台的游戏位置是 AI 专用副本、没有别的运行；游戏没开时它会请控制台服务启动副本。到达停止条件后主控按约定把游戏冻结在原地，命令核对完交还控制（`--keep-frozen` 时不交还）。要玩得更长，用 `--segments 0.0,1.0 --until-room ""` 之类的写法。结束后逐项列出“通过／失败／未覆盖”，有失败时退出码为 1。模型打不过去不算失败，核对的是主控这一侧。`--check <运行目录>` 只核对一次已经结束的运行；其余参数见 `--help`。

## 持续集成

`.github/workflows/tests.yml` 在每次推送和拉取请求时运行：

- Python 全量回归：Windows（Python 3.14）与 Linux（Python 3.13）各一次，命令与本机相同，失败不重跑；各进程的日志作为附件上传（通过时也传，保留 14 天：里面有真实系统步骤的用时，用来分清运行器慢还是用例卡住）。Linux 上另装 ffmpeg 与 Playwright 的 Chromium，页面测试和用真实 ffmpeg 的录制测试在那里运行；Windows 上没有装，这些用例自行跳过。
- 静态检查 `python tools/static_check.py`：只拦会在运行时出错的写法（未定义的名字、语法错误），未使用的导入等只报数量。本机运行需要先装 `pyflakes`。
- 端口检查 `dotnet run --project calibration/ipc_auth`：Windows 与 Linux 各一次，不需要游戏文件。

组件构建（桥接、常驻核心、OBS 插件、启动器）在 `release-components.yml`，两者互不依赖。

## 写出不看机器快慢的测试

持续集成的 Windows 运行器比开发用的电脑慢好几倍（磁盘尤其慢），只在那里失败的用例几乎都是测试自己的假设不成立。失败了先查原因，不靠重跑：

- **不依赖本机装了什么**：需要 ffmpeg、ziglang、Playwright 或真实桌面环境的地方用替身，或者按环境跳过并写明原因。
- **等待条件成立用上限，不用时间窗口**：`until(条件)` 的超时只是“卡住了”的判断，给足（十几秒）；不要假设某一步会在几十毫秒内完成。
- **“等待期间发生某事”与“等待自己到期”的先后由测试决定**：不要靠把等待设成 0.2 秒、再赌另一边更快。`codex_capacity_test` 的 `WaitClock` 是一个例子：模型等待读到的时钟与睡眠由测试控制，等待不占真实时间，只在测试放行后才到期。
- **两条语句之间也会过去时间**：要一个精确的时长，就直接设定它（例如暂停后把现实用时定在某个值），不要用“刚开始计时就暂停，所以大约是 0”。
- **别的进程写的文件，存在不等于写完**：读到空内容或半行要当作“还没写好”。
- **上限不能比被测代码自己的等待短**：`background_test` 等后台宿主启动，它的上限由被测的 `launch()` 自己的两段等待（`POWERSHELL_WAIT_S`、`HOST_READY_WAIT_S`）算出来，再加余量；被测代码改了等待，测试跟着变。
- **等真实的系统组件，上限按分钟给**：PowerShell 与 WMI、WebView2、新启动的 Python 进程这些，冷启动要多久由机器决定，运行器刚开机又同时跑着四个测试进程时可以用掉几十秒（2026-11-13 一次运行里，PowerShell 建进程超过 30 秒、WebView2 首次加载超过 15 秒，各让一项用例失败）。这类等待只用来判断“卡住了”：条件一成立就返回，所以上限给到一两分钟也不会让平时变慢；到期时把走到了哪一步、各步用时写进失败信息（`webview_host_fixture` 的 `timings` 与 `served`），才能分清是慢还是卡住。被测代码自己的等待如果也是这种性质（`launch()` 等 PowerShell），同样放宽，不在测试里绕开。
