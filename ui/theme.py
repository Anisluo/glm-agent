"""ui.theme —— 配色、全局 QSS、文档 CSS。"""

BG, SIDE, BORDER = "#faf9f5", "#f3f1ea", "#e6e3da"
TEXT, MUTED = "#2b2a27", "#8b887f"
ACCENT, ACCENT_H = "#c96442", "#b45535"
USER_BG, HOVER = "#efece3", "#ebe8df"
CODE_BG, CODE_HEAD = "#282c34", "#21252b"

QSS = f"""
QWidget {{ color: {TEXT}; }}
QMainWindow, #main {{ background: {BG}; }}
#sidebar {{ background: {SIDE}; border-right: 1px solid {BORDER}; }}
#brand {{ font-size: 11pt; font-weight: 600; }}
#brandStar {{ color: {ACCENT}; font-size: 15pt; }}
#newBtn {{ background: transparent; border: 1px solid {BORDER}; border-radius: 10px;
          padding: 8px 12px; text-align: left; font-weight: 600; }}
#newBtn:hover {{ background: {HOVER}; }}
#sectionLabel {{ color: {MUTED}; font-size: 9pt; padding: 6px 8px 2px; }}
#convList {{ background: transparent; border: none; outline: 0; }}
#convList::item {{ padding: 8px 10px; border-radius: 8px; margin: 1px 0; color: {TEXT}; }}
#convList::item:hover {{ background: {HOVER}; }}
#convList::item:selected {{ background: #e3dfd3; color: {TEXT}; }}
#sideBtn {{ background: transparent; border: none; border-radius: 8px; padding: 8px 10px;
           text-align: left; color: #55524b; }}
#sideBtn:hover {{ background: {HOVER}; }}
#topBar {{ background: {BG}; border-bottom: 1px solid #efece4; }}
#convTitle {{ font-weight: 600; font-size: 10.5pt; }}
#dirBtn {{ border: 1px solid {BORDER}; border-radius: 13px; padding: 4px 12px;
          background: transparent; color: #55524b; }}
#dirBtn:hover {{ background: {SIDE}; }}
#dirBtn::menu-indicator {{ image: none; }}
#chatInner, QScrollArea {{ background: {BG}; border: none; }}
#userBubble {{ background: {USER_BG}; border-radius: 14px; padding: 10px 14px; }}
#fileTag {{ background: white; border: 1px solid {BORDER}; border-radius: 8px;
           padding: 3px 8px; color: #55524b; font-size: 9pt; }}
#avatar {{ background: {ACCENT}; color: white; border-radius: 12px; font-size: 11pt; }}
#msgName {{ font-weight: 600; }}
#msgModel {{ color: {MUTED}; font-size: 9pt; }}
#thinkBtn {{ border: none; background: transparent; color: {MUTED}; padding: 2px 0; }}
#thinkBtn:hover {{ color: {TEXT}; }}
#thinkText {{ color: {MUTED}; border-left: 2px solid {BORDER}; padding-left: 10px; }}
#errorBox {{ background: #fbeceb; color: #b3261e; border: 1px solid #f3cfcc;
            border-radius: 8px; padding: 8px 12px; }}
#ghostBtn {{ border: none; background: transparent; color: {MUTED}; padding: 3px 8px; border-radius: 6px; }}
#ghostBtn:hover {{ background: {USER_BG}; color: {TEXT}; }}
#composer {{ background: white; border: 1px solid #ddd9ce; border-radius: 18px; }}
#input {{ background: transparent; border: none; font-size: 10.5pt; selection-background-color: #f1d3c5; }}
#chip, #attach {{ border: 1px solid {BORDER}; border-radius: 10px; padding: 4px 10px;
                 background: transparent; color: #55524b; font-size: 9pt; }}
#chip:hover, #attach:hover {{ background: {SIDE}; }}
#chip:checked {{ background: #f8e7df; border-color: #ecc3b2; color: {ACCENT}; }}
#attach {{ background: {SIDE}; }}
#sendBtn {{ background: {ACCENT}; color: white; border: none; border-radius: 10px;
           font-size: 12pt; font-weight: bold; }}
#sendBtn:hover {{ background: {ACCENT_H}; }}
#sendBtn:disabled {{ background: #e8cfc4; }}
#hint {{ color: {MUTED}; font-size: 8.5pt; }}
#welcomeStar {{ color: {ACCENT}; font-size: 30pt; }}
#welcomeTitle {{ font-family: Georgia, "STZhongsong", "SimSun", serif; font-size: 24pt; }}
#welcomeSub {{ color: {MUTED}; font-size: 11pt; }}
#suggest {{ border: 1px solid {BORDER}; border-radius: 12px; padding: 8px 14px;
           background: white; color: #55524b; }}
#suggest:hover {{ border-color: #d4cfc2; background: #fdfcf9; }}
#filePanel {{ background: {SIDE}; border-left: 1px solid {BORDER}; }}
#panelTitle {{ font-weight: 600; }}
#panelPath {{ color: {MUTED}; font-size: 8.5pt; }}
#fileTree {{ background: transparent; border: none; outline: 0; }}
#fileTree::item {{ padding: 3px 2px; border-radius: 4px; }}
#fileTree::item:hover {{ background: {HOVER}; }}
#fileTree::item:selected {{ background: #e3dfd3; color: {TEXT}; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: #d9d5ca; border-radius: 3px; min-height: 30px; }}
QScrollBar::handle:vertical:hover {{ background: #c8c3b6; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}
QScrollBar:horizontal {{ height: 0; }}
QDialog {{ background: {BG}; }}
QDialog QLineEdit, QDialog QComboBox, QDialog QPlainTextEdit, QDialog QDoubleSpinBox {{
    background: white; border: 1px solid #ddd9ce; border-radius: 8px; padding: 6px 8px; }}
QDialog QLineEdit:focus, QDialog QPlainTextEdit:focus {{ border-color: {ACCENT}; }}
QDialog QPushButton {{ background: white; border: 1px solid #ddd9ce; border-radius: 8px; padding: 6px 18px; }}
QDialog QPushButton:hover {{ background: {SIDE}; }}
QMenu {{ background: white; border: 1px solid {BORDER}; padding: 4px; }}
QMenu::item {{ padding: 6px 20px; border-radius: 6px; }}
QMenu::item:selected {{ background: {SIDE}; color: {TEXT}; }}
QToolTip {{ background: {TEXT}; color: white; border: none; padding: 4px 8px; }}
#toolCard {{ background: white; border: 1px solid {BORDER}; border-radius: 10px; }}
#toolStatus {{ font-size: 9pt; }}
QDialog #primaryBtn {{ background: {ACCENT}; color: white; border: none; }}
#statusLine {{ color: {MUTED}; font-size: 9pt; padding: 2px 0; }}
#divider {{ border: none; background: transparent; color: {MUTED}; font-size: 9pt; padding: 4px; }}
#divider:hover {{ color: {TEXT}; }}
QDialog #primaryBtn:hover {{ background: {ACCENT_H}; }}
"""

DOC_CSS = f"""
p, li {{ line-height: 165%; }}
code {{ background-color: {USER_BG}; color: #b5502f; font-family: Consolas, monospace; }}
h1 {{ font-size: 17pt; }} h2 {{ font-size: 14.5pt; }} h3 {{ font-size: 12.5pt; }}
blockquote {{ color: {MUTED}; }}
a {{ color: {ACCENT}; }}
th {{ background-color: {SIDE}; }}
"""
