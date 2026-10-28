# 保持录像的桥接代码重载

桥接 1.3.0 将常驻核心 `CelesteAIRuntime` 与可重载逻辑 `CelesteAIBridge` 拆成两个 Everest Mod。主控通过现有连接在动作边界重载桥接逻辑；常驻核心继续持有冻结门闸、帧号、IPC／OBS 时钟、注入按键绑定、双轨 FMOD 混音器及音频共享内存。主控不发送录制 stop/start，也不另开录制目录。当前版本为桥接 1.7.1、常驻核心 1.0.4，核心 ABI 仍为 1。

**1.7.0 仅更新 Bridge／主控，Runtime 不变。** Bridge 统一提供 `reload_boundary` 场景描述：真正后台加载或保存尚未结束为 `wait`，`Level` 为房间替换，其余非空场景为原对象保留。主控先取得短暂维护冻结，再读取稳定快照并交给核心；无存档主菜单、选档、地图、`LevelEnter` 明信片及 `IntroVignette` 序章均可重载，不要求虚构的关卡身份或代按确认。取消／失败时释放尚未交给核心的临时冻结，准备结果未知则先查询核心，不能提前释放或重复准备。

1.7.0 的身份快照表示与旧 Bridge 导入器不兼容，按当前版本规则递增 minor；新版接受旧快照，可从旧版受控升级。旧版无法读取新版导出的完整场景身份，回退到 1.6.x 或更早版本须关闭游戏后按下文回滚，不能热降级。已持有新版快照的失败交接应先用兼容的 1.7.x 构建接回。

**1.6.0／1.0.4 需要一次维护重启升级。** 核心原先假定普通 Mod 重载一定更换场景对象，这会使保留原对象的结算页永远无法接回。现在由主控通过可选 `reload_commit.expect_scene_change` 声明完成条件：替换或保留。核心仅验证通用对象引用与交接生命周期，不解释关卡、结算或 Session；保留条件绑定 prepare 时的原对象，重试不能换基线或切换条件。ABI 仍为 1，旧调用默认替换模式；新 Bridge 依赖核心 1.0.4。不要覆盖在用核心。

1.5.1 补充只读 `level.screenWipe`，维护退出在已知入场／转场期间有界等待，不再把转场本身当作禁止保存退出的理由；仅需更新 Bridge，Runtime 不变。

