"""ui —— 界面层：主题、渲染、控件与对话框。依赖 infra，不依赖 glm_client。"""
from ui.theme import DOC_CSS, QSS
from ui.render import diff_html, md_to_html
from ui.widgets import (AssistantMessage, ChatArea, EnterFilter, FilePanel, McpConfigDialog,
                        McpDialog, MemoryDialog, RichView, SettingsDialog, SummaryDivider,
                        ToolCard, UserMessage)
