"""ui.widgets —— 消息控件、目录面板、设置/记忆/MCP 对话框。"""
import html
import json
import math
import os
import time

from PyQt6.QtCore import QDir, QEvent, QObject, Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtGui import QCursor, QDesktopServices, QFileSystemModel, QFont, QIcon, QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
    QFileDialog, QFormLayout, QFrame, QHBoxLayout, QInputDialog, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMenu, QMessageBox, QPlainTextEdit,
    QPushButton, QScrollArea, QSizePolicy, QStackedWidget, QTextBrowser, QToolButton,
    QToolTip, QTreeView, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from skill_tools import SKILL_LABELS
from shell_tools import SHELL_LABELS
from memory_store import MEMORY_LABELS, TYPES as MEMORY_TYPES, MemoryStore, parse_json_object
from infra import (BASE_URLS, DEFAULT_CONFIG, HINT_TEXT, TOOL_LABELS, api_content, diff_stats,
                   lang_of, open_in_explorer, read_text_file, unified_diff)
from ui.theme import (ACCENT, ACCENT_H, BG, BORDER, CODE_BG, CODE_HEAD, DOC_CSS, HOVER,
                      MUTED, QSS, SIDE, TEXT, USER_BG)
from ui.render import diff_html, md_to_html

import logging
log = logging.getLogger("glm")


class SettingsDialog(QDialog):
    def __init__(self, cfg, parent=None):
        super().__init__(parent)
        self.setWindowTitle("设置")
        self.setMinimumWidth(520)
        form = QFormLayout(self)
        form.setContentsMargins(22, 20, 22, 18)
        form.setVerticalSpacing(12)

        self.key = QLineEdit(cfg["api_key"])
        self.key.setEchoMode(QLineEdit.EchoMode.Password)
        self.key.setPlaceholderText("在智谱开放平台获取的 API Key")
        show = QCheckBox("显示")
        show.toggled.connect(lambda on: self.key.setEchoMode(
            QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password))
        key_row = QHBoxLayout()
        key_row.addWidget(self.key)
        key_row.addWidget(show)
        form.addRow("API Key", key_row)

        self.base = QComboBox()
        self.base.setEditable(True)
        for name, url in BASE_URLS.items():
            self.base.addItem(f"{name}  {url}", url)
        idx = self.base.findData(cfg["base_url"])
        if idx >= 0:
            self.base.setCurrentIndex(idx)
        else:
            self.base.setEditText(cfg["base_url"])
        form.addRow("接口地址", self.base)

        self.model = QLineEdit(cfg["model"])
        form.addRow("模型名称", self.model)

        self.temp = QDoubleSpinBox()
        self.temp.setRange(0, 1)
        self.temp.setSingleStep(0.05)
        self.temp.setValue(float(cfg["temperature"]))
        form.addRow("Temperature", self.temp)

        self.system = QPlainTextEdit(cfg.get("system_prompt", ""))
        self.system.setFixedHeight(90)
        form.addRow("系统提示词", self.system)

        self.auto_memory = QCheckBox("对话结束后自动提炼要点到记忆库（会额外消耗少量额度）")
        self.auto_memory.setChecked(bool(cfg.get("auto_memory", True)))
        form.addRow("记忆库", self.auto_memory)

        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        btns.button(QDialogButtonBox.StandardButton.Save).setText("保存")
        btns.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        form.addRow(btns)

    def values(self):
        base = self.base.currentData()
        if base is None or self.base.currentText() != self.base.itemText(self.base.currentIndex()):
            base = self.base.currentText().strip()  # 用户手动输入的地址
        return {
            "api_key": self.key.text().strip(),
            "base_url": base,
            "model": self.model.text().strip() or DEFAULT_CONFIG["model"],
            "temperature": self.temp.value(),
            "system_prompt": self.system.toPlainText(),
            "auto_memory": self.auto_memory.isChecked(),
        }


