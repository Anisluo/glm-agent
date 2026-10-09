"""GLM 桌面聊天客户端（PyQt6）

运行:  python glm_client.py
依赖:  pip install -r requirements.txt   (PyQt6 requests markdown-it-py pygments)
"""
import difflib
import functools
import html
import json
import logging
import logging.handlers
import math
import os
import re
import threading
import shutil
import subprocess
import sys
import time
import traceback
import zlib

import requests
from skill_tools import (SKILL_LABELS, SKILL_TOOL_NAMES, SKILL_TOOLS, SLOW_TOOLS, ToolError, env_status, kill_current,
                         prefetch, run_skill_tool, safe_path, skill_dir, skill_prompt, tool_display)
from shell_tools import (SHELL_LABELS, SHELL_SLOW, SHELL_TOOL_NAMES, SHELL_TOOLS, kill_shell, run_shell_tool,
                         shell_display)
from memory_store import (COMPRESS_PROMPT, DISTILL_PROMPT, MEMORY_LABELS, MEMORY_PROMPT, MEMORY_TOOL_NAMES,
                          MEMORY_TOOLS, TYPES as MEMORY_TYPES, MemoryStore, parse_json_object, transcript)
from mcp_client import MCPManager
from markdown_it import MarkdownIt
from pygments import highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import get_lexer_by_name, get_lexer_for_filename
from pygments.lexers.special import TextLexer
from pygments.util import ClassNotFound
from PyQt6.QtCore import QDir, QEvent, QObject, Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtNetwork import QLocalServer, QLocalSocket
from PyQt6.QtGui import QCursor, QDesktopServices, QFileSystemModel, QFont, QIcon, QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
    QFileDialog, QFormLayout, QFrame, QHBoxLayout, QInputDialog, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMainWindow, QMenu, QMessageBox, QPlainTextEdit,
    QPushButton, QScrollArea, QSizePolicy, QStackedWidget, QTextBrowser, QToolButton,
    QToolTip, QTreeView, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

APP_NAME = "GLM-MCP-文档对比agent"
# 打包成 exe 后，配置和对话记录保存在 exe 所在目录
ROOT = os.path.dirname(sys.executable if getattr(sys, "frozen", False) else os.path.abspath(__file__))
CONFIG_PATH = os.path.join(ROOT, "config.json")
CONVS_PATH = os.path.join(ROOT, "conversations.json")
LOG_DIR = os.path.join(ROOT, "logs")
MEMORY_PATH = os.path.join(ROOT, "memory.json")
MCP_PATH = os.path.join(ROOT, "mcp_servers.json")
COMPRESS_AT = 90000          # 发给模型的历史超过这么多字符时，把较早的部分压缩成摘要
KEEP_TAIL = 25000            # 压缩后保留的最近原文大约这么多字符
SUMMARY_MARK = "⟦较早对话摘要⟧"
QUOTA_WORDS = ("余额", "额度", "上限", "充值", "资源包")
HINT_TEXT = "GLM 生成的内容可能有误，请核实重要信息"
HTTP = requests.Session()   # 复用连接，省去每轮请求重新握手 HTTPS 的时间
HTTP.mount("https://", requests.adapters.HTTPAdapter(pool_connections=4, pool_maxsize=16))
MAX_PARALLEL = 3                              # 同时向接口发请求的对话数上限，其余排队
API_SLOTS = threading.Semaphore(MAX_PARALLEL)
RETRIES = 4                                   # 连接失败 / 5xx / 并发限流时最多尝试 4 次
LOAD_PROBLEMS = []                            # 启动时读取数据遇到的问题，窗口打开后提示用户

log = logging.getLogger("glm")


def setup_logging():
    """日志写到 exe 旁边的 logs/app.log（最多 3 个 1MB 文件轮换），不记录 API Key。"""
    log.setLevel(logging.INFO)
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        h = logging.handlers.RotatingFileHandler(os.path.join(LOG_DIR, "app.log"), maxBytes=1_000_000,
                                                 backupCount=3, encoding="utf-8")
    except OSError:
        h = logging.NullHandler()
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(threadName)s] %(message)s"))
    log.addHandler(h)

BASE_URLS = {  # Coding Plan（编程套餐）的额度只能走 /api/coding/ 地址，按量计费的余额走普通地址
    "GLM Coding Plan（国内）": "https://open.bigmodel.cn/api/coding/paas/v4",
    "智谱开放平台 按量计费（国内）": "https://open.bigmodel.cn/api/paas/v4",
    "GLM Coding Plan（国际 Z.ai）": "https://api.z.ai/api/coding/paas/v4",
    "Z.ai 按量计费（国际）": "https://api.z.ai/api/paas/v4",
}
DEFAULT_CONFIG = {
    "api_key": "",
    "base_url": BASE_URLS["GLM Coding Plan（国内）"],
    "model": "glm-5.3",
    "temperature": 0.7,
    "reasoning_effort": "max",  # GLM-5.3 始终思考，只能选强度：low / high / max
    "auto_memory": True,        # 对话结束后自动把要点提炼到记忆库
    "system_prompt": "",
}
MAX_FILE_BYTES = 300 * 1024
MAX_ROUNDS = 80
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".idea", ".vscode", "dist", "build"}

# ---------------------------------------------------------------- 文件工具（仅限工作目录内）
TOOLS = [
    {"type": "function", "function": {
        "name": "list_dir", "description": "列出工作目录中某个目录的内容（不递归）。",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "相对于工作目录的路径，默认为 ."}}}}},
    {"type": "function", "function": {
        "name": "read_file", "description": "读取一个文本文件的完整内容。",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "相对于工作目录的文件路径"}}, "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "write_file", "description": "新建文件，或用给定内容整体覆盖已有文件。",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "相对于工作目录的文件路径"},
            "content": {"type": "string", "description": "文件的完整新内容"}}, "required": ["path", "content"]}}},
    {"type": "function", "function": {
        "name": "edit_file",
        "description": "把文件中的 old_string 替换为 new_string。old_string 必须与原文完全一致且在文件中唯一（除非 replace_all 为 true）。",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "相对于工作目录的文件路径"},
            "old_string": {"type": "string", "description": "要被替换的原文"},
            "new_string": {"type": "string", "description": "替换后的新文本"},
            "replace_all": {"type": "boolean", "description": "是否替换所有匹配，默认 false"}},
            "required": ["path", "old_string", "new_string"]}}},
]
TOOL_PROMPT = """你可以通过工具直接操作工作目录中的文件并运行命令：
- 查看：list_dir、read_file、find_files（按文件名通配）、grep_files（按内容正则搜索）；
- 修改文件前先用 read_file 读取最新内容；
- 小范围修改用 edit_file（old_string 必须与文件原文完全一致，并带足够上下文保证唯一）；新建或整体重写用 write_file；
- 路径一律使用相对于工作目录的路径，不能访问工作目录以外的文件；
- 文件管理：make_dir、move_path、copy_path、delete_path（移到回收站）；
- 命令：run_command 在工作目录用 Windows PowerShell 执行（可运行 python、pip、git、编译等），不要运行需要交互输入或不会自动退出的命令；
- 这些操作会直接执行、不再询问用户，所以动手前想清楚：删除只用于确实不需要的文件，命令避免影响工作目录以外的系统设置。
- 提速：互不依赖的读取类操作（读多份文件/PDF、多个页段、搜索、查记忆）请在同一次回复里同时发起多个工具调用，程序会并行执行，能明显减少等待；不要一次只读一点再等下一轮。
完成后用简短的中文说明改了哪些地方。"""
TOOL_LABELS = {"list_dir": ("📂", "查看目录"), "read_file": ("📄", "读取"),
               "write_file": ("📝", "写入"), "edit_file": ("✏️", "修改")}


# ---------------------------------------------------------------- 配色
BG, SIDE, BORDER = "#faf9f5", "#f3f1ea", "#e6e3da"
TEXT, MUTED = "#2b2a27", "#8b887f"
ACCENT, ACCENT_H = "#c96442", "#b45535"
USER_BG, HOVER = "#efece3", "#ebe8df"
CODE_BG, CODE_HEAD = "#282c34", "#21252b"

QSS = f"""
QWidget {{ color: {TEXT}; }}
QMainWindow, #main {{ background: {BG}; }}
#sidebar {{ background: {SIDE}; border-right: 1px solid {BORDER}; }}
#brand {{ font-size: 11pt; font-weight: 600; }}
#brandStar {{ color: {ACCENT}; font-size: 15pt; }}
#newBtn {{ background: transparent; border: 1px solid {BORDER}; border-radius: 10px;
          padding: 8px 12px; text-align: left; font-weight: 600; }}
#newBtn:hover {{ background: {HOVER}; }}
#sectionLabel {{ color: {MUTED}; font-size: 9pt; padding: 6px 8px 2px; }}
#convList {{ background: transparent; border: none; outline: 0; }}
#convList::item {{ padding: 8px 10px; border-radius: 8px; margin: 1px 0; color: {TEXT}; }}
#convList::item:hover {{ background: {HOVER}; }}
#convList::item:selected {{ background: #e3dfd3; color: {TEXT}; }}
#sideBtn {{ background: transparent; border: none; border-radius: 8px; padding: 8px 10px;
           text-align: left; color: #55524b; }}
#sideBtn:hover {{ background: {HOVER}; }}
#topBar {{ background: {BG}; border-bottom: 1px solid #efece4; }}
#convTitle {{ font-weight: 600; font-size: 10.5pt; }}
#dirBtn {{ border: 1px solid {BORDER}; border-radius: 13px; padding: 4px 12px;
          background: transparent; color: #55524b; }}
#dirBtn:hover {{ background: {SIDE}; }}
#dirBtn::menu-indicator {{ image: none; }}
#chatInner, QScrollArea {{ background: {BG}; border: none; }}
#userBubble {{ background: {USER_BG}; border-radius: 14px; padding: 10px 14px; }}
#fileTag {{ background: white; border: 1px solid {BORDER}; border-radius: 8px;
           padding: 3px 8px; color: #55524b; font-size: 9pt; }}
#avatar {{ background: {ACCENT}; color: white; border-radius: 12px; font-size: 11pt; }}
#msgName {{ font-weight: 600; }}
#msgModel {{ color: {MUTED}; font-size: 9pt; }}
#thinkBtn {{ border: none; background: transparent; color: {MUTED}; padding: 2px 0; }}
#thinkBtn:hover {{ color: {TEXT}; }}
#thinkText {{ color: {MUTED}; border-left: 2px solid {BORDER}; padding-left: 10px; }}
#errorBox {{ background: #fbeceb; color: #b3261e; border: 1px solid #f3cfcc;
            border-radius: 8px; padding: 8px 12px; }}
#ghostBtn {{ border: none; background: transparent; color: {MUTED}; padding: 3px 8px; border-radius: 6px; }}
#ghostBtn:hover {{ background: {USER_BG}; color: {TEXT}; }}
#composer {{ background: white; border: 1px solid #ddd9ce; border-radius: 18px; }}
#input {{ background: transparent; border: none; font-size: 10.5pt; selection-background-color: #f1d3c5; }}
#chip, #attach {{ border: 1px solid {BORDER}; border-radius: 10px; padding: 4px 10px;
                 background: transparent; color: #55524b; font-size: 9pt; }}
#chip:hover, #attach:hover {{ background: {SIDE}; }}
#chip:checked {{ background: #f8e7df; border-color: #ecc3b2; color: {ACCENT}; }}
#attach {{ background: {SIDE}; }}
#sendBtn {{ background: {ACCENT}; color: white; border: none; border-radius: 10px;
           font-size: 12pt; font-weight: bold; }}
#sendBtn:hover {{ background: {ACCENT_H}; }}
#sendBtn:disabled {{ background: #e8cfc4; }}
#hint {{ color: {MUTED}; font-size: 8.5pt; }}
#welcomeStar {{ color: {ACCENT}; font-size: 30pt; }}
#welcomeTitle {{ font-family: Georgia, "STZhongsong", "SimSun", serif; font-size: 24pt; }}
#welcomeSub {{ color: {MUTED}; font-size: 11pt; }}
#suggest {{ border: 1px solid {BORDER}; border-radius: 12px; padding: 8px 14px;
           background: white; color: #55524b; }}
#suggest:hover {{ border-color: #d4cfc2; background: #fdfcf9; }}
#filePanel {{ background: {SIDE}; border-left: 1px solid {BORDER}; }}
#panelTitle {{ font-weight: 600; }}
#panelPath {{ color: {MUTED}; font-size: 8.5pt; }}
#fileTree {{ background: transparent; border: none; outline: 0; }}
#fileTree::item {{ padding: 3px 2px; border-radius: 4px; }}
#fileTree::item:hover {{ background: {HOVER}; }}
#fileTree::item:selected {{ background: #e3dfd3; color: {TEXT}; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: #d9d5ca; border-radius: 3px; min-height: 30px; }}
QScrollBar::handle:vertical:hover {{ background: #c8c3b6; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}
QScrollBar:horizontal {{ height: 0; }}
QDialog {{ background: {BG}; }}
QDialog QLineEdit, QDialog QComboBox, QDialog QPlainTextEdit, QDialog QDoubleSpinBox {{
    background: white; border: 1px solid #ddd9ce; border-radius: 8px; padding: 6px 8px; }}
QDialog QLineEdit:focus, QDialog QPlainTextEdit:focus {{ border-color: {ACCENT}; }}
QDialog QPushButton {{ background: white; border: 1px solid #ddd9ce; border-radius: 8px; padding: 6px 18px; }}
QDialog QPushButton:hover {{ background: {SIDE}; }}
QMenu {{ background: white; border: 1px solid {BORDER}; padding: 4px; }}
QMenu::item {{ padding: 6px 20px; border-radius: 6px; }}
QMenu::item:selected {{ background: {SIDE}; color: {TEXT}; }}
QToolTip {{ background: {TEXT}; color: white; border: none; padding: 4px 8px; }}
#toolCard {{ background: white; border: 1px solid {BORDER}; border-radius: 10px; }}
#toolStatus {{ font-size: 9pt; }}
QDialog #primaryBtn {{ background: {ACCENT}; color: white; border: none; }}
#statusLine {{ color: {MUTED}; font-size: 9pt; padding: 2px 0; }}
#divider {{ border: none; background: transparent; color: {MUTED}; font-size: 9pt; padding: 4px; }}
#divider:hover {{ color: {TEXT}; }}
QDialog #primaryBtn:hover {{ background: {ACCENT_H}; }}
"""

