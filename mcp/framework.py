"""轻量 MCP 服务端框架 —— stdio 上的 JSON-RPC，零第三方依赖。

与本项目的客户端 mcp_client.py (StdioTransport) 对接，协议要点：
  * 每条消息一行 JSON，以 \\n 结尾；从 stdin 读，往 stdout 写。
  * stdout 只允许出现 JSON-RPC 报文；任何日志/调试一律走 stderr，
    否则会污染协议流，客户端会把它当成「非 JSON 输出」丢弃。
  * Windows 上必须把 stdin/stdout 强制成 UTF-8（客户端按 UTF-8 解析），
    本模块在导入时已处理。

写一个 agent 只需三步（见同目录的 datetime_agent.py / text_agent.py）：
    from framework import MCPServer
    srv = MCPServer("myname")
    @srv.tool("工具名", "给模型看的说明", {JSON Schema}, read_only=True)
    def my_tool(参数...): return "结果文本"
    srv.run()
"""
import json
import sys

PROTOCOL_VERSION = "2025-06-18"

# 关键：强制 UTF-8，且输出用 \n 结尾。否则 Windows 默认 GBK，中文会让客户端 json 解析失败。
try:
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8", newline="\n")
except AttributeError:  # 极老的 Python 没有 reconfigure，忽略
    pass


def log(*parts):
    """调试输出 —— 只能走 stderr。"""
    print("[mcp]", *parts, file=sys.stderr, flush=True)


class MCPServer:
    """一个 stdio MCP 服务端。注册若干工具后调用 run() 进入消息循环。"""

    def __init__(self, name, version="0.1.0"):
        self.name = name
        self.version = version
        self._tools = {}   # name -> {"spec": {...}, "fn": callable}

    def tool(self, name, description, input_schema=None, read_only=False):
        """装饰器：把一个函数注册为 MCP 工具。

        input_schema: JSON Schema(object)，描述参数；默认无参。
        read_only:    置 True 则声明 readOnlyHint，客户端会允许它与其它只读工具并行执行。
        """
        schema = input_schema or {"type": "object", "properties": {}}

        def deco(fn):
            spec = {"name": name, "description": description, "inputSchema": schema}
            if read_only:
                spec["annotations"] = {"readOnlyHint": True}
            self._tools[name] = {"spec": spec, "fn": fn}
            return fn
        return deco

    # ---------------------------------------------------------------- JSON-RPC
    def _handle(self, msg):
        """处理一条报文，返回要回复的对象；通知类（无需回复）返回 None。"""
        mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
        if method == "initialize":
            return self._ok(mid, {"protocolVersion": PROTOCOL_VERSION,
                                  "capabilities": {"tools": {}},
                                  "serverInfo": {"name": self.name, "version": self.version}})
        if method == "tools/list":
            return self._ok(mid, {"tools": [t["spec"] for t in self._tools.values()]})
        if method == "tools/call":
            return self._call(mid, params)
        if method == "ping":
            return self._ok(mid, {})
        if method and method.startswith("notifications/"):
            return None                      # 通知无 id，不回复
        if mid is not None:
            return self._err(mid, -32601, f"未知方法 {method}")
        return None

    def _call(self, mid, params):
        name, args = params.get("name"), params.get("arguments") or {}
        tool = self._tools.get(name)
        if not tool:
            return self._err(mid, -32602, f"未知工具 {name}")
        try:
            result = tool["fn"](**args) if isinstance(args, dict) else tool["fn"](args)
            text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, indent=2)
            return self._ok(mid, {"content": [{"type": "text", "text": text}], "isError": False})
        except Exception as e:                # 工具内部报错转成 isError，不让服务器崩溃
            log(f"工具 {name} 出错：{e}")
            return self._ok(mid, {"content": [{"type": "text", "text": f"错误：{e}"}], "isError": True})

    @staticmethod
    def _ok(mid, result):
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    @staticmethod
    def _err(mid, code, message):
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}

    def _send(self, obj):
        sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
        sys.stdout.flush()

    def run(self):
        """进入消息循环，阻塞直到 stdin 关闭（客户端断开）。"""
        log(f"{self.name} 启动，{len(self._tools)} 个工具，等待 stdio…")
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            for m in (msg if isinstance(msg, list) else [msg]):
                reply = self._handle(m)
                if reply is not None:
                    self._send(reply)
        log(f"{self.name} 退出。")