class RichView(QTextBrowser):
    """高度随内容自适应、无滚动条的 Markdown 显示区。"""

    def __init__(self):
        super().__init__()
        self.codes = []
        self.setOpenLinks(False)
        self.anchorClicked.connect(self._on_anchor)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setStyleSheet("QTextBrowser { background: transparent; border: none; }")
        doc = self.document()
        doc.setDefaultStyleSheet(DOC_CSS)
        doc.setDocumentMargin(0)
        doc.documentLayout().documentSizeChanged.connect(self._fit)

    def set_markdown(self, text):
        if getattr(self, "_rendered", None) == ("md", text):
            return
        self._rendered = ("md", text)
        h, self.codes = md_to_html(text)
        self.setHtml(h)

    def set_plain(self, text):
        """流式输出时的轻量渲染：纯文本转义即可，不跑 Markdown/代码高亮，避免每次刷新卡顿。"""
        if getattr(self, "_rendered", None) == ("plain", text):
            return
        self._rendered = ("plain", text)
        self.codes = []
        self.setHtml(f'<div style="white-space:pre-wrap;font-family:system-ui,\'Microsoft YaHei\',sans-serif">'
                     f'{html.escape(text)}</div>')

    def _fit(self, *_):
        self.setFixedHeight(math.ceil(self.document().size().height()) + 4)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._fit()

    def wheelEvent(self, e):
        e.ignore()  # 交给外层滚动区

    def _on_anchor(self, url):
        s = url.toString()
        if s.startswith("copy:"):
            QApplication.clipboard().setText(self.codes[int(s[5:])])
            QToolTip.showText(QCursor.pos(), "已复制代码")
        else:
            QDesktopServices.openUrl(url)


class UserMessage(QWidget):
    def __init__(self, m):
        super().__init__()
        self.text = m["content"]
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 14, 0, 0)
        v.setSpacing(6)
        if m.get("files"):
            row = QHBoxLayout()
            row.addStretch()
            for f in m["files"]:
                tag = QLabel("📎 " + os.path.basename(f["path"]), objectName="fileTag")
                tag.setToolTip(f["path"])
                row.addWidget(tag)
            v.addLayout(row)
        row = QHBoxLayout()
        row.addStretch()
        self.bubble = QLabel(self.text, objectName="userBubble")
        self.bubble.setTextFormat(Qt.TextFormat.PlainText)
        self.bubble.setWordWrap(True)
        self.bubble.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        row.addWidget(self.bubble)
        v.addLayout(row)

    def fit(self, avail):
        fm = self.bubble.fontMetrics()
        natural = max((fm.horizontalAdvance(l) for l in self.text.split("\n")), default=0) + 34
        self.bubble.setFixedWidth(max(40, min(natural, int(avail * 0.78))))


