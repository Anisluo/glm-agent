"""MCP（Model Context Protocol）客户端。

连接配置文件 mcp_servers.json 中的 MCP 服务器，读取它们的工具，交给 GLM 调用。配置格式与
主流 MCP 客户端通用的 mcpServers 格式相同：

{
  "mcpServers": {
    "filesystem": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "D:/docs"]},
    "remote":     {"url": "https://example.com/mcp", "headers": {"Authorization": "Bearer xxx"}},
    "legacy":     {"url": "https://example.com/sse", "type": "sse"}
  }
}

每个服务器可选：env（环境变量）、cwd、disabled（true 则不连接）、timeout（工具调用超时秒数，默认 300）。
支持的传输：stdio、Streamable HTTP（2025-03-26 起的规范）、HTTP+SSE（2024-11-05 旧规范）。
"""
import itertools
import json
import logging
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import zlib
from urllib.parse import urljoin

import requests

PROTOCOL_VERSION = "2025-06-18"
CLIENT_INFO = {"name": "GLM-MCP-文档对比agent", "version": "1.0.0"}
NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
log = logging.getLogger("glm.mcp")

EXAMPLE_CONFIG = {
    "mcpServers": {
        "示例-文件系统（把 disabled 改为 false 启用）": {
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-filesystem", "D:/datasheets"],
            "disabled": True,
        },
        "示例-远程服务器": {
            "url": "https://example.com/mcp",
            "headers": {"Authorization": "Bearer <你的令牌>"},
            "disabled": True,
        },
    }
}


class MCPError(Exception):
    pass


class _Cancelled(MCPError):
    pass


# ---------------------------------------------------------------- 传输层
class _Pending:
    """请求 id → 等待响应的队列。"""

    def __init__(self):
        self._ids = itertools.count(1)
        self._waiting = {}
        self._lock = threading.Lock()

    def new(self):
        with self._lock:
            rid = next(self._ids)
            q = self._waiting[rid] = queue.Queue(1)
        return rid, q

    def resolve(self, msg):
        with self._lock:
            q = self._waiting.pop(msg.get("id"), None)
        if q:
            q.put(msg)

    def drop(self, rid):
        with self._lock:
            self._waiting.pop(rid, None)

    def fail_all(self, text):
        with self._lock:
            items, self._waiting = list(self._waiting.items()), {}
        for rid, q in items:
            q.put({"id": rid, "error": {"message": text}})


def _wait(q, rid, method, timeout, cancel, on_cancel):
    deadline = time.time() + timeout
    while True:
        if cancel is not None and cancel.is_set():
            on_cancel(rid)
            raise _Cancelled("已停止")
        try:
            msg = q.get(timeout=0.2)
            break
        except queue.Empty:
            if time.time() > deadline:
                on_cancel(rid)
                raise MCPError(f"{method} 超时（{timeout} 秒）")
    if "error" in msg:
        err = msg["error"]
        raise MCPError(err.get("message", str(err)) if isinstance(err, dict) else str(err))
    return msg.get("result", {})


