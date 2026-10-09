"""命令执行与文件管理工具（都限制在工作目录内）。函数不碰 Qt 控件，可在后台线程运行。"""
import base64
import ctypes
import fnmatch
import html
import os
import re
import shutil
import subprocess
import sys
import threading

from skill_tools import NO_WINDOW, ToolError, safe_path

OUTPUT_LIMIT = 20000
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".idea", ".vscode"}

SHELL_TOOLS = [
    {"type": "function", "function": {
        "name": "run_command",
        "description": "在工作目录中用 Windows PowerShell 执行一条命令，返回输出和退出码。不要运行需要交互输入或永不退出的命令。",
        "parameters": {"type": "object", "properties": {
            "command": {"type": "string", "description": "PowerShell 命令"},
            "timeout_sec": {"type": "integer", "description": "超时秒数，默认 120，最大 1800"}},
            "required": ["command"]}}},
    {"type": "function", "function": {
        "name": "find_files", "description": "按通配符查找文件，如 **/*.py、docs/*.md。",
        "parameters": {"type": "object", "properties": {
            "pattern": {"type": "string"}}, "required": ["pattern"]}}},
    {"type": "function", "function": {
        "name": "grep_files", "description": "在文本文件中按正则表达式搜索内容，返回 文件:行号: 内容。",
        "parameters": {"type": "object", "properties": {
            "pattern": {"type": "string", "description": "正则表达式（不区分大小写）"},
            "glob": {"type": "string", "description": "限定文件的通配符，默认 **/*"}}, "required": ["pattern"]}}},
    {"type": "function", "function": {
        "name": "make_dir", "description": "新建文件夹（含上级目录）。",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "move_path", "description": "移动或重命名文件/文件夹。目标已存在时报错。",
        "parameters": {"type": "object", "properties": {
            "src": {"type": "string"}, "dst": {"type": "string"}}, "required": ["src", "dst"]}}},
    {"type": "function", "function": {
        "name": "copy_path", "description": "复制文件或文件夹。目标已存在时报错。",
        "parameters": {"type": "object", "properties": {
            "src": {"type": "string"}, "dst": {"type": "string"}}, "required": ["src", "dst"]}}},
    {"type": "function", "function": {
        "name": "delete_path", "description": "把文件或文件夹移到回收站（可从回收站恢复）。",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}},
]
SHELL_TOOL_NAMES = {t["function"]["name"] for t in SHELL_TOOLS}
SHELL_SLOW = {"run_command", "find_files", "grep_files", "copy_path"}
SHELL_LABELS = {"run_command": ("⌨️", "运行命令"), "find_files": ("🔎", "查找文件"), "grep_files": ("🔍", "搜索内容"),
                "make_dir": ("📁", "新建文件夹"), "move_path": ("↪️", "移动"), "copy_path": ("📋", "复制"),
                "delete_path": ("🗑️", "删除")}


def shell_display(name, args):
    if not isinstance(args, dict):
        return ""
    if name == "run_command":
        c = " ".join(str(args.get("command", "")).split())
        return c if len(c) <= 90 else c[:87] + "…"
    if name in ("move_path", "copy_path"):
        return f"{args.get('src', '')} → {args.get('dst', '')}"
    if name == "find_files":
        return args.get("pattern", "")
    if name == "grep_files":
        return f"「{args.get('pattern', '')}」 {args.get('glob') or ''}".strip()
    return args.get("path") or ""


# ---------------------------------------------------------------- 命令
_shells = {}  # 线程 id -> 正在运行的 PowerShell（多个对话可能同时运行命令）


def _kill_tree(pid):
    subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True, creationflags=NO_WINDOW)


def kill_shell(ident):
    proc = _shells.get(ident)
    if proc and proc.poll() is None:
        _kill_tree(proc.pid)