DOC_CSS = f"""
p, li {{ line-height: 165%; }}
code {{ background-color: {USER_BG}; color: #b5502f; font-family: Consolas, monospace; }}
h1 {{ font-size: 17pt; }} h2 {{ font-size: 14.5pt; }} h3 {{ font-size: 12.5pt; }}
blockquote {{ color: {MUTED}; }}
a {{ color: {ACCENT}; }}
th {{ background-color: {SIDE}; }}
"""


# ---------------------------------------------------------------- 工具函数
def _read_json(path):
    with open(path, encoding="utf-8-sig") as f:
        return json.load(f)


def load_json(path, default):
    """读取 JSON。文件损坏时先把它备份，再尝试用上一版 .bak 恢复，绝不让坏文件被空数据覆盖。"""
    try:
        return _read_json(path)
    except FileNotFoundError:
        return default
    except (ValueError, OSError) as e:
        bad = f"{os.path.splitext(path)[0]}.broken-{time.strftime('%Y%m%d-%H%M%S')}.json"
        try:
            shutil.copy2(path, bad)
        except OSError:
            bad = ""
        log.error("读取 %s 失败：%s；已备份为 %s", path, e, bad)
        msg = f"{os.path.basename(path)} 已损坏无法读取" + (f"，原文件已备份为 {os.path.basename(bad)}" if bad else "")
        try:
            data = _read_json(path + ".bak")
            LOAD_PROBLEMS.append(msg + "，已从上一次保存的备份恢复。")
            return data
        except (ValueError, OSError):
            LOAD_PROBLEMS.append(msg + "，也没有可用的备份。")
            return default


def save_json(path, data):
    """先写临时文件再替换，替换前把上一版留作 .bak；文件被占用时自动重试。成功返回 True。"""
    text = json.dumps(data, ensure_ascii=False, indent=2)
    tmp = path + ".tmp"
    for attempt in range(3):
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(text)
            if os.path.exists(path):
                shutil.copy2(path, path + ".bak")
            os.replace(tmp, path)
            return True
        except OSError as e:
            log.warning("保存 %s 失败（第 %d 次）：%s", path, attempt + 1, e)
            time.sleep(0.3)
    return False


def read_text_file(path):
    """读取文本文件，返回 (内容, 错误信息, 格式)。内容的换行统一为 \\n，格式用于写回时保持原编码和换行符。"""
    if os.path.getsize(path) > MAX_FILE_BYTES:
        return None, f"文件超过 {MAX_FILE_BYTES // 1024} KB", None
    with open(path, "rb") as f:
        data = f.read()
    if b"\0" in data:
        return None, "不是文本文件", None
    for enc in ("utf-8-sig", "gbk"):
        try:
            text = data.decode(enc)
        except UnicodeDecodeError:
            continue
        if enc == "utf-8-sig" and not data.startswith(b"\xef\xbb\xbf"):
            enc = "utf-8"
        return text.replace("\r\n", "\n"), None, {"enc": enc, "crlf": b"\r\n" in data}
    return None, "无法识别文件编码", None


def write_text_file(path, text, enc="utf-8", crlf=False):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    text = text.replace("\r\n", "\n")
    if crlf:
        text = text.replace("\n", "\r\n")
    try:
        data = text.encode(enc)
    except UnicodeEncodeError:
        data = text.encode("utf-8")
    with open(path, "wb") as f:
        f.write(data)


def unified_diff(old, new, rel):
    return "\n".join(difflib.unified_diff(
        (old or "").splitlines(), new.splitlines(),
        "a/" + rel if old is not None else "/dev/null", "b/" + rel, lineterm="", n=3))


def diff_stats(diff):
    lines = diff.splitlines()
    added = sum(1 for l in lines if l.startswith("+") and not l.startswith("+++"))
    removed = sum(1 for l in lines if l.startswith("-") and not l.startswith("---"))
    return added, removed


def diff_html(diff, max_lines=1500):
    out = []
    lines = diff.splitlines()
    for l in lines[:max_lines]:
        e = html.escape(l) or " "
        if l.startswith(("+++", "---")):
            out.append(f'<span style="color:{MUTED}">{e}</span>')
        elif l.startswith("+"):
            out.append(f'<span style="background-color:#e6ffec;color:#116329">{e}</span>')
        elif l.startswith("-"):
            out.append(f'<span style="background-color:#ffebe9;color:#a40e26">{e}</span>')
        elif l.startswith("@@"):
            out.append(f'<span style="color:#8250df">{e}</span>')
        else:
            out.append(e)
    if len(lines) > max_lines:
        out.append(f'<span style="color:{MUTED}">… 还有 {len(lines) - max_lines} 行未显示</span>')
    return ('<pre style="margin:0;white-space:pre-wrap;font-family:Consolas,monospace;font-size:9.5pt">'
            + "\n".join(out) + "</pre>")


def lang_of(path):
    try:
        return get_lexer_for_filename(path).aliases[0]
    except (ClassNotFound, IndexError):
        return ""


def dir_overview(root, limit=150):
    """目录前两层的结构，提供给模型做上下文。"""
    lines, visited = [], 0
    for dirpath, dirs, files in os.walk(root):
        visited += 1
        if visited > 300:
            lines.append("…")
            break
        depth = os.path.relpath(dirpath, root).count(os.sep) + (dirpath != root)
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith("."))
        if depth >= 2:
            dirs[:] = []
        indent = "  " * depth
        for d in dirs:
            lines.append(f"{indent}{d}/")
        for f in sorted(files):
            lines.append(f"{indent}{f}")
        if len(lines) > limit:
            lines = lines[:limit] + ["…"]
            break
    return "\n".join(lines)


def open_in_explorer(path, select=False):
    if select:
        subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
    else:
        os.startfile(path)


def api_content(m):
    """把用户消息和附带文件合成发给模型的文本。"""
    parts = [f"文件 `{f['path']}`：\n````{f.get('lang', '')}\n{f['content']}\n````" for f in m.get("files", [])]
    return "\n\n".join(parts + [m["content"]])


# ---------------------------------------------------------------- Markdown → HTML
_FMT = HtmlFormatter(style="one-dark", noclasses=True, nowrap=True)
MD_TABLE = f'<table border="1" cellspacing="0" cellpadding="6" style="border-collapse:collapse;border-color:{BORDER}">'


def _render_fence(self, tokens, idx, options, env):
    tok = tokens[idx]
    lang = tok.info.strip().split()[0] if tok.info.strip() else ""
    code = tok.content.rstrip("\n")
    try:
        lexer = get_lexer_by_name(lang) if lang else TextLexer()
    except ClassNotFound:
        lexer = TextLexer()
    body = highlight(code, lexer, _FMT).rstrip("\n")
    codes = env.setdefault("codes", [])
    codes.append(code)
    return (
        f'<table width="100%" cellspacing="0" cellpadding="0" bgcolor="{CODE_BG}" style="margin-top:6px;margin-bottom:10px">'
        f'<tr><td bgcolor="{CODE_HEAD}" style="padding:6px 12px;color:#9da5b4;font-size:9pt">{html.escape(lang or "text")}</td>'
        f'<td bgcolor="{CODE_HEAD}" align="right" style="padding:6px 12px;font-size:9pt">'
        f'<a href="copy:{len(codes) - 1}" style="color:#d7dae0;text-decoration:none">复制</a></td></tr>'
        f'<tr><td colspan="2" style="padding:10px 14px"><pre style="margin:0;white-space:pre-wrap;color:#abb2bf;'
        f'font-family:Consolas,\'Courier New\',monospace;font-size:10pt">{body}</pre></td></tr></table>'
    )


_md = MarkdownIt("commonmark", {"html": False, "breaks": True}).enable(["table", "strikethrough"])
_md.add_render_rule("fence", _render_fence)
_md.add_render_rule("code_block", _render_fence)


def md_to_html(text):
    env = {}
    out = _md.render(text, env).replace("<table>", MD_TABLE)
    return out, env.get("codes", [])


