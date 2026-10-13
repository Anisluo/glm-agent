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
- 第 3 步生成 .tex → **优先用 render_report**：把整理好的参数、点评、勘误、图表数据组织成 JSON（见工具说明），一次调用即可生成 .tex，程序自动处理转义、表格列宽、TikZ 图表刻度和 best/worst 上色，避免手写出错反复修。只有 render_report 覆盖不了的特殊版式，才 get_report_template + write_file 手写，或用 edit_file 局部微调 render_report 的产物。
- 第 4 步编译 → build_pdf(path=".tex 文件")，它会跑两遍 xelatex，返回错误与 ≥10pt 的溢出并清理 .aux/.log/.out。用 render_report 生成的 tex 通常一次编译通过；若仍有错误或显著溢出就修改 .tex 再编译，直到清零。本软件不能渲染页面预览，以日志为准。
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
        "name": "render_report",
        "description": ("【推荐】用结构化数据直接生成对比报告 .tex：你只需提供 JSON 数据，程序会自动完成 LaTeX 转义、"
                        "参数表列宽（≤5 款竖版 / >5 款横版）、TikZ 图表刻度（按数据最大值反算，杜绝溢出）、"
                        "best/worst 上色。生成后用 build_pdf 编译。优先用它，而不是手写 .tex。"),
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "输出的 .tex 文件名，默认 <theme>对比报告.tex"},
            "data": {"type": "object", "description": (
                "报告数据。字段：theme(主题)、date、test_condition、source_dir、overview_intro、"
                "overview_bullets(字符串数组)、conclusion；devices(数组，每项 file/model/vendor/version/note)；"
                "parameters(数组，每项 name、values(按 devices 顺序的字符串数组，缺项留空)、可选 best/worst 为最优/最差列的下标)；"
                "charts(数组，每项 title、note、bars[{name,value数值}])；reviews(数组 {model,vendor,text})；errata(字符串数组)。"
                "数值和单位写普通文本即可（如 ±18.5%、-40--125、°C），不要写 LaTeX 命令。")}},
            "required": ["data"]}}},
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
                "get_report_template": ("📐", "读取报告模板"), "render_report": ("🧩", "生成报告"),
                "build_pdf": ("🛠", "编译 PDF")}


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


# ---------------------------------------------------------------- 结构化报告渲染
# 让模型只产出结构化数据，.tex 由代码确定性生成：转义、列宽、TikZ 刻度都算好，
# 从根上避免「手写 tex → Overfull/语法错 → 反复改」的循环。
_TEX_ESCAPE = [("\\", r"\textbackslash "), ("&", r"\&"), ("%", r"\%"), ("#", r"\#"),
               ("_", r"\_"), ("{", r"\{"), ("}", r"\}"),
               ("~", r"\textasciitilde "), ("^", r"\textasciicircum ")]
_TEX_SYMBOL = [("±", r"$\pm$"), ("×", r"$\times$"), ("≤", r"$\le$"), ("≥", r"$\ge$"),
               ("≈", r"$\approx$"), ("µ", r"$\mu$"), ("μ", r"$\mu$"), ("Ω", r"$\Omega$"),
               ("°", r"$^\circ$"), ("−", "-"), ("∞", r"$\infty$")]


def _tex(s):
    """把用户文本转义成安全的 LaTeX；常见单位/符号转成数学写法。"""
    s = str(s if s is not None else "")
    for a, b in _TEX_ESCAPE:
        s = s.replace(a, b)
    for a, b in _TEX_SYMBOL:
        s = s.replace(a, b)
    return s or r"\nd"


def _num(v):
    f = float(v)
    return f"{f:.4g}"


