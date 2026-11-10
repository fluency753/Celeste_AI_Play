# Python 回归测试

在仓库根目录用 `.venv` 的 Python 运行。正式入口使用标准库 `unittest`，支持项目要求的 Python 3.11 及以上版本，不需要额外安装并行测试框架。

```powershell
# 默认：完整收集，按模块分配到4个独立Python进程
.\.venv\Scripts\python.exe tools/run_tests.py

# 串行：一个worker按原发现顺序执行，保留单进程的fixture生命周期
.\.venv\Scripts\python.exe tools/run_tests.py --serial

# 只运行指定模块、类或方法；支持原有calibration.前缀
.\.venv\Scripts\python.exe tools/run_tests.py calibration.codex_agent_test calibration.net_wait_test
.\.venv\Scripts\python.exe tools/run_tests.py calibration.regression_runner_test.RegressionRunnerTests.test_worker_exit_without_report_is_failure --serial -v

# 减少进程及浏览器／视频并发
.\.venv\Scripts\python.exe tools/run_tests.py --workers 2 --heavy-workers 1
```

仓库约定、发布前验证及开发文档默认使用此入口。原命令 `python -m unittest discover -s calibration -p '*_test.py'` 仍可用于直接串行诊断。原生 C# 校准、可选的真实模型／游戏验收仍按各自文档显式运行，不包含在此命令中。

## 调度与覆盖

默认发现 `calibration/` 下的 `*_test.py`，保留包、模块的 `load_tests` 与原发现上下文。通过 `unittest` 加载模块，**不执行测试文件的 `__main__`**；例如手工实机脚本 `m6_recording_test.py` 不会因此启动游戏或 OBS。无用例、导入错误或 worker 重新收集不一致均报失败。

测试模块内的类、方法留在同一进程，避免全局 monkeypatch 互相干扰。按历史耗时均衡分组；包含 Playwright、ffmpeg／ffprobe、pywebview 夹具及图集构建标记的模块保守归入重型组，只能分给前 `--heavy-workers` 个 worker。其余模块可填入全部 worker，防止重型组结束后进程闲置。默认4进程、重型上限2；`--workers 1` 仍采用调度顺序，需要原发现顺序时用 `--serial`。

`configs/test_timings.json` 是入库的历史耗时提示，`local/test-results/module-times.json` 是本机成功完整发现运行后更新的缓存。两者只决定顺序，不能决定收集哪些测试或复用旧结果。新增／改名模块自动收集；缺失、无效、非有限或负耗时回退到默认估值，不要求先跑一次串行。指定类／方法的局部运行不覆盖整个模块的耗时。缓存写入失败只告警，不改变测试结果。

父进程与 worker 的完整收集 ID 按数量对账，重复用例也不会被集合去重。正常执行 ID 与类／模块 fixture 阻塞的 ID 共同覆盖收集结果；`setUpClass` 的跳过／错误会保留 fixture 原因及 `blocked_ids`，不把合法的整类跳过当作漏跑，也不伪称这些方法实际运行过。

自动化测试必须隔离本机配置、工作区、端口及进程归属，不连接在用主控或修改用户配置。新增测试如果依赖进程外共享资源，应先隔离；暂不能隔离时用串行诊断，不直接提高并发绕过冲突。现有可选测试环境变量保持原约定，入口不会自动开启 Docker／实机验收。

## 输出与失败

每次运行创建独立的 `local/test-results/<时间-随机号>/`：

| 文件 | 内容 |
| --- | --- |
| `plan.json` | 发现目录、模式、完整用例 ID、分组与预测耗时 |
| `worker-N.log` | 完整 unittest 输出、失败堆栈和集成测试日志；`-v` 增加逐用例名称 |
| `worker-N.json` | 重新收集／实际启动 ID、fixture 阻塞与原因、失败／错误／跳过、完整收尾与实测耗时 |
| `worker-N.progress.json` | 尽力更新的进度，不替代最终结果 |
| `summary.json` | 汇总状态、耗时、数量、覆盖核对、失败及 worker 退出码 |

控制台每15秒显示在跑的用例，完成时显示每组结果、失败详情与报告路径。`collected_tests` 是发现的方法数，`tests_run` 是 unittest 实际开始的测试数；类级 fixture 跳过可能使后者更少。`skipped` 沿用 unittest 的计数，包括类级或子测试跳过，不能一律当作被跳过的方法数。

入口只统一自身日志输出编码，不额外给 worker 启用强制 UTF-8 模式；被测程序仍保留平台原有的子进程文本解码规则，Windows 原生命令输出不会因此改按 UTF-8 读取。

worker 异常退出、无有效结果、额外／未说明的缺失执行、未完成收尾均不能返回成功。失败不自动重试，也不会为了通过而减少收集范围。

退出码：`0` 全部通过，`1` 测试／收集／协议失败，`2` 参数错误，`130` 中断。`--output <新目录>` 可指定报告位置，已有目录拒绝覆盖；`--start-directory`、`--pattern` 可用于隔离测试目录，`--timings-cache` 可指定独立缓存。

## 中断与资源归属

第一次 Ctrl+C 请求 worker 在安全用例边界停止，让当前用例及 `tearDown`、类／模块清理正常完成。默认允许60秒收尾，可用 `--cancel-grace-seconds` 调整；超过宽限或再次 Ctrl+C 会强制回收本次启动的 worker。

Windows 使用带 `KILL_ON_JOB_CLOSE` 的 Job Object，并在归属建立后才放行 worker；正常结束、启动失败、强制回收都关闭本次 Job，包含 worker 创建的 Chrome、ffmpeg 及 detached 子进程，不搜索或终止用户应用。POSIX 使用独立 session／进程组回收；主动另开 session 的夹具应自行清理，正常中断优先保留其清理机会。手工实机程序不属于此入口的强制清理范围。