# ---------------------------------------------------------------- 网络请求线程
def chat_once(cfg, messages, effort="low", timeout=300, use_slot=True, stop=None):
    """非流式请求一次模型（压缩对话、提炼记忆用），返回回复文本；失败抛 RuntimeError。"""
    if use_slot:
        while not API_SLOTS.acquire(timeout=0.2):
            if stop and stop():
                raise RuntimeError("已停止")
    try:
        payload = {"model": cfg["model"], "messages": messages, "stream": False, "temperature": 0.3,
                   "reasoning_effort": effort}
        url = cfg["base_url"].rstrip("/") + "/chat/completions"
        last = "未知错误"
        for attempt in range(1, RETRIES + 1):
            if stop and stop():
                raise RuntimeError("已停止")
            try:
                r = HTTP.post(url, json=payload, timeout=(15, timeout),
                                  headers={"Authorization": "Bearer " + cfg["api_key"]})
            except (requests.ConnectionError, requests.Timeout) as e:
                last = f"网络连接失败：{e}"
            else:
                if r.status_code == 200:
                    j = r.json()
                    return ((j.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
                try:
                    msg = r.json().get("error", {}).get("message") or r.text
                except ValueError:
                    msg = r.text
                last = f"HTTP {r.status_code}: {msg[:300]}"
                if not (r.status_code >= 500 or (r.status_code == 429 and not any(k in msg for k in QUOTA_WORDS))):
                    break
            time.sleep(2 ** attempt)
        raise RuntimeError(last)
    finally:
        if use_slot:
            API_SLOTS.release()


class ChatWorker(QThread):
    reasoning = pyqtSignal(str)
    content = pyqtSignal(str)
    failed = pyqtSignal(str)
    notice = pyqtSignal(str)      # 排队、重试等状态提示
    summarized = pyqtSignal(str)  # 较早对话压缩完成
    tool_progress = pyqtSignal(object)  # {"name", "chars", "path"}：正在生成的工具调用

    def __init__(self, cfg, messages, tools=None, compress=None):
        super().__init__()
        self.cfg, self.messages, self.tools = cfg, messages, tools
        self.compress = compress      # 需要压缩的较早对话文本；系统提示里的 SUMMARY_MARK 会被替换成摘要
        self.tool_calls = []
        self._stop = False
        self._resp = None
        self.timing = None            # {"total", "think"} 秒：本步总用时、其中思考用时

    def stop(self):
        self._stop = True
        if self._resp is not None:
            try:
                self._resp.close()
            except Exception:
                pass

    def _sleep(self, sec):
        end = time.time() + sec
        while time.time() < end and not self._stop:
            time.sleep(0.1)

    def run(self):
        queued = False
        while not API_SLOTS.acquire(timeout=0.2):  # 多个对话同时运行时排队，避免触发接口并发限制
            if self._stop:
                return
            if not queued:
                queued = True
                self.notice.emit(f"同时运行的对话较多，排队中…（最多同时 {MAX_PARALLEL} 个）")
        try:
            self._run()
        except Exception as e:  # 兜底：线程里的任何异常都转成这个对话的错误，不影响程序
            log.exception("请求线程异常")
            if not self._stop:
                self.failed.emit(f"请求失败: {e}")
        finally:
            API_SLOTS.release()

    def _run(self):
        cfg = self.cfg
        if self.compress:
            self.notice.emit("对话较长，正在把较早的内容压缩成摘要…")
            try:
                summary = chat_once(cfg, [{"role": "system", "content": COMPRESS_PROMPT},
                                          {"role": "user", "content": self.compress}],
                                    use_slot=False, stop=lambda: self._stop).strip()
            except RuntimeError as e:
                log.warning("压缩对话失败：%s", e)
                summary = ""
            if self._stop:
                return
            if not summary:
                summary = "（自动摘要失败，以下是截断的较早对话记录）\n" + self.compress[-6000:]
            self.messages[0]["content"] = self.messages[0]["content"].replace(SUMMARY_MARK, summary)
            self.summarized.emit(summary)
            self.notice.emit("正在生成…")
        payload = {
            "model": cfg["model"],
            "messages": self.messages,
            "stream": True,
            "temperature": float(cfg.get("temperature", 0.7)),
            "reasoning_effort": cfg.get("reasoning_effort") or "max",
        }
        if self.tools:
            payload["tools"] = self.tools
            payload["tool_choice"] = "auto"
        url = cfg["base_url"].rstrip("/") + "/chat/completions"
        for attempt in range(1, RETRIES + 1):
            if self._stop:
                return
            delay = 2 ** attempt
            try:
                r = HTTP.post(url, json=payload, stream=True, timeout=(15, 600),
                                  headers={"Authorization": "Bearer " + cfg["api_key"], "Accept": "text/event-stream"})
            except (requests.ConnectionError, requests.Timeout) as e:
                log.warning("连接失败（第 %d 次）：%s", attempt, e)
                if attempt < RETRIES and not self._stop:
                    self.notice.emit(f"网络连接失败，{delay} 秒后自动重试（{attempt}/{RETRIES - 1}）…")
                    self._sleep(delay)
                    continue
                if not self._stop:
                    self.failed.emit(f"网络连接失败，请检查网络后重试。（{e}）")
                return
            self._resp = r
            if r.status_code == 200:
                log.info("请求成功 model=%s 消息数=%d 工具=%s", cfg["model"], len(self.messages), bool(self.tools))
                return self._read_stream(r)
            try:
                msg = r.json().get("error", {}).get("message") or r.text
            except ValueError:
                msg = r.text
            r.close()
            log.warning("HTTP %s（第 %d 次）：%s", r.status_code, attempt, msg[:300])
            quota = any(k in msg for k in ("余额", "额度", "上限", "充值", "资源包"))
            if (r.status_code >= 500 or (r.status_code == 429 and not quota)) and attempt < RETRIES:
                self.notice.emit(f"接口繁忙（HTTP {r.status_code}），{delay} 秒后自动重试（{attempt}/{RETRIES - 1}）…")
                self._sleep(delay)
                continue
            hint = ""
            if r.status_code == 429 and quota and "/coding/" not in cfg["base_url"]:
                hint = ("\n\n提示：如果你购买的是 GLM Coding Plan（编程套餐），它的额度只能通过专用地址使用。"
                        "请点左下角「设置」，把接口地址改为「GLM Coding Plan」后重试。")
            elif r.status_code == 429 and "/coding/" in cfg["base_url"]:
                hint = "\n\n提示：Coding Plan 的 5 小时或每周额度可能已用完，可在智谱控制台的用量统计中查看重置时间。"
            if not self._stop:
                self.failed.emit(f"HTTP {r.status_code}: {msg[:500]}{hint}")
            return

    def _report_progress(self, slot):
        now = time.time()
        if now - getattr(self, "_last_progress", 0) < 0.3:
            return
        self._last_progress = now
        a = slot["function"]["arguments"]
        m = re.search(r'"(?:path|title|command)"\s*:\s*"((?:[^"\\]|\\.)*)"', a)
        self.tool_progress.emit({"name": slot["function"]["name"], "chars": len(a),
                                 "path": m.group(1) if m else ""})

    def _read_stream(self, r):
        calls = {}
        t0, t_think, t_answer, sizes = time.time(), None, None, [0, 0]   # 思考开始/作答开始时间、思考/输出字数
        completed = False  # 收到 [DONE] 或 finish_reason 才算正常结束；否则是连接被提前关闭
        try:
            with r:
                # 按行直接读底层连接：iter_lines 会攒够 512 字节才返回，小段的思考/回复内容会被卡住、显示一顿一顿的
                r.raw.decode_content = True
                for line in iter(r.raw.readline, b""):
                    line = line.strip()
                    if self._stop:
                        return
                    if not line.startswith(b"data:"):
                        continue
                    data = line[5:].strip()
                    if data == b"[DONE]":
                        completed = True
                        break
                    try:
                        j = json.loads(data)
                    except ValueError:
                        continue
                    if "error" in j:
                        self.failed.emit(str(j["error"].get("message", j["error"])))
                        return
                    choice = (j.get("choices") or [{}])[0]
                    if choice.get("finish_reason"):
                        completed = True
                    delta = choice.get("delta") or {}
                    now = time.time()
                    if delta.get("reasoning_content"):
                        t_think = t_think or now
                        sizes[0] += len(delta["reasoning_content"])
                    if delta.get("content") or delta.get("tool_calls"):
                        t_answer = t_answer or now
                        sizes[1] += len(delta.get("content") or "") + sum(
                            len(str((tc.get("function") or {}).get("arguments") or "")) for tc in delta.get("tool_calls") or [])
                    if delta.get("reasoning_content"):
                        self.reasoning.emit(delta["reasoning_content"])
                    if delta.get("content"):
                        self.content.emit(delta["content"])
                    for tc in delta.get("tool_calls") or []:
                        i = tc.get("index")
                        if i is None:
                            i = len(calls) if tc.get("id") else max(calls, default=0)
                        slot = calls.setdefault(i, {"id": "", "type": "function",
                                                    "function": {"name": "", "arguments": ""}})
                        if tc.get("id"):
                            slot["id"] = tc["id"]
                        fn = tc.get("function") or {}
                        if fn.get("name"):
                            slot["function"]["name"] = fn["name"]
                        args = fn.get("arguments")
                        if isinstance(args, dict):
                            args = json.dumps(args, ensure_ascii=False)
                        if args:
                            slot["function"]["arguments"] += args
                        self._report_progress(slot)
                if completed:  # 读完剩余字节并标记已读完，关闭时连接会放回连接池，下一轮请求不必重新握手
                    try:
                        r.raw.drain_conn()
                        r._content_consumed = True
                    except Exception:
                        pass
        except Exception as e:  # 生成到一半连接断开：保留已收到的内容
            if not self._stop:
                log.warning("流式输出中断：%s", e)
                self.failed.emit(f"连接中断（{e}）。已收到的内容已保留，可以发送“继续”让 GLM 接着完成。")
            return
        if not completed:
            if not self._stop:
                log.warning("流式输出未正常结束（没有收到结束标记）")
                self.failed.emit("连接中断：回复没有正常结束。已收到的内容已保留，可以发送“继续”让 GLM 接着完成。")
            return
        end = time.time()
        think = ((t_answer or end) - t_think) if t_think else 0
        self.timing = {"total": round(end - t0, 1), "think": round(think, 1)}
        log.info("本步用时 %.1fs（等待首字 %.1fs，思考 %.1fs / %d 字，输出 %d 字）", end - t0,
                 ((t_think or t_answer or end) - t0), think, sizes[0], sizes[1])
        self.tool_calls = [calls[k] for k in sorted(calls) if calls[k]["function"]["name"]]
        for n, c in enumerate(self.tool_calls):
            c["id"] = c["id"] or f"call_{int(time.time())}_{n}"


class ToolThread(QThread):
    """在后台运行耗时的 skill 工具（解析 PDF、编译 LaTeX），避免界面卡住。"""
    done = pyqtSignal(str, object)

    def __init__(self, name, args, cwd, mcp=None):
        super().__init__()
        self.name, self.args, self.cwd, self.mcp = name, args, cwd, mcp
        self.ident = None
        self.cancel = threading.Event()   # MCP 调用可被“停止”打断

    def run(self):
        self.ident = threading.get_ident()
        try:
            if self.name.startswith("mcp__"):
                result, info = self.mcp.call(self.name, self.args, self.cancel)
            else:
                run = run_shell_tool if self.name in SHELL_TOOL_NAMES else run_skill_tool
                result, info = run(self.name, self.args, self.cwd)
        except Exception as e:
            result, info = f"错误：{e}", {"status": "error", "preview": str(e)}
        self.done.emit(result, info)


# ---------------------------------------------------------------- 设置对话框
class SettingsDialog(QDialog):
    def __init__(self, cfg, parent=None):
        super().__init__(parent)
        self.setWindowTitle("设置")
        self.setMinimumWidth(520)
        form = QFormLayout(self)
        form.setContentsMargins(22, 20, 22, 18)
        form.setVerticalSpacing(12)

        self.key = QLineEdit(cfg["api_key"])
        self.key.setEchoMode(QLineEdit.EchoMode.Password)
        self.key.setPlaceholderText("在智谱开放平台获取的 API Key")
        show = QCheckBox("显示")
        show.toggled.connect(lambda on: self.key.setEchoMode(
            QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password))
        key_row = QHBoxLayout()
        key_row.addWidget(self.key)
        key_row.addWidget(show)
        form.addRow("API Key", key_row)

        self.base = QComboBox()
        self.base.setEditable(True)
        for name, url in BASE_URLS.items():
            self.base.addItem(f"{name}  {url}", url)
        idx = self.base.findData(cfg["base_url"])
        if idx >= 0:
            self.base.setCurrentIndex(idx)
        else:
            self.base.setEditText(cfg["base_url"])
        form.addRow("接口地址", self.base)

        self.model = QLineEdit(cfg["model"])
        form.addRow("模型名称", self.model)

        self.temp = QDoubleSpinBox()
        self.temp.setRange(0, 1)
        self.temp.setSingleStep(0.05)
        self.temp.setValue(float(cfg["temperature"]))
        form.addRow("Temperature", self.temp)

        self.system = QPlainTextEdit(cfg.get("system_prompt", ""))
        self.system.setFixedHeight(90)
        form.addRow("系统提示词", self.system)

        self.auto_memory = QCheckBox("对话结束后自动提炼要点到记忆库（会额外消耗少量额度）")
        self.auto_memory.setChecked(bool(cfg.get("auto_memory", True)))
        form.addRow("记忆库", self.auto_memory)

        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        btns.button(QDialogButtonBox.StandardButton.Save).setText("保存")
        btns.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        form.addRow(btns)

    def values(self):
        base = self.base.currentData()
        if base is None or self.base.currentText() != self.base.itemText(self.base.currentIndex()):
            base = self.base.currentText().strip()  # 用户手动输入的地址
        return {
            "api_key": self.key.text().strip(),
            "base_url": base,
            "model": self.model.text().strip() or DEFAULT_CONFIG["model"],
            "temperature": self.temp.value(),
            "system_prompt": self.system.toPlainText(),
            "auto_memory": self.auto_memory.isChecked(),
        }


# ---------------------------------------------------------------- 消息控件
class RichView(QTextBrowser):
    """高度随内容自适应、无滚动条的 Markdown 显示区。"""

    def __init__(self):
        super().__init__()
        self.codes = []
        self.setOpenLinks(False)
        self.anchorClicked.connect(self._on_anchor)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setStyleSheet("QTextBrowser { background: transparent; border: none; }")
        doc = self.document()
        doc.setDefaultStyleSheet(DOC_CSS)
        doc.setDocumentMargin(0)
        doc.documentLayout().documentSizeChanged.connect(self._fit)

    def set_markdown(self, text):
        h, self.codes = md_to_html(text)
        self.setHtml(h)

    def _fit(self, *_):
        self.setFixedHeight(math.ceil(self.document().size().height()) + 4)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._fit()

    def wheelEvent(self, e):
        e.ignore()  # 交给外层滚动区

    def _on_anchor(self, url):
        s = url.toString()
        if s.startswith("copy:"):
            QApplication.clipboard().setText(self.codes[int(s[5:])])
            QToolTip.showText(QCursor.pos(), "已复制代码")
        else:
            QDesktopServices.openUrl(url)


class UserMessage(QWidget):
    def __init__(self, m):
        super().__init__()
        self.text = m["content"]
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 14, 0, 0)
        v.setSpacing(6)
        if m.get("files"):
            row = QHBoxLayout()
            row.addStretch()
            for f in m["files"]:
                tag = QLabel("📎 " + os.path.basename(f["path"]), objectName="fileTag")
                tag.setToolTip(f["path"])
                row.addWidget(tag)
            v.addLayout(row)
        row = QHBoxLayout()
        row.addStretch()
        self.bubble = QLabel(self.text, objectName="userBubble")
        self.bubble.setTextFormat(Qt.TextFormat.PlainText)
        self.bubble.setWordWrap(True)
        self.bubble.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        row.addWidget(self.bubble)
        v.addLayout(row)

    def fit(self, avail):
        fm = self.bubble.fontMetrics()
        natural = max((fm.horizontalAdvance(l) for l in self.text.split("\n")), default=0) + 34
        self.bubble.setFixedWidth(max(40, min(natural, int(avail * 0.78))))


class AssistantMessage(QWidget):
    def __init__(self, m, model, show_head=True):
        super().__init__()
        self._auto_fold = True
        self._thinking_now = False
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 8 if show_head else 0, 0, 0)
        v.setSpacing(6)

        if show_head:  # 同一轮回答里的后续步骤不再重复显示头像
            head = QHBoxLayout()
            head.setSpacing(8)
            avatar = QLabel("✦", objectName="avatar")
            avatar.setFixedSize(24, 24)
            avatar.setAlignment(Qt.AlignmentFlag.AlignCenter)
            head.addWidget(avatar)
            head.addWidget(QLabel("GLM", objectName="msgName"))
            head.addWidget(QLabel(m.get("model") or model, objectName="msgModel"))
            head.addStretch()
            v.addLayout(head)

        body = QVBoxLayout()
        body.setContentsMargins(32, 0, 0, 0)
        body.setSpacing(6)
        self.think_btn = QToolButton(objectName="thinkBtn")
        self.think_btn.setCheckable(True)
        self.think_btn.toggled.connect(self._on_think_toggled)
        self.think_btn.clicked.connect(lambda: setattr(self, "_auto_fold", False))
        self.think_lbl = QLabel(objectName="thinkText")
        self.think_lbl.setWordWrap(True)
        self.think_lbl.setTextFormat(Qt.TextFormat.PlainText)
        self.think_lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.view = RichView()
        self.err = QLabel(objectName="errorBox")
        self.err.setWordWrap(True)
        self.err.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        foot = QHBoxLayout()
        self.copy_btn = QPushButton("复制", objectName="ghostBtn")
        self.copy_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.copy_btn.clicked.connect(self._copy)
        foot.addWidget(self.copy_btn)
        self.time_lbl = QLabel(objectName="statusLine")
        foot.addWidget(self.time_lbl)
        foot.addStretch()
        self.status_lbl = QLabel(objectName="statusLine")
        for w in (self.think_btn, self.think_lbl, self.view, self.status_lbl, self.err):
            body.addWidget(w, 0, Qt.AlignmentFlag.AlignLeft if w is self.think_btn else Qt.AlignmentFlag(0))
        body.addLayout(foot)
        v.addLayout(body)
        self.update_msg(m, streaming=False)

    def _on_think_toggled(self, on):
        self.think_lbl.setVisible(on)
        self._set_think_text()

    def _set_think_text(self):
        arrow = "▾" if self.think_btn.isChecked() else "▸"
        label = "正在思考…" if self._thinking_now else "思考过程"
        self.think_btn.setText(f"{arrow} {label}")

    def update_msg(self, m, streaming):
        self.content = m.get("content", "")
        reasoning = m.get("reasoning", "").strip()
        self._thinking_now = bool(streaming and reasoning and not self.content)
        self.think_btn.setVisible(bool(reasoning))
        if reasoning:
            self.think_lbl.setText(reasoning)
            if self._auto_fold:
                self.think_btn.setChecked(bool(self._thinking_now))
        self.think_lbl.setVisible(bool(reasoning) and self.think_btn.isChecked())
        self._set_think_text()

        if self.content:
            self.view.set_markdown(self.content)
        self.view.setVisible(bool(self.content))
        if streaming and not self.status_lbl.text():
            self.status_lbl.setText("◐ 等待模型响应…")
        self.status_lbl.setVisible(streaming)

        self.err.setText(m.get("error", ""))
        self.err.setVisible(bool(m.get("error")))
        self.copy_btn.setVisible(not streaming and bool(self.content))
        t = m.get("timing")
        self.time_lbl.setText(f"⏱ 本步 {t['total']:.0f} 秒" + (f"（思考 {t['think']:.0f} 秒）" if t.get("think") else "")
                              if t else "")
        self.time_lbl.setVisible(bool(t) and not streaming)

    def set_status(self, text):
        self.status_lbl.setText(text)

    def _copy(self):
        QApplication.clipboard().setText(self.content)
        QToolTip.showText(QCursor.pos(), "已复制")