class AssistantMessage(QWidget):
    def __init__(self, m, model, show_head=True):
        super().__init__()
        self._auto_fold = True
        self._thinking_now = False
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 8 if show_head else 0, 0, 0)
        v.setSpacing(6)

        if show_head:  # 同一轮回答里的后续步骤不再重复显示头像
            head = QHBoxLayout()
            head.setSpacing(8)
            avatar = QLabel("✦", objectName="avatar")
            avatar.setFixedSize(24, 24)
            avatar.setAlignment(Qt.AlignmentFlag.AlignCenter)
            head.addWidget(avatar)
            head.addWidget(QLabel("GLM", objectName="msgName"))
            head.addWidget(QLabel(m.get("model") or model, objectName="msgModel"))
            head.addStretch()
            v.addLayout(head)

        body = QVBoxLayout()
        body.setContentsMargins(32, 0, 0, 0)
        body.setSpacing(6)
        self.think_btn = QToolButton(objectName="thinkBtn")
        self.think_btn.setCheckable(True)
        self.think_btn.toggled.connect(self._on_think_toggled)
        self.think_btn.clicked.connect(lambda: setattr(self, "_auto_fold", False))
        self.think_lbl = QLabel(objectName="thinkText")
        self.think_lbl.setWordWrap(True)
        self.think_lbl.setTextFormat(Qt.TextFormat.PlainText)
        self.think_lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.view = RichView()
        self.err = QLabel(objectName="errorBox")
        self.err.setWordWrap(True)
        self.err.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        foot = QHBoxLayout()
        self.copy_btn = QPushButton("复制", objectName="ghostBtn")
        self.copy_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.copy_btn.clicked.connect(self._copy)
        foot.addWidget(self.copy_btn)
        self.time_lbl = QLabel(objectName="statusLine")
        foot.addWidget(self.time_lbl)
        foot.addStretch()
        self.status_lbl = QLabel(objectName="statusLine")
        for w in (self.think_btn, self.think_lbl, self.view, self.status_lbl, self.err):
            body.addWidget(w, 0, Qt.AlignmentFlag.AlignLeft if w is self.think_btn else Qt.AlignmentFlag(0))
        body.addLayout(foot)
        v.addLayout(body)
        self.update_msg(m, streaming=False)

    def _on_think_toggled(self, on):
        self.think_lbl.setVisible(on)
        self._set_think_text()

    def _set_think_text(self):
        arrow = "▾" if self.think_btn.isChecked() else "▸"
        label = "正在思考…" if self._thinking_now else "思考过程"
        self.think_btn.setText(f"{arrow} {label}")

    def update_msg(self, m, streaming):
        self.content = m.get("content", "")
        reasoning = m.get("reasoning", "").strip()
        self._thinking_now = bool(streaming and reasoning and not self.content)
        self.think_btn.setVisible(bool(reasoning))
        if reasoning:
            self.think_lbl.setText(reasoning)
            if self._auto_fold:
                self.think_btn.setChecked(bool(self._thinking_now))
        self.think_lbl.setVisible(bool(reasoning) and self.think_btn.isChecked())
        self._set_think_text()

        if self.content:
            (self.view.set_plain if streaming else self.view.set_markdown)(self.content)
        self.view.setVisible(bool(self.content))
        if streaming and not self.status_lbl.text():
            self.status_lbl.setText("◐ 等待模型响应…")
        self.status_lbl.setVisible(streaming)

        self.err.setText(m.get("error", ""))
        self.err.setVisible(bool(m.get("error")))
        self.copy_btn.setVisible(not streaming and bool(self.content))
        t = m.get("timing")
        self.time_lbl.setText(f"⏱ 本步 {t['total']:.0f} 秒" + (f"（思考 {t['think']:.0f} 秒）" if t.get("think") else "")
                              if t else "")
        self.time_lbl.setVisible(bool(t) and not streaming)

    def set_status(self, text):
        self.status_lbl.setText(text)

    def _copy(self):
        QApplication.clipboard().setText(self.content)
        QToolTip.showText(QCursor.pos(), "已复制")


class ToolCard(QWidget):
    """一次文件操作的记录卡片：摘要、状态、可展开的详情、撤销。"""
    STATUS = {"ok": ("✓ 完成", "#1a7f37"), "rejected": ("已拒绝", MUTED), "error": ("失败", "#cf222e"),
              "undone": ("已撤销", MUTED), "skipped": ("已跳过", MUTED), "running": ("运行中…", ACCENT)}

    def __init__(self, m, on_undo):
        super().__init__()
        self.m = m
        outer = QHBoxLayout(self)
        outer.setContentsMargins(32, 0, 0, 0)
        card = QFrame(objectName="toolCard")
        outer.addWidget(card)
        v = QVBoxLayout(card)
        v.setContentsMargins(12, 6, 8, 6)
        v.setSpacing(4)

        head = QHBoxLayout()
        name = m.get("name", "")
        icon, verb = {**TOOL_LABELS, **SKILL_LABELS, **SHELL_LABELS, **MEMORY_LABELS}.get(
            name, ("🔌", "MCP") if name.startswith("mcp__") else ("🔧", name or "工具"))
        if m.get("name") == "write_file" and m.get("new_file"):
            verb = "新建"
        text = f"{icon}&nbsp; {verb} <b>{html.escape(m.get('path') or '')}</b>"
        if "added" in m:
            text += (f"&nbsp; <span style='color:#1a7f37'>+{m['added']}</span>"
                     f" <span style='color:#cf222e'>−{m['removed']}</span>")
        summary = QLabel(text)
        summary.setTextFormat(Qt.TextFormat.RichText)
        self.status = QLabel(objectName="toolStatus")
        self.undo_btn = QPushButton("撤销", objectName="ghostBtn")
        self.undo_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.undo_btn.clicked.connect(lambda: on_undo(self))
        self.toggle = QToolButton(objectName="thinkBtn")
        self.toggle.setCheckable(True)
        self.toggle.setText("详情 ▸")
        self.toggle.toggled.connect(self._on_toggle)
        head.addWidget(summary, 1)
        head.addWidget(self.status)
        self.open_btn = QPushButton("打开 PDF", objectName="ghostBtn")
        self.open_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.open_btn.clicked.connect(lambda: os.startfile(self.m["open_path"]))
        head.addWidget(self.undo_btn)
        head.addWidget(self.open_btn)
        head.addWidget(self.toggle)
        v.addLayout(head)
        self.detail = RichView()
        self.detail.hide()
        v.addWidget(self.detail)
        self.refresh()

    def _on_toggle(self, on):
        self.toggle.setText("详情 ▾" if on else "详情 ▸")
        if on and not self.detail.toPlainText():
            if self.m.get("diff"):
                self.detail.setHtml(diff_html(self.m["diff"]))
            else:
                self.detail.setHtml('<pre style="margin:0;white-space:pre-wrap;font-family:Consolas,monospace;'
                                    f'font-size:9.5pt;color:#55524b">{html.escape(self.m.get("preview", ""))}</pre>')
        self.detail.setVisible(on)

    def set_running(self, text):
        if self.m.get("status") == "running":
            self.status.setText(text)

    def refresh(self):
        text, color = self.STATUS.get(self.m.get("status"), ("", MUTED))
        self.status.setText(text)
        self.status.setStyleSheet(f"color: {color};")
        self.undo_btn.setVisible(self.m.get("status") == "ok" and ("abs" in self.m or "move_from" in self.m))
        self.open_btn.setVisible(bool(self.m.get("open_path")) and os.path.exists(self.m.get("open_path", "")))
        self.toggle.setVisible(bool(self.m.get("diff") or self.m.get("preview")))


