# 游戏操作策略与门禁（桥接 1.1.1）

主控默认禁止 `restart_chapter`（重启章节）、`return_to_map`（未通关返回地图）、`save_quit`（未通关保存退出），三项权限分别配置。正常死亡、暂停菜单 Retry、TAS QuickRestart（当前房间重试）、继续游戏和跳过剧情不受影响。真实章节完成，或主控已核验用户指定分段终点后的退出保留；交接许可不允许重启章节。

命令行提供 `--allow-restart-chapter`、`--allow-return-to-map`、`--allow-save-quit`，各自也支持 `--no-allow-...`；控制台开局与运行中、HUD 均显示三个开关。默认全关。旧 `--allow-chapter-reset`/`--no-allow-chapter-reset` 仍可一次设置全部三项；同次命令中独立选项覆盖旧总开关。

配置及 `run_state.json` 的权威字段是 `action_permissions: {restart_chapter: bool, return_to_map: bool, save_quit: bool}`。旧运行没有此字段时迁移旧总开关；有此字段后不受旧布尔摘要覆盖。运行中 `POST /api/control` 使用 `kind: "action_permissions"`，`text` 为 JSON 字符串，可部分更新，例如 `{"save_quit":true}`。未知键、非布尔值及空更新拒绝。旧 `chapter_reset` 指令作为全量别名保留。

策略等待当前游戏工具完成后生效，在 action lock 内保存完整权限并应用，锁外通过原消息队列通知会话。续跑优先继承 SQLite 中最新偏好，再读取运行快照；唤起聊天和接班会话同样保留规范权限。纯聊天不能修改试玩策略。旧运行实例未报告 `action_policy_supported: true` 时，界面禁用独立开关，API 拒绝更新，不向旧主控排入无法处理的指令。

Python 的 `action_policy.py` 统一验证、迁移、构造策略和生成提示。`GameSession._step_options` 为所有 step 调用强制设置带版本的 `action_policy`，覆盖 act、TAS、剧情工具、菜单宏和重试，调用者不能自行覆盖。地图/存档目标是强制正确性约束，不是可关闭的用户权限。`exit_level` 只选择当前允许的退出项：允许保存退出不等于允许返回地图。

桥接只有在真实菜单确认与真实地图入场两个钩子均安装成功时才报告 `hello.action_policy: 1`。新主控要求此能力，旧桥接在录制/进档前拒绝；已有连接在 step 发送前拒绝。新策略存在时，桥接忽略调用者的旧参数，拒绝未知版本或非法权限，不能静默退回旧协议。桥接兼容旧客户端字段，但旧 `allow_map_exit` 也受修复后的关卡生命周期限制。

用户指定新开局或需要重新进档的续跑，在 agent 接管前使用独立的启动连接；该连接明确允许退出旧地图以进入用户指定存档，并传入本次重置配置。它不会给后续 GameSession 放宽返回地图权限。单独运行 `tools/enter_save.py` 也仅为明确指定的进档导航授权退出。

临时退出许可只绑定该 step 创建时的原 `Level` 和 `Session` 对象；从菜单或加载场景创建时不绑定未来关卡。每个实际推进帧都观察场景变化，包括加载帧。首次离开原 Level 或替换其 Session 后永久撤销，即使复用 Session 载入另一 Level，或重新回到原 Level，也不会恢复。许可保留到实际离开，因此返回地图的主菜单与二级确认均可完成。

已对照所安装游戏的 `TextMenu.orig_Update`：先上下切换，再处理左右，随后调用实际选中 Item 的 `ConfirmPressed` 和 `OnPressed`，最后处理取消。桥接在该确认调用点前注入门禁，读取实际 Item，不猜测复合按键的最终选择。原生虚拟按钮负责按下边沿、重复、模拟摇杆和 TAS 绑定；同帧方向加确认选到安全项照常执行，选到危险项时两个确认回调均不执行。未知菜单项按原游戏逻辑处理。

地图入口在 `OuiChapterPanel.Start(checkpoint)` 方法入口核验；方向切换、按键边沿和重复均由原生面板处理。按原生 `StartRoutine` 规则纯解析检查点前缀的目的地图，再核验实际 SID/存档槽位；拒绝时不改变 Focused、Area，不播放启动音效或创建启动协程。因此方向优先时不会误拦，而方向到边界后实际触发的错误入场仍会被拒绝。

菜单识别 `menu_pause_restartarea`、`menu_pause_return`、`menu_pause_savequit`、`intro_vignette_quit` 以及二级 `menu_restart_continue`、`menu_return_continue` 的当前语言文本，映射为语义动作后读取策略；序章退出按原生 `ReturnToMap` 回调归入返回地图，取消仍允许。两类拦截统一输出 `result.block: {rule_id, action, reason, expected, actual}`，同时保留 `entry_blocked`/`chapter_reset_blocked` 停止原因和事件用于兼容。此前输入及本帧已经发生的导航仍计入 consumed，后续计划不执行，不自动关菜单或代选。

验证：Python 回归使用临时运行目录、真实策略分发与会话控制消费者，不连接在用主控。原生检查先构建桥接，再运行：

```powershell
dotnet run --project calibration/chapter_reset "-p:CelestePrefix=<游戏目录>" -- "<游戏目录>"
```

检查在独立进程安装生产菜单 ILHook 与入场 Hook，执行真实 TextMenu/Panel 更新、虚拟按钮、StepJob 和推进场景观察路径；仅外部场景加载效果替换为计数器、音频输出静音，不读取或修改存档。覆盖跨加载/同 Session 替换 Level、二级确认、菜单起始不授予未来关卡权限、全部八种权限组合、原生/TAS 复合输入、方向边界、按住确认、检查点跨图及错误存档；Python 覆盖配置/控制/恢复和 UI 混合权限。模型自主解题另需独立试玩。