class ToolCard(QWidget):
    """一次文件操作的记录卡片：摘要、状态、可展开的详情、撤销。"""
    STATUS = {"ok": ("✓ 完成", "#1a7f37"), "rejected": ("已拒绝", MUTED), "error": ("失败", "#cf222e"),
              "undone": ("已撤销", MUTED), "skipped": ("已跳过", MUTED), "running": ("运行中…", ACCENT)}

    def __init__(self, m, on_undo):
        super().__init__()
        self.m = m
        outer = QHBoxLayout(self)
        outer.setContentsMargins(32, 0, 0, 0)
        card = QFrame(objectName="toolCard")
        outer.addWidget(card)
        v = QVBoxLayout(card)
        v.setContentsMargins(12, 6, 8, 6)
        v.setSpacing(4)

        head = QHBoxLayout()
        name = m.get("name", "")
        icon, verb = {**TOOL_LABELS, **SKILL_LABELS, **SHELL_LABELS, **MEMORY_LABELS}.get(
            name, ("🔌", "MCP") if name.startswith("mcp__") else ("🔧", name or "工具"))
        if m.get("name") == "write_file" and m.get("new_file"):
            verb = "新建"
        text = f"{icon}&nbsp; {verb} <b>{html.escape(m.get('path') or '')}</b>"
        if "added" in m:
            text += (f"&nbsp; <span style='color:#1a7f37'>+{m['added']}</span>"
                     f" <span style='color:#cf222e'>−{m['removed']}</span>")
        summary = QLabel(text)
        summary.setTextFormat(Qt.TextFormat.RichText)
        self.status = QLabel(objectName="toolStatus")
        self.undo_btn = QPushButton("撤销", objectName="ghostBtn")
        self.undo_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.undo_btn.clicked.connect(lambda: on_undo(self))
        self.toggle = QToolButton(objectName="thinkBtn")
        self.toggle.setCheckable(True)
        self.toggle.setText("详情 ▸")
        self.toggle.toggled.connect(self._on_toggle)
        head.addWidget(summary, 1)
        head.addWidget(self.status)
        self.open_btn = QPushButton("打开 PDF", objectName="ghostBtn")
        self.open_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.open_btn.clicked.connect(lambda: os.startfile(self.m["open_path"]))
        head.addWidget(self.undo_btn)
        head.addWidget(self.open_btn)
        head.addWidget(self.toggle)
        v.addLayout(head)
        self.detail = RichView()
        self.detail.hide()
        v.addWidget(self.detail)
        self.refresh()

    def _on_toggle(self, on):
        self.toggle.setText("详情 ▾" if on else "详情 ▸")
        if on and not self.detail.toPlainText():
            if self.m.get("diff"):
                self.detail.setHtml(diff_html(self.m["diff"]))
            else:
                self.detail.setHtml('<pre style="margin:0;white-space:pre-wrap;font-family:Consolas,monospace;'
                                    f'font-size:9.5pt;color:#55524b">{html.escape(self.m.get("preview", ""))}</pre>')
        self.detail.setVisible(on)

    def set_running(self, text):
        if self.m.get("status") == "running":
            self.status.setText(text)

    def refresh(self):
        text, color = self.STATUS.get(self.m.get("status"), ("", MUTED))
        self.status.setText(text)
        self.status.setStyleSheet(f"color: {color};")
        self.undo_btn.setVisible(self.m.get("status") == "ok" and ("abs" in self.m or "move_from" in self.m))
        self.open_btn.setVisible(bool(self.m.get("open_path")) and os.path.exists(self.m.get("open_path", "")))
        self.toggle.setVisible(bool(self.m.get("diff") or self.m.get("preview")))


class ChatArea(QScrollArea):
    MAX_W = 820

    def __init__(self):
        super().__init__()
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        inner = QWidget(objectName="chatInner")
        h = QHBoxLayout(inner)
        h.setContentsMargins(24, 0, 24, 0)
        col = QWidget()
        col.setMaximumWidth(self.MAX_W)
        h.addStretch(1)
        h.addWidget(col, 20)
        h.addStretch(1)
        self.lay = QVBoxLayout(col)
        self.lay.setContentsMargins(0, 24, 0, 32)
        self.lay.setSpacing(10)
        self.lay.addStretch()
        self.setWidget(inner)

    def avail(self):
        return min(self.MAX_W, self.viewport().width() - 48)

    def add(self, w):
        self.lay.insertWidget(self.lay.count() - 1, w)
        if isinstance(w, UserMessage):
            w.fit(self.avail())

    def clear(self):
        while self.lay.count() > 1:
            self.lay.takeAt(0).widget().deleteLater()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        for i in range(self.lay.count() - 1):
            w = self.lay.itemAt(i).widget()
            if isinstance(w, UserMessage):
                w.fit(self.avail())

    def at_bottom(self):
        bar = self.verticalScrollBar()
        return bar.value() >= bar.maximum() - 60

    def scroll_bottom(self):
        QTimer.singleShot(30, lambda: self.verticalScrollBar().setValue(self.verticalScrollBar().maximum()))


# ---------------------------------------------------------------- 目录面板
class FilePanel(QFrame):
    attach = pyqtSignal(str)

    def __init__(self):
        super().__init__(objectName="filePanel")
        self.setFixedWidth(260)
        self.root = ""
        v = QVBoxLayout(self)
        v.setContentsMargins(12, 12, 8, 12)
        v.setSpacing(4)
        head = QHBoxLayout()
        self.title = QLabel(objectName="panelTitle")
        open_btn = QPushButton("打开", objectName="ghostBtn")
        open_btn.setToolTip("在资源管理器中打开")
        open_btn.clicked.connect(lambda: self.root and open_in_explorer(self.root))
        head.addWidget(self.title, 1)
        head.addWidget(open_btn)
        v.addLayout(head)
        self.path_lbl = QLabel(objectName="panelPath")
        v.addWidget(self.path_lbl)

        self.model = QFileSystemModel()
        self.model.setFilter(QDir.Filter.AllEntries | QDir.Filter.NoDotAndDotDot)
        self.tree = QTreeView(objectName="fileTree")
        self.tree.setModel(self.model)
        for col in (1, 2, 3):
            self.tree.hideColumn(col)
        self.tree.setHeaderHidden(True)
        self.tree.setAnimated(True)
        self.tree.doubleClicked.connect(self._on_double)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._menu)
        v.addWidget(self.tree, 1)
        tip = QLabel("双击文件添加到对话", objectName="panelPath")
        v.addWidget(tip)

    def set_root(self, path):
        self.root = path
        self.title.setText("📁 " + os.path.basename(path.rstrip("\\/")) if path else "")
        self.path_lbl.setText(self.path_lbl.fontMetrics().elidedText(path, Qt.TextElideMode.ElideMiddle, 230))
        self.path_lbl.setToolTip(path)
        if path:
            self.tree.setRootIndex(self.model.setRootPath(path))

    def _on_double(self, idx):
        if not self.model.isDir(idx):
            self.attach.emit(self.model.filePath(idx))

    def _menu(self, pos):
        idx = self.tree.indexAt(pos)
        if not idx.isValid():
            return
        path = self.model.filePath(idx)
        menu = QMenu(self)
        if not self.model.isDir(idx):
            menu.addAction("添加到对话", lambda: self.attach.emit(path))
            menu.addAction("用默认程序打开", lambda: os.startfile(path))
        menu.addAction("在资源管理器中显示", lambda: open_in_explorer(path, select=True))
        menu.exec(self.tree.viewport().mapToGlobal(pos))


# ---------------------------------------------------------------- 输入框：Enter 发送
class EnterFilter(QObject):
    def __init__(self, callback):
        super().__init__()
        self.callback = callback

    def eventFilter(self, obj, ev):
        if (ev.type() == QEvent.Type.KeyPress and ev.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
                and not ev.modifiers() & Qt.KeyboardModifier.ShiftModifier):
            self.callback()
            return True
        return False


# ---------------------------------------------------------------- 记忆库 / MCP 界面
class McpRelay(QObject):
    """MCP 后台线程状态变化 → 界面线程。"""
    changed = pyqtSignal()


class Distiller(QThread):
    """后台把一段对话提炼成要点和记忆条目。"""
    done = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, cfg, text):
        super().__init__()
        self.cfg, self.text = cfg, text

    def run(self):
        try:
            out = chat_once(self.cfg, [{"role": "system", "content": DISTILL_PROMPT},
                                       {"role": "user", "content": self.text}], effort="low")
            self.done.emit(parse_json_object(out))
        except Exception as e:
            self.failed.emit(str(e))


class SummaryDivider(QPushButton):
    """聊天里标出“以上较早的对话已压缩”的位置，点击查看摘要。"""

    def __init__(self, summary):
        super().__init__("—— 以上较早的对话已压缩为摘要发送给模型（点击查看摘要）——", objectName="divider")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.clicked.connect(lambda: QMessageBox.information(self.window(), "较早对话的摘要", summary or "（空）"))


class MemoryDialog(QDialog):
    """查看、搜索、编辑、删除记忆条目。"""

    def __init__(self, store, parent=None):
        super().__init__(parent)
        self.store, self.cur_id = store, None
        self.setWindowTitle("记忆库")
        self.resize(980, 640)
        v = QVBoxLayout(self)
        v.setContentsMargins(18, 14, 18, 14)
        top = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索标题、内容、标签（如器件型号）…")
        self.search.textChanged.connect(lambda: self.refresh())
        self.type_filter = QComboBox()
        self.type_filter.addItems(["全部类型"] + MEMORY_TYPES)
        self.type_filter.currentIndexChanged.connect(lambda: self.refresh())
        self.count = QLabel()
        self.count.setStyleSheet(f"color: {MUTED};")
        top.addWidget(self.search, 1)
        top.addWidget(self.type_filter)
        top.addWidget(self.count)
        v.addLayout(top)

        body = QHBoxLayout()
        self.list = QListWidget()
        self.list.setFixedWidth(340)
        self.list.currentRowChanged.connect(self.load)
        body.addWidget(self.list)
        form = QFormLayout()
        self.f_type = QComboBox()
        self.f_type.addItems(MEMORY_TYPES)
        self.f_title = QLineEdit()
        self.f_tags = QLineEdit()
        self.f_tags.setPlaceholderText("用逗号分隔，如 AD8421, 仪表放大器")
        self.f_content = QPlainTextEdit()
        self.f_meta = QLabel()
        self.f_meta.setStyleSheet(f"color: {MUTED}; font-size: 9pt;")
        form.addRow("类型", self.f_type)
        form.addRow("标题", self.f_title)
        form.addRow("标签", self.f_tags)
        form.addRow("内容", self.f_content)
        form.addRow("", self.f_meta)
        right = QWidget()
        right.setLayout(form)
        body.addWidget(right, 1)
        v.addLayout(body, 1)

        row = QHBoxLayout()
        for text, fn in (("新建", self.new), ("删除", self.delete)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            row.addWidget(b)
        row.addStretch()
        save = QPushButton("保存修改", objectName="primaryBtn")
        save.clicked.connect(self.save)
        close = QPushButton("关闭")
        close.clicked.connect(self.accept)
        row.addWidget(save)
        row.addWidget(close)
        v.addLayout(row)
        self.refresh()

    def refresh(self, select_id=None):
        q = self.search.text().strip()
        items = self.store.search(q, 500) if q else sorted(self.store.items, key=lambda m: m.get("updated", ""),
                                                           reverse=True)
        t = self.type_filter.currentText()
        if t != "全部类型":
            items = [m for m in items if m["type"] == t]
        self.list.blockSignals(True)
        self.list.clear()
        row_to_select = 0
        for i, m in enumerate(items):
            it = QListWidgetItem(f"[{m['type']}] {m['title']}")
            it.setData(Qt.ItemDataRole.UserRole, m["id"])
            it.setToolTip(m["content"][:500])
            self.list.addItem(it)
            if m["id"] == (select_id or self.cur_id):
                row_to_select = i
        self.list.blockSignals(False)
        self.count.setText(f"共 {len(self.store.items)} 条，显示 {len(items)} 条")
        if items:
            self.list.setCurrentRow(row_to_select)
            self.load(row_to_select)
        else:
            self.new()

    def load(self, row):
        it = self.list.item(row)
        m = self.store.get(it.data(Qt.ItemDataRole.UserRole)) if it else None
        if not m:
            return
        self.cur_id = m["id"]
        self.f_type.setCurrentText(m["type"])
        self.f_title.setText(m["title"])
        self.f_tags.setText(", ".join(m.get("tags", [])))
        self.f_content.setPlainText(m["content"])
        self.f_meta.setText(f"#{m['id']} · 创建 {m.get('created', '')} · 更新 {m.get('updated', '')}")

    def new(self):
        self.cur_id = None
        self.list.clearSelection()
        for w in (self.f_title, self.f_tags):
            w.clear()
        self.f_content.clear()
        self.f_meta.setText("新条目")
        self.f_title.setFocus()

    def save(self):
        title, content = self.f_title.text().strip(), self.f_content.toPlainText().strip()
        if not title or not content:
            QMessageBox.information(self, "记忆库", "标题和内容不能为空。")
            return
        tags = [t.strip() for t in self.f_tags.text().replace("，", ",").split(",") if t.strip()]
        m = self.store.get(self.cur_id) if self.cur_id else None
        if m:
            m.update(type=self.f_type.currentText(), title=title, content=content, tags=tags,
                     updated=time.strftime("%Y-%m-%d %H:%M"))
        else:
            m, _ = self.store.upsert(title, content, self.f_type.currentText(), tags, "手动添加")
        self.store.save()
        self.cur_id = m["id"]
        self.refresh(m["id"])

    def delete(self):
        m = self.store.get(self.cur_id) if self.cur_id else None
        if not m:
            return
        if QMessageBox.question(self, "删除记忆", f"删除「{m['title']}」？") == QMessageBox.StandardButton.Yes:
            self.store.delete(m["id"])
            self.store.save()
            self.cur_id = None
            self.refresh()


class McpConfigDialog(QDialog):
    """直接编辑 mcp_servers.json，保存前校验格式。"""

    def __init__(self, path, parent=None):
        super().__init__(parent)
        self.path = path
        self.setWindowTitle("编辑 MCP 配置")
        self.resize(820, 600)
        v = QVBoxLayout(self)
        v.setContentsMargins(18, 14, 18, 14)
        tip = QLabel("格式与主流 MCP 客户端通用的 <b>mcpServers</b> 相同，可以直接粘贴别处的配置。"
                     "本地服务器写 <code>command</code> + <code>args</code>；远程服务器写 <code>url</code>"
                     "（旧版 SSE 服务器加 <code>\"type\": \"sse\"</code>）。可选：<code>env</code>、<code>headers</code>、"
                     "<code>disabled</code>、<code>timeout</code>。")
        tip.setWordWrap(True)
        v.addWidget(tip)
        self.edit = QPlainTextEdit()
        self.edit.setStyleSheet("QPlainTextEdit { font-family: Consolas, monospace; font-size: 10pt; }")
        try:
            with open(path, encoding="utf-8-sig") as f:
                self.edit.setPlainText(f.read())
        except OSError:
            self.edit.setPlainText('{\n  "mcpServers": {}\n}')
        v.addWidget(self.edit, 1)
        row = QHBoxLayout()
        row.addStretch()
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        ok = QPushButton("保存并重新连接", objectName="primaryBtn")
        ok.clicked.connect(self.save)
        row.addWidget(cancel)
        row.addWidget(ok)
        v.addLayout(row)

    def save(self):
        text = self.edit.toPlainText()
        try:
            data = json.loads(text)
            servers = data.get("mcpServers", data.get("servers")) if isinstance(data, dict) else None
            if not isinstance(servers, dict):
                raise ValueError("顶层需要有 \"mcpServers\": { ... }")
            for name, c in servers.items():
                if not isinstance(c, dict) or not (c.get("command") or c.get("url")):
                    raise ValueError(f"服务器「{name}」需要 command 或 url")
        except ValueError as e:
            QMessageBox.warning(self, "配置有误", f"无法保存：{e}")
            return
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, self.path)
        self.accept()


