"""内置 skill：datasheet 对比报告（PDF 提取 → LaTeX → XeLaTeX 编译）。

这里的工具函数不碰任何 Qt 控件，可以放在后台线程里运行。
skill 文件优先从 exe 旁边的 skills/ 目录读取（方便用户修改），否则用打包进 exe 的版本。
"""
import difflib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import zlib

FROZEN = getattr(sys, "frozen", False)
EXE_DIR = os.path.dirname(sys.executable if FROZEN else os.path.abspath(__file__))
BUNDLE_DIR = getattr(sys, "_MEIPASS", EXE_DIR)
SKILL_NAME = "datasheet-compare"
READ_LIMIT = 30000          # read_pdf 单次返回的最大字符数
NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0   # 子进程不弹黑框


class ToolError(Exception):
    pass


def safe_path(root, rel):
    """把相对路径解析到工作目录内，越界则报错。"""
    base = os.path.realpath(root)
    p = os.path.realpath(os.path.join(base, rel or "."))
    if os.path.commonpath([base, p]) != base:
        raise ToolError("路径超出了工作目录，不允许访问")
    return p


# ---------------------------------------------------------------- skill 文件
def skill_dir():
    for base in (EXE_DIR, BUNDLE_DIR):
        d = os.path.join(base, "skills", SKILL_NAME)
        if os.path.isfile(os.path.join(d, "SKILL.md")):
            return d
    return None


def _read(path):
    with open(path, encoding="utf-8-sig") as f:
        return f.read()


def skill_prompt():
    d = skill_dir()
    if not d:
        return ""
    body = _read(os.path.join(d, "SKILL.md"))
    if body.startswith("---"):  # 去掉 frontmatter
        body = body.split("---", 2)[2]
    return "# 已启用内置 skill：datasheet 对比报告\n\n" + body.strip() + "\n\n" + ADAPTER


ADAPTER = """## 在本软件中如何执行这个 skill（与上文冲突时以本节为准）
你运行在「GLM-MCP-文档对比agent」桌面软件里，不能运行命令行或脚本，改用下列工具；工作目录就是 datasheet 所在目录，所有路径都相对于它。
- 第 0 步环境检查 → 程序已自动检查，结果在下方；只有结果显示缺项时才停下来告诉用户按「安装」一节安装（不要自己安装），就绪则直接开始。
- 第 1 步清点与提取 → pdf_info(path=".") 列出全部 PDF 的页数、字符数、是否扫描件；read_pdf 按页读文本（每次最多约 30000 字符，长文档分段读）；search_pdf 用关键词定位参数所在页（例如 "Absolute Maximum|Electrical Characteristics|Sensitivity"），再只读那几页；同型号多个版本用 diff_pdfs。**多份 datasheet 要在同一次回复里同时调用 read_pdf / search_pdf（连同 get_report_template），程序会并行执行**，不要一份一份地读。
- 扫描件：本软件不能看图，在样本说明和数据来源中注明“扫描件，未能读取”，对应参数填 \\nd，不要猜。
- 第 3 步写 .tex → 先 get_report_template 获取模板，再用 write_file 把 `<主题>对比报告.tex` 写到工作目录；局部修改用 edit_file。
- 第 4 步编译 → build_pdf(path=".tex 文件")，它会跑两遍 xelatex，返回错误与 ≥10pt 的溢出并清理 .aux/.log/.out。有错误或显著溢出就修改 .tex 再编译，直到清零。本软件不能渲染页面预览，以日志为准。
- 上下文有限：每读完一份 datasheet，立即在回复里用简短表格记下提取到的参数；较早的 PDF 原文之后会被省略。
- 全部完成后，用中文简要总结：报告文件名、对比了哪些型号、主要结论、仍存在的问题。"""