class ChatArea(QScrollArea):
    MAX_W = 820

    def __init__(self):
        super().__init__()
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        inner = QWidget(objectName="chatInner")
        h = QHBoxLayout(inner)
        h.setContentsMargins(24, 0, 24, 0)
        col = QWidget()
        col.setMaximumWidth(self.MAX_W)
        h.addStretch(1)
        h.addWidget(col, 20)
        h.addStretch(1)
        self.lay = QVBoxLayout(col)
        self.lay.setContentsMargins(0, 24, 0, 32)
        self.lay.setSpacing(10)
        self.lay.addStretch()
        self.setWidget(inner)

    def avail(self):
        return min(self.MAX_W, self.viewport().width() - 48)

    def add(self, w):
        self.lay.insertWidget(self.lay.count() - 1, w)
        if isinstance(w, UserMessage):
            w.fit(self.avail())

    def clear(self):
        while self.lay.count() > 1:
            self.lay.takeAt(0).widget().deleteLater()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        for i in range(self.lay.count() - 1):
            w = self.lay.itemAt(i).widget()
            if isinstance(w, UserMessage):
                w.fit(self.avail())

    def at_bottom(self):
        bar = self.verticalScrollBar()
        return bar.value() >= bar.maximum() - 60

    def scroll_bottom(self):
        QTimer.singleShot(30, lambda: self.verticalScrollBar().setValue(self.verticalScrollBar().maximum()))