class McpDialog(QDialog):
    """MCP 服务器状态与工具列表。"""
    STATUS = {"ok": "✓ 已连接", "connecting": "连接中…", "pending": "等待连接", "error": "✕ 连接失败",
              "disabled": "已停用"}

    def __init__(self, mgr, relay, parent=None):
        super().__init__(parent)
        self.mgr, self.relay = mgr, relay
        self.setWindowTitle("MCP 服务")
        self.resize(900, 580)
        v = QVBoxLayout(self)
        v.setContentsMargins(18, 14, 18, 14)
        info = QLabel(f"配置文件：{mgr.config_path}<br>启动时自动连接所有服务器，GLM 调用工具时直接执行。")
        info.setWordWrap(True)
        info.setStyleSheet(f"color: {MUTED};")
        v.addWidget(info)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["服务器 / 工具", "状态", "说明"])
        self.tree.setColumnWidth(0, 260)
        self.tree.setColumnWidth(1, 110)
        v.addWidget(self.tree, 1)
        row = QHBoxLayout()
        for text, fn in (("编辑配置…", self.edit), ("重新连接", self.reconnect),
                         ("打开所在文件夹", lambda: open_in_explorer(self.mgr.config_path, select=True))):
            b = QPushButton(text)
            b.clicked.connect(fn)
            row.addWidget(b)
        row.addStretch()
        close = QPushButton("关闭")
        close.clicked.connect(self.accept)
        row.addWidget(close)
        v.addLayout(row)
        relay.changed.connect(self.refresh)
        self.finished.connect(lambda: relay.changed.disconnect(self.refresh))
        self.refresh()

    def refresh(self):
        self.tree.clear()
        if self.mgr.config_error:
            QTreeWidgetItem(self.tree, ["配置文件", "✕ 有误", self.mgr.config_error])
        if not self.mgr.servers and not self.mgr.config_error:
            QTreeWidgetItem(self.tree, ["（还没有配置 MCP 服务器）", "", "点「编辑配置…」添加"])
        for s in self.mgr.servers.values():
            if s.status == "ok":
                srv = s.info.get("name", "")
                desc = f"{s.kind} · {srv} {s.info.get('version', '')} · {len(s.tools)} 个工具".replace("  ", " ")
            elif s.status == "error":
                desc = s.error
            else:
                desc = s.kind
            top = QTreeWidgetItem(self.tree, [s.name, self.STATUS.get(s.status, s.status), desc])
            top.setToolTip(2, desc)
            for t in s.tools:
                ann = t.get("annotations") or {}
                mode = "只读" if ann.get("readOnlyHint") else "可修改"
                d = (t.get("description") or "").strip().splitlines()
                child = QTreeWidgetItem(top, [t["name"], mode, d[0][:160] if d else ""])
                child.setToolTip(2, t.get("description") or "")
            top.setExpanded(s.status == "ok" and len(s.tools) <= 12)

    def edit(self):
        if McpConfigDialog(self.mgr.config_path, self).exec():
            self.reconnect()

    def reconnect(self):
        self.mgr.start_all()
        self.mgr.tool_specs()
        self.refresh()


# ---------------------------------------------------------------- 单个对话的运行
def guarded(fn):
    """对话流程里的任何异常都只让这个对话中止并显示错误，不会让程序闪退或卡在“运行中”。"""
    @functools.wraps(fn)
    def wrapper(self, *args):
        try:
            return fn(self, *args)
        except Exception as e:
            log.exception("对话「%s」在 %s 中出错", self.conv.get("title"), fn.__name__)
            self.fail(e)
    return wrapper


class Session(QObject):
    """一个对话的运行状态：请求模型、执行工具、停止。每个对话一个，可同时运行。"""

    def __init__(self, win, conv):
        super().__init__(win)
        self.win, self.conv = win, conv
        self.busy = False
        self.stop_requested = False
        self.worker = None
        self.tool_threads = []   # 正在后台执行的工具线程（只读工具可以多个同时跑）
        self.batch_left = 0      # 本批还没完成的后台工具数
        self.reply = None
        self.pending_calls = []
        self.rounds = 0
        self.dirty = False
        self.last_flush = 0.0
        self.running = []        # 正在后台执行的工具消息（用于显示已运行秒数）
        self.progress = None     # 当前这轮正在生成的工具调用进度
        self.started = 0.0

    def set_busy(self, busy):
        self.busy = busy
        self.win.on_session_state(self)

    @guarded
    def start(self):
        self.rounds = 0
        self.stop_requested = False
        self.set_busy(True)
        log.info("对话「%s」开始运行", self.conv.get("title"))
        self.start_round()

    def stop(self):
        if not self.busy:
            return
        self.stop_requested = True
        if self.worker:
            self.worker.stop()
        for th in list(self.tool_threads):
            th.cancel.set()
            if th.ident:
                kill_current(th.ident)
                kill_shell(th.ident)

    def wait(self):
        """退出程序前等后台线程结束。"""
        if self.worker:
            self.worker.wait(3000)
        for th in list(self.tool_threads):
            th.wait(5000)

    def fail(self, e):
        """内部错误：中止本轮、在界面上说明原因、恢复为可再次发送的状态。"""
        try:
            self.stop_requested = True
            self.pending_calls = []
            if self.worker:
                self.worker.stop()
            msg = (f"出错：程序内部错误（{type(e).__name__}: {e}），本轮已中止，可以重新发送。"
                   f"详细信息已写入 logs\\app.log")
            if self.reply is not None:
                self.reply["error"] = msg
                self.win.refresh_message(self.conv, self.reply, streaming=False)
            else:
                m = {"role": "assistant", "content": "", "reasoning": "", "error": msg}
                self.conv["messages"].append(m)
                self.win.show_message(self.conv, m)
            if not self.worker and not self.tool_threads:  # 否则等线程结束时再收尾
                self.end_turn()
        except Exception:
            log.exception("fail() 自身出错")
            self.busy = False

    @guarded
    def start_round(self):
        """一轮请求：模型可能直接回答，也可能调用工具；工具执行完再开始下一轮。"""
        win, conv = self.win, self.conv
        self.reply = None
        job = win.compression_job(conv)   # 历史太长时，先把较早的部分压缩成摘要
        if job:
            material, cut_at = job
            messages = win.build_messages(conv, upto=cut_at, summary=SUMMARY_MARK)
        else:
            material, cut_at = None, None
            messages = win.build_messages(conv)
        tools = win.tools_for(conv)
        self.reply = {"role": "assistant", "content": "", "reasoning": "", "model": win.cfg["model"]}
        self.started = time.time()
        conv["messages"].append(self.reply)
        win.show_message(conv, self.reply, streaming=True)

        self.worker = ChatWorker(dict(win.cfg), messages, tools, compress=material)
        if job:
            self.worker.summarized.connect(lambda text, c=cut_at: self.on_summarized(text, c))
        self.worker.reasoning.connect(self.on_reasoning)
        self.worker.content.connect(self.on_content)
        self.worker.failed.connect(self.on_failed)
        self.worker.notice.connect(self.on_notice)
        self.worker.tool_progress.connect(self.on_tool_progress)
        self.worker.finished.connect(self.on_finished)
        self.worker.start()

    @guarded
    def on_summarized(self, text, cut_at):
        self.conv["summary"] = text
        self.conv["summary_upto"] = cut_at
        log.info("对话「%s」已压缩前 %d 条消息", self.conv.get("title"), cut_at)
        self.win.save_convs()
        self.win.on_compressed(self.conv)

    @guarded
    def on_tool_progress(self, d):
        self.progress = d

    @guarded
    def on_notice(self, s):
        if self.reply is not None:
            self.reply["notice"] = s
            self.dirty = True

    @guarded
    def on_reasoning(self, s):
        self.reply.pop("notice", None)
        self.reply["reasoning"] += s
        self.dirty = True

    @guarded
    def on_content(self, s):
        self.reply.pop("notice", None)
        self.reply["content"] += s
        self.dirty = True

    @guarded
    def on_failed(self, msg):
        self.reply["error"] = "出错：" + msg
        self.dirty = True

    @guarded
    def on_finished(self):
        worker, self.worker = self.worker, None
        if worker is None:
            return
        worker.deleteLater()
        reply = self.reply
        self.progress = None
        if reply is None:  # 已经由 fail() 处理过
            return self.end_turn()
        reply.pop("notice", None)
        if worker.timing:
            reply["timing"] = worker.timing
        calls = [] if (self.stop_requested or reply.get("error")) else worker.tool_calls
        if calls:
            reply["tool_calls"] = calls
        elif not reply["content"] and not reply.get("error"):
            reply["content"] = "（已停止）" if self.stop_requested else "（没有返回内容）"
        self.dirty = False
        self.win.refresh_message(self.conv, reply, streaming=False)
        if calls:
            self.pending_calls = list(calls)
            return self.run_next_call()
        self.end_turn()

    def end_turn(self):
        self.reply = None
        self.pending_calls = []
        try:
            self.win.save_convs()
        finally:
            self.set_busy(False)
            log.info("对话「%s」结束", self.conv.get("title"))
        try:
            self.win.on_turn_finished(self.conv)
        except Exception:
            log.exception("安排记忆提炼失败")

    # 可以同时执行的只读工具：一次回复里连续出现的这类调用会并行跑
    PARALLEL = {"read_pdf", "search_pdf", "pdf_info", "diff_pdfs", "get_report_template", "check_env",
                "find_files", "grep_files", "list_dir", "read_file", "memory_search"}

    def _parallel_ok(self, call):
        name = call["function"]["name"]
        if name in self.PARALLEL:
            return True
        if name.startswith("mcp__"):
            _, t = self.win.mcp.tool_info(name)
            return bool(t and (t.get("annotations") or {}).get("readOnlyHint"))
        return False

    @guarded
    def run_next_call(self):
        if not self.pending_calls:  # 本轮工具都执行完了，把结果交给模型继续
            self.win.save_convs()
            self.rounds += 1
            if not self.stop_requested and self.rounds < MAX_ROUNDS:
                return self.start_round()
            return self.end_turn()
        batch = [self.pending_calls.pop(0)]
        if self._parallel_ok(batch[0]):
            while self.pending_calls and self._parallel_ok(self.pending_calls[0]) and len(batch) < 8:
                batch.append(self.pending_calls.pop(0))
        if len(batch) > 1:
            log.info("对话「%s」并行执行 %d 个工具", self.conv.get("title"), len(batch))
        self.batch_left = 0
        for call in batch:
            self._start_call(call)
        if self.batch_left == 0:  # 全是即时完成的工具
            QTimer.singleShot(0, self.run_next_call)

    def _start_call(self, call):
        name = call["function"]["name"]
        try:
            args = json.loads(call["function"]["arguments"] or "{}")
        except ValueError:
            args = None
        if name in SKILL_TOOL_NAMES:
            disp = tool_display(name, args)
        elif name in SHELL_TOOL_NAMES:
            disp = shell_display(name, args)
        elif name in MEMORY_TOOL_NAMES:
            a = args if isinstance(args, dict) else {}
            disp = str(a.get("query") or a.get("title") or f"#{a.get('id', '')}")
        elif name.startswith("mcp__"):
            disp = self.win.mcp.display(name, args)
        else:
            disp = (args.get("path") if isinstance(args, dict) else None) or "."
        m = {"role": "tool", "tool_call_id": call["id"], "name": name, "path": disp}
        self.conv["messages"].append(m)
        self.win.show_message(self.conv, m)
        if self.stop_requested:
            return self._finish(m, "用户已停止，未执行。", {"status": "skipped"})
        if args is None:
            return self._finish(m, "错误：工具参数不是合法的 JSON，请重新生成这次调用。",
                                {"status": "error", "preview": call["function"]["arguments"][:2000]})
        log.info("对话「%s」执行工具 %s %s", self.conv.get("title"), name, disp[:100])
        if name in SLOW_TOOLS or name in SHELL_SLOW or name.startswith("mcp__"):
            m["status"], m["_started"] = "running", time.time()
            self.running.append(m)
            self.win.refresh_message(self.conv, m)
            th = ToolThread(name, args, self.conv.get("cwd", ""), self.win.mcp)
            self.tool_threads.append(th)
            self.batch_left += 1
            th.done.connect(lambda r, info, m=m, th=th: self.thread_done(th, m, r, info))
            th.start()
            return
        try:
            result, info = self.win.run_tool(self, name, args)
        except (ToolError, OSError, ValueError) as e:
            result, info = f"错误：{e}", {"status": "error", "preview": str(e)}
        self._finish(m, result, info)

    def _finish(self, m, result, info):
        if m in self.running:
            self.running.remove(m)
        m.pop("_started", None)
        m["content"] = result
        m.update(info)
        if info.get("status") == "error":
            log.warning("工具 %s 失败：%s", m.get("name"), str(result)[:300])
        self.win.refresh_message(self.conv, m)

    @guarded
    def thread_done(self, th, m, result, info):
        th.wait()
        th.deleteLater()
        if th in self.tool_threads:
            self.tool_threads.remove(th)
        self._finish(m, result, info)
        self.batch_left -= 1
        if self.batch_left <= 0:
            QTimer.singleShot(0, self.run_next_call)


