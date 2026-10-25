# Repository Guidelines

## 项目结构

- `orchestrator/`：Python 主控、执行器适配、游戏工具、录制与提示词。
- `game-bridge/Source/`：C# Everest Mod，负责逐帧控制、观测、音频与游戏内 HUD。
- `overlay/`：网页 HUD、桌面控制台、消息历史与配置接口。
- `obs-plugin/`：C 编写的 OBS 锁步插件；`tools/`：部署、构建与诊断脚本。
- `calibration/`：Python 回归测试与原生 C# 检查；`kb/`：智能体知识库；`docs/`：文档与图片。
- `configs/`：共享默认配置。`local/`、`runs/`、`workspaces/`、`sim/` 为不入库的本机数据。

## 构建、测试与本地开发

在仓库根目录使用 PowerShell；运行 Python 命令前激活 `.venv`。

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

## 测试与集成审查

Python 使用 `unittest`，测试文件命名为 `calibration/*_test.py`，方法以 `test_` 开头。修复行为缺陷时补充针对性回归；模拟外部进程，避免测试接触真实主控状态。

修改公共行为前必须检查：

- 找全 `act`、`observe`、`cutscene`、重试、恢复及跨章节入口，共享规则集中处理。
- 按影响范围覆盖自动／智能体剧情模式、工具可用性、暂停恢复、死亡重生、重试与多章节边界。
- 启动及运行中切换模式时校验能力；不能仅凭角色不可控就认定正在等待输入。
- 联动核对 HUD 与录制时序、工具接口、提示词、知识库、版本及文档。

确定性的底层行为可直接验证；提示词理解与自主工具选择才需要独立 agent 验证。汇报具体通过的场景和未覆盖项，不用测试总数代替结论。

## 提交与合并请求

提交标题使用简洁中文，直接说明动作；正文说明行为变化与验证结果，保留准确的共同作者署名。PR 描述问题、范围、测试、相关 issue；界面变化附截图。推送与合并遵循用户指定的策略。

## 本机数据与运行安全

禁止提交凭据、存档、录像和反编译模拟器资源。部署前备份被替换的已安装文件。保留用户的 OBS／CeleMod 设置，显式指定测试存档；已有主控占用桥接时，不要另开连接。
