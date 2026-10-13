"""GLM 桌面聊天客户端（PyQt6）

运行:  python glm_client.py
依赖:  pip install -r requirements.txt   (PyQt6 requests markdown-it-py pygments)
"""
import difflib
import functools
import html
import json
import logging
import logging.handlers
import math
import os
import re
import threading
import shutil
import subprocess
import sys
import time
import traceback
import zlib

import requests
from skill_tools import (SKILL_LABELS, SKILL_TOOL_NAMES, SKILL_TOOLS, SLOW_TOOLS, ToolError, env_status, kill_current,
                         prefetch, run_skill_tool, safe_path, skill_dir, skill_prompt, tool_display)
from shell_tools import (SHELL_LABELS, SHELL_SLOW, SHELL_TOOL_NAMES, SHELL_TOOLS, kill_shell, run_shell_tool,
                         shell_display)
from memory_store import (COMPRESS_PROMPT, DISTILL_PROMPT, MEMORY_LABELS, MEMORY_PROMPT, MEMORY_TOOL_NAMES,
                          MEMORY_TOOLS, TYPES as MEMORY_TYPES, MemoryStore, parse_json_object, transcript)
from mcp_client import MCPManager
from mem import PARTS_LABELS, PARTS_PROMPT, PARTS_TOOL_NAMES, PARTS_TOOLS, PartsStore
from core import Session, chat_once
from infra import (BASE_URLS, DEFAULT_CONFIG, HINT_TEXT, SAVER, TOOL_LABELS, TOOL_PROMPT, TOOLS,
                   api_content, dir_overview, diff_stats, lang_of, load_json, open_in_explorer,
                   read_text_file, save_json, unified_diff, write_text_file)
from ui.theme import (ACCENT, ACCENT_H, BG, BORDER, CODE_BG, CODE_HEAD, DOC_CSS, HOVER,
                      MUTED, QSS, SIDE, TEXT, USER_BG)
from ui.render import diff_html, md_to_html
from ui.widgets import (AssistantMessage, ChatArea, EnterFilter, FilePanel, McpConfigDialog,
                        McpDialog, MemoryDialog, RichView, SettingsDialog, SummaryDivider,
                        ToolCard, UserMessage)
import infra.config as _cfg
LOAD_PROBLEMS = _cfg.LOAD_PROBLEMS
from markdown_it import MarkdownIt
from pygments import highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import get_lexer_by_name, get_lexer_for_filename
from pygments.lexers.special import TextLexer
from pygments.util import ClassNotFound
from PyQt6.QtCore import QDir, QEvent, QObject, Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtNetwork import QLocalServer, QLocalSocket
from PyQt6.QtGui import QCursor, QDesktopServices, QFileSystemModel, QFont, QIcon, QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
    QFileDialog, QFormLayout, QFrame, QHBoxLayout, QInputDialog, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMainWindow, QMenu, QMessageBox, QPlainTextEdit,
    QPushButton, QScrollArea, QSizePolicy, QStackedWidget, QTextBrowser, QToolButton,
    QToolTip, QTreeView, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

APP_NAME = "GLM-MCP-文档对比agent"
# 打包成 exe 后，配置和对话记录保存在 exe 所在目录
ROOT = os.path.dirname(sys.executable if getattr(sys, "frozen", False) else os.path.abspath(__file__))
# 所有 *.json(配置、对话、记忆、MCP 服务器）统一放在 config/ 子目录，保持根目录整洁
CONFIG_DIR = os.path.join(ROOT, "config")
os.makedirs(CONFIG_DIR, exist_ok=True)
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
CONVS_PATH = os.path.join(CONFIG_DIR, "conversations.json")
LOG_DIR = os.path.join(ROOT, "logs")
MEMORY_PATH = os.path.join(CONFIG_DIR, "memory.json")
MCP_PATH = os.path.join(CONFIG_DIR, "mcp_servers.json")
PARTS_DB_PATH = os.path.join(CONFIG_DIR, "parts.db")   # 元件参数库（SQLite）
COMPRESS_AT = 90000          # 发给模型的历史超过这么多字符时，把较早的部分压缩成摘要
KEEP_TAIL = 25000            # 压缩后保留的最近原文大约这么多字符

log = logging.getLogger("glm")


def setup_logging():
    """日志写到 exe 旁边的 logs/app.log（最多 3 个 1MB 文件轮换），不记录 API Key。"""
    log.setLevel(logging.INFO)
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        h = logging.handlers.RotatingFileHandler(os.path.join(LOG_DIR, "app.log"), maxBytes=1_000_000,
                                                 backupCount=3, encoding="utf-8")
    except OSError:
        h = logging.NullHandler()
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(threadName)s] %(message)s"))
    log.addHandler(h)








# ---------------------------------------------------------------- 工具函数



class McpRelay(QObject):
    """MCP 后台线程状态变化 → 界面线程。"""
    changed = pyqtSignal()


class Distiller(QThread):
    """后台把一段对话提炼成要点和记忆条目。"""
    done = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, cfg, text):
        super().__init__()
        self.cfg, self.text = cfg, text

    def run(self):
        try:
            out = chat_once(self.cfg, [{"role": "system", "content": DISTILL_PROMPT},
                                       {"role": "user", "content": self.text}], effort="low")
            self.done.emit(parse_json_object(out))
        except Exception as e:
            self.failed.emit(str(e))