# ---------------------------------------------------------------- 主窗口
class MainWindow(QMainWindow):
    SUGGESTIONS = ["对比目录里所有 datasheet，生成 PDF 对比报告", "检查报告编译环境是否就绪", "帮我写一个 Python 脚本"]

    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.resize(1240, 800)

        self.cfg = {**DEFAULT_CONFIG, **load_json(CONFIG_PATH, {})}
        for k in ("auto_apply", "auto_shell"):  # 旧版本的开关，现在所有操作都直接执行
            self.cfg.pop(k, None)
        self.convs = self._valid_convs(load_json(CONVS_PATH, []))
        self.cur = None
        self.memory = MemoryStore(MEMORY_PATH, load_json, save_json)
        self.distill_queue = []      # 等待提炼要点的对话 id
        self.distiller = None
        self.mcp = MCPManager(MCP_PATH)
        self.mcp_relay = McpRelay()
        self.mcp.on_change = self.mcp_relay.changed.emit   # 后台线程 → 界面线程
        self._save_warned = False
        self.sessions = {}   # 对话 id -> Session
        self.widgets = {}    # 当前对话里 id(消息) -> 控件
        self.pending_files = []

        central = QWidget(objectName="main")
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._build_sidebar())
        root.addWidget(self._build_main(), 1)
        self.file_panel = FilePanel()
        self.file_panel.attach.connect(self.add_file)
        root.addWidget(self.file_panel)
        self.setCentralWidget(central)

        self.timer = QTimer(self, interval=80)
        self.timer.timeout.connect(self.flush)
        self.timer.start()
        QShortcut(QKeySequence("Ctrl+N"), self, self.new_conv)
        QShortcut(QKeySequence("Escape"), self, self.stop_turn)

        if not self.convs:
            self.convs.append(self._blank())
        self.refresh_list(0)
        self.update_model_chip()
        if not self.cfg["api_key"]:
            QTimer.singleShot(300, self.open_settings)
        if LOAD_PROBLEMS:
            QTimer.singleShot(400, lambda: QMessageBox.warning(self, "数据恢复", "\n".join(LOAD_PROBLEMS)))
        self.mcp_relay.changed.connect(self.update_mcp_btn)
        self.update_memory_btn()
        QTimer.singleShot(200, self.mcp.start_all)   # 后台连接 MCP 服务器，不阻塞界面

    # ---------- 界面搭建
    def _build_sidebar(self):
        side = QFrame(objectName="sidebar")
        side.setFixedWidth(250)
        v = QVBoxLayout(side)
        v.setContentsMargins(12, 14, 12, 12)
        v.setSpacing(8)
        brand = QHBoxLayout()
        brand.addWidget(QLabel("✦", objectName="brandStar"))
        brand.addWidget(QLabel("GLM-MCP-文档对比agent", objectName="brand"))
        brand.addStretch()
        v.addLayout(brand)
        v.addSpacing(6)
        self.new_btn = QPushButton("＋  新对话", objectName="newBtn")
        self.new_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.new_btn.clicked.connect(self.new_conv)
        v.addWidget(self.new_btn)
        v.addWidget(QLabel("最近", objectName="sectionLabel"))
        self.conv_list = QListWidget(objectName="convList")
        self.conv_list.currentRowChanged.connect(self.select_conv)
        self.conv_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.conv_list.customContextMenuRequested.connect(self.conv_menu)
        v.addWidget(self.conv_list, 1)
        self.mem_btn = QPushButton(objectName="sideBtn")
        self.mem_btn.setToolTip("跨对话保存的器件参数、对比结论、偏好和对话要点")
        self.mem_btn.clicked.connect(self.open_memory)
        self.mcp_btn = QPushButton("🔌  MCP 服务", objectName="sideBtn")
        self.mcp_btn.setToolTip("查看和配置 MCP 服务器")
        self.mcp_btn.clicked.connect(self.open_mcp)
        for b in (self.mem_btn, self.mcp_btn):
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            v.addWidget(b)
        set_btn = QPushButton("⚙  设置", objectName="sideBtn")
        set_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        set_btn.clicked.connect(self.open_settings)
        v.addWidget(set_btn)
        return side

    def _build_main(self):
        main = QWidget(objectName="main")
        v = QVBoxLayout(main)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        top = QFrame(objectName="topBar")
        top.setFixedHeight(52)
        th = QHBoxLayout(top)
        th.setContentsMargins(20, 0, 16, 0)
        self.title_lbl = QLabel(objectName="convTitle")
        self.dir_btn = QPushButton(objectName="dirBtn")
        self.dir_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.dir_menu = QMenu(self)
        self.dir_btn.setMenu(self.dir_menu)
        self.dir_menu.aboutToShow.connect(self._fill_dir_menu)
        th.addWidget(self.title_lbl, 1)
        th.addWidget(self.dir_btn)
        v.addWidget(top)

        self.stack = QStackedWidget()
        self.stack.addWidget(self._build_welcome())
        self.chat = ChatArea()
        self.stack.addWidget(self.chat)
        v.addWidget(self.stack, 1)

        # 输入卡片
        wrap = QHBoxLayout()
        wrap.setContentsMargins(24, 4, 24, 6)
        card = QFrame(objectName="composer")
        card.setMaximumWidth(ChatArea.MAX_W)
        cv = QVBoxLayout(card)
        cv.setContentsMargins(14, 10, 10, 10)
        cv.setSpacing(6)
        self.attach_row = QHBoxLayout()
        self.attach_row.setSpacing(6)
        self.attach_row.addStretch()
        cv.addLayout(self.attach_row)
        self.input = QPlainTextEdit(objectName="input")
        self.input.setPlaceholderText("给 GLM 发送消息…   Enter 发送，Shift+Enter 换行")
        self._enter = EnterFilter(self.on_send)
        self.input.installEventFilter(self._enter)
        self.input.document().documentLayout().documentSizeChanged.connect(self._fit_input)
        self.input.textChanged.connect(self._update_send_state)
        cv.addWidget(self.input)
        bar = QHBoxLayout()
        bar.setSpacing(6)
        file_btn = QPushButton("＋ 文件", objectName="chip")
        file_btn.setToolTip("选择文件添加到对话")
        file_btn.clicked.connect(self.pick_files)
        self.model_chip = QPushButton(objectName="chip")
        self.model_chip.setToolTip("点击修改模型设置")
        self.model_chip.clicked.connect(self.open_settings)
        self.think_chip = QPushButton(objectName="chip")
        self.think_chip.setToolTip("思考强度：low 最快，max 最深入（GLM-5.3 不能关闭思考）")
        effort_menu = QMenu(self)
        for level, desc in (("low", "low  · 快速"), ("high", "high · 均衡"), ("max", "max  · 最深入（推荐）")):
            effort_menu.addAction(desc, lambda level=level: self._set_effort(level))
        self.think_chip.setMenu(effort_menu)
        self.think_chip.setStyleSheet("QPushButton::menu-indicator { image: none; }")
        self._set_effort(self.cfg.get("reasoning_effort") or "max", save=False)
        self.send_btn = QPushButton("↑", objectName="sendBtn")
        self.send_btn.setFixedSize(34, 34)
        self.send_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.send_btn.clicked.connect(self.on_send)
        self.skill_chip = QPushButton("📊 Datasheet 对比", objectName="chip")
        self.skill_chip.setToolTip("内置 skill：对比目录里的 datasheet，生成 LaTeX + PDF 报告")
        self.skill_chip.setCheckable(True)
        self.skill_chip.toggled.connect(self._toggle_skill)
        for w in (file_btn, self.model_chip, self.think_chip, self.skill_chip):
            w.setCursor(Qt.CursorShape.PointingHandCursor)
            bar.addWidget(w)
        bar.addStretch()
        bar.addWidget(self.send_btn)
        cv.addLayout(bar)
        wrap.addStretch(1)
        wrap.addWidget(card, 20)
        wrap.addStretch(1)
        v.addLayout(wrap)
        self.hint_lbl = QLabel(HINT_TEXT, objectName="hint")
        self.hint_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        v.addWidget(self.hint_lbl)
        v.addSpacing(8)
        self._fit_input()
        self._update_send_state()
        return main

    def _build_welcome(self):
        w = QWidget(objectName="main")
        v = QVBoxLayout(w)
        v.addStretch(2)
        star = QLabel("✦", objectName="welcomeStar")
        star.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.greet_lbl = QLabel(objectName="welcomeTitle")
        self.greet_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.welcome_sub = QLabel(objectName="welcomeSub")
        self.welcome_sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        v.addWidget(star)
        v.addWidget(self.greet_lbl)
        v.addWidget(self.welcome_sub)
        v.addSpacing(18)
        row = QHBoxLayout()
        row.addStretch()
        for s in self.SUGGESTIONS:
            b = QPushButton(s, objectName="suggest")
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.clicked.connect(lambda _, s=s: (self.input.setPlainText(s + "\n"), self._focus_input_end()))
            row.addWidget(b)
        row.addStretch()
        v.addLayout(row)
        v.addStretch(3)
        return w

    # ---------- 输入框
    def _focus_input_end(self):
        self.input.setFocus()
        self.input.moveCursor(self.input.textCursor().MoveOperation.End)

    def _fit_input(self, *_):
        lines = max(2, min(10, int(self.input.document().size().height())))
        self.input.setFixedHeight(lines * self.input.fontMetrics().lineSpacing() + 12)

    def _update_send_state(self):
        s = self.cur_session()
        busy = bool(s and s.busy)
        self.send_btn.setText("■" if busy else "↑")
        self.send_btn.setToolTip("停止生成 (Esc)" if busy else "发送 (Enter)")
        self.send_btn.setEnabled(busy or bool(self.input.toPlainText().strip()))

    def _set_effort(self, level, save=True):
        if level not in ("low", "high", "max"):
            level = "max"
        self.cfg["reasoning_effort"] = level
        self.cfg.pop("thinking", None)  # 旧版本的开关，GLM-5.3 不再支持
        self.think_chip.setText(f"思考：{level}  ▾")
        if save:
            save_json(CONFIG_PATH, self.cfg)

    def _toggle_skill(self, on):
        if self.cur is not None and self.cur.get("skill") != on:
            self.cur["skill"] = on
            self.save_convs()
            self.prefetch_dir(self.cur.get("cwd", ""))

    def prefetch_dir(self, cwd):
        """启用了 datasheet skill 的目录：后台提前解析 PDF、检查编译环境，GLM 用到时直接取结果。"""
        if not (cwd and os.path.isdir(cwd) and self.cur and self.skill_on(self.cur)):
            return
        done = self.__dict__.setdefault("_prefetched", set())
        if cwd not in done:
            done.add(cwd)
            threading.Thread(target=prefetch, args=(cwd,), name="prefetch", daemon=True).start()

    def update_model_chip(self):
        self.model_chip.setText(self.cfg["model"] + "  ▾")

    # ---------- 附件
    def pick_files(self):
        start = (self.cur or {}).get("cwd") or ""
        paths, _ = QFileDialog.getOpenFileNames(self, "选择文件", start)
        for p in paths:
            self.add_file(p)

    def add_file(self, path):
        path = os.path.normpath(path)
        if any(f["path"] == path for f in self.pending_files):
            return
        try:
            content, err, _ = read_text_file(path)
        except OSError as e:
            content, err = None, str(e)
        if err:
            QMessageBox.warning(self, "无法添加文件", f"{os.path.basename(path)}：{err}")
            return
        self.pending_files.append({"path": path, "lang": lang_of(path), "content": content})
        self._refresh_attachments()
        self.input.setFocus()

    def _refresh_attachments(self):
        while self.attach_row.count() > 1:
            self.attach_row.takeAt(0).widget().deleteLater()
        for f in self.pending_files:
            b = QPushButton(f"📄 {os.path.basename(f['path'])}  ✕", objectName="attach")
            b.setToolTip(f["path"] + "\n点击移除")
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.clicked.connect(lambda _, f=f: (self.pending_files.remove(f), self._refresh_attachments()))
            self.attach_row.insertWidget(self.attach_row.count() - 1, b)

    # ---------- 工作目录
    def _fill_dir_menu(self):
        m = self.dir_menu
        m.clear()
        cwd = (self.cur or {}).get("cwd")
        m.addAction("选择目录…", self.choose_dir)
        if cwd:
            m.addAction("在资源管理器中打开", lambda: open_in_explorer(cwd))
            m.addAction("隐藏文件面板" if self.file_panel.isVisible() else "显示文件面板",
                        lambda: self.file_panel.setVisible(not self.file_panel.isVisible()))
            m.addSeparator()
            m.addAction("取消关联目录", lambda: self.set_cwd(""))

    def choose_dir(self):
        start = (self.cur or {}).get("cwd") or os.path.expanduser("~")
        d = QFileDialog.getExistingDirectory(self, "选择工作目录", start)
        if d:
            self.set_cwd(os.path.normpath(d))

    def set_cwd(self, path):
        self.cur["cwd"] = path
        if path and "skill" not in self.cur:  # 目录里有 PDF 时默认启用 datasheet 对比 skill
            self.cur["skill"] = any(f.lower().endswith(".pdf") for f in os.listdir(path))
        self.save_convs()
        self.update_dir_ui()

    def update_dir_ui(self):
        cwd = (self.cur or {}).get("cwd", "")
        if cwd and not os.path.isdir(cwd):
            cwd = ""
        self.dir_btn.setText(f"📁 {os.path.basename(cwd.rstrip(chr(92) + '/')) or cwd}  ▾" if cwd else "📁 打开目录  ▾")
        self.dir_btn.setToolTip(cwd or "为当前对话关联一个工作目录")
        self.file_panel.set_root(cwd)
        self.file_panel.setVisible(bool(cwd))
        self.prefetch_dir(cwd)
        self.skill_chip.setVisible(bool(cwd) and skill_dir() is not None)
        self.skill_chip.setChecked(bool((self.cur or {}).get("skill")))
        self.input.setPlaceholderText(
            "让 GLM 操作目录里的文件、运行命令，或者直接提问…   Enter 发送" if cwd
            else "给 GLM 发送消息…   Enter 发送，Shift+Enter 换行")

    # ---------- 对话管理
    @staticmethod
    def _valid_convs(data):
        """丢弃结构不对的条目，并保证 id 唯一。"""
        out, ids = [], set()
        for c in data if isinstance(data, list) else []:
            if not (isinstance(c, dict) and isinstance(c.get("messages"), list)):
                continue
            c.setdefault("title", "对话")
            c["messages"] = [m for m in c["messages"] if isinstance(m, dict) and m.get("role")]
            if c.get("id") in ids or not isinstance(c.get("id"), int):
                c["id"] = max(ids, default=0) + 1
            ids.add(c["id"])
            out.append(c)
        return out

    def _blank(self, cwd="", skill=None):
        new_id = int(time.time() * 1000)
        while any(c["id"] == new_id for c in self.convs):
            new_id += 1
        c = {"id": new_id, "title": "新对话", "cwd": cwd, "messages": []}
        if skill is not None:
            c["skill"] = skill
        return c

    def save_convs(self):
        if save_json(CONVS_PATH, self.convs):
            self._save_warned = False
        elif not self._save_warned:  # 只提醒一次，避免反复弹窗
            self._save_warned = True
            QMessageBox.warning(self, "保存失败", f"对话记录无法写入：\n{CONVS_PATH}\n\n"
                                "可能被其它程序占用或磁盘已满。程序会在下次保存时自动重试。")

    def report_error(self, text):
        """全局异常的兜底提示（非阻塞，避免一连串弹窗）。"""
        if getattr(self, "_error_box", None) and self._error_box.isVisible():
            return
        self._error_box = QMessageBox(QMessageBox.Icon.Warning, "程序出现错误",
                                      f"{text}\n\n程序会继续运行，详细信息已写入：\n{os.path.join(LOG_DIR, 'app.log')}",
                                      parent=self)
        self._error_box.setModal(False)
        self._error_box.show()

    def refresh_list(self, row):
        self.conv_list.blockSignals(True)
        self.conv_list.clear()
        for c in self.convs:
            self.conv_list.addItem(QListWidgetItem(self.item_text(c)))
        self.conv_list.blockSignals(False)
        self.conv_list.setCurrentRow(row)
        self.select_conv(row)

    def select_conv(self, row):
        if 0 <= row < len(self.convs):
            self.cur = self.convs[row]
            self.title_lbl.setText(self.cur["title"])
            self.update_dir_ui()
            self.render_all()
            s = self.cur_session()
            self.dir_btn.setEnabled(not (s and s.busy))
            self._update_send_state()

    def new_conv(self):
        cwd = (self.cur or {}).get("cwd", "")  # 新对话沿用当前目录和 skill 开关
        skill = (self.cur or {}).get("skill")
        if self.cur and not self.cur["messages"]:
            self.convs.remove(self.cur)
        self.convs.insert(0, self._blank(cwd, skill))
        self.save_convs()
        self.refresh_list(0)
        self.input.setFocus()

    def conv_menu(self, pos):
        item = self.conv_list.itemAt(pos)
        if not item:
            return
        row = self.conv_list.row(item)
        menu = QMenu(self)
        menu.addAction("重命名", lambda: self.rename_conv(row))
        menu.addAction("删除", lambda: self.delete_conv(row))
        menu.exec(self.conv_list.viewport().mapToGlobal(pos))

    def rename_conv(self, row):
        c = self.convs[row]
        name, ok = QInputDialog.getText(self, "重命名", "对话名称：", text=c["title"])
        if ok and name.strip():
            c["title"] = name.strip()
            self.save_convs()
            self.refresh_list(row)
            self.title_lbl.setText(self.cur["title"])

    def delete_conv(self, row):
        if self.is_busy(self.convs[row]):
            QMessageBox.information(self, "删除", "这个对话正在运行，请先停止再删除。")
            return
        if QMessageBox.question(self, "删除", f"删除对话「{self.convs[row]['title']}」？") \
                != QMessageBox.StandardButton.Yes:
            return
        self.sessions.pop(self.convs[row]["id"], None)
        del self.convs[row]
        if not self.convs:
            self.convs.append(self._blank())
        self.save_convs()
        self.refresh_list(min(row, len(self.convs) - 1))

    # ---------- 渲染
    def render_all(self):
        self.chat.clear()
        self.widgets = {}
        msgs = self.cur["messages"]
        if not msgs:
            h = time.localtime().tm_hour
            greet = "夜深了" if h < 6 else "早上好" if h < 11 else "中午好" if h < 13 else "下午好" if h < 18 else "晚上好"
            self.greet_lbl.setText(greet)
            if not self.cfg["api_key"]:
                sub = "请先点击左下角「设置」填写 API Key"
            elif not self.cur.get("cwd"):
                sub = "点击右上角「📁 打开目录」后，GLM 就可以读写文件、运行命令"
            else:
                sub = "今天想做点什么？"
            self.welcome_sub.setText(sub)
            self.stack.setCurrentIndex(0)
            return
        self.stack.setCurrentIndex(1)
        for i, m in enumerate(msgs):
            if i and i == self.cur.get("summary_upto"):
                self.chat.add(SummaryDivider(self.cur.get("summary", "")))
            self._add_widget(m, msgs[i - 1]["role"] if i else None)
        s = self.sessions.get(self.cur["id"])
        if s and s.worker and s.reply:  # 这个对话正在后台生成，接着显示流式内容
            self.widgets[id(s.reply)].update_msg(s.reply, streaming=True)
        self.chat.scroll_bottom()

    def _add_widget(self, m, prev_role):
        if m["role"] == "user":
            w = UserMessage(m)
        elif m["role"] == "tool":
            w = ToolCard(m, self.undo_tool)
        else:
            w = AssistantMessage(m, self.cfg["model"], show_head=prev_role in (None, "user"))
        self.chat.add(w)
        self.widgets[id(m)] = w
        return w

    def show_message(self, conv, m, streaming=False):
        """对话新增一条消息；只有当前正在看的对话才需要画出来。"""
        if conv is not self.cur:
            return
        if self.stack.currentIndex() == 0:
            self.stack.setCurrentIndex(1)
        msgs = conv["messages"]
        i = next((k for k in range(len(msgs) - 1, -1, -1) if msgs[k] is m), 0)
        w = self._add_widget(m, msgs[i - 1]["role"] if i else None)
        if isinstance(w, AssistantMessage):
            w.update_msg(m, streaming)
        self.chat.scroll_bottom()

    def refresh_message(self, conv, m, streaming=False):
        if conv is not self.cur or id(m) not in self.widgets:
            return
        bottom = self.chat.at_bottom()
        w = self.widgets[id(m)]
        if isinstance(w, AssistantMessage):
            w.update_msg(m, streaming)
        elif isinstance(w, ToolCard):
            w.refresh()
        if bottom:
            self.chat.scroll_bottom()

    def flush(self):
        """定时刷新当前对话正在流式输出的那条消息；内容很长时降低刷新频率，避免界面卡顿。"""
        now = time.time()
        for s in self.sessions.values():
            if not (s.dirty and s.reply is not None):
                continue
            size = len(s.reply.get("content") or "") + len(s.reply.get("reasoning") or "")
            if size > 15000 and now - s.last_flush < (0.6 if size < 60000 else 1.5):
                continue
            s.dirty = False
            s.last_flush = now
            try:
                self.refresh_message(s.conv, s.reply, streaming=s.worker is not None)
            except Exception:
                log.exception("刷新消息失败")
        if now - getattr(self, "_last_status", 0) >= 0.5:
            self._last_status = now
            try:
                self.update_status_line(now)
            except Exception:
                log.exception("刷新状态行失败")

    def update_status_line(self, now):
        s = self.cur_session()
        if not (s and s.busy):
            return
        spin = "◐◓◑◒"[int(now * 2) % 4]
        r = s.reply
        if s.worker and r is not None and id(r) in self.widgets:
            p = s.progress
            if r.get("notice"):
                text = r["notice"]
            elif p and p["name"] in ("write_file", "edit_file"):
                text = f"正在撰写 {p['path'] or '文件'}，已生成 {p['chars']:,} 字"
            elif p:
                verb = {**TOOL_LABELS, **SKILL_LABELS, **SHELL_LABELS, **MEMORY_LABELS}.get(p["name"], ("", "调用工具"))[1]
                text = f"正在准备{verb}{' ' + p['path'] if p['path'] else ''}"
            elif r.get("content"):
                text = "正在输出回复"
            elif r.get("reasoning"):
                text = "正在思考"
            else:
                text = "等待模型响应"
            self.widgets[id(r)].set_status(f"{spin} {text} · 已用时 {int(now - s.started)} 秒")
        for m in list(s.running):
            if id(m) in self.widgets and m.get("_started"):
                self.widgets[id(m)].set_running(f"{spin} 运行中 {int(now - m['_started'])} 秒")

    # ---------- 发送 / 停止
    def build_messages(self, conv, upto=None, summary=None):
        """组装发给模型的消息：系统提示（目录、工具、skill、MCP、相关记忆、较早对话摘要）+ 摘要之后的对话原文。"""
        parts = [self.cfg.get("system_prompt", "").strip()]
        cwd = conv.get("cwd")
        if cwd and os.path.isdir(cwd):
            parts.append(f"用户当前的工作目录是 {cwd}，目录结构（前两层）如下：\n{dir_overview(cwd)}\n\n{TOOL_PROMPT}")
            if self.skill_on(conv):
                parts.append(skill_prompt())
                parts.append("### 环境检查结果（程序已自动完成，环境就绪时无需再调用 check_env）\n" + env_status())
        else:
            parts.append("当前对话没有关联工作目录，读写文件和运行命令的工具不可用；"
                         "需要操作文件时，请提示用户点击右上角「📁 打开目录」。")
        n_srv, n_tools = self.mcp.summary()
        if n_tools:
            parts.append(f"## MCP 工具\n已连接 {n_srv} 个 MCP 服务器，共 {n_tools} 个工具（函数名以 mcp__服务器名__ 开头），"
                         "需要外部数据或能力时按需调用。")
        last_user = next((m.get("content", "") for m in reversed(conv["messages"]) if m["role"] == "user"), "")
        digest = self.memory.digest(f"{conv.get('title', '')} {last_user}", exclude_source=conv["id"])
        parts.append(MEMORY_PROMPT + ("\n\n### 相关记忆（自动检索）\n" + digest if digest else "\n\n（记忆库中暂无相关条目）"))
        start = conv.get("summary_upto", 0) if upto is None else upto
        summ = conv.get("summary") if summary is None else summary
        if summ and start:
            parts.append("## 更早对话的摘要（原文已压缩，界面上仍可查看）\n" + summ)
        msgs = [{"role": "system", "content": "\n\n".join(x for x in parts if x)}]
        history = conv["messages"][start:]
        # 较早的大段 PDF / 文件原文只保留开头，避免超出上下文
        tool_ids = [i for i, m in enumerate(history) if m["role"] == "tool"]
        old_tools = set(tool_ids[:-12])
        for i, m in enumerate(history):
            role = m["role"]
            if role == "user":
                msgs.append({"role": "user", "content": api_content(m)})
            elif role == "assistant":
                if m.get("tool_calls"):
                    msgs.append({"role": "assistant", "content": m.get("content") or "", "tool_calls": m["tool_calls"]})
                elif m.get("content") and not m.get("error"):
                    msgs.append({"role": "assistant", "content": m["content"]})
            elif role == "tool":
                content = m.get("content", "")
                if i in old_tools and len(content) > 1500:
                    content = content[:1500] + "\n…（较早的工具输出已省略以节省上下文，如需请重新读取）"
                msgs.append({"role": "tool", "tool_call_id": m["tool_call_id"], "content": content})
        return msgs

    def tools_for(self, conv):
        """记忆和 MCP 工具始终可用；文件、命令和 skill 工具需要关联工作目录。"""
        tools = MEMORY_TOOLS + self.mcp.tool_specs()
        cwd = conv.get("cwd")
        if cwd and os.path.isdir(cwd):
            tools = TOOLS + SHELL_TOOLS + (SKILL_TOOLS if self.skill_on(conv) else []) + tools
        return tools

    def compression_job(self, conv):
        """历史过长时返回 (待压缩的较早对话文本, 压缩到第几条消息)；切点总在某条用户消息之前。"""
        msgs = conv["messages"]
        start = conv.get("summary_upto", 0)
        size = sum(len(json.dumps(m, ensure_ascii=False)) for m in self.build_messages(conv)[1:])
        if size < COMPRESS_AT:
            return None
        last_user = max((i for i, m in enumerate(msgs) if m["role"] == "user"), default=-1)
        sizes = [len(json.dumps(m, ensure_ascii=False)) for m in msgs]
        cut_at = None
        for i in range(start + 1, last_user + 1):
            if msgs[i]["role"] != "user":
                continue
            cut_at = i
            if sum(sizes[i:]) <= KEEP_TAIL:
                break
        if not cut_at:
            return None
        prev = f"【此前已有的摘要】\n{conv['summary']}\n\n" if conv.get("summary") and start else ""
        return prev + "【需要压缩的对话】\n" + transcript(msgs[start:cut_at], limit=60000), cut_at

    def on_compressed(self, conv):
        if conv is self.cur:
            self.render_all()

    def skill_on(self, conv):
        cwd = conv.get("cwd")
        return bool(conv.get("skill") and cwd and os.path.isdir(cwd) and skill_dir())

    def cur_session(self):
        return self.sessions.get(self.cur["id"]) if self.cur else None

    def is_busy(self, conv):
        s = self.sessions.get(conv["id"])
        return bool(s and s.busy)

    def item_text(self, conv):
        return ("● " if self.is_busy(conv) else "") + conv["title"]

    def on_session_state(self, s):
        """某个对话开始或结束运行：更新列表标记和按钮。"""
        if s.conv in self.convs:
            item = self.conv_list.item(self.convs.index(s.conv))
            if item:
                item.setText(self.item_text(s.conv))
        if s.conv is self.cur:
            self.dir_btn.setEnabled(not s.busy)
            self._update_send_state()
            if not s.busy:
                self.input.setFocus()

    def stop_turn(self):
        s = self.cur_session()
        if s:
            s.stop()

    def on_send(self):
        s = self.cur_session()
        if s and s.busy:
            return s.stop()
        text = self.input.toPlainText().strip()
        if not text:
            return
        if not self.cfg["api_key"]:
            return self.open_settings()
        self.input.clear()

        conv = self.cur
        user = {"role": "user", "content": text}
        if self.pending_files:
            user["files"] = self.pending_files
            self.pending_files = []
            self._refresh_attachments()
        conv["messages"].append(user)
        if conv["title"] == "新对话":
            conv["title"] = text[:24]
            self.title_lbl.setText(conv["title"])
        self.show_message(conv, user)
        if not s:
            s = self.sessions[conv["id"]] = Session(self, conv)
        s.start()

    def run_tool(self, s, name, args):
        if name in MEMORY_TOOL_NAMES:
            result = self.memory.run_tool(name, args, source=s.conv["id"])
            self.update_memory_btn()
            return result
        if name in SKILL_TOOL_NAMES:
            return run_skill_tool(name, args, s.conv["cwd"])
        if name in SHELL_TOOL_NAMES:
            return run_shell_tool(name, args, s.conv["cwd"])
        if not isinstance(args, dict):
            raise ToolError("参数不是合法的 JSON 对象")
        cwd = s.conv["cwd"]
        rel = args.get("path") or "."
        p = safe_path(cwd, rel)

        if name == "list_dir":
            if not os.path.isdir(p):
                raise ToolError("目录不存在")
            entries = sorted(os.scandir(p), key=lambda e: (not e.is_dir(), e.name.lower()))
            lines = [e.name + "/" if e.is_dir() else f"{e.name}  ({e.stat().st_size} B)" for e in entries[:300]]
            text = "\n".join(lines) or "（空目录）"
            return text, {"status": "ok", "preview": text}

        if name == "read_file":
            if not os.path.isfile(p):
                raise ToolError("文件不存在")
            text, err, _ = read_text_file(p)
            if err:
                raise ToolError(err)
            return text or "（空文件）", {"status": "ok", "preview": text[:4000]}

        if name not in ("write_file", "edit_file"):
            raise ToolError(f"未知的工具 {name}")
        if os.path.isdir(p):
            raise ToolError("这是一个目录，不是文件")
        exists = os.path.isfile(p)
        old, fmt = None, {"enc": "utf-8", "crlf": False}
        if exists:
            old, err, fmt = read_text_file(p)
            if err:
                raise ToolError(err)
        if name == "write_file":
            new = args.get("content")
            if not isinstance(new, str):
                raise ToolError("缺少 content 参数")
        else:
            if not exists:
                raise ToolError("文件不存在；新建文件请使用 write_file")
            old_str, new_str = args.get("old_string"), args.get("new_string")  # 注意不要用 s：s 是当前对话
            if not isinstance(old_str, str) or not isinstance(new_str, str) or not old_str:
                raise ToolError("old_string / new_string 参数无效")
            n = old.count(old_str)
            if n == 0:
                raise ToolError("文件中找不到 old_string，请先用 read_file 读取最新内容再修改")
            if n > 1 and not args.get("replace_all"):
                raise ToolError(f"old_string 在文件中出现了 {n} 次，请提供更多上下文使其唯一，或设置 replace_all")
            new = old.replace(old_str, new_str) if args.get("replace_all") else old.replace(old_str, new_str, 1)
        if new == old:
            return "内容没有变化，未写入。", {"status": "ok", "preview": "内容没有变化"}

        diff = unified_diff(old, new, rel)
        added, removed = diff_stats(diff)
        info = {"diff": diff, "added": added, "removed": removed, "new_file": not exists,
                "abs": p, "old": old, "enc": fmt["enc"], "crlf": fmt["crlf"]}
        write_text_file(p, new, fmt["enc"], fmt["crlf"])
        info["status"] = "ok"
        return f"已{'新建' if not exists else '修改'} {rel}（+{added} −{removed}）。", info

    # ---------- 记忆库
    def update_memory_btn(self):
        self.mem_btn.setText(f"🧠  记忆库（{len(self.memory.items)}）")

    def open_memory(self):
        MemoryDialog(self.memory, self).exec()
        self.update_memory_btn()

    def flash_hint(self, text, ms=8000):
        self.hint_lbl.setText(text)
        self.hint_lbl.setStyleSheet(f"color: {ACCENT};")
        QTimer.singleShot(ms, lambda: (self.hint_lbl.setText(HINT_TEXT), self.hint_lbl.setStyleSheet("")))

    def _conv_note(self, conv):
        return next((m for m in self.memory.items if m["type"] == "对话要点" and m.get("source") == conv["id"]), None)

    def on_turn_finished(self, conv):
        """一轮对话结束：排队在后台把新增内容提炼成要点存进记忆库。"""
        if not (self.cfg.get("auto_memory", True) and self.cfg["api_key"]):
            return
        new = conv["messages"][conv.get("distilled_upto", 0):]
        if not any(m["role"] == "assistant" and m.get("content") and not m.get("error") for m in new):
            return
        if conv["id"] not in self.distill_queue:
            self.distill_queue.append(conv["id"])
        self.run_distill()

    def run_distill(self):
        if self.distiller or not self.distill_queue:
            return
        cid = self.distill_queue.pop(0)
        conv = next((c for c in self.convs if c["id"] == cid), None)
        if not conv or self.is_busy(conv):  # 还在运行的对话等它结束后会重新排队
            return self.run_distill()
        upto = len(conv["messages"])
        text = transcript(conv["messages"][conv.get("distilled_upto", 0):upto], limit=40000)
        note = self._conv_note(conv)
        related = [m for m in self.memory.search(text[:6000], 12) if m["type"] != "对话要点"]
        ctx = "\n".join(f"- [{m['type']}] {m['title']}：{m['content'][:800]}" for m in related)
        prompt = (f"对话标题：{conv['title']}\n\n"
                  + (f"## 该对话之前提炼的要点（请在此基础上更新，输出完整版）\n{note['content']}\n\n" if note else "")
                  + (f"## 已有相关记忆\n{ctx}\n\n" if ctx else "")
                  + f"## 对话记录（新增部分）\n{text}")
        self.distiller = Distiller(dict(self.cfg), prompt)
        self.distiller.done.connect(lambda r, c=conv, u=upto: self.on_distilled(c, u, r))
        self.distiller.failed.connect(lambda e: log.warning("提炼记忆失败：%s", e))
        self.distiller.finished.connect(self._distill_finished)
        self.distiller.start()
        log.info("开始提炼对话「%s」的要点", conv["title"])

    def _distill_finished(self):
        if self.distiller:
            self.distiller.deleteLater()
            self.distiller = None
        QTimer.singleShot(0, self.run_distill)

    def on_distilled(self, conv, upto, result):
        try:
            added = updated = 0
            summ = str(result.get("summary") or "").strip()
            if summ:
                note = self._conv_note(conv)
                if note:
                    note.update(title=f"对话要点：{conv['title']}", content=summ,
                                updated=time.strftime("%Y-%m-%d %H:%M"))
                    updated += 1
                else:
                    self.memory.upsert(f"对话要点：{conv['title']}", summ, "对话要点", [], conv["id"])
                    added += 1
            for item in result.get("memories") or []:
                if isinstance(item, dict) and item.get("title") and item.get("content"):
                    _, is_new = self.memory.upsert(item["title"], item["content"], item.get("type"),
                                                   item.get("tags") if isinstance(item.get("tags"), list) else [],
                                                   conv["id"])
                    added += is_new
                    updated += not is_new
            self.memory.save()
            conv["distilled_upto"] = upto
            self.save_convs()
            self.update_memory_btn()
            log.info("对话「%s」要点已提炼：新增 %d，更新 %d", conv["title"], added, updated)
            if added or updated:
                self.flash_hint(f"🧠 已把「{conv['title'][:16]}」的要点存入记忆库：新增 {added} 条，更新 {updated} 条")
        except Exception:
            log.exception("保存提炼结果失败")

    # ---------- MCP
    def update_mcp_btn(self):
        n_srv, n_tools = self.mcp.summary()
        servers = self.mcp.servers.values()
        state = ""
        if any(s.status in ("pending", "connecting") for s in servers):
            state = " · 连接中"
        elif any(s.status == "error" for s in servers) or self.mcp.config_error:
            state = " ⚠"
        self.mcp_btn.setText(f"🔌  MCP 服务（{n_tools} 个工具）{state}" if self.mcp.servers else "🔌  MCP 服务")

    def open_mcp(self):
        McpDialog(self.mcp, self.mcp_relay, self).exec()

    def undo_tool(self, card):
        m = card.m
        if "move_from" in m:
            if QMessageBox.question(self, "撤销移动", f"把文件移回原位置？\n{m['move_to']}\n→ {m['move_from']}") \
                    != QMessageBox.StandardButton.Yes:
                return
            try:
                if os.path.exists(m["move_from"]):
                    raise OSError("原位置已经有同名文件")
                shutil.move(m["move_to"], m["move_from"])
            except OSError as e:
                QMessageBox.warning(self, "撤销失败", str(e))
                return
            m["status"] = "undone"
            card.refresh()
            return self.save_convs()
        if m.get("new_file"):
            q = f"删除 GLM 新建的文件？\n{m['abs']}"
        else:
            q = f"把文件恢复到这次修改之前的内容？\n{m['abs']}\n\n注意：这次修改之后对该文件的其它改动也会被覆盖。"
        if QMessageBox.question(self, "撤销修改", q) != QMessageBox.StandardButton.Yes:
            return
        try:
            if m.get("new_file"):
                os.remove(m["abs"])
            else:
                write_text_file(m["abs"], m["old"], m["enc"], m["crlf"])
        except OSError as e:
            QMessageBox.warning(self, "撤销失败", str(e))
            return
        m["status"] = "undone"
        card.refresh()
        self.save_convs()

    # ---------- 设置
    def open_settings(self):
        dlg = SettingsDialog(self.cfg, self)
        if dlg.exec():
            self.cfg.update(dlg.values())
            save_json(CONFIG_PATH, self.cfg)
            self.update_model_chip()
            if self.cur and not self.cur["messages"]:
                self.render_all()

    def closeEvent(self, ev):
        running = [s for s in self.sessions.values() if s.busy]
        if running and QMessageBox.question(
                self, "退出", f"还有 {len(running)} 个对话正在运行，退出会中止它们。\n确定要退出吗？") \
                != QMessageBox.StandardButton.Yes:
            ev.ignore()
            return
        log.info("程序退出")
        for s in self.sessions.values():
            s.stop()
        for s in self.sessions.values():
            s.wait()
        self.mcp.stop_all()
        if self.distiller:
            self.distiller.wait(3000)
        self.save_convs()
        super().closeEvent(ev)


