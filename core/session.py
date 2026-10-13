"""core.session —— 一个对话的运行状态机：请求模型、执行工具、停止。

Session 不认识任何 UI，只通过注入的 host（见 core.host.SessionHost）驱动界面与取数据。
"""
import functools
import json
import time

from PyQt6.QtCore import QObject, QTimer

from skill_tools import SKILL_TOOL_NAMES, SLOW_TOOLS, ToolError, kill_current, tool_display
from shell_tools import SHELL_SLOW, SHELL_TOOL_NAMES, kill_shell, shell_display
from memory_store import MEMORY_TOOL_NAMES

from core.worker import ChatWorker, SUMMARY_MARK, ToolThread
import logging

log = logging.getLogger("glm")
MAX_ROUNDS = 80


def guarded(fn):
    """对话流程里的任何异常都只让这个对话中止并显示错误，不会让程序闪退或卡在“运行中”。"""
    @functools.wraps(fn)
    def wrapper(self, *args):
        try:
            return fn(self, *args)
        except Exception as e:
            log.exception("对话「%s」在 %s 中出错", self.conv.get("title"), fn.__name__)
            self.fail(e)
    return wrapper


class Session(QObject):
    """一个对话的运行状态：请求模型、执行工具、停止。每个对话一个，可同时运行。"""

    def __init__(self, host, conv):
        super().__init__(host)
        self.host, self.conv = host, conv
        self.busy = False
        self.stop_requested = False
        self.worker = None
        self.tool_threads = []   # 正在后台执行的工具线程（只读工具可以多个同时跑）
        self.batch_left = 0      # 本批还没完成的后台工具数
        self.reply = None
        self.pending_calls = []
        self.rounds = 0
        self.dirty = False
        self.last_flush = 0.0
        self.running = []        # 正在后台执行的工具消息（用于显示已运行秒数）
        self.progress = None     # 当前这轮正在生成的工具调用进度
        self.started = 0.0

    def set_busy(self, busy):
        self.busy = busy
        self.host.on_session_state(self)

    @guarded
    def start(self):
        self.rounds = 0
        self.stop_requested = False
        self.set_busy(True)
        log.info("对话「%s」开始运行", self.conv.get("title"))
        self.start_round()

    def stop(self):
        if not self.busy:
            return
        self.stop_requested = True
        if self.worker:
            self.worker.stop()
        for th in list(self.tool_threads):
            th.cancel.set()
            if th.ident:
                kill_current(th.ident)
                kill_shell(th.ident)

    def wait(self):
        """退出程序前等后台线程结束。"""
        if self.worker:
            self.worker.wait(3000)
        for th in list(self.tool_threads):
            th.wait(5000)

    def fail(self, e):
        """内部错误：中止本轮、在界面上说明原因、恢复为可再次发送的状态。"""
        try:
            self.stop_requested = True
            self.pending_calls = []
            if self.worker:
                self.worker.stop()
            msg = (f"出错：程序内部错误（{type(e).__name__}: {e}），本轮已中止，可以重新发送。"
                   f"详细信息已写入 logs\\app.log")
            if self.reply is not None:
                self.reply["error"] = msg
                self.host.refresh_message(self.conv, self.reply, streaming=False)
            else:
                m = {"role": "assistant", "content": "", "reasoning": "", "error": msg}
                self.conv["messages"].append(m)
                self.host.show_message(self.conv, m)
            if not self.worker and not self.tool_threads:  # 否则等线程结束时再收尾
                self.end_turn()
        except Exception:
            log.exception("fail() 自身出错")
            self.busy = False

    @guarded
    def start_round(self):
        """一轮请求：模型可能直接回答，也可能调用工具；工具执行完再开始下一轮。"""
        host, conv = self.host, self.conv
        self.reply = None
        job = host.compression_job(conv)   # 历史太长时，先把较早的部分压缩成摘要
        if job:
            material, cut_at = job
            messages = host.build_messages(conv, upto=cut_at, summary=SUMMARY_MARK)
        else:
            material, cut_at = None, None
            messages = host.build_messages(conv)
        tools = host.tools_for(conv)
        self.reply = {"role": "assistant", "content": "", "reasoning": "", "model": host.cfg["model"]}
        self.started = time.time()
        conv["messages"].append(self.reply)
        host.show_message(conv, self.reply, streaming=True)

        self.worker = ChatWorker(dict(host.cfg), messages, tools, compress=material)
        if job:
            self.worker.summarized.connect(lambda text, c=cut_at: self.on_summarized(text, c))
        self.worker.reasoning.connect(self.on_reasoning)
        self.worker.content.connect(self.on_content)
        self.worker.failed.connect(self.on_failed)
        self.worker.notice.connect(self.on_notice)
        self.worker.tool_progress.connect(self.on_tool_progress)
        self.worker.finished.connect(self.on_finished)
        self.worker.start()

    @guarded
    def on_summarized(self, text, cut_at):
        self.conv["summary"] = text
        self.conv["summary_upto"] = cut_at
        log.info("对话「%s」已压缩前 %d 条消息", self.conv.get("title"), cut_at)
        self.host.save_convs()
        self.host.on_compressed(self.conv)

    @guarded
    def on_tool_progress(self, d):
        self.progress = d

    @guarded
    def on_notice(self, s):
        if self.reply is not None:
            self.reply["notice"] = s
            self.dirty = True

    @guarded
    def on_reasoning(self, s):
        self.reply.pop("notice", None)
        self.reply["reasoning"] += s
        self.dirty = True

    @guarded
    def on_content(self, s):
        self.reply.pop("notice", None)
        self.reply["content"] += s
        self.dirty = True

    @guarded
    def on_failed(self, msg):
        self.reply["error"] = "出错：" + msg
        self.dirty = True

    @guarded
    def on_finished(self):
        worker, self.worker = self.worker, None
        if worker is None:
            return
        worker.deleteLater()
        reply = self.reply
        self.progress = None
        if reply is None:  # 已经由 fail() 处理过
            return self.end_turn()
        reply.pop("notice", None)
        if worker.timing:
            reply["timing"] = worker.timing
        calls = [] if (self.stop_requested or reply.get("error")) else worker.tool_calls
        if calls:
            reply["tool_calls"] = calls
        elif not reply["content"] and not reply.get("error"):
            reply["content"] = "（已停止）" if self.stop_requested else "（没有返回内容）"
        self.dirty = False
        self.host.refresh_message(self.conv, reply, streaming=False)
        if calls:
            self.pending_calls = list(calls)
            return self.run_next_call()
        self.end_turn()

    def end_turn(self):
        self.reply = None
        self.pending_calls = []
        try:
            self.host.save_convs(force=True)
        finally:
            self.set_busy(False)
            log.info("对话「%s」结束", self.conv.get("title"))
        try:
            self.host.on_turn_finished(self.conv)
        except Exception:
            log.exception("安排记忆提炼失败")

    # 可以同时执行的只读工具：一次回复里连续出现的这类调用会并行跑
    PARALLEL = {"read_pdf", "search_pdf", "pdf_info", "diff_pdfs", "get_report_template", "check_env",
                "find_files", "grep_files", "list_dir", "read_file", "memory_search"}

    def _parallel_ok(self, call):
        name = call["function"]["name"]
        if name in self.PARALLEL:
            return True
        if name.startswith("mcp__"):
            _, t = self.host.mcp.tool_info(name)
            return bool(t and (t.get("annotations") or {}).get("readOnlyHint"))
        return False

    @guarded
    def run_next_call(self):
        if not self.pending_calls:  # 本轮工具都执行完了，把结果交给模型继续
            self.host.save_convs()
            self.rounds += 1
            if not self.stop_requested and self.rounds < MAX_ROUNDS:
                return self.start_round()
            return self.end_turn()
        batch = [self.pending_calls.pop(0)]
        if self._parallel_ok(batch[0]):
            while self.pending_calls and self._parallel_ok(self.pending_calls[0]) and len(batch) < 8:
                batch.append(self.pending_calls.pop(0))
        if len(batch) > 1:
            log.info("对话「%s」并行执行 %d 个工具", self.conv.get("title"), len(batch))
        self.batch_left = 0
        for call in batch:
            self._start_call(call)
        if self.batch_left == 0:  # 全是即时完成的工具
            QTimer.singleShot(0, self.run_next_call)

    def _start_call(self, call):
        name = call["function"]["name"]
        try:
            args = json.loads(call["function"]["arguments"] or "{}")
        except ValueError:
            args = None
        if name in SKILL_TOOL_NAMES:
            disp = tool_display(name, args)
        elif name in SHELL_TOOL_NAMES:
            disp = shell_display(name, args)
        elif name in MEMORY_TOOL_NAMES:
            a = args if isinstance(args, dict) else {}
            disp = str(a.get("query") or a.get("title") or f"#{a.get('id', '')}")
        elif name.startswith("mcp__"):
            disp = self.host.mcp.display(name, args)
        else:
            disp = (args.get("path") if isinstance(args, dict) else None) or "."
        m = {"role": "tool", "tool_call_id": call["id"], "name": name, "path": disp}
        self.conv["messages"].append(m)
        self.host.show_message(self.conv, m)
        if self.stop_requested:
            return self._finish(m, "用户已停止，未执行。", {"status": "skipped"})
        if args is None:
            return self._finish(m, "错误：工具参数不是合法的 JSON，请重新生成这次调用。",
                                {"status": "error", "preview": call["function"]["arguments"][:2000]})
        log.info("对话「%s」执行工具 %s %s", self.conv.get("title"), name, disp[:100])
        if name in SLOW_TOOLS or name in SHELL_SLOW or name.startswith("mcp__"):
            m["status"], m["_started"] = "running", time.time()
            self.running.append(m)
            self.host.refresh_message(self.conv, m)
            th = ToolThread(name, args, self.conv.get("cwd", ""), self.host.mcp)
            self.tool_threads.append(th)
            self.batch_left += 1
            th.done.connect(lambda r, info, m=m, th=th: self.thread_done(th, m, r, info))
            th.start()
            return
        try:
            result, info = self.host.run_tool(self, name, args)
        except (ToolError, OSError, ValueError) as e:
            result, info = f"错误：{e}", {"status": "error", "preview": str(e)}
        self._finish(m, result, info)

    def _finish(self, m, result, info):
        if m in self.running:
            self.running.remove(m)
        m.pop("_started", None)
        m["content"] = result
        m.update(info)
        if info.get("status") == "error":
            log.warning("工具 %s 失败：%s", m.get("name"), str(result)[:300])
        self.host.refresh_message(self.conv, m)

    @guarded
    def thread_done(self, th, m, result, info):
        th.wait()
        th.deleteLater()
        if th in self.tool_threads:
            self.tool_threads.remove(th)
        self._finish(m, result, info)
        self.batch_left -= 1
        if self.batch_left <= 0:
            QTimer.singleShot(0, self.run_next_call)