class FilePanel(QFrame):
    attach = pyqtSignal(str)

    def __init__(self):
        super().__init__(objectName="filePanel")
        self.setFixedWidth(260)
        self.root = ""
        v = QVBoxLayout(self)
        v.setContentsMargins(12, 12, 8, 12)
        v.setSpacing(4)
        head = QHBoxLayout()
        self.title = QLabel(objectName="panelTitle")
        open_btn = QPushButton("打开", objectName="ghostBtn")
        open_btn.setToolTip("在资源管理器中打开")
        open_btn.clicked.connect(lambda: self.root and open_in_explorer(self.root))
        head.addWidget(self.title, 1)
        head.addWidget(open_btn)
        v.addLayout(head)
        self.path_lbl = QLabel(objectName="panelPath")
        v.addWidget(self.path_lbl)

        self.model = QFileSystemModel()
        self.model.setFilter(QDir.Filter.AllEntries | QDir.Filter.NoDotAndDotDot)
        self.tree = QTreeView(objectName="fileTree")
        self.tree.setModel(self.model)
        for col in (1, 2, 3):
            self.tree.hideColumn(col)
        self.tree.setHeaderHidden(True)
        self.tree.setAnimated(True)
        self.tree.doubleClicked.connect(self._on_double)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._menu)
        v.addWidget(self.tree, 1)
        tip = QLabel("双击文件添加到对话", objectName="panelPath")
        v.addWidget(tip)

    def set_root(self, path):
        self.root = path
        self.title.setText("📁 " + os.path.basename(path.rstrip("\\/")) if path else "")
        self.path_lbl.setText(self.path_lbl.fontMetrics().elidedText(path, Qt.TextElideMode.ElideMiddle, 230))
        self.path_lbl.setToolTip(path)
        if path:
            self.tree.setRootIndex(self.model.setRootPath(path))

    def _on_double(self, idx):
        if not self.model.isDir(idx):
            self.attach.emit(self.model.filePath(idx))

    def _menu(self, pos):
        idx = self.tree.indexAt(pos)
        if not idx.isValid():
            return
        path = self.model.filePath(idx)
        menu = QMenu(self)
        if not self.model.isDir(idx):
            menu.addAction("添加到对话", lambda: self.attach.emit(path))
            menu.addAction("用默认程序打开", lambda: os.startfile(path))
        menu.addAction("在资源管理器中显示", lambda: open_in_explorer(path, select=True))
        menu.exec(self.tree.viewport().mapToGlobal(pos))


class EnterFilter(QObject):
    def __init__(self, callback):
        super().__init__()
        self.callback = callback

    def eventFilter(self, obj, ev):
        if (ev.type() == QEvent.Type.KeyPress and ev.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
                and not ev.modifiers() & Qt.KeyboardModifier.ShiftModifier):
            self.callback()
            return True
        return False


class SummaryDivider(QPushButton):
    """聊天里标出“以上较早的对话已压缩”的位置，点击查看摘要。"""

    def __init__(self, summary):
        super().__init__("—— 以上较早的对话已压缩为摘要发送给模型（点击查看摘要）——", objectName="divider")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.clicked.connect(lambda: QMessageBox.information(self.window(), "较早对话的摘要", summary or "（空）"))