# ---------------------------------------------------------------- 单个对话的运行
# ---------------------------------------------------------------- 主窗口
class MainWindow(QMainWindow):
    SUGGESTIONS = ["对比目录里所有 datasheet，生成 PDF 对比报告", "检查报告编译环境是否就绪", "帮我写一个 Python 脚本"]

    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.resize(1240, 800)

        self.cfg = {**DEFAULT_CONFIG, **load_json(CONFIG_PATH, {})}
        for k in ("auto_apply", "auto_shell"):  # 旧版本的开关，现在所有操作都直接执行
            self.cfg.pop(k, None)
        self.convs = self._valid_convs(load_json(CONVS_PATH, []))
        self.cur = None
        self.memory = MemoryStore(MEMORY_PATH, load_json, save_json)
        self.parts = PartsStore(PARTS_DB_PATH)   # 结构化器件参数库（SQLite，跨对话）
        self.distill_queue = []      # 等待提炼要点的对话 id
        self.distiller = None
        self.mcp = MCPManager(MCP_PATH)
        self.mcp_relay = McpRelay()
        self.mcp.on_change = self.mcp_relay.changed.emit   # 后台线程 → 界面线程
        self._save_warned = False
        self._convs_dirty = False
        self._last_conv_save = 0.0
        self._save_timer = QTimer(self, singleShot=True, interval=1500)
        self._save_timer.timeout.connect(self._do_save_convs)
        self.sessions = {}   # 对话 id -> Session
        self.widgets = {}    # 当前对话里 id(消息) -> 控件
        self.pending_files = []

        central = QWidget(objectName="main")
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._build_sidebar())
        root.addWidget(self._build_main(), 1)
        self.file_panel = FilePanel()
        self.file_panel.attach.connect(self.add_file)
        root.addWidget(self.file_panel)
        self.setCentralWidget(central)

        self.timer = QTimer(self, interval=80)
        self.timer.timeout.connect(self.flush)
        self.timer.start()
        QShortcut(QKeySequence("Ctrl+N"), self, self.new_conv)
        QShortcut(QKeySequence("Escape"), self, self.stop_turn)

        if not self.convs:
            self.convs.append(self._blank())
        self.refresh_list(0)
        self.update_model_chip()
        if not self.cfg["api_key"]:
            QTimer.singleShot(300, self.open_settings)
        if LOAD_PROBLEMS:
            QTimer.singleShot(400, lambda: QMessageBox.warning(self, "数据恢复", "\n".join(LOAD_PROBLEMS)))
        self.mcp_relay.changed.connect(self.update_mcp_btn)
        self.update_memory_btn()
        QTimer.singleShot(200, self.mcp.start_all)   # 后台连接 MCP 服务器，不阻塞界面

    # ---------- 界面搭建
    def _build_sidebar(self):
        side = QFrame(objectName="sidebar")
        side.setFixedWidth(250)
        v = QVBoxLayout(side)
        v.setContentsMargins(12, 14, 12, 12)
        v.setSpacing(8)
        brand = QHBoxLayout()
        brand.addWidget(QLabel("✦", objectName="brandStar"))
        brand.addWidget(QLabel("GLM-MCP-文档对比agent", objectName="brand"))
        brand.addStretch()
        v.addLayout(brand)
        v.addSpacing(6)
        self.new_btn = QPushButton("＋  新对话", objectName="newBtn")
        self.new_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.new_btn.clicked.connect(self.new_conv)
        v.addWidget(self.new_btn)
        v.addWidget(QLabel("最近", objectName="sectionLabel"))
        self.conv_list = QListWidget(objectName="convList")
        self.conv_list.currentRowChanged.connect(self.select_conv)
        self.conv_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.conv_list.customContextMenuRequested.connect(self.conv_menu)
        v.addWidget(self.conv_list, 1)
        self.mem_btn = QPushButton(objectName="sideBtn")
        self.mem_btn.setToolTip("跨对话保存的器件参数、对比结论、偏好和对话要点")
        self.mem_btn.clicked.connect(self.open_memory)
        self.mcp_btn = QPushButton("🔌  MCP 服务", objectName="sideBtn")
        self.mcp_btn.setToolTip("查看和配置 MCP 服务器")
        self.mcp_btn.clicked.connect(self.open_mcp)
        for b in (self.mem_btn, self.mcp_btn):
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            v.addWidget(b)
        set_btn = QPushButton("⚙  设置", objectName="sideBtn")
        set_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        set_btn.clicked.connect(self.open_settings)
        v.addWidget(set_btn)
        return side

    def _build_main(self):
        main = QWidget(objectName="main")
        v = QVBoxLayout(main)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        top = QFrame(objectName="topBar")
        top.setFixedHeight(52)
        th = QHBoxLayout(top)
        th.setContentsMargins(20, 0, 16, 0)
        self.title_lbl = QLabel(objectName="convTitle")
        self.dir_btn = QPushButton(objectName="dirBtn")
        self.dir_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.dir_menu = QMenu(self)
        self.dir_btn.setMenu(self.dir_menu)
        self.dir_menu.aboutToShow.connect(self._fill_dir_menu)
        th.addWidget(self.title_lbl, 1)
        th.addWidget(self.dir_btn)
        v.addWidget(top)

        self.stack = QStackedWidget()
        self.stack.addWidget(self._build_welcome())
        self.chat = ChatArea()
        self.stack.addWidget(self.chat)
        v.addWidget(self.stack, 1)

        # 输入卡片
        wrap = QHBoxLayout()
        wrap.setContentsMargins(24, 4, 24, 6)
        card = QFrame(objectName="composer")
        card.setMaximumWidth(ChatArea.MAX_W)
        cv = QVBoxLayout(card)
        cv.setContentsMargins(14, 10, 10, 10)
        cv.setSpacing(6)
        self.attach_row = QHBoxLayout()
        self.attach_row.setSpacing(6)
        self.attach_row.addStretch()
        cv.addLayout(self.attach_row)
        self.input = QPlainTextEdit(objectName="input")
        self.input.setPlaceholderText("给 GLM 发送消息…   Enter 发送，Shift+Enter 换行")
        self._enter = EnterFilter(self.on_send)
        self.input.installEventFilter(self._enter)
        self.input.document().documentLayout().documentSizeChanged.connect(self._fit_input)
        self.input.textChanged.connect(self._update_send_state)
        cv.addWidget(self.input)
        bar = QHBoxLayout()
        bar.setSpacing(6)
        file_btn = QPushButton("＋ 文件", objectName="chip")
        file_btn.setToolTip("选择文件添加到对话")
        file_btn.clicked.connect(self.pick_files)
        self.model_chip = QPushButton(objectName="chip")
        self.model_chip.setToolTip("点击修改模型设置")
        self.model_chip.clicked.connect(self.open_settings)
        self.think_chip = QPushButton(objectName="chip")
        self.think_chip.setToolTip("思考强度：low 最快，max 最深入（GLM-5.3 不能关闭思考）")
        effort_menu = QMenu(self)
        for level, desc in (("low", "low  · 快速"), ("high", "high · 均衡"), ("max", "max  · 最深入（推荐）")):
            effort_menu.addAction(desc, lambda level=level: self._set_effort(level))
        self.think_chip.setMenu(effort_menu)
        self.think_chip.setStyleSheet("QPushButton::menu-indicator { image: none; }")
        self._set_effort(self.cfg.get("reasoning_effort") or "max", save=False)
        self.send_btn = QPushButton("↑", objectName="sendBtn")
        self.send_btn.setFixedSize(34, 34)
        self.send_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.send_btn.clicked.connect(self.on_send)
        self.skill_chip = QPushButton("📊 Datasheet 对比", objectName="chip")
        self.skill_chip.setToolTip("内置 skill：对比目录里的 datasheet，生成 LaTeX + PDF 报告")
        self.skill_chip.setCheckable(True)
        self.skill_chip.toggled.connect(self._toggle_skill)
        for w in (file_btn, self.model_chip, self.think_chip, self.skill_chip):
            w.setCursor(Qt.CursorShape.PointingHandCursor)
            bar.addWidget(w)
        bar.addStretch()
        bar.addWidget(self.send_btn)
        cv.addLayout(bar)
        wrap.addStretch(1)
        wrap.addWidget(card, 20)
        wrap.addStretch(1)
        v.addLayout(wrap)
        self.hint_lbl = QLabel(HINT_TEXT, objectName="hint")
        self.hint_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        v.addWidget(self.hint_lbl)
        v.addSpacing(8)
        self._fit_input()
        self._update_send_state()
        return main

    def _build_welcome(self):
        w = QWidget(objectName="main")
        v = QVBoxLayout(w)
        v.addStretch(2)
        star = QLabel("✦", objectName="welcomeStar")
        star.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.greet_lbl = QLabel(objectName="welcomeTitle")
        self.greet_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.welcome_sub = QLabel(objectName="welcomeSub")
        self.welcome_sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        v.addWidget(star)
        v.addWidget(self.greet_lbl)
        v.addWidget(self.welcome_sub)
        v.addSpacing(18)
        row = QHBoxLayout()
        row.addStretch()
        for s in self.SUGGESTIONS:
            b = QPushButton(s, objectName="suggest")
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.clicked.connect(lambda _, s=s: (self.input.setPlainText(s + "\n"), self._focus_input_end()))
            row.addWidget(b)
        row.addStretch()
        v.addLayout(row)
        v.addStretch(3)
        return w

    # ---------- 输入框
    def _focus_input_end(self):
        self.input.setFocus()
        self.input.moveCursor(self.input.textCursor().MoveOperation.End)

    def _fit_input(self, *_):
        lines = max(2, min(10, int(self.input.document().size().height())))
        self.input.setFixedHeight(lines * self.input.fontMetrics().lineSpacing() + 12)

    def _update_send_state(self):
        s = self.cur_session()
        busy = bool(s and s.busy)
        self.send_btn.setText("■" if busy else "↑")
        self.send_btn.setToolTip("停止生成 (Esc)" if busy else "发送 (Enter)")
        self.send_btn.setEnabled(busy or bool(self.input.toPlainText().strip()))

    def _set_effort(self, level, save=True):
        if level not in ("low", "high", "max"):
            level = "max"
        self.cfg["reasoning_effort"] = level
        self.cfg.pop("thinking", None)  # 旧版本的开关，GLM-5.3 不再支持
        self.think_chip.setText(f"思考：{level}  ▾")
        if save:
            save_json(CONFIG_PATH, self.cfg)

    def _toggle_skill(self, on):
        if self.cur is not None and self.cur.get("skill") != on:
            self.cur["skill"] = on
            self.save_convs()
            self.prefetch_dir(self.cur.get("cwd", ""))

    def prefetch_dir(self, cwd):
        """启用了 datasheet skill 的目录：后台提前解析 PDF、检查编译环境，GLM 用到时直接取结果。"""
        if not (cwd and os.path.isdir(cwd) and self.cur and self.skill_on(self.cur)):
            return
        done = self.__dict__.setdefault("_prefetched", set())
        if cwd not in done:
            done.add(cwd)
            threading.Thread(target=prefetch, args=(cwd,), name="prefetch", daemon=True).start()

    def update_model_chip(self):
        self.model_chip.setText(self.cfg["model"] + "  ▾")

    # ---------- 附件
    def pick_files(self):
        start = (self.cur or {}).get("cwd") or ""
        paths, _ = QFileDialog.getOpenFileNames(self, "选择文件", start)
        for p in paths:
            self.add_file(p)

    def add_file(self, path):
        path = os.path.normpath(path)
        if any(f["path"] == path for f in self.pending_files):
            return
        try:
            content, err, _ = read_text_file(path)
        except OSError as e:
            content, err = None, str(e)
        if err:
            QMessageBox.warning(self, "无法添加文件", f"{os.path.basename(path)}：{err}")
            return
        self.pending_files.append({"path": path, "lang": lang_of(path), "content": content})
        self._refresh_attachments()
        self.input.setFocus()

    def _refresh_attachments(self):
        while self.attach_row.count() > 1:
            self.attach_row.takeAt(0).widget().deleteLater()
        for f in self.pending_files:
            b = QPushButton(f"📄 {os.path.basename(f['path'])}  ✕", objectName="attach")
            b.setToolTip(f["path"] + "\n点击移除")
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.clicked.connect(lambda _, f=f: (self.pending_files.remove(f), self._refresh_attachments()))
            self.attach_row.insertWidget(self.attach_row.count() - 1, b)

    # ---------- 工作目录
    def _fill_dir_menu(self):
        m = self.dir_menu
        m.clear()
        cwd = (self.cur or {}).get("cwd")
        m.addAction("选择目录…", self.choose_dir)
        if cwd:
            m.addAction("在资源管理器中打开", lambda: open_in_explorer(cwd))
            m.addAction("隐藏文件面板" if self.file_panel.isVisible() else "显示文件面板",
                        lambda: self.file_panel.setVisible(not self.file_panel.isVisible()))
            m.addSeparator()
            m.addAction("取消关联目录", lambda: self.set_cwd(""))

    def choose_dir(self):
        start = (self.cur or {}).get("cwd") or os.path.expanduser("~")
        d = QFileDialog.getExistingDirectory(self, "选择工作目录", start)
        if d:
            self.set_cwd(os.path.normpath(d))

    def set_cwd(self, path):
        self.cur["cwd"] = path
        if path and "skill" not in self.cur:  # 目录里有 PDF 时默认启用 datasheet 对比 skill
            self.cur["skill"] = any(f.lower().endswith(".pdf") for f in os.listdir(path))
        self.save_convs()
        self.update_dir_ui()

    def update_dir_ui(self):
        cwd = (self.cur or {}).get("cwd", "")
        if cwd and not os.path.isdir(cwd):
            cwd = ""
        self.dir_btn.setText(f"📁 {os.path.basename(cwd.rstrip(chr(92) + '/')) or cwd}  ▾" if cwd else "📁 打开目录  ▾")
        self.dir_btn.setToolTip(cwd or "为当前对话关联一个工作目录")
        self.file_panel.set_root(cwd)
        self.file_panel.setVisible(bool(cwd))
        self.prefetch_dir(cwd)
        self.skill_chip.setVisible(bool(cwd) and skill_dir() is not None)
        self.skill_chip.setChecked(bool((self.cur or {}).get("skill")))
        self.input.setPlaceholderText(
            "让 GLM 操作目录里的文件、运行命令，或者直接提问…   Enter 发送" if cwd
            else "给 GLM 发送消息…   Enter 发送，Shift+Enter 换行")

    # ---------- 对话管理
    @staticmethod
    def _valid_convs(data):
        """丢弃结构不对的条目，并保证 id 唯一。"""
        out, ids = [], set()
        for c in data if isinstance(data, list) else []:
            if not (isinstance(c, dict) and isinstance(c.get("messages"), list)):
                continue
            c.setdefault("title", "对话")
            c["messages"] = [m for m in c["messages"] if isinstance(m, dict) and m.get("role")]
            if c.get("id") in ids or not isinstance(c.get("id"), int):
                c["id"] = max(ids, default=0) + 1
            ids.add(c["id"])
            out.append(c)
        return out

    def _blank(self, cwd="", skill=None):
        new_id = int(time.time() * 1000)
        while any(c["id"] == new_id for c in self.convs):
            new_id += 1
        c = {"id": new_id, "title": "新对话", "cwd": cwd, "messages": []}
        if skill is not None:
            c["skill"] = skill
        return c

    def save_convs(self, force=False):
        """标记对话需要保存。频繁调用时合并，至多每 1.5 秒序列化一次，写盘在后台线程完成。"""
        self._convs_dirty = True
        if force or time.time() - self._last_conv_save >= 1.5:
            self._do_save_convs()
        elif not self._save_timer.isActive():
            self._save_timer.start()

    def _do_save_convs(self):
        if not self._convs_dirty:
            return
        self._convs_dirty = False
        self._last_conv_save = time.time()
        try:
            text = json.dumps(self.convs, ensure_ascii=False, indent=2)
        except Exception:
            return log.exception("序列化对话失败")
        SAVER.submit(CONVS_PATH, text)

    def report_error(self, text):
        """全局异常的兜底提示（非阻塞，避免一连串弹窗）。"""
        if getattr(self, "_error_box", None) and self._error_box.isVisible():
            return
        self._error_box = QMessageBox(QMessageBox.Icon.Warning, "程序出现错误",
                                      f"{text}\n\n程序会继续运行，详细信息已写入：\n{os.path.join(LOG_DIR, 'app.log')}",
                                      parent=self)
        self._error_box.setModal(False)
        self._error_box.show()

    def refresh_list(self, row):
        self.conv_list.blockSignals(True)
        self.conv_list.clear()
        for c in self.convs:
            self.conv_list.addItem(QListWidgetItem(self.item_text(c)))
        self.conv_list.blockSignals(False)
        self.conv_list.setCurrentRow(row)
        self.select_conv(row)

    def select_conv(self, row):
        if 0 <= row < len(self.convs):
            self.cur = self.convs[row]
            self.title_lbl.setText(self.cur["title"])
            self.update_dir_ui()
            self.render_all()
            s = self.cur_session()
            self.dir_btn.setEnabled(not (s and s.busy))
            self._update_send_state()

    def new_conv(self):
        cwd = (self.cur or {}).get("cwd", "")  # 新对话沿用当前目录和 skill 开关
        skill = (self.cur or {}).get("skill")
        if self.cur and not self.cur["messages"]:
            self.convs.remove(self.cur)
        self.convs.insert(0, self._blank(cwd, skill))
        self.save_convs()
        self.refresh_list(0)
        self.input.setFocus()

    def conv_menu(self, pos):
        item = self.conv_list.itemAt(pos)
        if not item:
            return
        row = self.conv_list.row(item)
        menu = QMenu(self)
        menu.addAction("重命名", lambda: self.rename_conv(row))
        menu.addAction("删除", lambda: self.delete_conv(row))
        menu.exec(self.conv_list.viewport().mapToGlobal(pos))

    def rename_conv(self, row):
        c = self.convs[row]
        name, ok = QInputDialog.getText(self, "重命名", "对话名称：", text=c["title"])
        if ok and name.strip():
            c["title"] = name.strip()
            self.save_convs()
            self.refresh_list(row)
            self.title_lbl.setText(self.cur["title"])

    def delete_conv(self, row):
        if self.is_busy(self.convs[row]):
            QMessageBox.information(self, "删除", "这个对话正在运行，请先停止再删除。")
            return
        if QMessageBox.question(self, "删除", f"删除对话「{self.convs[row]['title']}」？") \
                != QMessageBox.StandardButton.Yes:
            return
        self.sessions.pop(self.convs[row]["id"], None)
        del self.convs[row]
        if not self.convs:
            self.convs.append(self._blank())
        self.save_convs(force=True)
        self.refresh_list(min(row, len(self.convs) - 1))

    # ---------- 渲染
    def render_all(self):
        self.chat.clear()
        self.widgets = {}
        msgs = self.cur["messages"]
        if not msgs:
            h = time.localtime().tm_hour
            greet = "夜深了" if h < 6 else "早上好" if h < 11 else "中午好" if h < 13 else "下午好" if h < 18 else "晚上好"
            self.greet_lbl.setText(greet)
            if not self.cfg["api_key"]:
                sub = "请先点击左下角「设置」填写 API Key"
            elif not self.cur.get("cwd"):
                sub = "点击右上角「📁 打开目录」后，GLM 就可以读写文件、运行命令"
            else:
                sub = "今天想做点什么？"
            self.welcome_sub.setText(sub)
            self.stack.setCurrentIndex(0)
            return
        self.stack.setCurrentIndex(1)
        for i, m in enumerate(msgs):
            if i and i == self.cur.get("summary_upto"):
                self.chat.add(SummaryDivider(self.cur.get("summary", "")))
            self._add_widget(m, msgs[i - 1]["role"] if i else None)
        s = self.sessions.get(self.cur["id"])
        if s and s.worker and s.reply:  # 这个对话正在后台生成，接着显示流式内容
            self.widgets[id(s.reply)].update_msg(s.reply, streaming=True)
        self.chat.scroll_bottom()

    def _add_widget(self, m, prev_role):
        if m["role"] == "user":
            w = UserMessage(m)
        elif m["role"] == "tool":
            w = ToolCard(m, self.undo_tool)
        else:
            w = AssistantMessage(m, self.cfg["model"], show_head=prev_role in (None, "user"))
        self.chat.add(w)
        self.widgets[id(m)] = w
        return w

    def show_message(self, conv, m, streaming=False):
        """对话新增一条消息；只有当前正在看的对话才需要画出来。"""
        if conv is not self.cur:
            return
        if self.stack.currentIndex() == 0:
            self.stack.setCurrentIndex(1)
        msgs = conv["messages"]
        i = next((k for k in range(len(msgs) - 1, -1, -1) if msgs[k] is m), 0)
        w = self._add_widget(m, msgs[i - 1]["role"] if i else None)
        if isinstance(w, AssistantMessage):
            w.update_msg(m, streaming)
        self.chat.scroll_bottom()

    def refresh_message(self, conv, m, streaming=False):
        if conv is not self.cur or id(m) not in self.widgets:
            return
        bottom = self.chat.at_bottom()
        w = self.widgets[id(m)]
        if isinstance(w, AssistantMessage):
            w.update_msg(m, streaming)
        elif isinstance(w, ToolCard):
            w.refresh()
        if bottom:
            self.chat.scroll_bottom()

    def flush(self):
        """定时刷新当前对话正在流式输出的那条消息；内容很长时降低刷新频率，避免界面卡顿。"""
        now = time.time()
        for s in self.sessions.values():
            if not (s.dirty and s.reply is not None):
                continue
            size = len(s.reply.get("content") or "") + len(s.reply.get("reasoning") or "")
            if size > 15000 and now - s.last_flush < (0.6 if size < 60000 else 1.5):
                continue
            s.dirty = False
            s.last_flush = now
            try:
                self.refresh_message(s.conv, s.reply, streaming=s.worker is not None)
            except Exception:
                log.exception("刷新消息失败")
        if now - getattr(self, "_last_status", 0) >= 0.5:
            self._last_status = now
            try:
                self.update_status_line(now)
            except Exception:
                log.exception("刷新状态行失败")

    def update_status_line(self, now):
        s = self.cur_session()
        if not (s and s.busy):
            return
        spin = "◐◓◑◒"[int(now * 2) % 4]
        r = s.reply
        if s.worker and r is not None and id(r) in self.widgets:
            p = s.progress
            if r.get("notice"):
                text = r["notice"]
            elif p and p["name"] in ("write_file", "edit_file"):
                text = f"正在撰写 {p['path'] or '文件'}，已生成 {p['chars']:,} 字"
            elif p:
                verb = {**TOOL_LABELS, **SKILL_LABELS, **SHELL_LABELS, **MEMORY_LABELS, **PARTS_LABELS}.get(p["name"], ("", "调用工具"))[1]
                text = f"正在准备{verb}{' ' + p['path'] if p['path'] else ''}"
            elif r.get("content"):
                text = "正在输出回复"
            elif r.get("reasoning"):
                text = "正在思考"
            else:
                text = "等待模型响应"
            self.widgets[id(r)].set_status(f"{spin} {text} · 已用时 {int(now - s.started)} 秒")
        for m in list(s.running):
            if id(m) in self.widgets and m.get("_started"):
                self.widgets[id(m)].set_running(f"{spin} 运行中 {int(now - m['_started'])} 秒")

    # ---------- 发送 / 停止
    def build_messages(self, conv, upto=None, summary=None):
        """组装发给模型的消息：系统提示（目录、工具、skill、MCP、相关记忆、较早对话摘要）+ 摘要之后的对话原文。"""
        parts = [self.cfg.get("system_prompt", "").strip()]
        cwd = conv.get("cwd")
        if cwd and os.path.isdir(cwd):
            parts.append(f"用户当前的工作目录是 {cwd}，目录结构（前两层）如下：\n{dir_overview(cwd)}\n\n{TOOL_PROMPT}")
            if self.skill_on(conv):
                parts.append(skill_prompt())
                parts.append("### 环境检查结果（程序已自动完成，环境就绪时无需再调用 check_env）\n" + env_status())
        else:
            parts.append("当前对话没有关联工作目录，读写文件和运行命令的工具不可用；"
                         "需要操作文件时，请提示用户点击右上角「📁 打开目录」。")
        n_srv, n_tools = self.mcp.summary()
        if n_tools:
            parts.append(f"## MCP 工具\n已连接 {n_srv} 个 MCP 服务器，共 {n_tools} 个工具（函数名以 mcp__服务器名__ 开头），"
                         "需要外部数据或能力时按需调用。")
        last_user = next((m.get("content", "") for m in reversed(conv["messages"]) if m["role"] == "user"), "")
        digest = self.memory.digest(f"{conv.get('title', '')} {last_user}", exclude_source=conv["id"])
        parts.append(MEMORY_PROMPT + ("\n\n### 相关记忆（自动检索）\n" + digest if digest else "\n\n（记忆库中暂无相关条目）"))
        if conv.get("cwd") or self.parts.count():   # 在做文件/datasheet 工作、或库里已有数据时提示
            parts.append(PARTS_PROMPT)
        start = conv.get("summary_upto", 0) if upto is None else upto
        summ = conv.get("summary") if summary is None else summary
        if summ and start:
            parts.append("## 更早对话的摘要（原文已压缩，界面上仍可查看）\n" + summ)
        msgs = [{"role": "system", "content": "\n\n".join(x for x in parts if x)}]
        history = conv["messages"][start:]
        # 较早的大段 PDF / 文件原文只保留开头，避免超出上下文
        tool_ids = [i for i, m in enumerate(history) if m["role"] == "tool"]
        old_tools = set(tool_ids[:-12])
        for i, m in enumerate(history):
            role = m["role"]
            if role == "user":
                msgs.append({"role": "user", "content": api_content(m)})
            elif role == "assistant":
                if m.get("tool_calls"):
                    msgs.append({"role": "assistant", "content": m.get("content") or "", "tool_calls": m["tool_calls"]})
                elif m.get("content") and not m.get("error"):
                    msgs.append({"role": "assistant", "content": m["content"]})
            elif role == "tool":
                tcid = m.get("tool_call_id")
                if not tcid:   # 缺 tool_call_id 的工具消息无法发给接口，跳过（兼容异常/旧数据）
                    continue
                content = m.get("content", "")
                if i in old_tools and len(content) > 1500:
                    content = content[:1500] + "\n…（较早的工具输出已省略以节省上下文，如需请重新读取）"
                msgs.append({"role": "tool", "tool_call_id": tcid, "content": content})
        return msgs

    def tools_for(self, conv):
        """记忆和 MCP 工具始终可用；文件、命令和 skill 工具需要关联工作目录。"""
        tools = MEMORY_TOOLS + PARTS_TOOLS + self.mcp.tool_specs()
        cwd = conv.get("cwd")
        if cwd and os.path.isdir(cwd):
            tools = TOOLS + SHELL_TOOLS + (SKILL_TOOLS if self.skill_on(conv) else []) + tools
        return tools

    def compression_job(self, conv):
        """历史过长时返回 (待压缩的较早对话文本, 压缩到第几条消息)；切点总在某条用户消息之前。"""
        msgs = conv["messages"]
        start = conv.get("summary_upto", 0)
        size = sum(len(json.dumps(m, ensure_ascii=False)) for m in self.build_messages(conv)[1:])
        if size < COMPRESS_AT:
            return None
        last_user = max((i for i, m in enumerate(msgs) if m["role"] == "user"), default=-1)
        sizes = [len(json.dumps(m, ensure_ascii=False)) for m in msgs]
        cut_at = None
        for i in range(start + 1, last_user + 1):
            if msgs[i]["role"] != "user":
                continue
            cut_at = i
            if sum(sizes[i:]) <= KEEP_TAIL:
                break
        if not cut_at:
            return None
        prev = f"【此前已有的摘要】\n{conv['summary']}\n\n" if conv.get("summary") and start else ""
        return prev + "【需要压缩的对话】\n" + transcript(msgs[start:cut_at], limit=60000), cut_at

    def on_compressed(self, conv):
        if conv is self.cur:
            self.render_all()

    def skill_on(self, conv):
        cwd = conv.get("cwd")
        return bool(conv.get("skill") and cwd and os.path.isdir(cwd) and skill_dir())

    def cur_session(self):
        return self.sessions.get(self.cur["id"]) if self.cur else None

    def is_busy(self, conv):
        s = self.sessions.get(conv["id"])
        return bool(s and s.busy)

    def item_text(self, conv):
        return ("● " if self.is_busy(conv) else "") + conv["title"]

    def on_session_state(self, s):
        """某个对话开始或结束运行：更新列表标记和按钮。"""
        if s.conv in self.convs:
            item = self.conv_list.item(self.convs.index(s.conv))
            if item:
                item.setText(self.item_text(s.conv))
        if s.conv is self.cur:
            self.dir_btn.setEnabled(not s.busy)
            self._update_send_state()
            if not s.busy:
                self.input.setFocus()

    def stop_turn(self):
        s = self.cur_session()
        if s:
            s.stop()

    def on_send(self):
        s = self.cur_session()
        if s and s.busy:
            return s.stop()
        text = self.input.toPlainText().strip()
        if not text:
            return
        if not self.cfg["api_key"]:
            return self.open_settings()
        self.input.clear()

        conv = self.cur
        user = {"role": "user", "content": text}
        if self.pending_files:
            user["files"] = self.pending_files
            self.pending_files = []
            self._refresh_attachments()
        conv["messages"].append(user)
        if conv["title"] == "新对话":
            conv["title"] = text[:24]
            self.title_lbl.setText(conv["title"])
        self.show_message(conv, user)
        if not s:
            s = self.sessions[conv["id"]] = Session(self, conv)
        s.start()

    def run_tool(self, s, name, args):
        if name in PARTS_TOOL_NAMES:
            return self.parts.run_tool(name, args)
        if name in MEMORY_TOOL_NAMES:
            result = self.memory.run_tool(name, args, source=s.conv["id"])
            self.update_memory_btn()
            return result
        if name in SKILL_TOOL_NAMES:
            return run_skill_tool(name, args, s.conv["cwd"])
        if name in SHELL_TOOL_NAMES:
            return run_shell_tool(name, args, s.conv["cwd"])
        if not isinstance(args, dict):
            raise ToolError("参数不是合法的 JSON 对象")
        cwd = s.conv["cwd"]
        rel = args.get("path") or "."
        p = safe_path(cwd, rel)

        if name == "list_dir":
            if not os.path.isdir(p):
                raise ToolError("目录不存在")
            entries = sorted(os.scandir(p), key=lambda e: (not e.is_dir(), e.name.lower()))
            lines = [e.name + "/" if e.is_dir() else f"{e.name}  ({e.stat().st_size} B)" for e in entries[:300]]
            text = "\n".join(lines) or "（空目录）"
            return text, {"status": "ok", "preview": text}

        if name == "read_file":
            if not os.path.isfile(p):
                raise ToolError("文件不存在")
            text, err, _ = read_text_file(p)
            if err:
                raise ToolError(err)
            return text or "（空文件）", {"status": "ok", "preview": text[:4000]}

        if name not in ("write_file", "edit_file"):
            raise ToolError(f"未知的工具 {name}")
        if os.path.isdir(p):
            raise ToolError("这是一个目录，不是文件")
        exists = os.path.isfile(p)
        old, fmt = None, {"enc": "utf-8", "crlf": False}
        if exists:
            old, err, fmt = read_text_file(p)
            if err:
                raise ToolError(err)
        if name == "write_file":
            new = args.get("content")
            if not isinstance(new, str):
                raise ToolError("缺少 content 参数")
        else:
            if not exists:
                raise ToolError("文件不存在；新建文件请使用 write_file")
            old_str, new_str = args.get("old_string"), args.get("new_string")  # 注意不要用 s：s 是当前对话
            if not isinstance(old_str, str) or not isinstance(new_str, str) or not old_str:
                raise ToolError("old_string / new_string 参数无效")
            n = old.count(old_str)
            if n == 0:
                raise ToolError("文件中找不到 old_string，请先用 read_file 读取最新内容再修改")
            if n > 1 and not args.get("replace_all"):
                raise ToolError(f"old_string 在文件中出现了 {n} 次，请提供更多上下文使其唯一，或设置 replace_all")
            new = old.replace(old_str, new_str) if args.get("replace_all") else old.replace(old_str, new_str, 1)
        if new == old:
            return "内容没有变化，未写入。", {"status": "ok", "preview": "内容没有变化"}

        diff = unified_diff(old, new, rel)
        added, removed = diff_stats(diff)
        info = {"diff": diff, "added": added, "removed": removed, "new_file": not exists,
                "abs": p, "old": old, "enc": fmt["enc"], "crlf": fmt["crlf"]}
        write_text_file(p, new, fmt["enc"], fmt["crlf"])
        info["status"] = "ok"
        return f"已{'新建' if not exists else '修改'} {rel}（+{added} −{removed}）。", info

    # ---------- 记忆库
    def update_memory_btn(self):
        self.mem_btn.setText(f"🧠  记忆库（{len(self.memory.items)}）")

    def open_memory(self):
        MemoryDialog(self.memory, self).exec()
        self.update_memory_btn()

    def flash_hint(self, text, ms=8000):
        self.hint_lbl.setText(text)
        self.hint_lbl.setStyleSheet(f"color: {ACCENT};")
        QTimer.singleShot(ms, lambda: (self.hint_lbl.setText(HINT_TEXT), self.hint_lbl.setStyleSheet("")))

    def _conv_note(self, conv):
        return next((m for m in self.memory.items if m["type"] == "对话要点" and m.get("source") == conv["id"]), None)

    def on_turn_finished(self, conv):
        """一轮对话结束：排队在后台把新增内容提炼成要点存进记忆库。"""
        if not (self.cfg.get("auto_memory", True) and self.cfg["api_key"]):
            return
        new = conv["messages"][conv.get("distilled_upto", 0):]
        if not any(m["role"] == "assistant" and m.get("content") and not m.get("error") for m in new):
            return
        if conv["id"] not in self.distill_queue:
            self.distill_queue.append(conv["id"])
        self.run_distill()

    def run_distill(self):
        if self.distiller or not self.distill_queue:
            return
        cid = self.distill_queue.pop(0)
        conv = next((c for c in self.convs if c["id"] == cid), None)
        if not conv or self.is_busy(conv):  # 还在运行的对话等它结束后会重新排队
            return self.run_distill()
        upto = len(conv["messages"])
        text = transcript(conv["messages"][conv.get("distilled_upto", 0):upto], limit=40000)
        note = self._conv_note(conv)
        related = [m for m in self.memory.search(text[:6000], 12) if m["type"] != "对话要点"]
        ctx = "\n".join(f"- [{m['type']}] {m['title']}：{m['content'][:800]}" for m in related)
        prompt = (f"对话标题：{conv['title']}\n\n"
                  + (f"## 该对话之前提炼的要点（请在此基础上更新，输出完整版）\n{note['content']}\n\n" if note else "")
                  + (f"## 已有相关记忆\n{ctx}\n\n" if ctx else "")
                  + f"## 对话记录（新增部分）\n{text}")
        self.distiller = Distiller(dict(self.cfg), prompt)
        self.distiller.done.connect(lambda r, c=conv, u=upto: self.on_distilled(c, u, r))
        self.distiller.failed.connect(lambda e: log.warning("提炼记忆失败：%s", e))
        self.distiller.finished.connect(self._distill_finished)
        self.distiller.start()
        log.info("开始提炼对话「%s」的要点", conv["title"])

    def _distill_finished(self):
        if self.distiller:
            self.distiller.deleteLater()
            self.distiller = None
        QTimer.singleShot(0, self.run_distill)

    def on_distilled(self, conv, upto, result):
        try:
            added = updated = 0
            summ = str(result.get("summary") or "").strip()
            if summ:
                note = self._conv_note(conv)
                if note:
                    note.update(title=f"对话要点：{conv['title']}", content=summ,
                                updated=time.strftime("%Y-%m-%d %H:%M"))
                    updated += 1
                else:
                    self.memory.upsert(f"对话要点：{conv['title']}", summ, "对话要点", [], conv["id"])
                    added += 1
            for item in result.get("memories") or []:
                if isinstance(item, dict) and item.get("title") and item.get("content"):
                    _, is_new = self.memory.upsert(item["title"], item["content"], item.get("type"),
                                                   item.get("tags") if isinstance(item.get("tags"), list) else [],
                                                   conv["id"])
                    added += is_new
                    updated += not is_new
            self.memory.save()
            conv["distilled_upto"] = upto
            self.save_convs()
            self.update_memory_btn()
            log.info("对话「%s」要点已提炼：新增 %d，更新 %d", conv["title"], added, updated)
            if added or updated:
                self.flash_hint(f"🧠 已把「{conv['title'][:16]}」的要点存入记忆库：新增 {added} 条，更新 {updated} 条")
        except Exception:
            log.exception("保存提炼结果失败")

    # ---------- MCP
    def update_mcp_btn(self):
        n_srv, n_tools = self.mcp.summary()
        servers = self.mcp.servers.values()
        state = ""
        if any(s.status in ("pending", "connecting") for s in servers):
            state = " · 连接中"
        elif any(s.status == "error" for s in servers) or self.mcp.config_error:
            state = " ⚠"
        self.mcp_btn.setText(f"🔌  MCP 服务（{n_tools} 个工具）{state}" if self.mcp.servers else "🔌  MCP 服务")

    def open_mcp(self):
        McpDialog(self.mcp, self.mcp_relay, self).exec()

    def undo_tool(self, card):
        m = card.m
        if "move_from" in m:
            if QMessageBox.question(self, "撤销移动", f"把文件移回原位置？\n{m['move_to']}\n→ {m['move_from']}") \
                    != QMessageBox.StandardButton.Yes:
                return
            try:
                if os.path.exists(m["move_from"]):
                    raise OSError("原位置已经有同名文件")
                shutil.move(m["move_to"], m["move_from"])
            except OSError as e:
                QMessageBox.warning(self, "撤销失败", str(e))
                return
            m["status"] = "undone"
            card.refresh()
            return self.save_convs()
        if m.get("new_file"):
            q = f"删除 GLM 新建的文件？\n{m['abs']}"
        else:
            q = f"把文件恢复到这次修改之前的内容？\n{m['abs']}\n\n注意：这次修改之后对该文件的其它改动也会被覆盖。"
        if QMessageBox.question(self, "撤销修改", q) != QMessageBox.StandardButton.Yes:
            return
        try:
            if m.get("new_file"):
                os.remove(m["abs"])
            else:
                write_text_file(m["abs"], m["old"], m["enc"], m["crlf"])
        except OSError as e:
            QMessageBox.warning(self, "撤销失败", str(e))
            return
        m["status"] = "undone"
        card.refresh()
        self.save_convs()

    # ---------- 设置
    def open_settings(self):
        dlg = SettingsDialog(self.cfg, self)
        if dlg.exec():
            self.cfg.update(dlg.values())
            save_json(CONFIG_PATH, self.cfg)
            self.update_model_chip()
            if self.cur and not self.cur["messages"]:
                self.render_all()

    def closeEvent(self, ev):
        running = [s for s in self.sessions.values() if s.busy]
        if running and QMessageBox.question(
                self, "退出", f"还有 {len(running)} 个对话正在运行，退出会中止它们。\n确定要退出吗？") \
                != QMessageBox.StandardButton.Yes:
            ev.ignore()
            return
        log.info("程序退出")
        for s in self.sessions.values():
            s.stop()
        for s in self.sessions.values():
            s.wait()
        self.mcp.stop_all()
        if self.distiller:
            self.distiller.wait(3000)
        self.save_convs(force=True)
        self.parts.close()
        SAVER.flush()
        super().closeEvent(ev)