class ErrorRelay(QObject):
    """把任意线程里的未捕获异常转到界面线程提示。"""
    error = pyqtSignal(str)


def install_exception_hooks(relay):
    def hook(exc_type, exc, tb):
        log.error("未捕获的异常：\n%s", "".join(traceback.format_exception(exc_type, exc, tb)))
        try:
            relay.error.emit(f"{exc_type.__name__}: {exc}")
        except Exception:
            pass
    # PyQt6 默认遇到未捕获异常会直接终止程序；替换 excepthook 后改为记录并继续运行
    sys.excepthook = hook
    threading.excepthook = lambda a: hook(a.exc_type, a.exc_value, a.exc_traceback)


def single_instance_server(window):
    """程序只允许打开一个窗口：否则两个窗口会互相覆盖对话记录。"""
    name = "glm-mcp-doc-agent-" + format(zlib.crc32(ROOT.lower().encode("utf-8")), "08x")
    server = QLocalServer(window)
    QLocalServer.removeServer(name)  # 清理上次异常退出留下的残留
    server.listen(name)

    def activate():
        conn = server.nextPendingConnection()
        if conn:
            conn.disconnectFromServer()
        window.showNormal()
        window.raise_()
        window.activateWindow()
    server.newConnection.connect(activate)
    return server