class MemoryDialog(QDialog):
    """查看、搜索、编辑、删除记忆条目。"""

    def __init__(self, store, parent=None):
        super().__init__(parent)
        self.store, self.cur_id = store, None
        self.setWindowTitle("记忆库")
        self.resize(980, 640)
        v = QVBoxLayout(self)
        v.setContentsMargins(18, 14, 18, 14)
        top = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索标题、内容、标签（如器件型号）…")
        self.search.textChanged.connect(lambda: self.refresh())
        self.type_filter = QComboBox()
        self.type_filter.addItems(["全部类型"] + MEMORY_TYPES)
        self.type_filter.currentIndexChanged.connect(lambda: self.refresh())
        self.count = QLabel()
        self.count.setStyleSheet(f"color: {MUTED};")
        top.addWidget(self.search, 1)
        top.addWidget(self.type_filter)
        top.addWidget(self.count)
        v.addLayout(top)

        body = QHBoxLayout()
        self.list = QListWidget()
        self.list.setFixedWidth(340)
        self.list.currentRowChanged.connect(self.load)
        body.addWidget(self.list)
        form = QFormLayout()
        self.f_type = QComboBox()
        self.f_type.addItems(MEMORY_TYPES)
        self.f_title = QLineEdit()
        self.f_tags = QLineEdit()
        self.f_tags.setPlaceholderText("用逗号分隔，如 AD8421, 仪表放大器")
        self.f_content = QPlainTextEdit()
        self.f_meta = QLabel()
        self.f_meta.setStyleSheet(f"color: {MUTED}; font-size: 9pt;")
        form.addRow("类型", self.f_type)
        form.addRow("标题", self.f_title)
        form.addRow("标签", self.f_tags)
        form.addRow("内容", self.f_content)
        form.addRow("", self.f_meta)
        right = QWidget()
        right.setLayout(form)
        body.addWidget(right, 1)
        v.addLayout(body, 1)

        row = QHBoxLayout()
        for text, fn in (("新建", self.new), ("删除", self.delete)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            row.addWidget(b)
        row.addStretch()
        save = QPushButton("保存修改", objectName="primaryBtn")
        save.clicked.connect(self.save)
        close = QPushButton("关闭")
        close.clicked.connect(self.accept)
        row.addWidget(save)
        row.addWidget(close)
        v.addLayout(row)
        self.refresh()

    def refresh(self, select_id=None):
        q = self.search.text().strip()
        items = self.store.search(q, 500) if q else sorted(self.store.items, key=lambda m: m.get("updated", ""),
                                                           reverse=True)
        t = self.type_filter.currentText()
        if t != "全部类型":
            items = [m for m in items if m["type"] == t]
        self.list.blockSignals(True)
        self.list.clear()
        row_to_select = 0
        for i, m in enumerate(items):
            it = QListWidgetItem(f"[{m['type']}] {m['title']}")
            it.setData(Qt.ItemDataRole.UserRole, m["id"])
            it.setToolTip(m["content"][:500])
            self.list.addItem(it)
            if m["id"] == (select_id or self.cur_id):
                row_to_select = i
        self.list.blockSignals(False)
        self.count.setText(f"共 {len(self.store.items)} 条，显示 {len(items)} 条")
        if items:
            self.list.setCurrentRow(row_to_select)
            self.load(row_to_select)
        else:
            self.new()

    def load(self, row):
        it = self.list.item(row)
        m = self.store.get(it.data(Qt.ItemDataRole.UserRole)) if it else None
        if not m:
            return
        self.cur_id = m["id"]
        self.f_type.setCurrentText(m["type"])
        self.f_title.setText(m["title"])
        self.f_tags.setText(", ".join(m.get("tags", [])))
        self.f_content.setPlainText(m["content"])
        self.f_meta.setText(f"#{m['id']} · 创建 {m.get('created', '')} · 更新 {m.get('updated', '')}")

    def new(self):
        self.cur_id = None
        self.list.clearSelection()
        for w in (self.f_title, self.f_tags):
            w.clear()
        self.f_content.clear()
        self.f_meta.setText("新条目")
        self.f_title.setFocus()

    def save(self):
        title, content = self.f_title.text().strip(), self.f_content.toPlainText().strip()
        if not title or not content:
            QMessageBox.information(self, "记忆库", "标题和内容不能为空。")
            return
        tags = [t.strip() for t in self.f_tags.text().replace("，", ",").split(",") if t.strip()]
        m = self.store.get(self.cur_id) if self.cur_id else None
        if m:
            m.update(type=self.f_type.currentText(), title=title, content=content, tags=tags,
                     updated=time.strftime("%Y-%m-%d %H:%M"))
        else:
            m, _ = self.store.upsert(title, content, self.f_type.currentText(), tags, "手动添加")
        self.store.save()
        self.cur_id = m["id"]
        self.refresh(m["id"])

    def delete(self):
        m = self.store.get(self.cur_id) if self.cur_id else None
        if not m:
            return
        if QMessageBox.question(self, "删除记忆", f"删除「{m['title']}」？") == QMessageBox.StandardButton.Yes:
            self.store.delete(m["id"])
            self.store.save()
            self.cur_id = None
            self.refresh()


class McpConfigDialog(QDialog):
    """直接编辑 mcp_servers.json，保存前校验格式。"""

    def __init__(self, path, parent=None):
        super().__init__(parent)
        self.path = path
        self.setWindowTitle("编辑 MCP 配置")
        self.resize(820, 600)
        v = QVBoxLayout(self)
        v.setContentsMargins(18, 14, 18, 14)
        tip = QLabel("格式与主流 MCP 客户端通用的 <b>mcpServers</b> 相同，可以直接粘贴别处的配置。"
                     "本地服务器写 <code>command</code> + <code>args</code>；远程服务器写 <code>url</code>"
                     "（旧版 SSE 服务器加 <code>\"type\": \"sse\"</code>）。可选：<code>env</code>、<code>headers</code>、"
                     "<code>disabled</code>、<code>timeout</code>。")
        tip.setWordWrap(True)
        v.addWidget(tip)
        self.edit = QPlainTextEdit()
        self.edit.setStyleSheet("QPlainTextEdit { font-family: Consolas, monospace; font-size: 10pt; }")
        try:
            with open(path, encoding="utf-8-sig") as f:
                self.edit.setPlainText(f.read())
        except OSError:
            self.edit.setPlainText('{\n  "mcpServers": {}\n}')
        v.addWidget(self.edit, 1)
        row = QHBoxLayout()
        row.addStretch()
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        ok = QPushButton("保存并重新连接", objectName="primaryBtn")
        ok.clicked.connect(self.save)
        row.addWidget(cancel)
        row.addWidget(ok)
        v.addLayout(row)

    def save(self):
        text = self.edit.toPlainText()
        try:
            data = json.loads(text)
            servers = data.get("mcpServers", data.get("servers")) if isinstance(data, dict) else None
            if not isinstance(servers, dict):
                raise ValueError("顶层需要有 \"mcpServers\": { ... }")
            for name, c in servers.items():
                if not isinstance(c, dict) or not (c.get("command") or c.get("url")):
                    raise ValueError(f"服务器「{name}」需要 command 或 url")
        except ValueError as e:
            QMessageBox.warning(self, "配置有误", f"无法保存：{e}")
            return
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, self.path)
        self.accept()


