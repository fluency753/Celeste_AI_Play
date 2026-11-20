# 去掉程序目录上的完整性标签（Mandatory Label），恢复成没有标签的原状。
# 有的沙箱工具会给目录打上“低完整性”标签，之后从目录里启动的 Python、游戏副本、私有 OBS 都以低完整性运行：
# 控制台的后台服务建不出来（WMI 拒绝访问，0x80041003）。用 icacls 改成“中”也不对：用内置管理员账户时其他程序
# 都是高完整性，控制台以中完整性运行时窗口里的网页组件（WebView2）启动失败，窗口一片空白。
# 用法（PowerShell，不需要管理员）：
#   powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\clear_integrity_label.ps1
# 默认处理程序根目录（本脚本的上一级），-Path 可指定别的目录。先在临时目录上自测，不通过就不动目标目录。
param([string]$Path = (Split-Path -Parent $PSScriptRoot))
$ErrorActionPreference = 'Stop'
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class MandatoryLabel {
    [DllImport("advapi32.dll", CharSet = CharSet.Unicode)]
    static extern uint SetNamedSecurityInfoW(string name, int type, uint info, IntPtr owner, IntPtr group, IntPtr dacl, IntPtr sacl);
    [DllImport("advapi32.dll", SetLastError = true)]
    static extern bool InitializeAcl(IntPtr acl, uint length, uint revision);
    // SE_FILE_OBJECT = 1，LABEL_SECURITY_INFORMATION = 0x10。设成空的 SACL 表示不要标签，子项继承来的标签随之去掉。
    public static uint Remove(string path) {
        IntPtr acl = Marshal.AllocHGlobal(8);
        try {
            if (!InitializeAcl(acl, 8, 2)) return (uint)Marshal.GetLastWin32Error();
            return SetNamedSecurityInfoW(path, 1, 0x10, IntPtr.Zero, IntPtr.Zero, IntPtr.Zero, acl);
        } finally { Marshal.FreeHGlobal(acl); }
    }
}
'@

function Get-Label([string]$p) {
    $line = icacls $p 2>$null | Select-String 'Mandatory Label'
    if ($line) { $line.ToString().Trim() } else { '(无标签)' }
}

if (-not (Test-Path -LiteralPath $Path -PathType Container)) { throw "找不到目录：$Path" }
$Path = (Resolve-Path -LiteralPath $Path).Path

# 自测：临时目录树打上继承的低完整性标签，再去掉，确认子项也没有标签
$scratch = Join-Path ([IO.Path]::GetTempPath()) ('celeste_label_selftest_' + [guid]::NewGuid().ToString('N').Substring(0, 8))
New-Item -ItemType Directory -Path (Join-Path $scratch 'a\b') | Out-Null
Set-Content -Path (Join-Path $scratch 'a\b\c.txt') -Value 'x'
try {
    icacls $scratch /setintegritylevel '(OI)(CI)L' /C /Q | Out-Null
    $rc = [MandatoryLabel]::Remove($scratch)
    $left = @($scratch, (Join-Path $scratch 'a'), (Join-Path $scratch 'a\b\c.txt')) | Where-Object { (Get-Label $_) -ne '(无标签)' }
} finally {
    Remove-Item -LiteralPath $scratch -Recurse -Force -ErrorAction SilentlyContinue
}
if ($rc -ne 0 -or $left) {
    Write-Output "自测没有通过（返回码 $rc），$Path 没有改动。"
    exit 1
}

Write-Output "改之前：$Path => $(Get-Label $Path)"
$rc = [MandatoryLabel]::Remove($Path)
if ($rc -ne 0) {
    Write-Output "没能去掉标签（Windows 错误码 $rc）。"
    exit 1
}
foreach ($p in @($Path, (Join-Path $Path '.venv\Scripts\python.exe'), (Join-Path $Path '.venv\Scripts\pythonw.exe'))) {
    if (Test-Path -LiteralPath $p) { Write-Output "$p => $(Get-Label $p)" }
}