1.5.0 在 Bridge 增加主菜单按钮、方向关系、保存状态及进程身份观测，支持主控经原生菜单退出游戏；维护退出命令见 [使用说明](USAGE.md#34-维护时正常退出游戏)。不新增退出函数调用或 Runtime 游戏策略。

1.4.0 仅在 Bridge 新增 PICO-8 双入口拦截，Runtime 保持 1.0.3；已安装相同核心时可按下述受控流程更新 Bridge。新版主控在进档／录制前核验 `pico8_entry_guard`，不能沿用缺少入口保护的旧 Bridge。

**1.3.3／1.0.3 需要一次维护升级。** 本次把场景策略与游戏身份核验移出核心，因此要替换已安装的核心文件并重启；以后调整这些规则只改 Bridge／主控。核心不判断剧情、死亡、玩家是否可操作，也不解释存档、地图、房间或章节计时。它只透明保存桥接快照，并维护资源、所有者、帧号、冻结及重载生命周期；维护加载器调度和用于本次加载完成／撤销判断的场景对象引用仍是驻留基础设施。

这是不兼容的部署结构变化。按仓库当前版本原则递增 minor 到 1.3.0，暂不递增 major。首次从旧版迁移必须安全结束主控、保存退出并收尾录像，关闭游戏后安装两个 Mod，再重新启动；仅复制新的桥接 DLL 到旧版安装不能获得不停录重载能力。常驻核心自身的代码、ABI 或已安装核心文件需要更新时，也必须维护重启。

**1.3.1／1.0.2 必须维护重启安装。** 1.3.0／1.0.1 的重载会让 Everest 按玩家自己的设置重建全部按键（见下文“输入绑定”），这个问题只能由新核心修复，桥接 1.3.1 在 `everest.yaml` 中要求核心 1.0.2。新主控只在 `hello` 报告 `input_reinit_guard: true` 且 `input_bindings_ok` 时才发起受控重载；旧核心上会直接拒绝，提示先维护重启。

## 安装与使用

首次安装／维护更新，在游戏关闭后运行：

```powershell
.\tools\deploy_bridge.ps1 -CelesteDir "<Celeste 安装目录>"
```

脚本备份并安装 `Mods\CelesteAIRuntime\` 与 `Mods\CelesteAIBridge\`，保留已有核心设置；首次迁移沿用原桥接的自定义端口。两个 Mod 都应启用。主控启动后，`hello.bridge_reload: 1`、`hello.input_reinit_guard: true` 和控制状态 `bridge_reload_control: true` 表示该实例提供受控入口。

已经安装常驻核心、主控正在试玩、只更新兼容桥接逻辑时：

```powershell
.\tools\deploy_bridge.ps1 -CelesteDir "<Celeste 安装目录>" -HotReload -HudPort 47792
```

脚本先构建，核对常驻核心构建产物与已安装文件哈希相同，才通过在用主控替换桥接逻辑。核心不同则拒绝，不在录制中替换核心。

常驻核心 1.0.1 起同时固定 informational version 和 SourceLink 元数据，避免无关 Git 提交改变核心 DLL 哈希；1.0.2 起再用 `PathMap` 把仓库根目录映射为 `/_/`，嵌入 DLL 的 PDB 路径与 PDB 标识不再随检出位置变化。核心代码、依赖或构建条件实际变化仍须维护重启，不能跳过哈希校验。核心构建会读取 `game-bridge\local.props` 里的游戏目录，与桥接一致。

也可单独提交已构建的桥接 DLL：

```powershell
.\.venv\Scripts\python.exe tools\reload_bridge.py --hud-port 47792 --dll "<构建目录>\CelesteAIBridge.dll" --expected-version 1.7.1
```

该工具只访问现有 HUD 的 `/api/control`，不会创建第二条桥接连接顶掉主控。DLL 路径、可选 PDB 与哈希由主控检查；`--dll` 与部署脚本一样要求常驻核心逐字节相同：主控在 DLL 同目录或仓库 `game-runtime\bin` 查找一同构建的 `CelesteAIRuntime.dll`（也可用 `--runtime-dll` 指定），与游戏实际加载的核心（`hello.runtime_path`，旧桥接没有时按 `Mods\CelesteAIRuntime\bin` 推断）比较哈希，不同或找不到就拒绝，不替换文件。必须先拿到常驻核心确认的 lease 和实际安装路径，才备份并原子替换 payload。备份放在运行目录的 `bridge_reload_backups/<lease>/`，交接阶段保存在 `bridge_reload.json`。

## 运行行为与边界

主控取得共享 action lock，等待当前 step、呈现确认和回放结束；从读取 `hello`／观测到常驻核心确认冻结之间持续占用游戏连接锁，回放线程不能插入一段回放。原录音计量（帧号、两路音频帧与回放帧）以核心冻结后在 prepare／status 回复中报告的数值为准；重试时若交接记录还是旧版的快照，也改用冻结中的数值，避免因准备前多画了一段回放而永远核验失败。重载空档由核心冻结关卡、向 OBS 回复 `N`，不推进桥接帧、录音帧或成片帧，并把冻结期间的游戏循环限制在约每秒 60 次（锁步录制会关闭固定帧率；冻结帧不进录像，限速只省 CPU／显卡）。新逻辑完成加载后，核验原存档、SID、side、房间、章节计时、桥接帧号、录音计量、注入按键绑定及原录制目录，再结束交接并恢复重载前的用户暂停状态。

实际卸载／加载仍走 **`Everest.Loader.ReloadMod` 的普通生命周期**。在 `Level` 中，其默认 `ReloadLevel` 会重建房间，玩家状态回到 Normal，手持物、跟随物等按 Everest 原行为处理；`transitioning=true` 的换房过渡沿用 Everest 的复活点调整，不额外拒绝或代跑过渡。在 `AreaComplete` 结算页，Everest 的 `ReloadLevel` 不做房间重建，受控重载保留同一个结算对象及界面进度，不代按确认。主控分别核验房间身份，或结算 Session 与 UI；两种路径都核验冻结帧与录像计量。

剧情、死亡、暂停、游戏冻结、结算标记或玩家暂不可操作，不作为一刀切拒绝条件；`bridge_reload.json.scene_mode` 与 `room_rebuild` 记录替换房间或保留结算的实际语义。若两次查询中的任一次仍在自动加载／退出场景，主控使用内部 `reload_ready` 截断等到目的场景，在其更新前停止，输入消耗必须为 0；加载帧单独计量，之后重新读取冻结状态及存档身份，再开始 prepare。不会凭过期 `hello.frozen` 拒绝刚完成的加载，也不确认明信片、结算或切换章节。加载等待使用原重载期限，超时会取消本次等待、不替换文件或停止录像；`--retry` 在新冻结边界重试，保留原更新请求与用户暂停状态。

非关卡场景沿用 Everest 原对象保留语义，由核心核验 prepare 时的同一场景引用，Bridge 核验完整场景类型、可空存档与章节身份，主控复核菜单选择及已有 UI／剧情输入节点。`LevelEnter`／`IntroVignette` 的入场协程直接留在原位置，不必先推进到关卡。所有场景保存中均先等原生保存协程完成；等待保存只冻结场景，不消费输入或增加录像计量，真正后台加载才推进并单独计量。旧桥接仍按原有 Level／AreaComplete 路径升级到新版，不要求旧快照凭空提供新增字段。任务 `done` 本身不禁止重载：原主控仍持有连接且桥接 Active 时可以维护；已结束的任务保持 done／暂停，不自动恢复模型或旧动作。原主控已关闭连接时不会为了重载另连接管游戏。

加载器内部的交互仍有边界：`OverworldLoader` 可能显示解锁 C 面／变体明信片并等待确认，此时自动加载不会自行结束。当前不会代按，会在原期限到达后结束等待，尚未 prepare 或替换 DLL；应显式完成提示后重试。这类加载器内部交互的原地保留重载尚未适配，不能据上述普通非关卡场景验收推断已经支持。

这些提示通常出现在通关后的返回流程，常规完成路径在进入 `AreaComplete` 前已经由 `LevelExit` 保存；它们不是已确认的章节进度丢失路径。源码入口也包括 `AreaQuit` 返回地图，旧版本设置迁移时标题画面会扫描原生槽位 0–2 的 24 心存档，可能补弹变体提示。因此不能将提示条件简化成“只会在结算之后出现”。

状态下限以原生保存退出后能恢复的进度为准。Everest 对关卡重建直接复用原 `Session`，保留房间、计时、剧情标记、收集物与钥匙等章节进度；`StartSession` 发现同一对象时不会重置其他 Mod 的会话。房间实体、玩家动作、手持物与跟随物按原生重载处理，不承诺任意第三方实体的内部状态完整保留。原生保存退出还会增加一次死亡，受控重载不额外施加这个副作用。

Bridge 的 schema-1 快照新增可选 `identity`，新快照严格校验游戏身份，校验失败继续冻结；旧快照没有此字段时保留兼容并记录日志，仍由主控在 `reload_finish` 前核对观测。ABI 保持 1，不自动修改旧会话、存档或录制。

两个 Mod 的 `SupportsCodeReload` 都关闭自动文件监视触发。即使启用了 Everest 的 `CodeReload_WIP`，也应使用这里的受控入口，避免绕开动作锁与交接。常驻核心不会被桥接逻辑的重载卸载；存在依赖桥接的其他 Mod 时，保录像重载会拒绝，改走维护重启。

重载后重建观测、输入和几何基线，清理陈旧工作区状态，后续游戏操作必须先重新观测；不会补按、重试或重新执行重载前的 act。旧轮次排队动作不能沿用旧实体编号直接执行。

### 输入绑定

接管期间核心把游戏按键换成桥接专用的一套（手柄 X 为抓、Y 为对话，菜单与 TAS 通道用小键盘和 F13–F21），并清空其他 Mod 的快捷键。Everest 每注册一个 Mod（包括重载后的新桥接）都会调用 `Input.Initialize`，按玩家保存的设置重建全部虚拟按键，并让其他 Mod 的快捷键重新生效。1.0.2 起核心挂在 `Input.Initialize` 之后：接管期间无论谁调用，都在主线程重新套用桥接绑定并再次清空 Mod 快捷键（工作线程上的调用留到下一个主线程节拍、维护场景结束后处理）；核心自己的调用不会重复触发。玩家保存的绑定、抓取／冲刺模式与手柄焦点设置不受影响，退出接管时照常还原。

`hello` 报告 `input_reinit_guard`（钩子已安装）、`input_bindings_installed`、`input_bindings_ok`、`input_binding_mismatches`（例如 `Grab`、`mod:<Mod>.<设置>`）和 `input_bindings_reapplied`（重新套用次数）。新桥接接回后若 `input_bindings_ok` 不为 true，主控保持冻结、不结束交接，可检查后用 `--retry` 重新核验。

## 撤销、读取冻结状态与必须重启的情形

**撤销**：交接尚未提交（`stage: prepared`）、原桥接逻辑仍然挂接、场景与帧号都未变化时，常驻核心报告 `abortable: true`，可以撤销：

```powershell
.\.venv\Scripts\python.exe tools\reload_bridge.py --hud-port 47792 --abort
```

主控先把本次已替换的桥接 DLL／PDB 从 `bridge_reload_backups/<lease>/` 还原（原来不存在的文件移到同一备份目录，不删除），再请求 `reload_abort`。原桥接从冻结前的同一帧继续，房间没有重建，观测仍有效；试玩恢复重载前的暂停状态。主控核对 DLL 或核心失败时，也会这样自动撤销尚未提交的交接。

原主控已经退出（崩溃或被关闭）时，HUD 控制不可用，可直接连接常驻核心撤销；主控仍连接时工具拒绝，避免顶掉连接：

```powershell
.\.venv\Scripts\python.exe tools\reload_bridge.py --abort --direct --bridge-port 47774 --run-dir "<原运行目录>"
```

`--run-dir` 指向原主控的运行目录（含 `bridge_reload.json`），用来还原已替换的文件；省略时只撤销冻结，并提示下次启动游戏可能载入已替换的文件。

**冻结期间的 hello**：交接未就绪时，常驻核心自己回答只读的 `hello`：`reload_hold: true`、`bridge: null`、帧号、录音计量、时钟状态，以及 `reload` 对象（`stage`、`lease`、`error`、`abortable`、`restart_required`、`runtime_version`、`runtime_path`）。它不包含任何桥接能力字段，不能推进游戏。新启动的主控和启动前检查看到 `reload_hold` 时拒绝接管，并说明能否撤销或必须重启，不会发送 `activate` 或输入。

**不能撤销**：已经提交（`reloading`、`ready`、`failed`）后，原桥接已卸载或房间已重建，不能回到原状态：原主控仍在时用 `--retry`（可加 `--dll` 指定兼容构建）接回；没有兼容构建或原主控已退出时只能重启游戏。桥接逻辑在受控流程之外被卸载（例如手动触发 Everest 重载）时，核心不知道原桥接的元数据，`restart_required: true`，只能重启游戏释放冻结。

## 失败与重试

加载失败、版本／状态／按键绑定核验失败、准备结果未确认或观测发布失败时，试玩保持暂停，原录制与交接保留。不要直接点“继续”绕过未接回的重载，也不要关闭 OBS 试图修复。

```powershell
.\.venv\Scripts\python.exe tools\reload_bridge.py --hud-port 47792 --retry
```

- prepare 确认丢失时，重试先查询现有 lease，不重新发 prepare；交接仍为 `prepared` 时也可直接 `--abort`。
- 原 commit 仍在 `reloading` 时，只等待原尝试；不会同时覆盖正在读取的 DLL 或重复 commit。
- 原 `expected_version` 在重试中继续生效，只有显式传新的 `--expected-version` 才修改要求。
- 已完成 resident finish、但工作区／观测刷新失败时，只补齐刷新，不再替换 DLL、重载房间或执行动作。
- 明确更换失败构建时，可在原尝试终止后加 `--dll`；备份与 lease 保留，不覆盖其他交接。
- 已撤销的交接不能重试；需要更新时重新发起重载。

控制任务取消会等待后台 worker 停止后才释放动作锁，避免控制收尾与未完成 commit 并发；不会通过取消来自动归还游戏控制。

## 回滚

回滚只在关闭游戏后进行。部署脚本每次维护部署都会把原来的两个 Mod 复制到 `local\backups\<时间>-bridge-runtime-deploy\`（路径在脚本结束时打印）。用该目录回滚：

```powershell
.\tools\deploy_bridge.ps1 -CelesteDir "<Celeste 安装目录>" -Rollback "<备份目录>"
```

脚本先把当前的 `Mods\CelesteAIBridge` 与 `Mods\CelesteAIRuntime` 移到 `<Celeste 安装目录>\CelesteAI-backups\<时间>-before-rollback\`（与 Mods 同一磁盘、不在 Mods 内，Everest 不会加载；只移动不删除），再还原备份里的 Mod。备份里没有常驻核心（例如回到 1.1.x）时，核心只移走不还原：否则旧桥接与核心会争用 47774／47776 端口、各装一套冻结门闸，谁先加载谁拿到连接。回到 1.3.0 之前的桥接时，主控也需要回到对应版本。核心设置文件 `Saves\modsettings-CelesteAIRuntime.celeste` 保留不动，核心不加载时无影响。

## 验证范围

**1.7.0（本次）**：Python 回归覆盖无存档菜单、入场／序章／未知非关卡、取得冻结后失败释放、取消恰在 prepare 前、准备结果未知且核心 idle、释放断线及反复重连失败、保留场景 UI 改变时保持冻结、旧版升级后新增字段兼容。原生 `calibration/chapter_reset` 直接检查真实 Bridge：保存等待在普通和锁步节拍下不消费占位输入、不增帧，保存完成后在首个场景边界截断；新旧快照身份核验及维护冻结交接通过。Runtime 构建哈希保持不变。

私有实际游戏通过无存档主菜单、选档页、地图、`LevelEnter` 入场呈现、`IntroVignette` 序章与 `Level` 普通重载；最终构建复测中连接、时钟与双轨 PCM 句柄连续，共 504 帧，无接收错误。实际取得维护冻结后注入一次读取错误，确认 worker 释放临时冻结且未 prepare／commit。两个私有进程均原生菜单正常退出，原游戏进程与 slot 10 哈希不变。最终证据 `local/reload-remaining-final-private/acceptance.json`（首轮 `local/reload-remaining-private/acceptance.json`）；本轮接收器代替 OBS 编码器，入场测试不声称明信片已达到可确认阶段。独立真实 Codex 接收 agent 经生产通知、执行器和工具入口先 observe；故意先 act 时实际收到“旧计划未执行，请先观测”的原因，随后观测，未推进。证据 `local/reload-remaining-receiver-20261028-172709/summary.json`；游戏／核心传输重放私有实测观测，未连接在用游戏。独立审查已复核完整调用链和失败恢复，回归与实测分别保留证据。

**1.3.1／1.0.2（本次修复）**：Python 回归覆盖回放线程在快照与 prepare 之间插入一段回放（修复前必然核验失败、重试也无法完成）、旧交接记录的冻结计量重试、剧情／结算／暂停／死亡／不可操作时在 prepare 前拒绝、核心 `refused` 不进入冻结、旧核心缺少按键重新套用时拒绝发起、接回后按键绑定不一致时保持冻结、核心哈希不同时在 prepare 前拒绝或准备后自动撤销、撤销还原已替换文件、提交后的交接拒绝撤销并说明重试或重启、新主控拒绝接管冻结中的游戏，以及经真实 CLI 子进程的 `--abort --direct`。原生 `calibration/runtime_reload` 增加：缺少可操作玩家的关卡被拒绝、冻结期间只读 hello、只有未提交的交接可撤销、重试等待本次的房间重建。私有隔离游戏检查 `isolated_game_check.py` 在每次重载后核对 `input_bindings_ok`、`input_bindings_reapplied` 增加，并在结束交接后实际按一次抓与对话，确认没有被解释成冲刺或跳。10-28 已在用户电脑编译（桥接与核心 0 警告 0 错误），原生 `calibration/runtime_reload`、`calibration/chapter_reset` 与 `resident_build_check.ps1` 均 PASS；**1.3.1／1.0.2 的私有隔离游戏与真实 OBS 验收待实机重跑**，下面是 1.3.0／1.0.1 的记录。

789 项 Python 回归通过，覆盖生产 worker、文件备份替换、SQLite 控制消费者、动作锁、暂停／取消、确认丢失、版本约束和刷新失败补齐。实际 CLI 子进程经生产 HTTP 服务接收成功与失败结果，resident RPC 使用隔离替身，不连接在用主控。原生 `calibration/runtime_reload` 检查常驻核心的交接与冻结边界；双轨 FMOD 另有真实本机 bank 校准。私有隔离游戏连续两次普通重载期间，IPC 无重连，480 个 PCM 帧连续，同存档、SID、房间和章节计时，交接期间计量不增长。另以坏 DLL 触发旧模块已卸载、`payload_version=null` 的 orphan 状态，再用同一 lease 修复并恢复，验证 240 个连续 PCM 帧和 417 次冻结期间的 N 应答；证据见本机 `local/runtime-reload-game-v2/reload-acceptance-20261028-003117/report.json`。

1.0.1 已通过两个不同提交号与当前检出的三次强制构建哈希校验，并再次在错位端口的私有游戏中完成启动及普通重载、240 帧双路有声 PCM；新核心的证据为 `local/runtime-reload-game-v2/reload-acceptance-20261028-004525/report.json`，该轮接收器替代 OBS。

另用完全私有的 portable OBS、标准插件及真实 FFmpeg 编码器完成两次普通重载：同一编码进程、同一 `video.mkv`，CSV 连续 480 帧，ffprobe 视频帧数 480，成片与双路有声 WAV 均为 8 秒；解码并检查重载前后画面，确认均为游戏画面。原 OBS 配置和原存档校验值不变。为避免连接在用游戏，测试插件仅在本机副本中将时钟／音频共享内存端口从 47776 改为 48976；生产插件源码未改。证据在 `local/runtime-reload-game-v2/obs-reload-acceptance-20261028-003753/report.json`，步骤与边界见 [验收说明](../calibration/runtime_reload/README.md)。这证明当前测试组合的同编码进程连续录制，不代表任意 Mod 组合均兼容。这些 1.3.0 验收的重载后只发送中立帧，没有发现按键被 Everest 重建的问题，因此 1.3.1 的隔离检查增加了实际按键核对。

真实临时 Codex 接收 agent 还经生产工具链验证：旧 act 被拒绝且未推进，默认 observe 的随机图片数字能正确识别，重新决定的 act 执行成功，文字结果不包含 Base64 图像。此项仅替换外部游戏桥接；可用 `calibration/runtime_reload/agent_check.py` 显式重跑。

端口迁移另以故意错位的设置实测：旧桥接仍为 47774／47776，核心为 48974／48976；实际 OBS、PCM 与 HUD 均使用核心端口，跨重载连续 240 帧、4 秒成片及双路有声 WAV，原配置不变。证据为 `local/runtime-reload-game-v2/obs-reload-acceptance-20261028-004003/report.json`。运行端口只由核心设置维护，旧桥接设置仅作首次部署迁移来源。
