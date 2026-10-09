"""提取目录内所有 datasheet PDF 的文本；扫描件自动渲染成 PNG 供视觉读取；可 diff 两个版本。

用法:
  python -X utf8 extract_pdfs.py <目录> [--out <输出目录>] [--dpi 110]
  python -X utf8 extract_pdfs.py --diff old.pdf new.pdf
"""
import argparse, difflib, os, sys, tempfile

try:
    import pypdf
except ImportError:
    sys.exit("缺少 pypdf: pip install pypdf")


def page_texts(path):
    return [(p.extract_text() or "") for p in pypdf.PdfReader(path).pages]


def lines(path):
    return [l.strip() for t in page_texts(path) for l in t.splitlines() if l.strip()]


def render(path, out_dir, dpi):
    try:
        import pymupdf
    except ImportError:
        print("  缺少 pymupdf，无法渲染扫描件: pip install pymupdf")
        return
    doc = pymupdf.open(path)
    tag = os.path.splitext(os.path.basename(path))[0].replace(" ", "_")
    for i, page in enumerate(doc):
        png = os.path.join(out_dir, f"{tag}_p{i + 1}.png")
        page.get_pixmap(dpi=dpi).save(png)
    print(f"  扫描件 -> 已渲染 {len(doc)} 页 PNG 到 {out_dir}（用 Read 工具看图读数）")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dir", nargs="?")
    ap.add_argument("--out")
    ap.add_argument("--dpi", type=int, default=110)
    ap.add_argument("--diff", nargs=2, metavar=("OLD", "NEW"))
    ap.add_argument("--scan-threshold", type=int, default=100, help="每页平均字符数低于此值视为扫描件")
    a = ap.parse_args()

    if a.diff:
        old, new = a.diff
        n = 0
        for l in difflib.unified_diff(lines(old), lines(new), lineterm="", n=0):
            if l.startswith(("+", "-")) and not l.startswith(("+++", "---")):
                print(l); n += 1
        print(f"\n共 {n} 行差异（页眉版本号行可忽略）")
        return

    if not a.dir:
        ap.error("需要目录参数或 --diff")
    out = a.out or os.path.join(tempfile.gettempdir(), "ds_extract")
    txt_dir, img_dir = os.path.join(out, "txt"), os.path.join(out, "img")
    os.makedirs(txt_dir, exist_ok=True); os.makedirs(img_dir, exist_ok=True)

    pdfs = sorted(f for f in os.listdir(a.dir) if f.lower().endswith(".pdf"))
    print(f"{len(pdfs)} 份 PDF，文本输出到 {txt_dir}\n")
    for f in pdfs:
        path = os.path.join(a.dir, f)
        try:
            texts = page_texts(path)
        except Exception as e:
            print(f"{f}: 读取失败 {e}"); continue
        full = "\n".join(texts)
        name = os.path.splitext(f)[0].replace(" ", "_")
        with open(os.path.join(txt_dir, name + ".txt"), "w", encoding="utf-8") as fh:
            fh.write(full)
        avg = len(full.strip()) / max(len(texts), 1)
        print(f"{f}: {len(texts)} 页, {len(full)} 字符" + ("  [扫描件]" if avg < a.scan_threshold else ""))
        if avg < a.scan_threshold:
            render(path, img_dir, a.dpi)


if __name__ == "__main__":
    main()
