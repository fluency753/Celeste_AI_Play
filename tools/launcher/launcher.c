/* CelesteAIConsole.exe / CelesteAIHud.exe：桌面窗口启动器（Windows）。
 *
 * 放在仓库根目录，双击后用同一目录下 .venv 里的 Python 运行 `python -m overlay.console`（不弹黑色命令行窗口）。
 * 控制台已经开着时只把它的窗口提到前面。命令行参数原样传给 overlay.console（例如 --browser、--restart-service）。
 * HUD_LAUNCHER 构建改为 overlay.window，由 Python 入口统一选择服务、单例唤醒和报告错误。
 * 它本身不含 Python 与窗口代码，所以仓库更新后不需要重新生成；构建见 tools/build_launcher.py。
 */
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <wchar.h>

#ifdef HUD_LAUNCHER
static const wchar_t TITLE[] = L"Celeste AI HUD";
#define MODULE L"overlay.window"
#define MODULE_FILE L"window.py"
#else
static const wchar_t TITLE[] = L"Celeste AI 控制台";
#define MODULE L"overlay.console"
#define MODULE_FILE L"console.py"
#endif
#define PATH_CHARS 32768

static void fail(const wchar_t *text) {
    MessageBoxW(NULL, text, TITLE, MB_OK | MB_ICONERROR);
}

static int exists(const wchar_t *path) {
    return GetFileAttributesW(path) != INVALID_FILE_ATTRIBUTES;
}

/* 跳过命令行里的程序名，返回其余参数（可能为空串）。 */
static const wchar_t *arguments(void) {
    const wchar_t *p = GetCommandLineW();
    if (*p == L'"') {
        p++;
        while (*p && *p != L'"') p++;
        if (*p) p++;
    } else {
        while (*p && *p != L' ' && *p != L'\t') p++;
    }
    while (*p == L' ' || *p == L'\t') p++;
    return p;
}

int WINAPI WinMain(HINSTANCE instance, HINSTANCE previous, LPSTR ignored, int show) {
    (void)instance; (void)previous; (void)ignored; (void)show;
    const wchar_t *args = arguments();
#ifndef HUD_LAUNCHER
    HWND window = FindWindowW(NULL, TITLE);
    if (window && !*args) {
        if (IsIconic(window)) ShowWindow(window, SW_RESTORE);
        SetForegroundWindow(window);
        return 0;
    }
#endif

    static wchar_t dir[PATH_CHARS], python[PATH_CHARS], module[PATH_CHARS], command[PATH_CHARS * 2];
    DWORD n = GetModuleFileNameW(NULL, dir, PATH_CHARS);
    if (n == 0 || n >= PATH_CHARS) {
        fail(L"找不到启动器所在的目录。");
        return 1;
    }
    wchar_t *slash = wcsrchr(dir, L'\\');
    if (slash) *slash = L'\0';

    swprintf(module, PATH_CHARS, L"%ls\\overlay\\%ls", dir, MODULE_FILE);
    if (!exists(module)) {
        fail(L"这个启动器要放在 Celeste_AI_Play 仓库的根目录（与 overlay、orchestrator 文件夹同一层）。");
        return 1;
    }
    swprintf(python, PATH_CHARS, L"%ls\\.venv\\Scripts\\pythonw.exe", dir);
    if (!exists(python)) {
        swprintf(python, PATH_CHARS, L"%ls\\.venv\\Scripts\\python.exe", dir);
        if (!exists(python)) {
            fail(L"还没有 Python 虚拟环境（.venv）。\n\n请先在这个目录里运行一键部署：\n"
                 L"powershell -ExecutionPolicy Bypass -File .\\setup.ps1");
            return 1;
        }
    }
    swprintf(command, PATH_CHARS * 2, L"\"%ls\" -X utf8 -m %ls %ls", python, MODULE, args);

    STARTUPINFOW startup;
    PROCESS_INFORMATION process;
    ZeroMemory(&startup, sizeof startup);
    startup.cb = sizeof startup;
    ZeroMemory(&process, sizeof process);
    if (!CreateProcessW(python, command, NULL, NULL, FALSE, CREATE_NO_WINDOW, NULL, dir, &startup, &process)) {
        wchar_t text[512];
        swprintf(text, 512, L"没能启动窗口（错误 %lu）。可以在仓库目录里运行：\n.venv\\Scripts\\python.exe -m %ls",
                 GetLastError(), MODULE);
        fail(text);
        return 1;
    }
    CloseHandle(process.hThread);
    CloseHandle(process.hProcess);
    return 0;
}