def _param_table(devices, params):
    """参数总表：≤5 款竖版、>5 款横版（landscape）；列宽按版心自动算，杜绝列宽溢出。"""
    n = len(devices)
    land = n > 5
    # 扣掉每列两侧的 \tabcolsep(4pt≈0.141cm）和一点安全余量，避免表格宽于版心
    sep = 0.141
    usable = (25.4 if land else 16.6) - 2 * (n + 1) * sep - 0.3
    first = 2.9 if land else 3.4
    w = max(1.6, round((usable - first) / max(n, 1), 2))
    cols = f">{{\\raggedright\\arraybackslash}}p{{{first}cm}}" + f"*{{{n}}}{{>{{\\centering\\arraybackslash}}p{{{w}cm}}}}"
    head = r"\rowcolor{hdr}\hd{参数}" + "".join(f" & \\hd{{{_tex(d.get('model', ''))}}}" for d in devices) + r"\\"
    rows = []
    for k, p in enumerate(params):
        vals = list(p.get("values", []))
        vals += [""] * (n - len(vals))
        cells = []
        for j in range(n):
            cell = _tex(vals[j]) if str(vals[j]).strip() else r"\nd"
            if p.get("best") == j:
                cell = f"\\best{{{cell}}}"
            elif p.get("worst") == j:
                cell = f"\\worst{{{cell}}}"
            cells.append(cell)
        rc = r"\rowcolor{rowa}" if k % 2 else ""
        rows.append(rc + _tex(p.get("name", "")) + " & " + " & ".join(cells) + r"\\")
    body = ("\\begin{landscape}\n\\small\n" if land else "")
    body += "\\setlength{\\tabcolsep}{4pt}\n"
    body += (f"\\begin{{longtable}}{{{cols}}}\n\\toprule\n{head}\n\\midrule\n\\endhead\n"
             + "\n".join(rows) + "\n\\bottomrule\n\\end{longtable}\n")
    body += "绿色为该行最优，红色为需留意的短板，“--”表示规格书未给出。\n"
    body += ("\\normalsize\n\\end{landscape}\n" if land else "")
    return body


def _chart(ch, idx):
    """单向条形图：x 刻度由数据最大值反算，使最长条 ≈ 6cm，从根本上避免水平溢出。"""
    bars = [b for b in ch.get("bars", []) if isinstance(b.get("value", None), (int, float))]
    if not bars:
        return ""
    maxv = max(b["value"] for b in bars) or 1.0
    xu = round(6.0 / maxv, 5)          # 最长条固定 6cm
    pad = maxv * 0.02
    lines = [f"\\begin{{tikzpicture}}[x={xu}cm,y=1cm]  % 自动刻度：满刻度 {_num(maxv)} × x ≈ 6cm"]
    for i, b in enumerate(bars, 1):
        y = -0.5 * i
        name, v = _tex(b.get("name", "")), float(b["value"])
        lines.append(f"\\node[anchor=east,font=\\small] at (-{pad:.4g},{y}) {{{name}}};")
        lines.append(f"\\fill[trk]  (0,{y}-0.14) rectangle ({_num(maxv)},{y}+0.14);")
        lines.append(f"\\fill[barb] (0,{y}-0.14) rectangle ({_num(v)},{y}+0.14);")
        lines.append(f"\\node[anchor=west,font=\\scriptsize] at ({_num(v)}+{pad:.4g},{y}) {{{_num(v)}}};")
    lines.append("\\end{tikzpicture}")
    title = _tex(ch.get("title", f"图 {idx}"))
    note = _tex(ch.get("note", ""))
    cap = f"\n\n\\small 图 {idx}：{note}。" if note else ""
    return (f"\\begin{{center}}\n\\textbf{{\\small {title}}}\\par\\vspace{{2pt}}\n"
            + "\n".join(lines) + cap + "\n\\end{center}\n")


