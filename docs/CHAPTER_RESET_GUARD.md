# 游戏操作策略与门禁（桥接 1.7.1）

主控默认禁止 `restart_chapter`（重启章节）、`return_to_map`（未通关返回地图）、`save_quit`（未通关保存退出），三项权限分别配置。正常死亡、暂停菜单 Retry、TAS QuickRestart（当前房间重试）、继续游戏和跳过剧情不受影响。真实章节完成，或主控已核验用户指定分段终点后的退出保留；交接许可不允许重启章节。

命令行提供 `--allow-restart-chapter`、`--allow-return-to-map`、`--allow-save-quit`，各自也支持 `--no-allow-...`；控制台开局与运行中、HUD 均显示三个开关。默认全关。旧 `--allow-chapter-reset`/`--no-allow-chapter-reset` 仍可一次设置全部三项；同次命令中独立选项覆盖旧总开关。计划外地图开关 `--allow-detour`（见 [USAGE.md](USAGE.md)“计划外地图（绕行收集）”）开着时必须允许返回地图（开启时一并打开，另两项不变；命令行与 `--no-allow-return-to-map` 或 `--no-allow-chapter-reset` 合用时拒绝启动，运行中控制台与 HUD 拒绝单独禁止返回地图，先关掉计划外地图才行）；它把入场目标从计划章节换成原存档槽位里的官方地图集合（`entry_target.maps`，`sid` 为空），菜单策略不变。

配置及 `run_state.json` 的权威字段是 `action_permissions: {restart_chapter: bool, return_to_map: bool, save_quit: bool}`。旧运行没有此字段时迁移旧总开关；有此字段后不受旧布尔摘要覆盖。运行中 `POST /api/control` 使用 `kind: "action_permissions"`，`text` 为 JSON 字符串，可部分更新，例如 `{"save_quit":true}`。未知键、非布尔值及空更新拒绝。旧 `chapter_reset` 指令作为全量别名保留。

策略等待当前游戏工具完成后生效，在 action lock 内保存完整权限并应用，锁外通过原消息队列通知会话。续跑优先继承 SQLite 中最新偏好，再读取运行快照；唤起聊天和接班会话同样保留规范权限。纯聊天不能修改试玩策略。旧运行实例未报告 `action_policy_supported: true` 时，界面禁用独立开关，API 拒绝更新，不向旧主控排入无法处理的指令。

Python 的 `action_policy.py` 统一验证、迁移、构造策略和生成提示。`GameSession._step_options` 为所有 step 调用强制设置带版本的 `action_policy`，覆盖 act、TAS、剧情工具、菜单宏和重试，调用者不能自行覆盖。地图/存档目标是强制正确性约束，不是可关闭的用户权限。`exit_level` 默认只在返回地图权限下导航，不结束任务，不以保存退出替代；`finish_run=true` 才按允许的返回地图／保存退出操作结束整次试玩。允许保存退出不等于允许返回地图。

桥接只有在原生菜单回调身份表建立成功、真实菜单确认与真实地图入场两个钩子均安装成功时才报告 `hello.action_policy: 1`（`entry_guard` 也按入场钩子是否装上报告）。原生构造方法／回调不匹配，或钩子装不上（游戏或 Everest 更新、别的 Mod 改写了 `TextMenu.orig_Update` 或 `OuiChapterPanel.Start`）时只记日志、对应门禁不可用，桥接其余功能照常加载；MonoMod 在钩子链变化时重跑注入函数，注入函数找不到调用点也只标记不可用、不抛异常。身份表与确认钩子分别记录就绪状态，后者成功不能覆盖前者失败。新主控要求此能力，旧桥接在录制/进档前拒绝；已有连接在 step 发送前拒绝。新策略存在时，桥接忽略调用者的旧参数，拒绝未知版本或非法权限，不能静默退回旧协议。桥接兼容旧客户端字段，但旧 `allow_map_exit` 也受修复后的关卡生命周期限制。