class ErrorRelay(QObject):
    """把任意线程里的未捕获异常转到界面线程提示。"""
    error = pyqtSignal(str)


def install_exception_hooks(relay):
    def hook(exc_type, exc, tb):
        log.error("未捕获的异常：\n%s", "".join(traceback.format_exception(exc_type, exc, tb)))
        try:
            relay.error.emit(f"{exc_type.__name__}: {exc}")
        except Exception:
            pass
    # PyQt6 默认遇到未捕获异常会直接终止程序；替换 excepthook 后改为记录并继续运行
    sys.excepthook = hook
    threading.excepthook = lambda a: hook(a.exc_type, a.exc_value, a.exc_traceback)


def single_instance_server(window):
    """程序只允许打开一个窗口：否则两个窗口会互相覆盖对话记录。"""
    name = "glm-mcp-doc-agent-" + format(zlib.crc32(ROOT.lower().encode("utf-8")), "08x")
    server = QLocalServer(window)
    QLocalServer.removeServer(name)  # 清理上次异常退出留下的残留
    server.listen(name)

    def activate():
        conn = server.nextPendingConnection()
        if conn:
            conn.disconnectFromServer()
        window.showNormal()
        window.raise_()
        window.activateWindow()
    server.newConnection.connect(activate)
    return server