def render_report(data, cwd, path):
    """用结构化数据（data）确定性生成对比报告 .tex，返回 (结果文本, 是否成功, 文件绝对路径)。"""
    if not isinstance(data, dict):
        raise ToolError("data 必须是对象（JSON）")
    devices = data.get("devices") or []
    params = data.get("parameters") or []
    if not devices:
        raise ToolError("devices 不能为空（至少一款器件）")
    rel = path or (str(data.get("theme", "对比")) + "对比报告.tex")
    if not rel.lower().endswith(".tex"):
        rel += ".tex"
    out = safe_path(cwd, rel)

    models = " / ".join(_tex(d.get("model", "")) for d in devices)
    n = len(devices)
    P = [r"\documentclass[11pt,a4paper]{ctexart}",
         r"\usepackage[margin=2.0cm]{geometry}",
         r"\usepackage{booktabs,longtable,array,multirow}",
         r"\usepackage[table]{xcolor}", r"\usepackage{pdflscape}", r"\usepackage{tikz}",
         r"\usepackage{hyperref}",
         r"\hypersetup{colorlinks=true,linkcolor=blue!60!black,urlcolor=blue!60!black}",
         r"\definecolor{hdr}{RGB}{31,78,121}", r"\definecolor{rowa}{RGB}{240,245,250}",
         r"\definecolor{barb}{RGB}{47,111,179}", r"\definecolor{barr}{RGB}{184,58,58}",
         r"\definecolor{trk}{RGB}{236,239,243}",
         r"\newcommand{\best}[1]{\textcolor{green!45!black}{\textbf{#1}}}",
         r"\newcommand{\worst}[1]{\textcolor{red!70!black}{\textbf{#1}}}",
         r"\newcommand{\nd}{--}", r"\newcommand{\hd}[1]{\textcolor{white}{\textbf{#1}}}",
         r"\renewcommand{\arraystretch}{1.3}",
         f"\\title{{\\textbf{{{_tex(data.get('theme', ''))} {n} 款竞品对比报告}}\\\\[4pt]\\large {models}}}",
         f"\\author{{基于目录内 {n} 份原厂规格书整理}}",
         f"\\date{{{_tex(data.get('date', ''))}}}",
         r"\begin{document}", r"\maketitle", ""]

    P.append(r"\section{概述与结论}")
    if data.get("overview_intro"):
        P.append(_tex(data["overview_intro"]))
    bullets = data.get("overview_bullets") or []
    if bullets:
        P.append(r"\begin{itemize}")
        P += [f"  \\item {_tex(b)}" for b in bullets]
        P.append(r"\end{itemize}")
    if data.get("conclusion"):
        P.append(f"\\textbf{{{_tex(data['conclusion'])}}}")
    P.append("")

    # 样本说明
    P.append(r"\section{样本说明}")
    P.append(r"\setlength{\tabcolsep}{4pt}")
    P.append(r"\begin{longtable}{>{\raggedright\arraybackslash}p{3.6cm}>{\raggedright\arraybackslash}p{2.8cm}"
             r">{\raggedright\arraybackslash}p{2.2cm}>{\raggedright\arraybackslash}p{2.6cm}"
             r">{\raggedright\arraybackslash}p{3.2cm}}")
    P.append(r"\toprule")
    P.append(r"\rowcolor{hdr}\hd{文件} & \hd{型号} & \hd{厂商} & \hd{版本 / 日期} & \hd{备注}\\")
    P.append(r"\midrule" + "\n\\endhead")
    for k, d in enumerate(devices):
        rc = r"\rowcolor{rowa}" if k % 2 else ""
        P.append(rc + " & ".join(_tex(d.get(x, "")) for x in ("file", "model", "vendor", "version", "note")) + r"\\")
    P.append(r"\bottomrule" + "\n\\end{longtable}\n")

    # 参数总表
    if params:
        P.append(f"\\section{{参数总表（{_tex(data.get('test_condition', '典型值'))}）}}")
        P.append(_param_table(devices, params))

    # 关键参数图
    charts = data.get("charts") or []
    if charts:
        P.append(r"\section{关键参数图}")
        for i, ch in enumerate(charts, 1):
            P.append(_chart(ch, i))

    # 逐款点评
    reviews = data.get("reviews") or []
    if reviews:
        P.append(r"\section{逐款点评}")
        P.append(r"\begin{description}")
        for r in reviews:
            head = _tex(r.get("model", ""))
            if r.get("vendor"):
                head += f"（{_tex(r['vendor'])}）"
            P.append(f"  \\item[{head}] {_tex(r.get('text', ''))}")
        P.append(r"\end{description}")

    # 勘误
    errata = data.get("errata") or []
    if errata:
        P.append(r"\section{规格书疑点与勘误}")
        P.append(r"\begin{enumerate}")
        P += [f"  \\item {_tex(e)}" for e in errata]
        P.append(r"\end{enumerate}")

    if data.get("source_dir"):
        P.append(f"\n\\vspace{{1em}}\n\\noindent\\small 数据来源：目录 \\texttt{{{_tex(data['source_dir'])}}}"
                 f" 内 {n} 份 PDF。")
    P.append(r"\end{document}")

    tex = "\n".join(P) + "\n"
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        f.write(tex)
    msg = (f"已生成 {os.path.basename(out)}（{n} 款器件、{len(params)} 个参数、{len(charts)} 张图）。"
           f"图表刻度与表格列宽已自动算好，接下来直接用 build_pdf 编译即可。")
    return msg, True, out


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
    if name == "render_report":
        r, ok, out = render_report(args.get("data"), cwd, args.get("path"))
        return r, {"status": "ok" if ok else "error", "preview": r}
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