def _decode(data):
    for enc in ("utf-8", "gbk"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            pass
    return data.decode("utf-8", "replace")


def _clip(text):
    if len(text) <= OUTPUT_LIMIT:
        return text
    head, tail = text[:8000], text[-10000:]
    return f"{head}\n…（中间省略 {len(text) - 18000} 个字符）…\n{tail}"


def _strip_clixml(text):
    """PowerShell 在输出被重定向时可能把错误/进度写成 CLIXML，这里还原成普通文本。"""
    if "#< CLIXML" not in text:
        return text

    def repl(m):
        errs = re.findall(r'<S S="Error">(.*?)</S>', m.group(0), re.S)
        msg = "".join(errs).replace("_x000D__x000A_", "\n").replace("_x000A_", "\n")
        return html.unescape(msg)

    text = text.replace("#< CLIXML", "")
    return re.sub(r"<Objs\b.*?</Objs>", repl, text, flags=re.S)


def run_command(command, cwd, timeout):
    if not command or not command.strip():
        raise ToolError("command 不能为空")
    timeout = max(5, min(int(timeout or 120), 1800))
    script = ("[Console]::OutputEncoding = [System.Text.Encoding]::UTF8\n"
              "$OutputEncoding = [System.Text.Encoding]::UTF8\n"
              "$ProgressPreference = 'SilentlyContinue'\n"
              f"{command}\n"
              "if ($LASTEXITCODE) { exit $LASTEXITCODE }")
    encoded = base64.b64encode(script.encode("utf-16-le")).decode()
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    ident = threading.get_ident()
    proc = _shells[ident] = subprocess.Popen(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-OutputFormat", "Text", "-EncodedCommand", encoded],
        cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
        creationflags=NO_WINDOW, env=env)
    timed_out = False
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_tree(proc.pid)
        out, _ = proc.communicate()
    finally:
        _shells.pop(ident, None)
    text = _clip(_strip_clixml(_decode(out or b"")).replace("\r\n", "\n").strip())
    code = proc.returncode
    head = f"（超时 {timeout} 秒，已终止）" if timed_out else f"退出码 {code}"
    result = f"{head}\n{text or '（无输出）'}"
    return result, {"status": "ok" if code == 0 and not timed_out else "error", "preview": result}


# ---------------------------------------------------------------- 查找
def _walk_files(root):
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for f in files:
            yield os.path.relpath(os.path.join(dirpath, f), root).replace("\\", "/")


def _match(rel, pattern):
    pattern = pattern.replace("\\", "/")
    if fnmatch.fnmatch(rel, pattern):
        return True
    if pattern.startswith("**/"):  # **/*.py 也匹配根目录下的 a.py
        return fnmatch.fnmatch(rel, pattern[3:])
    return "/" not in pattern and fnmatch.fnmatch(os.path.basename(rel), pattern)


def find_files(cwd, pattern):
    if not pattern:
        raise ToolError("pattern 不能为空")
    hits = [r for r in _walk_files(cwd) if _match(r, pattern)]
    more = f"\n…（共 {len(hits)} 个，只显示前 300 个）" if len(hits) > 300 else ""
    return ("\n".join(hits[:300]) or "没有匹配的文件") + more


def grep_files(cwd, pattern, glob):
    try:
        rx = re.compile(pattern, re.I)
    except re.error:
        rx = re.compile(re.escape(pattern), re.I)
    hits = []
    for rel in _walk_files(cwd):
        if glob and not _match(rel, glob):
            continue
        p = os.path.join(cwd, rel)
        try:
            if os.path.getsize(p) > 1024 * 1024:
                continue
            with open(p, "rb") as f:
                data = f.read()
        except OSError:
            continue
        if b"\0" in data:
            continue
        for n, line in enumerate(_decode(data).splitlines(), 1):
            if rx.search(line):
                hits.append(f"{rel}:{n}: {line.strip()[:200]}")
                if len(hits) >= 200:
                    return "\n".join(hits) + "\n…（命中过多，只显示前 200 条）"
    return "\n".join(hits) or "没有找到匹配内容"


# ---------------------------------------------------------------- 文件管理
def send_to_recycle_bin(path):
    if sys.platform != "win32":
        raise ToolError("仅支持 Windows 回收站")
    from ctypes import wintypes

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT), ("pFrom", wintypes.LPCWSTR),
                    ("pTo", wintypes.LPCWSTR), ("fFlags", ctypes.c_ushort), ("fAnyOperationsAborted", wintypes.BOOL),
                    ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", wintypes.LPCWSTR)]

    FO_DELETE, FOF_SILENT, FOF_NOCONFIRMATION, FOF_ALLOWUNDO, FOF_NOERRORUI = 3, 0x4, 0x10, 0x40, 0x400
    op = SHFILEOPSTRUCTW(None, FO_DELETE, os.path.abspath(path) + "\0", None,
                         FOF_SILENT | FOF_NOCONFIRMATION | FOF_ALLOWUNDO | FOF_NOERRORUI, False, None, None)
    rc = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    if rc != 0 or os.path.exists(path):
        raise ToolError(f"移到回收站失败（错误码 {rc}）")


def run_shell_tool(name, args, cwd):
    """返回 (给模型的结果文本, 卡片信息)。"""
    if not isinstance(args, dict):
        raise ToolError("参数不是合法的 JSON 对象")
    if name == "run_command":
        return run_command(args.get("command"), cwd, args.get("timeout_sec"))
    if name == "find_files":
        r = find_files(cwd, args.get("pattern"))
        return r, {"status": "ok", "preview": r}
    if name == "grep_files":
        r = grep_files(cwd, args.get("pattern") or "", args.get("glob"))
        return r, {"status": "ok", "preview": r}

    base = os.path.realpath(cwd)
    if name == "make_dir":
        p = safe_path(cwd, args.get("path"))
        os.makedirs(p, exist_ok=True)
        return f"已创建文件夹 {args.get('path')}", {"status": "ok"}
    if name == "delete_path":
        p = safe_path(cwd, args.get("path"))
        if p == base:
            raise ToolError("不能删除工作目录本身")
        if not os.path.exists(p):
            raise ToolError("文件或文件夹不存在")
        send_to_recycle_bin(p)
        return f"已把 {args.get('path')} 移到回收站", {"status": "ok", "preview": "已移到回收站，可从回收站恢复"}
    if name in ("move_path", "copy_path"):
        src, dst = safe_path(cwd, args.get("src")), safe_path(cwd, args.get("dst"))
        if src == base:
            raise ToolError("不能移动或复制工作目录本身")
        if not os.path.exists(src):
            raise ToolError("源路径不存在")
        if os.path.exists(dst):
            raise ToolError("目标已存在，请换一个名字或先删除")
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if name == "move_path":
            shutil.move(src, dst)
            return f"已移动 {args.get('src')} → {args.get('dst')}", {"status": "ok", "move_from": src, "move_to": dst}
        if os.path.isdir(src):
            shutil.copytree(src, dst)
        else:
            shutil.copy2(src, dst)
        return f"已复制 {args.get('src')} → {args.get('dst')}", {"status": "ok"}
    raise ToolError(f"未知的工具 {name}")
