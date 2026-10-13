"""text —— 一个示例 MCP agent：文本统计/处理的只读工具。

对「文档对比」场景比较实用：数字数、统计词频等。
"""
import os
import re
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from framework import MCPServer

srv = MCPServer("text", version="0.1.0")

_TEXT_ARG = {"type": "object",
             "properties": {"text": {"type": "string", "description": "要处理的文本"}},
             "required": ["text"]}


@srv.tool("stats", "统计一段文本的字符数、单词数、行数、非空行数。", _TEXT_ARG, read_only=True)
def stats(text):
    lines = text.splitlines()
    words = len(re.findall(r"[A-Za-z0-9]+", text)) + len(re.findall(r"[一-鿿]", text))
    return (f"字符数：{len(text)}\n词数（英文单词+中文字）：{words}\n"
            f"行数：{len(lines)}\n非空行数：{sum(1 for l in lines if l.strip())}")


@srv.tool(
    "top_words",
    "统计出现频率最高的若干个词（英文按单词、中文按单字）。",
    {"type": "object",
     "properties": {"text": {"type": "string", "description": "要分析的文本"},
                    "n": {"type": "integer", "description": "返回前几名，默认 10"}},
     "required": ["text"]},
    read_only=True,
)
def top_words(text, n=10):
    tokens = re.findall(r"[A-Za-z]+", text.lower()) + re.findall(r"[一-鿿]", text)
    top = Counter(tokens).most_common(int(n))
    return "\n".join(f"{i + 1}. {w}  ×{c}" for i, (w, c) in enumerate(top)) or "（没有可统计的词）"


if __name__ == "__main__":
    srv.run()