class StdioTransport:
    """本地子进程，按行收发 JSON-RPC。"""

    def __init__(self, name, cfg, on_notify):
        self.name, self.on_notify = name, on_notify
        cmd = cfg.get("command")
        if not cmd:
            raise MCPError("缺少 command")
        exe = shutil.which(cmd) or cmd  # Windows 下 npx 等是 .cmd，需要解析成完整路径
        env = dict(os.environ)
        env.update({k: str(v) for k, v in (cfg.get("env") or {}).items()})
        try:
            self.proc = subprocess.Popen([exe, *[str(a) for a in cfg.get("args", [])]], cwd=cfg.get("cwd") or None,
                                         env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=subprocess.PIPE, creationflags=NO_WINDOW)
        except OSError as e:
            raise MCPError(f"无法启动 {cmd}：{e}")
        self.pending = _Pending()
        self._wlock = threading.Lock()
        self.stderr_tail = []
        threading.Thread(target=self._read, name=f"mcp-{name}-out", daemon=True).start()
        threading.Thread(target=self._read_err, name=f"mcp-{name}-err", daemon=True).start()

    def _read(self):
        for line in self.proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                log.info("[%s] 非 JSON 输出：%s", self.name, line[:200])
                continue
            for m in msg if isinstance(msg, list) else [msg]:
                self._dispatch(m)
        tail = " | ".join(self.stderr_tail[-3:])
        self.pending.fail_all("服务器进程已退出" + (f"：{tail}" if tail else ""))
        self.on_notify("closed", {})

    def _read_err(self):
        for line in self.proc.stderr:
            text = line.decode("utf-8", "replace").rstrip()
            if text:
                self.stderr_tail = (self.stderr_tail + [text])[-20:]
                log.info("[%s] %s", self.name, text[:300])

    def _dispatch(self, m):
        if "id" in m and ("result" in m or "error" in m):
            self.pending.resolve(m)
        elif "method" in m and "id" in m:  # 服务器发来的请求
            if m["method"] == "ping":
                self._send({"jsonrpc": "2.0", "id": m["id"], "result": {}})
            else:
                self._send({"jsonrpc": "2.0", "id": m["id"],
                            "error": {"code": -32601, "message": f"客户端不支持 {m['method']}"}})
        elif "method" in m:
            self.on_notify(m["method"], m.get("params") or {})

    def _send(self, obj):
        data = (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
        with self._wlock:
            try:
                self.proc.stdin.write(data)
                self.proc.stdin.flush()
            except OSError as e:
                raise MCPError(f"写入服务器失败：{e}")

    def request(self, method, params, timeout=60, cancel=None):
        if self.proc.poll() is not None:
            raise MCPError("服务器进程已退出")
        rid, q = self.pending.new()
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})
        return _wait(q, rid, method, timeout, cancel, self._cancel)

    def _cancel(self, rid):
        self.pending.drop(rid)
        try:
            self.notify("notifications/cancelled", {"requestId": rid, "reason": "客户端取消"})
        except MCPError:
            pass

    def notify(self, method, params=None):
        self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def close(self):
        if self.proc.poll() is None:
            try:
                self.proc.stdin.close()
                self.proc.wait(2)
            except Exception:
                pass
        if self.proc.poll() is None:
            if sys.platform == "win32":
                subprocess.run(["taskkill", "/T", "/F", "/PID", str(self.proc.pid)], capture_output=True,
                               creationflags=NO_WINDOW)
            else:
                self.proc.kill()


def _sse_events(resp):
    """解析 text/event-stream，逐个产出 (event, data)。

    按行直接读底层连接：requests 的 iter_lines 会攒够一块数据才返回，短小的事件（如 endpoint）会被卡住。
    """
    resp.raw.decode_content = True
    event, data = "message", []
    for raw in iter(resp.raw.readline, b""):
        line = raw.decode("utf-8", "replace").rstrip("\r\n")
        if not line:
            if data:
                yield event, "\n".join(data)
            event, data = "message", []
        elif line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            data.append(line[5:].lstrip())
    if data:
        yield event, "\n".join(data)