用户指定新开局或需要重新进档的续跑，在 agent 接管前使用独立的启动连接；该连接明确允许退出旧地图以进入用户指定存档，并传入本次重置配置。它不会给后续 GameSession 放宽返回地图权限。单独运行 `tools/enter_save.py` 也仅为明确指定的进档导航授权退出。

恢复入口使用 `load_save_and_freeze(client, save_name, save_slot=...)`，只按原生继续载入存档：有进行中的章节就停在首次进入该章节的场景，无进行中章节就保留原生地图，不代选、退出或重启。桥接 1.1.2 的 `hello.chapter_loaded_stop` 声明此能力；每条载入输入带 `stop_on: ["chapter_loaded"]`，加载帧也检查截断，进入 Level、IntroVignette 或 LevelEnter 后不再执行菜单输入的剩余帧。已在正确章节时零输入返回。无章节参数的冷启动续跑使用此入口；旧桥接明确拒绝，不能声称已经立即冻结。

明确需要退出章节打开地图时用 `open_chapter_select`；旧 `stop_at_chapter_select=True` 在发送输入前报错并指向两个命名入口，避免误把“停在地图”理解为“载入后冻结”。独立 CLI 默认采用载入后冻结，须指定存档槽位，例如 `.\.venv\Scripts\python.exe tools/enter_save.py test --save-slot 10`；其他目的需明确 `--destination play` 或 `--destination chapter-select`。load 模式返回后保持接管。

受信主控的启动/人工恢复使用独立连接上的 `controller_navigation` 作用域授权当前关卡退出，保留存档/地图目标核验，作用域结束或失败后恢复原策略。agent 工具即使调用同一菜单宏，也继续受 GameSession 的权限约束；不得按“调用了宏”就获得主控身份。所有菜单宏检查完整 step 结果，被门禁拒绝时立即抛出携带原观测、规则和帧计量的 `MenuActionBlocked`，不再等待、补按或重试退出；退出超时也不能返回假成功。

临时退出许可只绑定该 step 创建时的原 `Level` 和 `Session` 对象；从菜单或加载场景创建时不绑定未来关卡。每个实际推进帧都观察场景变化，包括加载帧。首次离开原 Level 或替换其 Session 后永久撤销，即使复用 Session 载入另一 Level，或重新回到原 Level，也不会恢复。许可保留到实际离开，因此返回地图的主菜单与二级确认均可完成。

已对照所安装游戏的 `TextMenu.orig_Update`：先上下切换，再处理左右，随后调用实际选中 Item 的 `ConfirmPressed` 和 `OnPressed`，最后处理取消。桥接在该确认调用点前注入门禁，读取实际 Item，不猜测复合按键的最终选择。原生虚拟按钮负责按下边沿、重复、模拟摇杆和 TAS 绑定；同帧方向加确认选到安全项照常执行，选到危险项时两个确认回调均不执行。

1.7.1 起，动作与安全项按按钮当前绑定的原生回调识别，不按显示文字猜测。桥接读取实际 `Level.orig_Pause`、`Level.GiveUp` 和 `IntroVignette.OpenMenu` 的按钮构造与回调关联，核验原生 `TextMenu.Button` 本类型、单一回调及其所属场景；`GiveUp` 的确认读取真实闭包 `restartArea` 区分重启和返回，取消单独识别。菜单重建、已有菜单和桥接重载后均可重新识别；改标签不改变实际动作，替换／追加回调、伪造文字或按钮子类不能沿用安全身份。

在 **Level／IntroVignette 且三项权限没有全部允许**时，未知菜单项一律拦下（`rule_id: menu.unknown_item`，`action: menu_item`，`actual` 是按钮文字）；继续、重试、跳过剧情、取消以及获准的原生动作正常执行。选项、辅助、变体和其他未知按钮也受此限制。通关状态或当前关卡离开许可只豁免已认证的返回地图／保存退出，不豁免未知动作或重启；防止危险回调变成 unknown 后反而放行。三项全允许时，以及地图与主菜单里，未知项按原游戏逻辑处理。只在主控的 step 执行期间生效，用户自己操作菜单不受影响。

