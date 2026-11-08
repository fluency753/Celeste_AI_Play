# 预编译组件包

预编译组件通过两个 Release 附件提供：`Celeste_AI_Play-<标签>-components-windows-x64.zip` 和 `components.json`。GitHub 自动生成的 Source code 仍是源码包；组件包需要搭配同一版本的源码，不是独立客户端。

## 安装

照常运行 `setup.ps1`。向导先检查源码根目录的 `components/` 和 `local/components/<标签>/components/`，没有匹配包时从当前项目版本的精确 Release 标签下载，绝不取 `latest`。在线下载先按精确标签查询 GitHub Release 附件的 `digest` 和大小，核对完整 ZIP 后再解压；不再生成或要求独立 SHA256SUMS 文件。每次使用本地包、缓存或下载包前，均核对包内清单中的项目／组件版本、源码指纹和所有文件的 SHA256；只有校验完成才发布缓存。源码指纹将 CRLF／LF 统一处理，支持没有 `.git` 的源码压缩包；本机配置、凭据、笔记与构建缓存不参与。

离线部署：从同一个 Release 下载源码和组件包，把组件 ZIP 解压到源码根目录，得到 `components/manifest.json`，再运行向导。程序根目录已有启动器；包中 `launchers/` 附有同版启动器文件，不应直接在该子目录启动。Python 环境和 Python 依赖仍由 `setup.ps1` 准备。

公测版可以指定精确标签（也可直接解压对应组件包）：

```powershell
powershell -ExecutionPolicy Bypass -File .\setup.ps1 -ReleaseTag v1.22.0-beta.1
powershell -ExecutionPolicy Bypass -File .\setup.ps1 -Only bridge,recording
```

没有该 Release／附件、暂时离线、源码或版本已改变时，向导说明原因，允许改为源码编译；GitHub 附件缺少有效 SHA256／大小、下载结果不匹配、已有包内容损坏、路径非法或文件校验失败则停止安装这个组件，不绕过校验回退：向导说明原因（不打出 Python 报错），已安装的文件不动，接着做后面的步骤，排查后用 `setup.ps1 -Only bridge`（或 `recording`）单独重做。开发版没有对应发布包时通常走源码编译。`-Check` 不下载、不解压、不创建缓存，也不安装 SDK。

## 安装保护与兼容范围

- Bridge／Runtime 使用同一个包，仍由 `tools/deploy_bridge.ps1` 备份并安装。常驻核心字节相同时可走在用主控或空闲维护的受控重载；首次安装或 Runtime 字节不同就拒绝运行中替换，由使用者安排维护重启，不自动关闭游戏。同版本核心也可能因工具链或引用程序集变化而产生不同字节，不能只看版本号判断可重载。
- OBS 插件目前打包针对 **33.0.0** 构建的版本，仅在 OBS 产品版本匹配时使用；其他版本可回退源码构建。两条安装路径均拒绝 OBS 正在运行的情况，不替使用者停止 OBS。替换前备份旧 DLL，用临时文件发布；失败保留原 DLL 与备份，不改 OBS 设置。
- .NET SDK 仅源码编译 Bridge／Runtime 时需要；预编译安装不需要 SDK。源码构建 OBS 插件仍需 zig 和对应 libobs 头文件（脚本先经 HTTPS 匿名下载，不通再试 SSH，都不通时说明原因）。
- 包内不包含《蔚蓝》、Everest 或 FMOD 的程序集／资源，不包含预置模拟器、个人提示词、存档、录像或登录数据。

手动安装已校验的本地组件：

```powershell
.\tools\deploy_bridge.ps1 -CelesteDir "<游戏目录>" -ComponentRoot ".\components"
```

此入口会重新校验整个包，再沿原有部署流程执行；传入组件目录不会绕过运行中检查。

## 清单与构建

清单 `schema=1` 记录 `platform`、`project_version`、`tag`、`source_commit`、`source_dirty`、`source_digest`、`component_versions`、标准 Everest 引用来源和校验值、`obs_versions`、工具链，以及 `files` 中每个文件的字节数与 SHA256。固定文件范围为两个 Mod 的 YAML／DLL／PDB、OBS DLL 和两个启动器；ZIP 禁止额外文件、重复条目、路径穿越、链接与超限内容。

发布构建工具为 `tools/package_release.py`，只构建、不安装或上传。默认从官方固定校验值的标准 Everest 6642 引用包构建，工具链及 OBS 版本写在 `configs/component_build.json`。生成的 ZIP 不包含这些构建引用。

```powershell
python tools/package_release.py --tag v1.22.0 --output build/release
python tools/publish_release.py --tag v1.22.0 --check
```

本机验证可以显式传入 `--reference-dir "<已核验的标准 Everest 目录>"`，产物标记为 `explicit-local`；上传器会拒绝此类候选，正式发布必须使用固定官方引用。上传器还核对清单、源提交、标签、干净检出与固定工具链。发布步骤见仓库中的[发布流程](https://github.com/fluency753/Celeste_AI_Play/blob/main/docs/%E5%BC%80%E5%8F%91/%E5%8F%91%E5%B8%83%E6%B5%81%E7%A8%8B.md)。
