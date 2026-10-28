# 常驻运行时重载验收

1.6.0／1.0.4 的新增边界：原生检查执行通用 preserve 模式，核验 AreaComplete 原对象保持、同 Session 的错误新对象拒绝，以及失败重试不能重定原引用或切换模式。`calibration/chapter_reset` 另执行普通／锁步真实 Tick，从 LevelLoader／LevelExit／OverworldLoader 到 Level／AreaComplete／Overworld 都以 `reload_ready`、0 输入消耗截断；旧 `chapter_loaded` 行为不变。Python 回归覆盖 hello/observe 加载完成竞争、真实 socket 超时取消、重试和 done 但仍持有连接的维护。

本机私有验收证据位于 `local/reload-boundaries-private/`：实际普通 Everest ReloadMod、同 IPC／clock／双 PCM 接收句柄，加载等待与真实结算分别核验。正加载帧验收使用私有测试模块暂缓 LevelLoader.Update 60 ticks，让真实 StartLevel 延后；不改 Loaded、Scene 或生产代码，另保留无需延迟的 0 帧竞争窗口验收。该接收器替代 OBS，不代表重新验收了真实 OBS 编码器。

构建元数据回归（仅构建，不安装或连接游戏）：

```powershell
.\calibration\runtime_reload\resident_build_check.ps1 -CelesteDir "<游戏目录>"
```

分别使用两个不同 SourceRevisionId 和当前检出版本构建核心，要求 DLL 哈希一致，防止 SourceLink 或版本提交号导致兼容热重载被误拒绝。

先构建 `game-runtime` 和 `game-bridge`。原生检查保留真实 TCP IPC、时钟令牌、两套 FMOD mixer、共享 PCM 与可回收程序集委托，替换的外部边界是 Everest `ReloadMod`，不声称覆盖实际游戏加载器。替身 Level 没有 Tracker 与玩家，Runtime 不再据此拒绝，也无需 Hook 绕过场景策略；透明快照回调调用真实 Bridge Export／Import，覆盖错存档、房间与计时拒绝以及旧 schema-1 快照兼容。另核对冻结期间只读 `hello`、只有未提交的交接可 `reload_abort`、提交失败后拒绝撤销，以及上次尝试已替换场景时重试仍等待本次加载完成：

```powershell
dotnet run --project calibration/runtime_reload "-p:CelestePrefix=<游戏目录>" -- "<游戏目录>"
```

真实游戏检查必须使用独立副本，不连接在用主控。准备器只创建新目录；复制根程序集、bootstrap 配置、私有 .NET runtime、显式存档和必要设置，只链接 Content/native 库资源。不得在副本里运行安装器或更新器。原始存档和设置校验值写入清单：

```powershell
.\.venv\Scripts\python.exe calibration/runtime_reload/prepare_isolated_game.py --game "<游戏目录>" --output local/runtime-reload-game --slot 10
```

启动副本时，工作目录必须为该副本目录。先用私有 `piton-runtime/dotnet.exe Celeste.dll` 重定向标准输出与错误到本地日志，避免缺 bootstrap 依赖弹窗；运行环境须有图形和音频设备。私有端口默认 48974/48976，拒用生产 47774/47776。准备器写入私有 `steam_appid.txt`，避免 Steam 启动器把副本重定向回原安装。

```powershell
.\.venv\Scripts\python.exe calibration/runtime_reload/isolated_game_check.py --isolation local/runtime-reload-game --cycles 2
```

增加 `--bad-dll --cycles 1` 可在 prepare 后备份并破坏私有 payload DLL，确认真实 Loader 卸载旧模块、失败保持冻结与时钟应答，再恢复文件并用同一 lease 重试。原 DLL 在 `finally` 中恢复，备份和失败状态保留在证据目录；不要对原安装运行此场景。