class McpDialog(QDialog):
    """MCP 服务器状态与工具列表。"""
    STATUS = {"ok": "✓ 已连接", "connecting": "连接中…", "pending": "等待连接", "error": "✕ 连接失败",
              "disabled": "已停用"}

    def __init__(self, mgr, relay, parent=None):
        super().__init__(parent)
        self.mgr, self.relay = mgr, relay
        self.setWindowTitle("MCP 服务")
        self.resize(900, 580)
        v = QVBoxLayout(self)
        v.setContentsMargins(18, 14, 18, 14)
        info = QLabel(f"配置文件：{mgr.config_path}<br>启动时自动连接所有服务器，GLM 调用工具时直接执行。")
        info.setWordWrap(True)
        info.setStyleSheet(f"color: {MUTED};")
        v.addWidget(info)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["服务器 / 工具", "状态", "说明"])
        self.tree.setColumnWidth(0, 260)
        self.tree.setColumnWidth(1, 110)
        v.addWidget(self.tree, 1)
        row = QHBoxLayout()
        for text, fn in (("编辑配置…", self.edit), ("重新连接", self.reconnect),
                         ("打开所在文件夹", lambda: open_in_explorer(self.mgr.config_path, select=True))):
            b = QPushButton(text)
            b.clicked.connect(fn)
            row.addWidget(b)
        row.addStretch()
        close = QPushButton("关闭")
        close.clicked.connect(self.accept)
        row.addWidget(close)
        v.addLayout(row)
        relay.changed.connect(self.refresh)
        self.finished.connect(lambda: relay.changed.disconnect(self.refresh))
        self.refresh()

    def refresh(self):
        self.tree.clear()
        if self.mgr.config_error:
            QTreeWidgetItem(self.tree, ["配置文件", "✕ 有误", self.mgr.config_error])
        if not self.mgr.servers and not self.mgr.config_error:
            QTreeWidgetItem(self.tree, ["（还没有配置 MCP 服务器）", "", "点「编辑配置…」添加"])
        for s in self.mgr.servers.values():
            if s.status == "ok":
                srv = s.info.get("name", "")
                desc = f"{s.kind} · {srv} {s.info.get('version', '')} · {len(s.tools)} 个工具".replace("  ", " ")
            elif s.status == "error":
                desc = s.error
            else:
                desc = s.kind
            top = QTreeWidgetItem(self.tree, [s.name, self.STATUS.get(s.status, s.status), desc])
            top.setToolTip(2, desc)
            for t in s.tools:
                ann = t.get("annotations") or {}
                mode = "只读" if ann.get("readOnlyHint") else "可修改"
                d = (t.get("description") or "").strip().splitlines()
                child = QTreeWidgetItem(top, [t["name"], mode, d[0][:160] if d else ""])
                child.setToolTip(2, t.get("description") or "")
            top.setExpanded(s.status == "ok" and len(s.tools) <= 12)

    def edit(self):
        if McpConfigDialog(self.mgr.config_path, self).exec():
            self.reconnect()

    def reconnect(self):
        self.mgr.start_all()
        self.mgr.tool_specs()
        self.refresh()
