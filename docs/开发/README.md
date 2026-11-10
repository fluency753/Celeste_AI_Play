# 开发记录

这里是开发过程中的记录，给参与开发的人（与 AI 开发助手）看，普通使用不需要读。发布到 GitHub Release 的源码压缩包不含这个目录（见根目录 `.gitattributes`），git 克隆照常包含。

| 文件 | 内容 |
| --- | --- |
| [发布流程.md](发布流程.md) | 版本号、版本摘要、标签与 GitHub Release 的发布步骤 |
| [TODO_DETAILS.md](TODO_DETAILS.md) | 开发计划（`TODO.md`）各项的背景、范围与验收要求 |
| [AGENT_DOCUMENTS.md](AGENT_DOCUMENTS.md) | 智能体文档目录、按工具组织的接口、动态工具名与旧路径兼容 |
| [INCIDENT_20261024.md](INCIDENT_20261024.md) | 故障说明：后台进程依附在聊天会话上，随会话关闭而退出（因此长时间运行要用独立后台宿主启动） |
| [NOTE_REMINDERS.md](NOTE_REMINDERS.md) | 主控对智能体笔记维护的提醒规则 |
| [设置分层.md](设置分层.md) | 运行设置拆成本机、模型档案、玩法配置、本次运行四层（另有独立的直播与录屏）：设计与分步进度 |
| [tas-input-alignment.md](tas-input-alignment.md) | TAS 输入格式的对齐约定 |
| [codex/](codex/CODEX.md) | Codex 开发助手专用的开发与审查入口、状态边界复盘与验收记录 |

[Python回归测试](../TESTING.md)：默认并行入口、串行诊断、结果与中断清理。

详细的更新记录在根目录的 `CHANGELOG.md`（同样不进发布压缩包）；给使用者看的版本摘要在根目录的 `版本摘要.md`。