地图入口在 `OuiChapterPanel.Start(checkpoint)` 方法入口核验；方向切换、按键边沿和重复均由原生面板处理。按原生 `StartRoutine` 规则纯解析检查点前缀的目的地图，再核验实际 SID/存档槽位；拒绝时不改变 Focused、Area，不播放启动音效或创建启动协程。因此方向优先时不会误拦，而方向到边界后实际触发的错误入场仍会被拒绝。

原生构造中的 `menu_pause_restartarea`、`menu_pause_return`、`menu_pause_savequit`、`intro_vignette_quit` 等键用于定位实际回调，不拿其译文匹配按钮；二级“是”即使重启／返回显示完全相同，也按实际确认行为使用独立权限。序章退出归入返回地图，原生取消仍允许。两类拦截统一输出 `result.block: {rule_id, action, reason, expected, actual}`，同时保留 `entry_blocked`/`chapter_reset_blocked` 停止原因和事件用于兼容。此前输入及本帧已经发生的导航仍计入 consumed，后续计划不执行，不自动关菜单或代选；主控在 act 结果里附中文说明（`action_policy.block_note`：拦的是哪项权限或哪个菜单项，菜单仍开着先按 B 返回或选「继续」，不要用退出重进代替找路；入场被拦时写明地图或存档与任务不符）。

验证：Python 回归使用临时运行目录、真实策略分发与会话控制消费者，不连接在用主控。原生检查先构建桥接，再运行：

```powershell
dotnet run --project calibration/chapter_reset "-p:CelestePrefix=<游戏目录>" -- "<游戏目录>"
```

检查在独立进程安装生产菜单 ILHook 与入场 Hook，执行真实 TextMenu/Panel 更新、虚拟按钮、StepJob 和推进场景观察路径；仅外部场景加载效果替换为计数器、音频输出静音，不读取或修改存档。覆盖跨加载/同 Session 替换 Level、二级确认、菜单起始不授予未来关卡权限、全部八种权限组合、原生/TAS 复合输入、方向边界、按住确认、检查点跨图及错误存档；Python 覆盖配置/控制/恢复和 UI 混合权限。模型自主解题另需独立试玩。

1.7.1 原生回归实际调用 `Level.GiveUp` 和 `IntroVignette.OpenMenu` 构造菜单，中文／英文确认均显示同样的“是”／“Yes”，验证八种权限组合、存量菜单重识别、同帧导航确认、回调替换／追加、旧场景回调、派生按钮以及通关／离开授权下的未知项。原测试把译文直接设成键名，漏掉了真实翻译碰撞；此次保留原生构造与确认分派，仅替换退出副作用和外部资源布局。日志 `local/menu-native-identity-test-final.log`，独立复跑 `local/menu-action-independent-native-final.log`。

最终构建私有实机通过返回地图拒／允、重启章节拒／允及原生选项未知项拦截五项，均以真实原生菜单进入确认。三个独立接收模型经生产 `GameSession`／工具分发／`CodexRunner`／`CodexRPC` 直接操作私有游戏，正确读到 `permission.return_to_map`、1/121 帧与后续未执行，允许返回后到达 `Overworld`，以及 `menu.unknown_item` 的原因；模型没有重试，测试控制端显式释放前次按键边沿。游戏与传输均未用替身。证据 `local/menu-action-final-private/acceptance.json`；只计最终 DLL `EA69D464…`，初轮旧构建及验收脚本的转场等待／标题 ID 假设失败不计入通过。两个私有进程正常退出，原游戏、slot 10 与 Runtime 哈希不变。本次菜单补丁未测试 OBS 录制。

Python 全部 929 项通过（`local/menu-action-python-full.log`），直接受影响 86 项通过（`local/menu-action-python.log`）；独立审查另复跑两个直接模块 20 项及原生检查。自动化、实机工具接收和独立审查分别保留证据。
