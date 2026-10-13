"""datetime —— 一个示例 MCP agent：时间/日期相关的只读工具。

启动方式由 config/mcp_servers.json 配置（command=python, args=[本文件路径]）。
"""
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # 让 framework 可被导入
from framework import MCPServer

srv = MCPServer("datetime", version="0.1.0")


@srv.tool("now", "返回当前的日期和时间（本机时区）。", read_only=True)
def now():
    n = datetime.now()
    return n.strftime("%Y-%m-%d %H:%M:%S  星期") + "一二三四五六日"[n.weekday()]


@srv.tool(
    "days_between",
    "计算两个日期之间相差的天数（格式 YYYY-MM-DD）。",
    {"type": "object",
     "properties": {"start": {"type": "string", "description": "起始日期，如 2026-01-01"},
                    "end": {"type": "string", "description": "结束日期，如 2026-10-13"}},
     "required": ["start", "end"]},
    read_only=True,
)
def days_between(start, end):
    a = datetime.strptime(start, "%Y-%m-%d")
    b = datetime.strptime(end, "%Y-%m-%d")
    return f"{start} 到 {end} 相差 {(b - a).days} 天。"


@srv.tool(
    "add_days",
    "在某个日期上加减天数，返回新日期。",
    {"type": "object",
     "properties": {"date": {"type": "string", "description": "基准日期 YYYY-MM-DD"},
                    "days": {"type": "integer", "description": "要增加的天数，可为负数"}},
     "required": ["date", "days"]},
    read_only=True,
)
def add_days(date, days):
    d = datetime.strptime(date, "%Y-%m-%d") + timedelta(days=int(days))
    return d.strftime("%Y-%m-%d")


if __name__ == "__main__":
    srv.run()
