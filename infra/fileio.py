"""infra.fileio —— 文本文件读写、diff 数据、目录概览等（无界面依赖）。"""
import difflib
import os
import subprocess

from pygments.lexers import get_lexer_for_filename
from pygments.util import ClassNotFound

from infra.config import MAX_FILE_BYTES, SKIP_DIRS

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
