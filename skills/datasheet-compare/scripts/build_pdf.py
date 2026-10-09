"""编译 .tex 两遍，汇报错误与显著溢出，渲染页面 PNG 供检查，清理中间文件。

用法: python -X utf8 build_pdf.py <文件.tex> [--dpi 55] [--keep]
退出码: 0 成功且无错误；1 有 LaTeX 错误或未生成 PDF
"""
import argparse, os, re, subprocess, sys


def run(tex):
    return subprocess.run(["xelatex", "-interaction=nonstopmode", tex], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", cwd=os.path.dirname(os.path.abspath(tex)) or ".")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("tex"); ap.add_argument("--dpi", type=int, default=55); ap.add_argument("--keep", action="store_true")
    a = ap.parse_args()
    tex = os.path.abspath(a.tex); d = os.path.dirname(tex); base = os.path.splitext(os.path.basename(tex))[0]

    run(tex); r = run(tex)
    out = r.stdout
    errors = [l for l in out.splitlines() if l.startswith("!")]
    over = [(float(m.group(1)), l) for l in out.splitlines()
            for m in [re.search(r"Overfull \\hbox \(([\d.]+)pt too wide", l)] if m and float(m.group(1)) >= 10]
    pdf = os.path.join(d, base + ".pdf")

    print("== LaTeX 错误 ==" if errors else "== 无 LaTeX 错误 ==")
    for e in errors[:15]: print("  " + e)
    print(f"== 显著溢出 (>=10pt): {len(over)} 处 ==")
    for pt, l in over[:15]: print(f"  {pt:.0f}pt  {l.strip()}")
    if not os.path.exists(pdf):
        print("未生成 PDF"); sys.exit(1)

    if not a.keep:
        for ext in (".aux", ".log", ".out", ".toc"):
            p = os.path.join(d, base + ext)
            if os.path.exists(p): os.remove(p)
    try:
        import pymupdf
        doc = pymupdf.open(pdf)
        img_dir = os.path.join(d, "_preview"); os.makedirs(img_dir, exist_ok=True)
        for i, pg in enumerate(doc):
            pg.get_pixmap(dpi=a.dpi).save(os.path.join(img_dir, f"p{i + 1}.png"))
        print(f"PDF {len(doc)} 页；预览 PNG 在 {img_dir}（检查完可删）")
    except ImportError:
        print("未装 pymupdf，跳过预览渲染")
    sys.exit(1 if errors else 0)


if __name__ == "__main__":
    main()
