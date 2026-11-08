# 标准 Everest 兼容检查

本检查运行独立进程，使用真实 Everest 与生产 Runtime DLL；不启动游戏、不打开网络连接、不改安装目录。只为依赖图构造最小模块和程序集上下文，保留生产 `Prepare → CheckReverseDependencies`、`Commit → CheckReverseDependencies` 调用链。冻结门闸就绪状态与附件回调是测试边界，不代表实机冻结门闸验收。

先用标准 Everest 6642 的已安装程序集构建全部生产代码，避免仅在本机定制版编译，遗漏分支专用成员：

```powershell
dotnet build game-bridge -t:Rebuild -c Release "-p:CelestePrefix=<标准 6642 游戏目录>"
dotnet run --project calibration/everest_compat "-p:CelestePrefix=<标准 6642 游戏目录>" -- "<标准 6642 游戏目录>"
```

若同时验证另一份 Everest 安装，接着运行以下命令。测试项目引用已构建的 Runtime，不重新构建它；两份日志中的 Runtime SHA256 必须一致，Everest SHA256 应分别匹配两份安装。这可以区分同一生产 DLL 跨环境执行与两次各自构建通过。

```powershell
dotnet run --project calibration/everest_compat "-p:CelestePrefix=<另一份游戏目录>" -- "<另一份游戏目录>"
```

覆盖：无依赖、无程序集的 Mod、仅声明依赖、仅实际程序集依赖、依赖查询异常、允许安全 prepare、prepare 后出现新声明／实际依赖、commit 查询失败。拒绝 prepare 不生成 hold／lease／快照；拒绝 commit 保留原 prepared lease 和快照，不进入加载器状态。内部字段通过项目既有 publicizer 访问，生产代码不调用或探测 Ultra API。标准字段枚举不是原子快照，不能保证与任意第三方后台动态加载之间的原子性；枚举报错时拒绝维护。

2026-11-08 验证：官方 `stable-1.6642.0` 发布包在隔离目录生成标准程序集，完整 Bridge／Runtime 构建通过（零警告、零错误）；本机定制版 Everest（当时为 `6642-ultra-36fc9`）构建也通过。同一份标准构建的 Runtime DLL 在两套程序集环境中均通过上述检查。既有 `calibration/runtime_reload` 在标准程序集下通过真实 IPC／clock／FMOD／交接检查，其 Everest 加载器边界仍使用替身，本次没有启动游戏或复测实际房间重建。Python 重载相关 4 模块整组 95 项通过。


2026-11-08 补充：本机升级到定制版 Everest `1.6662.0-ultra-25e71-stable` 后，AI 专用副本用它重新构建并部署 Bridge 1.11.1／Runtime 1.0.6（零警告、零错误），本检查在该副本的程序集下通过，检查用的 Runtime 与副本里安装的 SHA256 相同。定制版仍保留标准的 `ActiveDependencyContexts` 字段。

验证来源：[官方 6642 发布](https://github.com/EverestAPI/Everest/releases/tag/stable-1.6642.0)、[标准依赖集合源码](https://github.com/EverestAPI/Everest/blob/stable-1.6642.0/Celeste.Mod.mm/Mod/Module/EverestModuleAssemblyContext.cs)、[标准重载加载器](https://github.com/EverestAPI/Everest/blob/stable-1.6642.0/Celeste.Mod.mm/Mod/Everest/Everest.Loader.cs)。不把本次已验证版本解释为更早版本均已通过；依赖声明中的 1.5935.0 保持不变，5935～6641 未在本轮验证。