def another_instance_running():
    name = "glm-mcp-doc-agent-" + format(zlib.crc32(ROOT.lower().encode("utf-8")), "08x")
    sock = QLocalSocket()
    sock.connectToServer(name)
    ok = sock.waitForConnected(500)
    sock.disconnectFromServer()
    return ok


def self_test(pdfs):
    """检查打包后的程序：依赖能否导入、编译环境、PDF 解析速度。用法：exe --selftest [a.pdf b.pdf]"""
    import skill_tools
    out = [f"{APP_NAME} 自检  {time.strftime('%Y-%m-%d %H:%M:%S')}", f"程序目录：{ROOT}"]
    for mod in ("PyQt6.QtWidgets", "PyQt6.QtNetwork", "requests", "markdown_it", "pygments", "pymupdf", "pypdf"):
        try:
            __import__(mod)
            out.append(f"[OK] 导入 {mod}")
        except Exception as e:
            out.append(f"[失败] 导入 {mod}：{e}")
    out.append(skill_tools.check_env())
    for p in pdfs:
        t = time.time()
        try:
            pages = skill_tools._extract(p)
            out.append(f"[OK] 解析 {os.path.basename(p)}：{len(pages)} 页，{sum(map(len, pages))} 字，{time.time() - t:.2f} 秒")
        except Exception as e:
            out.append(f"[失败] 解析 {p}：{e}")
    os.makedirs(LOG_DIR, exist_ok=True)
    with open(os.path.join(LOG_DIR, "selftest.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")


def main():
    # 打包成无控制台的 exe 时 stdout/stderr 为 None，任何打印都会出错，先指向空设备
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")
    setup_logging()
    if "--selftest" in sys.argv:  # 打包后自检：结果写入 logs/selftest.txt，不打开窗口
        return self_test([a for a in sys.argv[1:] if a.lower().endswith(".pdf")])
    log.info("程序启动 %s", APP_NAME)
    if sys.platform == "win32":  # 任务栏显示独立图标
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("glm.mcp.doc.agent")
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    if another_instance_running():  # 已经打开了：把那个窗口调到前面，自己退出
        log.info("检测到已运行的实例，激活后退出")
        return
    relay = ErrorRelay()
    install_exception_hooks(relay)
    icon = os.path.join(getattr(sys, "_MEIPASS", ROOT), "app.ico")
    if os.path.exists(icon):
        app.setWindowIcon(QIcon(icon))
    app.setStyle("Fusion")
    app.setFont(QFont("Microsoft YaHei UI", 10))
    app.setStyleSheet(QSS)
    w = MainWindow()
    relay.error.connect(w.report_error)
    app._single = single_instance_server(w)
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
