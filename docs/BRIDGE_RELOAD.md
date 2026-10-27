# 保持录像的桥接代码重载

桥接 1.3.0 将常驻核心 `CelesteAIRuntime` 与可重载逻辑 `CelesteAIBridge` 拆成两个 Everest Mod。主控通过现有连接在动作边界重载桥接逻辑；常驻核心继续持有冻结门闸、帧号、IPC／OBS 时钟、双轨 FMOD 混音器及音频共享内存。主控不发送录制 stop/start，也不另开录制目录。

这是不兼容的部署结构变化。按仓库当前版本原则递增 minor 到 1.3.0，暂不递增 major。首次从旧版迁移必须安全结束主控、保存退出并收尾录像，关闭游戏后安装两个 Mod，再重新启动；仅复制新的桥接 DLL 到旧版安装不能获得不停录重载能力。常驻核心自身的代码、ABI 或已安装核心文件需要更新时，也必须维护重启。

## 安装与使用

首次安装／维护更新，在游戏关闭后运行：

```powershell
.\tools\deploy_bridge.ps1 -CelesteDir "<Celeste 安装目录>"
```

脚本备份并安装 `Mods\CelesteAIRuntime\` 与 `Mods\CelesteAIBridge\`，保留已有核心设置；首次迁移沿用原桥接的自定义端口。两个 Mod 都应启用。主控启动后，`hello.bridge_reload: 1` 和控制状态 `bridge_reload_control: true` 表示该实例提供受控入口。

已经安装常驻核心、主控正在试玩、只更新兼容桥接逻辑时：

```powershell
.\tools\deploy_bridge.ps1 -CelesteDir "<Celeste 安装目录>" -HotReload -HudPort 47792
```

脚本先构建，核对常驻核心构建产物与已安装文件哈希相同，才通过在用主控替换桥接逻辑。核心不同则拒绝，不在录制中替换核心。

常驻核心 1.0.1 起同时固定 informational version 和 SourceLink 元数据，避免无关 Git 提交改变核心 DLL 哈希。核心代码、依赖或构建条件实际变化仍须维护重启，不能跳过哈希校验。1.0.0 升级到 1.0.1 也按核心更新处理，桥接逻辑仍为 1.3.0、核心 ABI 仍为 1。

1.0.1 已通过两个不同提交号与当前检出的三次强制构建哈希校验，并再次在错位端口的私有游戏中完成启动及普通重载、240 帧双路有声 PCM；新核心的证据为 `local/runtime-reload-game-v2/reload-acceptance-20261028-004525/report.json`，该轮接收器替代 OBS，完整 OBS 验证见下文。

也可单独提交已构建的桥接 DLL：

```powershell
.\.venv\Scripts\python.exe tools\reload_bridge.py --hud-port 47792 --dll "<构建目录>\CelesteAIBridge.dll" --expected-version 1.3.0
```

该工具只访问现有 HUD 的 `/api/control`，不会创建第二条桥接连接顶掉主控。DLL 路径、可选 PDB 与哈希由主控检查；必须先拿到常驻核心确认的 lease 和实际安装路径，才备份并原子替换 payload。备份放在运行目录的 `bridge_reload_backups/<lease>/`，交接阶段保存在 `bridge_reload.json`。

## 运行行为与边界

主控取得共享 action lock，等待当前 step、呈现确认和回放结束；只支持已加载且不在换房转场中的 Level。重载空档由核心冻结关卡、向 OBS 回复 `N`，不推进桥接帧、录音帧或成片帧。新逻辑完成加载后，核验原存档、SID、side、房间、章节计时、桥接帧号、录音计量及原录制目录，再结束交接并恢复重载前的用户暂停状态。

实际卸载／加载仍走 **`Everest.Loader.ReloadMod` 的普通生命周期**，包含其默认的 `ReloadLevel`：当前房间会重建，玩家状态可回到 Normal，手持物、跟随物等按 Everest 原行为处理。这不是保持原 Level 对象和所有玩家内部状态的“无副作用热补丁”；它不重启整个章节、不返回地图，Session 与章节计时由核心核验。

两个 Mod 的 `SupportsCodeReload` 都关闭自动文件监视触发。即使启用了 Everest 的 `CodeReload_WIP`，也应使用这里的受控入口，避免绕开动作锁与交接。常驻核心不会被桥接逻辑的重载卸载；存在依赖桥接的其他 Mod 时，保录像重载会拒绝，改走维护重启。

重载后重建观测、输入和几何基线，清理陈旧工作区状态，后续游戏操作必须先重新观测；不会补按、重试或重新执行重载前的 act。旧轮次排队动作不能沿用旧实体编号直接执行。

## 失败与重试

加载失败、版本／状态核验失败、准备结果未确认或观测发布失败时，试玩保持暂停，原录制与交接保留。不要直接点“继续”绕过未接回的重载，也不要关闭 OBS 试图修复。

```powershell
.\.venv\Scripts\python.exe tools\reload_bridge.py --hud-port 47792 --retry
```

- prepare 确认丢失时，重试先查询现有 lease，不重新发 prepare。
- 原 commit 仍在 `reloading` 时，只等待原尝试；不会同时覆盖正在读取的 DLL 或重复 commit。
- 原 `expected_version` 在重试中继续生效，只有显式传新的 `--expected-version` 才修改要求。
- 已完成 resident finish、但工作区／观测刷新失败时，只补齐刷新，不再替换 DLL、重载房间或执行动作。
- 明确更换失败构建时，可在原尝试终止后加 `--dll`；备份与 lease 保留，不覆盖其他交接。

控制任务取消会等待后台 worker 停止后才释放动作锁，避免控制收尾与未完成 commit 并发；不会通过取消来自动归还游戏控制。

## 验证范围

789 项 Python 回归通过，覆盖生产 worker、文件备份替换、SQLite 控制消费者、动作锁、暂停／取消、确认丢失、版本约束和刷新失败补齐。实际 CLI 子进程经生产 HTTP 服务接收成功与失败结果，resident RPC 使用隔离替身，不连接在用主控。原生 `calibration/runtime_reload` 检查常驻核心的交接与冻结边界；双轨 FMOD 另有真实本机 bank 校准。私有隔离游戏连续两次普通重载期间，IPC 无重连，480 个 PCM 帧连续，同存档、SID、房间和章节计时，交接期间计量不增长。另以坏 DLL 触发旧模块已卸载、`payload_version=null` 的 orphan 状态，再用同一 lease 修复并恢复，验证 240 个连续 PCM 帧和 417 次冻结期间的 N 应答；证据见本机 `local/runtime-reload-game-v2/reload-acceptance-20261028-003117/report.json`。

另用完全私有的 portable OBS、标准插件及真实 FFmpeg 编码器完成两次普通重载：同一编码进程、同一 `video.mkv`，CSV 连续 480 帧，ffprobe 视频帧数 480，成片与双路有声 WAV 均为 8 秒；解码并检查重载前后画面，确认均为游戏画面。原 OBS 配置和原存档校验值不变。为避免连接在用游戏，测试插件仅在本机副本中将时钟／音频共享内存端口从 47776 改为 48976；生产插件源码未改。证据在 `local/runtime-reload-game-v2/obs-reload-acceptance-20261028-003753/report.json`，步骤与边界见 [验收说明](../calibration/runtime_reload/README.md)。这证明当前测试组合的同编码进程连续录制，不代表任意 Mod 组合均兼容。

真实临时 Codex 接收 agent 还经生产工具链验证：旧 act 被拒绝且未推进，默认 observe 的随机图片数字能正确识别，重新决定的 act 执行成功，文字结果不包含 Base64 图像。此项仅替换外部游戏桥接；可用 `calibration/runtime_reload/agent_check.py` 显式重跑。

端口迁移另以故意错位的设置实测：旧桥接仍为 47774／47776，核心为 48974／48976；实际 OBS、PCM 与 HUD 均使用核心端口，跨重载连续 240 帧、4 秒成片及双路有声 WAV，原配置不变。证据为 `local/runtime-reload-game-v2/obs-reload-acceptance-20261028-004003/report.json`。运行端口只由核心设置维护，旧桥接设置仅作首次部署迁移来源。
