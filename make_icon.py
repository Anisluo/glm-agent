"""生成程序图标 app.ico（陶土色圆角方块 + 白色四角星）。"""
import os
import sys

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import QColor, QGuiApplication, QImage, QPainter, QPainterPath

app = QGuiApplication(sys.argv)
size = 256
img = QImage(size, size, QImage.Format.Format_ARGB32)
img.fill(Qt.GlobalColor.transparent)
p = QPainter(img)
p.setRenderHint(QPainter.RenderHint.Antialiasing)
p.setPen(Qt.PenStyle.NoPen)
p.setBrush(QColor("#c96442"))
p.drawRoundedRect(QRectF(8, 8, 240, 240), 56, 56)

c, r, w = 128, 82, 20  # 四角星：中心、半径、腰宽
star = QPainterPath(QPointF(c, c - r))
for x, y in [(c + w, c - w), (c + r, c), (c + w, c + w), (c, c + r), (c - w, c + w), (c - r, c), (c - w, c - w)]:
    star.lineTo(x, y)
star.closeSubpath()
p.setBrush(QColor("white"))
p.drawPath(star)
p.end()

out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.ico")
print("saved" if img.save(out, "ICO") else "FAILED", out)
