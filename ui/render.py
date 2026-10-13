"""ui.render —— Markdown 渲染为 HTML、diff 彩色 HTML（界面展示用）。"""
import html

from markdown_it import MarkdownIt
from pygments import highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import get_lexer_by_name
from pygments.lexers.special import TextLexer
from pygments.util import ClassNotFound

from ui.theme import BORDER, CODE_BG, CODE_HEAD, MUTED, USER_BG

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