SKILL_TOOLS = [
    {"type": "function", "function": {
        "name": "check_env", "description": "检查 XeLaTeX、PDF 解析库和中文字体是否就绪。",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "pdf_info",
        "description": "path 为目录时列出其中所有 PDF 的页数、字符数和是否扫描件；为文件时给出每页字符数和首页摘要。",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "目录或 PDF 文件，默认 ."}}}}},
    {"type": "function", "function": {
        "name": "read_pdf", "description": "读取 PDF 指定页范围的文本（页码从 1 开始，含首尾）。",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"},
            "start_page": {"type": "integer", "description": "起始页，默认 1"},
            "end_page": {"type": "integer", "description": "结束页，默认起始页+7"}}, "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "search_pdf",
        "description": "在 PDF（或目录下所有 PDF）中按关键词搜索，返回所在页码和上下文。多个关键词用 | 分隔，不区分大小写。",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "PDF 文件或目录，默认 ."},
            "keywords": {"type": "string"},
            "max_hits": {"type": "integer", "description": "每个文件最多返回的命中数，默认 20"}}, "required": ["keywords"]}}},
    {"type": "function", "function": {
        "name": "diff_pdfs", "description": "逐行对比同一型号两个版本 PDF 的文本差异。",
        "parameters": {"type": "object", "properties": {
            "old": {"type": "string"}, "new": {"type": "string"}}, "required": ["old", "new"]}}},
    {"type": "function", "function": {
        "name": "get_report_template", "description": "获取对比报告的 LaTeX 模板（样式、宏、表格、TikZ 图）。",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "build_pdf",
        "description": "用 XeLaTeX 编译 .tex 两遍，返回错误和显著溢出，并清理中间文件。",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": ".tex 文件路径"}}, "required": ["path"]}}},
]
SKILL_TOOL_NAMES = {t["function"]["name"] for t in SKILL_TOOLS}
SLOW_TOOLS = {"check_env", "pdf_info", "read_pdf", "search_pdf", "diff_pdfs", "build_pdf"}
SKILL_LABELS = {"check_env": ("🧰", "检查环境"), "pdf_info": ("📑", "PDF 概况"), "read_pdf": ("📖", "读取 PDF"),
                "search_pdf": ("🔍", "搜索 PDF"), "diff_pdfs": ("🆚", "对比版本"),
                "get_report_template": ("📐", "读取报告模板"), "build_pdf": ("🛠", "编译 PDF")}


def tool_display(name, args):
    """卡片上显示的操作对象。"""
    if not isinstance(args, dict):
        return ""
    if name == "read_pdf":
        s = args.get("start_page") or 1
        return f"{args.get('path', '')}  第 {s}–{args.get('end_page') or s + 7} 页"
    if name == "search_pdf":
        return f"{args.get('path') or '.'}  「{args.get('keywords', '')}」"
    if name == "diff_pdfs":
        return f"{args.get('old', '')} → {args.get('new', '')}"
    if name in ("check_env", "get_report_template"):
        return ""
    return args.get("path") or "."


# ---------------------------------------------------------------- 环境
def find_xelatex():
    exe = shutil.which("xelatex")
    if exe:
        return exe
    cands = [os.path.join(os.environ.get("LOCALAPPDATA", ""), r"Programs\MiKTeX\miktex\bin\x64\xelatex.exe"),
             r"C:\Program Files\MiKTeX\miktex\bin\x64\xelatex.exe"]
    for root in (r"C:\texlive", r"D:\texlive"):
        if os.path.isdir(root):
            for year in sorted(os.listdir(root), reverse=True):
                cands.append(os.path.join(root, year, r"bin\windows\xelatex.exe"))
                cands.append(os.path.join(root, year, r"bin\win32\xelatex.exe"))
    return next((c for c in cands if os.path.isfile(c)), None)


def check_env():
    lines, ok = [], True
    xe = find_xelatex()
    if xe:
        try:
            ver = subprocess.run([xe, "--version"], capture_output=True, text=True, encoding="utf-8",
                                 errors="replace", timeout=30, creationflags=NO_WINDOW).stdout.splitlines()[0]
        except Exception as e:
            ver = f"（无法获取版本：{e}）"
        lines.append(f"[OK] xelatex: {xe}  {ver}")
    else:
        ok = False
        lines.append("[缺失] xelatex —— 请安装 MiKTeX：winget install --id MiKTeX.MiKTeX -e "
                     "--accept-package-agreements --accept-source-agreements，装好后重启本软件")
    try:
        import pymupdf
        lines.append(f"[OK] PDF 解析库 PyMuPDF {pymupdf.__version__}")
    except ImportError:
        try:
            import pypdf
            lines.append(f"[OK] PDF 解析库 pypdf {pypdf.__version__}（较慢）")
        except ImportError:
            ok = False
            lines.append("[缺失] PDF 解析库（PyMuPDF / pypdf）")
    fonts = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")
    cjk = any(os.path.exists(os.path.join(fonts, f)) for f in ("simsun.ttc", "msyh.ttc", "simhei.ttf"))
    lines.append("[OK] 中文字体" if cjk else "[缺失] 中文字体 SimSun/微软雅黑/SimHei")
    ok = ok and cjk
    lines.append("[说明] 扫描件无法读取（本软件不支持看图）；首次编译时 MiKTeX 可能会自动下载 ctex 等宏包，需要联网。")
    lines.append("环境就绪。" if ok else "环境有缺项，请先让用户安装。")
    return "\n".join(lines)


_env = {"t": 0.0, "text": ""}


def env_status(max_age=600):
    """缓存的环境检查结果（10 分钟内不重复检查），写进提示词，省掉 GLM 专门调用 check_env 的一轮。"""
    if time.time() - _env["t"] > max_age or not _env["text"]:
        _env.update(t=time.time(), text=check_env())
    return _env["text"]


def prefetch(directory):
    """后台预热：检查环境、提前解析目录里的 PDF。出错忽略，GLM 真正读取时会再报告。"""
    try:
        env_status()
        for f in _pdfs_in(directory):
            try:
                pdf_pages(os.path.join(directory, f))
            except Exception:
                pass
    except Exception:
        pass


# ---------------------------------------------------------------- PDF
_cache = {}
CACHE_DIR = os.path.join(EXE_DIR, "cache", "pdf")   # 解析结果存盘：同一份 PDF 只解析一次，重启后也能直接用


def _extract(path):
    """优先用 PyMuPDF（C 实现，比 pypdf 快几十倍，表格文字顺序也更好），没有再退回 pypdf。"""
    try:
        import pymupdf
        with pymupdf.open(path) as doc:
            return [page.get_text() for page in doc]
    except ImportError:
        import pypdf
        return [(p.extract_text() or "") for p in pypdf.PdfReader(path).pages]


def pdf_pages(path):
    st = os.stat(path)
    key = (path, st.st_mtime)
    if key in _cache:
        return _cache[key]
    tag = format(zlib.crc32(f"{os.path.abspath(path).lower()}|{st.st_size}|{st.st_mtime}".encode("utf-8")), "08x")
    disk = os.path.join(CACHE_DIR, f"{tag}.json")
    try:
        with open(disk, encoding="utf-8") as f:
            pages = json.load(f)
    except (OSError, ValueError):
        try:
            pages = _extract(path)
        except ImportError:
            raise ToolError("缺少 PDF 解析库（PyMuPDF / pypdf）")
        except Exception as e:
            raise ToolError(f"无法解析 PDF：{e}")
        try:
            os.makedirs(CACHE_DIR, exist_ok=True)
            with open(disk + ".tmp", "w", encoding="utf-8") as f:
                json.dump(pages, f, ensure_ascii=False)
            os.replace(disk + ".tmp", disk)
        except OSError:
            pass  # 缓存写不进去不影响使用
    _cache[key] = pages
    return pages


def _is_scan(pages):
    return sum(len(t.strip()) for t in pages) / max(len(pages), 1) < 100


def _pdfs_in(d):
    return sorted(f for f in os.listdir(d) if f.lower().endswith(".pdf"))


def pdf_info(p):
    if os.path.isdir(p):
        files = _pdfs_in(p)
        if not files:
            return "目录中没有 PDF 文件。"
        out = [f"共 {len(files)} 份 PDF："]
        for f in files:
            try:
                pages = pdf_pages(os.path.join(p, f))
                chars = sum(len(t) for t in pages)
                out.append(f"- {f}: {len(pages)} 页, {chars} 字符" + ("  [扫描件，无法读取]" if _is_scan(pages) else ""))
            except ToolError as e:
                out.append(f"- {f}: {e}")
        return "\n".join(out)
    if not p.lower().endswith(".pdf") or not os.path.isfile(p):
        raise ToolError("不是 PDF 文件")
    pages = pdf_pages(p)
    per = ", ".join(f"p{i + 1}:{len(t)}" for i, t in enumerate(pages))
    head = pages[0][:800] if pages else ""
    return (f"{os.path.basename(p)}: {len(pages)} 页" + ("  [扫描件，无法读取]" if _is_scan(pages) else "")
            + f"\n每页字符数: {per}\n\n首页开头:\n{head}")


def read_pdf(p, start, end):
    pages = pdf_pages(p)
    n = len(pages)
    start = max(1, int(start or 1))
    end = min(n, int(end or start + 7))
    if start > n:
        raise ToolError(f"该 PDF 只有 {n} 页")
    out, total = [], 0
    for i in range(start, end + 1):
        block = f"=== 第 {i} 页 / 共 {n} 页 ===\n{pages[i - 1].strip()}\n"
        if total + len(block) > READ_LIMIT and out:
            out.append(f"…（达到单次读取上限，请从第 {i} 页继续读取）")
            break
        out.append(block[:READ_LIMIT])
        total += len(block)
    return "\n".join(out)


def search_pdf(p, keywords, max_hits):
    words = [w.strip() for w in re.split(r"[|｜]", keywords or "") if w.strip()]
    if not words:
        raise ToolError("keywords 不能为空")
    pat = re.compile("|".join(re.escape(w) for w in words), re.I)
    files = [os.path.join(p, f) for f in _pdfs_in(p)] if os.path.isdir(p) else [p]
    max_hits = int(max_hits or 20)
    out = []
    for f in files:
        try:
            pages = pdf_pages(f)
        except ToolError as e:
            out.append(f"## {os.path.basename(f)}: {e}")
            continue
        hits = []
        for i, t in enumerate(pages):
            ls = t.splitlines()
            for j, line in enumerate(ls):
                if pat.search(line):
                    ctx = " ⏎ ".join(x.strip() for x in ls[max(0, j - 1): j + 2])
                    hits.append(f"  p{i + 1}: {ctx[:300]}")
                    if len(hits) >= max_hits:
                        break
            if len(hits) >= max_hits:
                break
        out.append(f"## {os.path.basename(f)}（{len(hits)} 处）" + ("\n" + "\n".join(hits) if hits else ""))
    return "\n".join(out)[:READ_LIMIT]


def diff_pdfs(a, b):
    la = [l.strip() for t in pdf_pages(a) for l in t.splitlines() if l.strip()]
    lb = [l.strip() for t in pdf_pages(b) for l in t.splitlines() if l.strip()]
    diff = [l for l in difflib.unified_diff(la, lb, lineterm="", n=0)
            if l.startswith(("+", "-")) and not l.startswith(("+++", "---"))]
    text = "\n".join(diff)
    if len(text) > READ_LIMIT:
        text = text[:READ_LIMIT] + "\n…（差异过多，已截断）"
    return f"{text}\n\n共 {len(diff)} 行差异（页眉版本号行可忽略）"


# ---------------------------------------------------------------- LaTeX
_procs = {}  # 线程 id -> 正在运行的 xelatex（多个对话可能同时编译）


def kill_current(ident):
    """用户点停止时结束该线程正在运行的 xelatex。"""
    proc = _procs.get(ident)
    if proc and proc.poll() is None:
        proc.kill()


def _xelatex(xe, tex, cwd):
    ident = threading.get_ident()
    proc = _procs[ident] = subprocess.Popen(
        [xe, "-interaction=nonstopmode", os.path.basename(tex)], cwd=cwd, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, creationflags=NO_WINDOW)
    try:
        proc.communicate(timeout=600)
    except subprocess.TimeoutExpired:
        proc.kill()
        raise ToolError("xelatex 运行超过 10 分钟，已终止")
    finally:
        _procs.pop(ident, None)
    return proc.returncode


def build_pdf(tex):
    if not tex.lower().endswith(".tex") or not os.path.isfile(tex):
        raise ToolError("需要一个存在的 .tex 文件")
    xe = find_xelatex()
    if not xe:
        raise ToolError("找不到 xelatex，请先安装 MiKTeX")
    d, base = os.path.dirname(tex), os.path.splitext(os.path.basename(tex))[0]
    pdf, log = os.path.join(d, base + ".pdf"), os.path.join(d, base + ".log")
    before = os.path.getmtime(pdf) if os.path.exists(pdf) else 0
    for _ in range(2):  # 跑两遍：longtable 列宽、引用；第一遍就出错则不再跑
        if _xelatex(xe, tex, d) != 0:
            break
    text = ""
    if os.path.exists(log):
        with open(log, encoding="utf-8", errors="replace") as f:
            text = f.read()
    lines = text.splitlines()
    errors = []
    for i, l in enumerate(lines):
        if l.startswith("!"):
            ctx = [x for x in lines[i + 1: i + 4] if x.strip()]
            errors.append(" | ".join([l] + ctx[:2]))
    over = [(float(m.group(1)), l.strip()) for l in lines
            for m in [re.search(r"Overfull \\hbox \(([\d.]+)pt too wide\) (.*)", l)] if m and float(m.group(1)) >= 10]
    for ext in (".aux", ".log", ".out", ".toc"):
        f = os.path.join(d, base + ext)
        if os.path.exists(f):
            try:
                os.remove(f)
            except OSError:
                pass
    built = os.path.exists(pdf) and os.path.getmtime(pdf) > before
    out = ["== LaTeX 错误 ==" if errors else "== 无 LaTeX 错误 =="]
    out += ["  " + e for e in errors[:15]]
    out.append(f"== 显著溢出 (>=10pt): {len(over)} 处 ==")
    out += [f"  {pt:.0f}pt  {l}" for pt, l in over[:15]]
    out.append(f"已生成 {base}.pdf" if built else "未生成新的 PDF")
    return "\n".join(out), built and not errors, pdf if built else None


def report_template():
    d = skill_dir()
    if not d:
        raise ToolError("找不到 skill 文件")
    return _read(os.path.join(d, "templates", "report_template.tex"))


# ---------------------------------------------------------------- 分发
def run_skill_tool(name, args, cwd):
    """返回 (给模型的结果文本, 卡片信息)。"""
    if not isinstance(args, dict):
        raise ToolError("参数不是合法的 JSON 对象")
    if name == "check_env":
        r = check_env()
        return r, {"status": "ok" if "环境就绪" in r else "error", "preview": r}
    if name == "get_report_template":
        r = report_template()
        return r, {"status": "ok", "preview": r}
    if name == "pdf_info":
        r = pdf_info(safe_path(cwd, args.get("path")))
    elif name == "read_pdf":
        r = read_pdf(safe_path(cwd, args.get("path")), args.get("start_page"), args.get("end_page"))
    elif name == "search_pdf":
        r = search_pdf(safe_path(cwd, args.get("path")), args.get("keywords"), args.get("max_hits"))
    elif name == "diff_pdfs":
        r = diff_pdfs(safe_path(cwd, args.get("old")), safe_path(cwd, args.get("new")))
    elif name == "build_pdf":
        r, ok, pdf = build_pdf(safe_path(cwd, args.get("path")))
        info = {"status": "ok" if ok else "error", "preview": r}
        if pdf:
            info["open_path"] = pdf
        return r, info
    else:
        raise ToolError(f"未知的工具 {name}")
    return r, {"status": "ok", "preview": r[:4000]}