检查实际载入 slot 10，保留同一 IPC/时钟连接及两个 WAV 输出句柄，调用普通 `Everest.Loader.ReloadMod`，对比同存档、SID、房间、IGT、帧号、录音计数和音乐时间线，并验证共享 PCM 可读且有声。1.3.1 起每次重载前后都要求 `hello.input_bindings_ok`，重载后 `input_bindings_reapplied` 必须增加（Everest 注册新模块时重建了按键，核心已重新套用）；结束交接后实际按一次抓（G）与对话（T），确认没有按玩家默认绑定变成冲刺或跳，结果记入每轮的 `binding_probe`。结果和音频保存在副本中的 `reload-acceptance-*`。该接收器执行 OBS 插件线协议与 PCM 消费，但不启动 OBS 编码器，不覆盖真实 OBS 插件编码输出。关闭测试时只关闭已核对路径的私有进程，不退出用户原游戏。

## 真实 OBS 编码验收

已另行使用本机 `local/runtime-reload-obs` 完整独立副本验证，使用 `portable_mode.txt` 与 `--portable --multi`，私有 profile/scene/config，不复用或写入原 OBS 配置。此 OBS 33 安装的标准插件位于 `core/`，副本需包含该目录；不要加载原安装中的第三方插件。私有场景只捕获 `dotnet.exe` 的 Celeste 窗口，避免匹配原安装的 `Celeste.exe`。

生产插件仍固定使用 47776。此次只把 `obs-plugin/celeste-lockstep.c` 复制到 `local/runtime-reload-obs/private-plugin-build/`，在副本中将 `CLOCK_PORT` 和 `Local\\CelesteAIRecordAudio-47776` 两处端口改为 48976，使用现有 zig 和缓存 OBS 33 头文件编译，安装到私有 OBS；生产源码与插件均未改动。完整逐行差异为私有目录的 `private-port-only.diff`。

本机实际脚本为 `local/prepare_reload_obs.py` 与 `local/isolated_obs_reload_check.py`，测试证据 `local/runtime-reload-game-v2/obs-reload-acceptance-20261028-003753/`。同一真实 FFmpeg 编码器 PID 42248、同 CreationDate 和 `video.mkv` 输出跨过两次普通 ReloadMod。CSV 的 480 个 game_frame 连续，ffprobe 成片 480 个视频包、时长 8 秒，两份 48 kHz stereo WAV 均为 8 秒且有声。实际解码并目视检查重载前 1 秒、重载后 4 秒画面，确认均为 Celestial Resort 游戏画面。报告包含原 OBS 配置与原 Saves 校验值及保持不变的结果。测试结束仅关闭私有 OBS／游戏进程。

最终 payload 还额外测试端口迁移：私有 `CelesteAIBridge` 旧设置故意保留 47774/47776，`CelesteAIRuntime` 设置为 48974/48976，所有连接仍只使用私有端口。证据 `obs-reload-acceptance-20261028-004003/`：同编码器 PID 54192 跨一次重载，240 连续帧，视频和双 WAV 均 4 秒有声，48974 HUD 心跳延迟 264ms，原 OBS 配置与 Saves 校验值未变；前后解码画面也已目视确认。该用例验证录音 mmap 与 HUD 均以常驻 Runtime 设置为准。

真实 Codex 模型的接收与恢复验收另行显式运行（须已登录 CLI；会产生模型调用，不属于默认自动化测试）：

```powershell
.\.venv\Scripts\python.exe calibration/runtime_reload/agent_check.py --model <可用的 Codex 模型> --output local/reload-agent-check
```

输出目录必须为新的 `local/` 子目录。此检查通过生产 `CodexRunner`、MCP 工具及 `GameSession`，仅将外部游戏桥接替换为隔离 fixture；不连接游戏、OBS、Docker 或已有模型会话。独立临时模型先尝试旧动作，收到重载后必须 observe 的拒绝，再读取真实图像输入中的随机数字，最后重新操作；证据记录实际工具结果、最终回答、原生图像输入数、文字结果有无 Base64，以及外部 step 次数。为专门检查重载屏障，fixture 将单次动作下限设为一帧。此验收不能替代真实游戏重载或 OBS 编码验收。
