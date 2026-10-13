"""core.worker —— 网络请求线程与一次性请求（与界面无关）。"""
import json
import logging
import re
import threading
import time

import requests
from PyQt6.QtCore import QThread, pyqtSignal

from memory_store import COMPRESS_PROMPT
from shell_tools import SHELL_TOOL_NAMES, run_shell_tool
from skill_tools import run_skill_tool

log = logging.getLogger("glm")

MAX_PARALLEL = 3                              # 同时向接口发请求的对话数上限，其余排队
API_SLOTS = threading.Semaphore(MAX_PARALLEL)
RETRIES = 4                                   # 连接失败 / 5xx / 并发限流时最多尝试 4 次
QUOTA_WORDS = ("余额", "额度", "上限", "充值", "资源包")
SUMMARY_MARK = "⟦较早对话摘要⟧"
HTTP = requests.Session()   # 复用连接，省去每轮请求重新握手 HTTPS 的时间
HTTP.mount("https://", requests.adapters.HTTPAdapter(pool_connections=4, pool_maxsize=16))


def chat_once(cfg, messages, effort="low", timeout=300, use_slot=True, stop=None):
    """非流式请求一次模型（压缩对话、提炼记忆用），返回回复文本；失败抛 RuntimeError。"""
    if use_slot:
        while not API_SLOTS.acquire(timeout=0.2):
            if stop and stop():
                raise RuntimeError("已停止")
    try:
        payload = {"model": cfg["model"], "messages": messages, "stream": False, "temperature": 0.3,
                   "reasoning_effort": effort}
        url = cfg["base_url"].rstrip("/") + "/chat/completions"
        last = "未知错误"
        for attempt in range(1, RETRIES + 1):
            if stop and stop():
                raise RuntimeError("已停止")
            try:
                r = HTTP.post(url, json=payload, timeout=(15, timeout),
                                  headers={"Authorization": "Bearer " + cfg["api_key"]})
            except (requests.ConnectionError, requests.Timeout) as e:
                last = f"网络连接失败：{e}"
            else:
                if r.status_code == 200:
                    j = r.json()
                    return ((j.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
                try:
                    msg = r.json().get("error", {}).get("message") or r.text
                except ValueError:
                    msg = r.text
                last = f"HTTP {r.status_code}: {msg[:300]}"
                if not (r.status_code >= 500 or (r.status_code == 429 and not any(k in msg for k in QUOTA_WORDS))):
                    break
            time.sleep(2 ** attempt)
        raise RuntimeError(last)
    finally:
        if use_slot:
            API_SLOTS.release()


class ChatWorker(QThread):
    reasoning = pyqtSignal(str)
    content = pyqtSignal(str)
    failed = pyqtSignal(str)
    notice = pyqtSignal(str)      # 排队、重试等状态提示
    summarized = pyqtSignal(str)  # 较早对话压缩完成
    tool_progress = pyqtSignal(object)  # {"name", "chars", "path"}：正在生成的工具调用

    def __init__(self, cfg, messages, tools=None, compress=None):
        super().__init__()
        self.cfg, self.messages, self.tools = cfg, messages, tools
        self.compress = compress      # 需要压缩的较早对话文本；系统提示里的 SUMMARY_MARK 会被替换成摘要
        self.tool_calls = []
        self._stop = False
        self._resp = None
        self.timing = None            # {"total", "think"} 秒：本步总用时、其中思考用时

    def stop(self):
        self._stop = True
        if self._resp is not None:
            try:
                self._resp.close()
            except Exception:
                pass

    def _sleep(self, sec):
        end = time.time() + sec
        while time.time() < end and not self._stop:
            time.sleep(0.1)

    def run(self):
        queued = False
        while not API_SLOTS.acquire(timeout=0.2):  # 多个对话同时运行时排队，避免触发接口并发限制
            if self._stop:
                return
            if not queued:
                queued = True
                self.notice.emit(f"同时运行的对话较多，排队中…（最多同时 {MAX_PARALLEL} 个）")
        try:
            self._run()
        except Exception as e:  # 兜底：线程里的任何异常都转成这个对话的错误，不影响程序
            log.exception("请求线程异常")
            if not self._stop:
                self.failed.emit(f"请求失败: {e}")
        finally:
            API_SLOTS.release()

    def _run(self):
        cfg = self.cfg
        if self.compress:
            self.notice.emit("对话较长，正在把较早的内容压缩成摘要…")
            try:
                summary = chat_once(cfg, [{"role": "system", "content": COMPRESS_PROMPT},
                                          {"role": "user", "content": self.compress}],
                                    use_slot=False, stop=lambda: self._stop).strip()
            except RuntimeError as e:
                log.warning("压缩对话失败：%s", e)
                summary = ""
            if self._stop:
                return
            if not summary:
                summary = "（自动摘要失败，以下是截断的较早对话记录）\n" + self.compress[-6000:]
            self.messages[0]["content"] = self.messages[0]["content"].replace(SUMMARY_MARK, summary)
            self.summarized.emit(summary)
            self.notice.emit("正在生成…")
        payload = {
            "model": cfg["model"],
            "messages": self.messages,
            "stream": True,
            "temperature": float(cfg.get("temperature", 0.7)),
            "reasoning_effort": cfg.get("reasoning_effort") or "max",
        }
        if self.tools:
            payload["tools"] = self.tools
            payload["tool_choice"] = "auto"
        url = cfg["base_url"].rstrip("/") + "/chat/completions"
        for attempt in range(1, RETRIES + 1):
            if self._stop:
                return
            delay = 2 ** attempt
            try:
                r = HTTP.post(url, json=payload, stream=True, timeout=(15, 600),
                                  headers={"Authorization": "Bearer " + cfg["api_key"], "Accept": "text/event-stream"})
            except (requests.ConnectionError, requests.Timeout) as e:
                log.warning("连接失败（第 %d 次）：%s", attempt, e)
                if attempt < RETRIES and not self._stop:
                    self.notice.emit(f"网络连接失败，{delay} 秒后自动重试（{attempt}/{RETRIES - 1}）…")
                    self._sleep(delay)
                    continue
                if not self._stop:
                    self.failed.emit(f"网络连接失败，请检查网络后重试。（{e}）")
                return
            self._resp = r
            if r.status_code == 200:
                log.info("请求成功 model=%s 消息数=%d 工具=%s", cfg["model"], len(self.messages), bool(self.tools))
                return self._read_stream(r)
            try:
                msg = r.json().get("error", {}).get("message") or r.text
            except ValueError:
                msg = r.text
            r.close()
            log.warning("HTTP %s（第 %d 次）：%s", r.status_code, attempt, msg[:300])
            quota = any(k in msg for k in ("余额", "额度", "上限", "充值", "资源包"))
            if (r.status_code >= 500 or (r.status_code == 429 and not quota)) and attempt < RETRIES:
                self.notice.emit(f"接口繁忙（HTTP {r.status_code}），{delay} 秒后自动重试（{attempt}/{RETRIES - 1}）…")
                self._sleep(delay)
                continue
            hint = ""
            if r.status_code == 429 and quota and "/coding/" not in cfg["base_url"]:
                hint = ("\n\n提示：如果你购买的是 GLM Coding Plan（编程套餐），它的额度只能通过专用地址使用。"
                        "请点左下角「设置」，把接口地址改为「GLM Coding Plan」后重试。")
            elif r.status_code == 429 and "/coding/" in cfg["base_url"]:
                hint = "\n\n提示：Coding Plan 的 5 小时或每周额度可能已用完，可在智谱控制台的用量统计中查看重置时间。"
            if not self._stop:
                self.failed.emit(f"HTTP {r.status_code}: {msg[:500]}{hint}")
            return

    def _report_progress(self, slot):
        now = time.time()
        if now - getattr(self, "_last_progress", 0) < 0.3:
            return
        self._last_progress = now
        a = slot["function"]["arguments"]
        m = re.search(r'"(?:path|title|command)"\s*:\s*"((?:[^"\\]|\\.)*)"', a)
        self.tool_progress.emit({"name": slot["function"]["name"], "chars": len(a),
                                 "path": m.group(1) if m else ""})

    def _read_stream(self, r):
        calls = {}
        t0, t_think, t_answer, sizes = time.time(), None, None, [0, 0]   # 思考开始/作答开始时间、思考/输出字数
        completed = False  # 收到 [DONE] 或 finish_reason 才算正常结束；否则是连接被提前关闭
        try:
            with r:
                # 按行直接读底层连接：iter_lines 会攒够 512 字节才返回，小段的思考/回复内容会被卡住、显示一顿一顿的
                r.raw.decode_content = True
                for line in iter(r.raw.readline, b""):
                    line = line.strip()
                    if self._stop:
                        return
                    if not line.startswith(b"data:"):
                        continue
                    data = line[5:].strip()
                    if data == b"[DONE]":
                        completed = True
                        break
                    try:
                        j = json.loads(data)
                    except ValueError:
                        continue
                    if "error" in j:
                        self.failed.emit(str(j["error"].get("message", j["error"])))
                        return
                    choice = (j.get("choices") or [{}])[0]
                    if choice.get("finish_reason"):
                        completed = True
                    delta = choice.get("delta") or {}
                    now = time.time()
                    if delta.get("reasoning_content"):
                        t_think = t_think or now
                        sizes[0] += len(delta["reasoning_content"])
                    if delta.get("content") or delta.get("tool_calls"):
                        t_answer = t_answer or now
                        sizes[1] += len(delta.get("content") or "") + sum(
                            len(str((tc.get("function") or {}).get("arguments") or "")) for tc in delta.get("tool_calls") or [])
                    if delta.get("reasoning_content"):
                        self.reasoning.emit(delta["reasoning_content"])
                    if delta.get("content"):
                        self.content.emit(delta["content"])
                    for tc in delta.get("tool_calls") or []:
                        i = tc.get("index")
                        if i is None:
                            i = len(calls) if tc.get("id") else max(calls, default=0)
                        slot = calls.setdefault(i, {"id": "", "type": "function",
                                                    "function": {"name": "", "arguments": ""}})
                        if tc.get("id"):
                            slot["id"] = tc["id"]
                        fn = tc.get("function") or {}
                        if fn.get("name"):
                            slot["function"]["name"] = fn["name"]
                        args = fn.get("arguments")
                        if isinstance(args, dict):
                            args = json.dumps(args, ensure_ascii=False)
                        if args:
                            slot["function"]["arguments"] += args
                        self._report_progress(slot)
                if completed:  # 读完剩余字节并标记已读完，关闭时连接会放回连接池，下一轮请求不必重新握手
                    try:
                        r.raw.drain_conn()
                        r._content_consumed = True
                    except Exception:
                        pass
        except Exception as e:  # 生成到一半连接断开：保留已收到的内容
            if not self._stop:
                log.warning("流式输出中断：%s", e)
                self.failed.emit(f"连接中断（{e}）。已收到的内容已保留，可以发送“继续”让 GLM 接着完成。")
            return
        if not completed:
            if not self._stop:
                log.warning("流式输出未正常结束（没有收到结束标记）")
                self.failed.emit("连接中断：回复没有正常结束。已收到的内容已保留，可以发送“继续”让 GLM 接着完成。")
            return
        end = time.time()
        think = ((t_answer or end) - t_think) if t_think else 0
        self.timing = {"total": round(end - t0, 1), "think": round(think, 1)}
        log.info("本步用时 %.1fs（等待首字 %.1fs，思考 %.1fs / %d 字，输出 %d 字）", end - t0,
                 ((t_think or t_answer or end) - t0), think, sizes[0], sizes[1])
        self.tool_calls = [calls[k] for k in sorted(calls) if calls[k]["function"]["name"]]
        for n, c in enumerate(self.tool_calls):
            c["id"] = c["id"] or f"call_{int(time.time())}_{n}"


class ToolThread(QThread):
    """在后台运行耗时的 skill 工具（解析 PDF、编译 LaTeX），避免界面卡住。"""
    done = pyqtSignal(str, object)

    def __init__(self, name, args, cwd, mcp=None):
        super().__init__()
        self.name, self.args, self.cwd, self.mcp = name, args, cwd, mcp
        self.ident = None
        self.cancel = threading.Event()   # MCP 调用可被“停止”打断

    def run(self):
        self.ident = threading.get_ident()
        try:
            if self.name.startswith("mcp__"):
                result, info = self.mcp.call(self.name, self.args, self.cancel)
            else:
                run = run_shell_tool if self.name in SHELL_TOOL_NAMES else run_skill_tool
                result, info = run(self.name, self.args, self.cwd)
        except Exception as e:
            result, info = f"错误：{e}", {"status": "error", "preview": str(e)}
        self.done.emit(result, info)
