# 章节重置门禁

主控默认禁止菜单重启章节、未通关返回地图和保存退出，避免用重置代替脱离路线求解。正常死亡、暂停菜单 Retry、TAS QuickRestart（当前房间重试）、继续游戏和跳过剧情不受影响。真实章节完成，或主控已核验用户指定分段终点后的离开/换章保留；分段交接只允许退出，不允许重启章节。

命令行 `--allow-chapter-reset` 显式允许，`--no-allow-chapter-reset` 禁止；控制台开局设置“允许章节重置”默认关闭。运行中可在控制台或 HUD 的“章节重置”下拉框修改；`POST /api/control` 使用 `kind: "chapter_reset"`、`text: "allow" | "deny"`。策略等待当前游戏工具完成后生效，保存于该运行的 preferences，随续跑继承，并通过原消息队列通知原模型会话。纯聊天不能改试玩策略。

`GameSession._step_options` 是所有桥接 step 调用的权威策略来源，覆盖 act、TAS、剧情工具、菜单宏和重试，调用者不能自行覆盖。显式 `exit_level` 在发送菜单输入前检查同一配置。默认门禁要求 `hello.chapter_reset_guard: true`（桥接 1.0.28）；旧桥接在开局录制/进档前拒绝，已有连接则在 step 发送前拒绝。不能只发一条提示词后声称硬门禁已加载。

用户指定新开局或需要重新进档的续跑，在 agent 接管前使用独立的启动连接；该连接明确允许退出旧地图以进入用户指定存档，并传入本次重置配置。它不会给后续 GameSession 放宽返回地图权限。单独运行 `tools/enter_save.py` 也仅为明确指定的进档导航授权退出。

已对照所安装游戏的 `TextMenu.orig_Update`：先上下切换，再处理左右，随后调用实际选中 Item 的 `ConfirmPressed` 和 `OnPressed`，最后处理取消。桥接在该确认调用点前注入门禁，读取实际 Item，不猜测复合按键的最终选择。原生虚拟按钮负责按下边沿、重复、模拟摇杆和 TAS 绑定；同帧方向加确认选到安全项照常执行，选到危险项时两个确认回调均不执行。未知菜单项按原游戏逻辑处理。

识别主菜单 `menu_pause_restartarea`、`menu_pause_return`、`menu_pause_savequit`、序章 `intro_vignette_quit`，以及二级菜单 `menu_restart_continue`、`menu_return_continue` 的当前语言文本。二级取消仍允许。门禁阻断后本批以 `chapter_reset_blocked` 停止并记录原因；此前的真实输入及本帧已经发生的导航仍计入 consumed，后续计划不执行，主控不自动关菜单或代选。

验证：Python 回归使用临时运行目录、真实策略分发与会话控制消费者，不连接在用主控。原生检查先构建桥接，再运行：

```powershell
dotnet run --project calibration/chapter_reset "-p:CelestePrefix=<游戏目录>" -- "<游戏目录>"
```

检查在独立进程安装生产 ILHook，执行真实 TextMenu 更新、虚拟按钮和 StepJob，菜单效果替换为计数器，音频输出静音，不读取或修改存档。它覆盖主/二级危险项、原生/TAS 确认、正常选项、同帧导航后安全/危险确认、配置允许和完成交接。自主模型能否找到路线另需独立试玩；这些回归不证明模型解题能力。
