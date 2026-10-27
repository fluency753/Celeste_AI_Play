# Repository Guidelines

## 项目结构

- `orchestrator/`：Python 主控、执行器适配、游戏工具、录制与提示词。
- `game-bridge/Source/`：C# Everest Mod，负责逐帧控制、观测、音频与游戏内 HUD。
- `overlay/`：网页 HUD、桌面控制台、消息历史与配置接口。
- `obs-plugin/`：C 编写的 OBS 锁步插件；`tools/`：部署、构建与诊断脚本。
- `calibration/`：Python 回归测试与原生 C# 检查；`kb/`：智能体知识库；`docs/`：文档与图片。
- `configs/`：共享默认配置。`local/`、`runs/`、`workspaces/`、`sim/` 为不入库的本机数据。

## 构建、测试与本地开发

在仓库根目录使用 PowerShell；Python 使用 `.venv` 环境，可直接调用 `.\.venv\Scripts\python.exe`。

- `powershell -ExecutionPolicy Bypass -File .\setup.ps1 -Check`：检查部署环境。
- `python -m overlay.server --port 47790`：启动本地 `/hud` 与 `/console`。
- `python -m unittest discover -s calibration -p '*_test.py'`：运行 Python 回归测试。
- `dotnet build game-bridge -c Release "-p:CelestePrefix=<游戏目录>"`：引用已安装的游戏程序集构建桥接。
- `dotnet run --project calibration/story_text "-p:CelestePrefix=<游戏目录>"`：运行原生剧情检查。
- `python tools/build_obs_plugin.py --no-install`：仅构建 OBS 插件，不安装。

## 命令执行规范

状态查询、构建、部署和启动分别执行，按依赖顺序检查结果。不要把不相关的操作塞进同一条复杂 PowerShell 命令，尤其不要混合 HTTP 状态查询与 `Start-Process` 等启动操作。确需组合的步骤应服务于同一项明确操作，失败后停止后续依赖步骤。

## 编码与命名规范

Python、C# 使用四空格缩进，具体写法遵循相邻代码。Python 函数与模块使用 `snake_case`，C# 类型与方法使用 `PascalCase`。保留文件编码及 `.gitattributes` 指定的换行，避免无关格式修改。仓库尚未统一配置格式化或静态检查工具。

## 测试与行为约束

Python 使用 `unittest`，文件为 `calibration/*_test.py`，方法以 `test_` 开头。修复行为缺陷须补针对性回归，保留内部调用链，仅模拟外部边界。自动化回归不得连接在用主控或修改用户配置。

按影响范围检查调用入口、剧情模式、暂停恢复、重试与跨章节行为，并同步受影响的工具、提示词、HUD、录制和文档。代码审查与试玩验证分开：确定性行为直接测试；提示词理解和自主工具选择用独立试玩 agent 验证。报告实际通过的场景和未覆盖项。

agent 剧情模式下，移动、菜单、剧情选择、结算和换章由 agent 显式操作。主控负责执行、观测、录制及目标核验，不在收尾或恢复时隐式代按、代选或传送；自动剧情模式按其约定处理。

## 提交与本机安全

本仓库由多人协作开发。每完成一项可独立交付的改动，在必要验证和适用审查通过后及时提交并推送，不将已完成的改动长期留在工作区或仅保留于本机，也不等待用户再次提醒。开始修改及推送前同步检查远端更新；存在并发提交时保留双方改动，解决冲突并完成受影响的验证后再推送，不用强制推送覆盖他人提交。仅提交本任务范围内的文件，交付时报告提交哈希、验证结果与推送状态。用户明确要求暂不提交或推送时遵从。

提交信息使用中文，说明行为变化和验证结果。禁止提交凭据、存档、录像及反编译模拟器资源。部署前备份被替换文件，保留用户的 OBS／CeleMod 设置。实机测试显式指定存档；桥接已有主控占用时，不另开连接。

## Codex 专用入口

仅 Codex 在本仓库执行开发、修复或代码审查时，继续读取 [Codex 开发与审查入口](docs/codex/CODEX.md)。其中的审查流程与复盘资料不作为其他 agent 的必读内容或执行要求，也不加入游戏 agent 的提示词或知识库。