class HttpTransport:
    """Streamable HTTP：每个请求 POST，响应为 JSON 或 SSE 流。"""

    def __init__(self, name, cfg, on_notify):
        self.name, self.url, self.on_notify = name, cfg["url"], on_notify
        self.headers = {k: str(v) for k, v in (cfg.get("headers") or {}).items()}
        self.session_id = None
        self.http = requests.Session()
        self._ids = itertools.count(1)

    def _headers(self):
        h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
             "MCP-Protocol-Version": PROTOCOL_VERSION, **self.headers}
        if self.session_id:
            h["Mcp-Session-Id"] = self.session_id
        return h

    def request(self, method, params, timeout=60, cancel=None):
        rid = next(self._ids)
        box = {}

        def run():
            try:
                box["result"] = self._post({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}},
                                           rid, timeout)
            except Exception as e:
                box["error"] = e
        t = threading.Thread(target=run, daemon=True)
        t.start()
        deadline = time.time() + timeout + 5
        while t.is_alive():
            t.join(0.2)
            if cancel is not None and cancel.is_set():
                self._notify_quiet("notifications/cancelled", {"requestId": rid, "reason": "客户端取消"})
                raise _Cancelled("已停止")
            if time.time() > deadline:
                raise MCPError(f"{method} 超时（{timeout} 秒）")
        if "error" in box:
            raise box["error"] if isinstance(box["error"], MCPError) else MCPError(str(box["error"]))
        return box["result"]

    def _post(self, body, rid, timeout):
        try:
            r = self.http.post(self.url, json=body, headers=self._headers(), timeout=(15, timeout), stream=True)
        except requests.RequestException as e:
            raise MCPError(f"连接失败：{e}")
        with r:
            if r.headers.get("Mcp-Session-Id"):
                self.session_id = r.headers["Mcp-Session-Id"]
            if r.status_code == 404 and self.session_id:
                raise MCPError("会话已过期，请重新连接")
            if r.status_code >= 400:
                raise MCPError(f"HTTP {r.status_code}：{r.text[:300]}")
            ctype = r.headers.get("Content-Type", "")
            if "text/event-stream" in ctype:
                for _, data in _sse_events(r):
                    try:
                        msg = json.loads(data)
                    except ValueError:
                        continue
                    for m in msg if isinstance(msg, list) else [msg]:
                        if m.get("id") == rid and ("result" in m or "error" in m):
                            return self._unwrap(m)
                        if "method" in m and "id" not in m:
                            self.on_notify(m["method"], m.get("params") or {})
                raise MCPError("服务器关闭了连接但没有返回结果")
            if r.status_code == 202 or not r.content:
                return {}
            msg = r.json()
            for m in msg if isinstance(msg, list) else [msg]:
                if m.get("id") == rid:
                    return self._unwrap(m)
            raise MCPError("响应中没有对应的结果")

    @staticmethod
    def _unwrap(m):
        if "error" in m:
            err = m["error"]
            raise MCPError(err.get("message", str(err)) if isinstance(err, dict) else str(err))
        return m.get("result", {})

    def _notify_quiet(self, method, params):
        try:
            self.notify(method, params)
        except Exception:
            pass

    def notify(self, method, params=None):
        try:
            self.http.post(self.url, json={"jsonrpc": "2.0", "method": method, "params": params or {}},
                           headers=self._headers(), timeout=15).close()
        except requests.RequestException as e:
            raise MCPError(str(e))

    def close(self):
        if self.session_id:
            try:
                self.http.delete(self.url, headers=self._headers(), timeout=5)
            except requests.RequestException:
                pass
        self.http.close()