def another_instance_running():
    name = "glm-mcp-doc-agent-" + format(zlib.crc32(ROOT.lower().encode("utf-8")), "08x")
    sock = QLocalSocket()
    sock.connectToServer(name)
    ok = sock.waitForConnected(500)
    sock.disconnectFromServer()
    return ok


def self_test(pdfs):
    """检查打包后的程序：依赖能否导入、编译环境、PDF 解析速度。用法：exe --selftest [a.pdf b.pdf]"""
    import skill_tools
    out = [f"{APP_NAME} 自检  {time.strftime('%Y-%m-%d %H:%M:%S')}", f"程序目录：{ROOT}"]
    for mod in ("PyQt6.QtWidgets", "PyQt6.QtNetwork", "requests", "markdown_it", "pygments", "pymupdf", "pypdf"):
        try:
            __import__(mod)
            out.append(f"[OK] 导入 {mod}")
        except Exception as e:
            out.append(f"[失败] 导入 {mod}：{e}")
    out.append(skill_tools.check_env())
    for p in pdfs:
        t = time.time()
        try:
            pages = skill_tools._extract(p)
            out.append(f"[OK] 解析 {os.path.basename(p)}：{len(pages)} 页，{sum(map(len, pages))} 字，{time.time() - t:.2f} 秒")
        except Exception as e:
            out.append(f"[失败] 解析 {p}：{e}")
    os.makedirs(LOG_DIR, exist_ok=True)
    with open(os.path.join(LOG_DIR, "selftest.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")


def main():
    # 打包成无控制台的 exe 时 stdout/stderr 为 None，任何打印都会出错，先指向空设备
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")
    setup_logging()
    if "--selftest" in sys.argv:  # 打包后自检：结果写入 logs/selftest.txt，不打开窗口
        return self_test([a for a in sys.argv[1:] if a.lower().endswith(".pdf")])
    log.info("程序启动 %s", APP_NAME)
    if sys.platform == "win32":  # 任务栏显示独立图标
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("glm.mcp.doc.agent")
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    if another_instance_running():  # 已经打开了：把那个窗口调到前面，自己退出
        log.info("检测到已运行的实例，激活后退出")
        return
    relay = ErrorRelay()
    install_exception_hooks(relay)
    icon = os.path.join(getattr(sys, "_MEIPASS", ROOT), "app.ico")
    if os.path.exists(icon):
        app.setWindowIcon(QIcon(icon))
    app.setStyle("Fusion")
    app.setFont(QFont("Microsoft YaHei UI", 10))
    app.setStyleSheet(QSS)
    w = MainWindow()
    relay.error.connect(w.report_error)
    app._single = single_instance_server(w)
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