class SseTransport:
    """旧版 HTTP+SSE：GET 建立事件流，服务器先发 endpoint 事件，之后 POST 请求、响应从事件流返回。"""

    def __init__(self, name, cfg, on_notify):
        self.name, self.url, self.on_notify = name, cfg["url"], on_notify
        self.headers = {k: str(v) for k, v in (cfg.get("headers") or {}).items()}
        self.http = requests.Session()
        self.pending = _Pending()
        self.endpoint = None
        self._ready = threading.Event()
        self._closed = False
        self._error = None
        threading.Thread(target=self._listen, name=f"mcp-{name}-sse", daemon=True).start()
        if not self._ready.wait(20):
            raise MCPError(self._error or "等待 SSE endpoint 超时")
        if not self.endpoint:
            raise MCPError(self._error or "SSE 连接失败")

    def _listen(self):
        try:
            r = self.http.get(self.url, headers={"Accept": "text/event-stream", **self.headers}, stream=True,
                              timeout=(15, None))
            if r.status_code >= 400:
                self._error = f"HTTP {r.status_code}：{r.text[:200]}"
                self._ready.set()
                return
            for event, data in _sse_events(r):
                if self._closed:
                    break
                if event == "endpoint":
                    self.endpoint = urljoin(self.url, data.strip())
                    self._ready.set()
                    continue
                try:
                    msg = json.loads(data)
                except ValueError:
                    continue
                for m in msg if isinstance(msg, list) else [msg]:
                    if "id" in m and ("result" in m or "error" in m):
                        self.pending.resolve(m)
                    elif "method" in m and "id" not in m:
                        self.on_notify(m["method"], m.get("params") or {})
        except Exception as e:
            self._error = f"SSE 连接中断：{e}"
        self._ready.set()
        self.pending.fail_all(self._error or "SSE 连接已关闭")
        if not self._closed:
            self.on_notify("closed", {})

    def _post(self, body):
        try:
            r = self.http.post(self.endpoint, json=body, headers={"Content-Type": "application/json", **self.headers},
                               timeout=30)
        except requests.RequestException as e:
            raise MCPError(f"连接失败：{e}")
        if r.status_code >= 400:
            raise MCPError(f"HTTP {r.status_code}：{r.text[:300]}")

    def request(self, method, params, timeout=60, cancel=None):
        rid, q = self.pending.new()
        self._post({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})
        return _wait(q, rid, method, timeout, cancel, self._cancel)

    def _cancel(self, rid):
        self.pending.drop(rid)
        try:
            self.notify("notifications/cancelled", {"requestId": rid, "reason": "客户端取消"})
        except MCPError:
            pass

    def notify(self, method, params=None):
        self._post({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def close(self):
        self._closed = True
        self.http.close()


# ---------------------------------------------------------------- 服务器与管理器
class MCPServer:
    def __init__(self, name, cfg):
        self.name, self.cfg = name, cfg
        self.status = "disabled" if cfg.get("disabled") else "pending"   # pending / connecting / ok / error / disabled
        self.error = ""
        self.tools = []
        self.info = {}
        self.transport = None
        self._lock = threading.Lock()

    @property
    def kind(self):
        if self.cfg.get("command"):
            return "stdio"
        return "sse" if str(self.cfg.get("type", "")).lower() == "sse" else "http"

    def connect(self, on_change):
        self.status, self.error = "connecting", ""
        on_change()
        try:
            cls = {"stdio": StdioTransport, "sse": SseTransport, "http": HttpTransport}[self.kind]
            if self.kind != "stdio" and not self.cfg.get("url"):
                raise MCPError("缺少 command 或 url")

            def notify(method, params):
                if method == "notifications/tools/list_changed":
                    threading.Thread(target=self._reload_tools, args=(on_change,), daemon=True).start()
                elif method == "closed" and self.status == "ok":
                    self.status, self.error = "error", "连接已断开"
                    on_change()
            self.transport = cls(self.name, self.cfg, notify)
            init = self.transport.request("initialize", {
                "protocolVersion": PROTOCOL_VERSION, "capabilities": {}, "clientInfo": CLIENT_INFO}, timeout=60)
            self.info = init.get("serverInfo") or {}
            self.transport.notify("notifications/initialized")
            self.tools = self._list_tools()
            self.status = "ok"
            log.info("MCP 服务器 %s 已连接：%s，%d 个工具", self.name, self.info.get("name", ""), len(self.tools))
        except Exception as e:
            self.status, self.error = "error", str(e)
            log.warning("MCP 服务器 %s 连接失败：%s", self.name, e)
            self.close()
        on_change()

    def _list_tools(self):
        tools, cursor = [], None
        for _ in range(50):
            res = self.transport.request("tools/list", {"cursor": cursor} if cursor else {}, timeout=60)
            tools += [t for t in res.get("tools", []) if isinstance(t, dict) and t.get("name")]
            cursor = res.get("nextCursor")
            if not cursor:
                break
        return tools

    def _reload_tools(self, on_change):
        try:
            self.tools = self._list_tools()
            on_change()
        except Exception as e:
            log.warning("MCP 服务器 %s 刷新工具失败：%s", self.name, e)

    def call(self, tool, args, cancel=None):
        if self.status != "ok" or not self.transport:
            raise MCPError(f"服务器 {self.name} 未连接" + (f"：{self.error}" if self.error else ""))
        timeout = int(self.cfg.get("timeout") or 300)
        return self.transport.request("tools/call", {"name": tool, "arguments": args or {}}, timeout, cancel)

    def close(self):
        t, self.transport = self.transport, None
        if t:
            try:
                t.close()
            except Exception:
                pass


def format_result(res):
    """把 tools/call 的结果转成给模型看的文本。"""
    parts = []
    for c in res.get("content") or []:
        kind = c.get("type")
        if kind == "text":
            parts.append(c.get("text", ""))
        elif kind == "image":
            parts.append(f"[图片 {c.get('mimeType', '')}，本客户端不显示图片内容]")
        elif kind == "audio":
            parts.append(f"[音频 {c.get('mimeType', '')}]")
        elif kind == "resource":
            r = c.get("resource") or {}
            parts.append(r.get("text") or f"[资源 {r.get('uri', '')}]")
        elif kind == "resource_link":
            parts.append(f"[资源链接 {c.get('name', '')} {c.get('uri', '')}]")
    if not parts and res.get("structuredContent") is not None:
        parts.append(json.dumps(res["structuredContent"], ensure_ascii=False, indent=2))
    text = "\n".join(p for p in parts if p) or "（工具没有返回内容）"
    if len(text) > 30000:
        text = text[:30000] + "\n…（结果过长，已截断）"
    return text, bool(res.get("isError"))


def _clean_schema(schema):
    s = dict(schema) if isinstance(schema, dict) else {}
    s.pop("$schema", None)
    s.setdefault("type", "object")
    if s.get("type") == "object":
        s.setdefault("properties", {})
    return s


class MCPManager:
    def __init__(self, config_path):
        self.config_path = config_path
        self.servers = {}
        self.names = {}          # 给模型的函数名 → (服务器名, 工具名)
        self.on_change = lambda: None
        self.config_error = ""

    def ensure_config(self):
        if not os.path.exists(self.config_path):
            with open(self.config_path, "w", encoding="utf-8") as f:
                json.dump(EXAMPLE_CONFIG, f, ensure_ascii=False, indent=2)

    def read_config(self):
        self.ensure_config()
        with open(self.config_path, encoding="utf-8-sig") as f:
            data = json.load(f)
        servers = data.get("mcpServers", data.get("servers", {})) if isinstance(data, dict) else None
        if not isinstance(servers, dict):
            raise ValueError("配置里需要有 mcpServers 对象")
        return servers

    def start_all(self):
        """（重新）读取配置并在后台连接所有服务器。"""
        self.stop_all()
        try:
            servers = self.read_config()
            self.config_error = ""
        except (OSError, ValueError) as e:
            self.config_error = f"MCP 配置有误：{e}"
            log.warning(self.config_error)
            servers = {}
        self.servers = {n: MCPServer(n, c if isinstance(c, dict) else {}) for n, c in servers.items()}
        for s in self.servers.values():
            if s.status != "disabled":
                threading.Thread(target=s.connect, args=(self._changed,), name=f"mcp-connect-{s.name}",
                                 daemon=True).start()
        self._changed()

    def _changed(self):
        try:
            self.on_change()
        except Exception:
            log.exception("MCP 状态回调出错")

    def stop_all(self):
        for s in self.servers.values():
            s.close()

    def tool_specs(self):
        specs, names = [], {}
        for s in list(self.servers.values()):
            if s.status != "ok":
                continue
            # 函数名只能用英文字母数字；中文服务器名换成稳定的短编码，真实名称写在说明里
            srv = re.sub(r"[^a-zA-Z0-9-]+", "_", s.name).strip("_")
            if len(srv) < 2:
                srv = "s" + format(zlib.crc32(s.name.encode("utf-8")), "08x")[:5]
            for t in s.tools:
                base = re.sub(r"[^a-zA-Z0-9_-]", "_", f"mcp__{srv}__{t['name']}")[:60]
                fname, k = base, 2
                while fname in names:
                    fname, k = f"{base[:57]}_{k}", k + 1
                names[fname] = (s.name, t["name"])
                desc = f"[MCP 服务器 {s.name}] {t.get('description') or t.get('title') or t['name']}"
                specs.append({"type": "function", "function": {
                    "name": fname, "description": desc[:1024], "parameters": _clean_schema(t.get("inputSchema"))}})
        self.names = names
        return specs

    def resolve(self, fname):
        if fname not in self.names:
            self.tool_specs()
        return self.names.get(fname)

    def tool_info(self, fname):
        hit = self.resolve(fname)
        if not hit:
            return None, None
        s = self.servers.get(hit[0])
        t = next((t for t in (s.tools if s else []) if t["name"] == hit[1]), None)
        return s, t

    def display(self, fname, args):
        hit = self.resolve(fname)
        name = f"{hit[0]} · {hit[1]}" if hit else fname
        brief = json.dumps(args, ensure_ascii=False) if isinstance(args, dict) and args else ""
        return f"{name}  {brief[:80]}" if brief else name

    def call(self, fname, args, cancel=None):
        """在后台线程中调用，返回 (结果文本, 卡片信息)。"""
        hit = self.resolve(fname)
        if not hit:
            raise MCPError("找不到这个 MCP 工具（服务器可能已断开），请刷新后重试")
        s = self.servers[hit[0]]
        try:
            res = s.call(hit[1], args if isinstance(args, dict) else {}, cancel)
        except _Cancelled:
            return "用户已停止这次 MCP 调用。", {"status": "skipped"}
        text, is_error = format_result(res)
        return text, {"status": "error" if is_error else "ok", "preview": text[:4000]}

    def summary(self):
        ok = [s for s in self.servers.values() if s.status == "ok"]
        return len(ok), sum(len(s.tools) for s in ok)
