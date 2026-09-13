"""字幕样式共享面板：花字预设 + 样式表单 + 竖版实时预览。

供「音视频处理 → 视频加字幕」等页面使用，与批量混剪智能字幕的
花字预设库（services.subtitle_presets）保持同一套样式体系。

预览说明：
  - 画布按视频实际分辨率等比显示（默认 9:16 竖版），完整不裁切；
  - 拖入视频后可用 set_video_frame() 设置真实帧作为背景；
  - 字幕按真实 margin_v / 字号比例绘制在底部，所见即所得。
"""

import os

from PyQt5.QtCore import QPoint, QSize, Qt, pyqtSignal
from PyQt5.QtGui import QColor, QFont, QIcon, QPainter, QPixmap
from PyQt5.QtWidgets import (
    QComboBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from services.subtitle_presets import SUB_STYLES, random_style, render_style_swatch


class SubtitleStylePanel(QWidget):
    """字幕样式面板（花字预设 + 参数表单 + 竖版实时预览）。"""

    styleChanged = pyqtSignal()

    PREVIEW_W = 216
    PREVIEW_H = 384

    def __init__(self, parent=None, system_fonts=None, default_size=72, show_preview=True):
        super().__init__(parent)
        self._system_fonts = system_fonts or {}
        self._default_size = default_size
        self._frame_pixmap = None
        self._preset_buttons = []
        self._current_preset = {}
        # show_preview=False 时不建内置小预览（页面用右侧独立大预览替代）
        self.show_preview = bool(show_preview)
        self._style = {
            "font_name": "微软雅黑",
            "size": default_size,
            "color": "#FFFFFF",
            "border_w": 4,
            "border_color": "#000000",
            "margin_v": 120,
            "shadow": 0,
            "bold": -1,
            "play_res_w": 1080,
            "play_res_h": 1920,
        }
        self.preview_canvas = None
        self._build_ui()
        self._load_default_preset()
        self._update_preview()

    # ─────────────── UI 构建 ───────────────
    def _build_ui(self):
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(12)

        # 左：竖版预览（show_preview=False 时由页面提供独立大预览）
        if getattr(self, "show_preview", True):
            preview_col = QVBoxLayout()
            preview_title = QLabel("字幕预览（真实位置 · 不裁切）")
            preview_title.setObjectName("appSubtitle")
            preview_col.addWidget(preview_title)

            self.preview_canvas = QLabel()
            self.preview_canvas.setFixedSize(QSize(self.PREVIEW_W, self.PREVIEW_H))
            self.preview_canvas.setAlignment(Qt.AlignCenter)
            self.preview_canvas.setStyleSheet(
                "QLabel{background:#111827;border:1px solid #334155;border-radius:6px;}"
            )
            preview_col.addWidget(self.preview_canvas)
            self.preview_tip = QLabel("拖入视频后自动显示真实画面")
            self.preview_tip.setObjectName("mutedText")
            self.preview_tip.setWordWrap(True)
            preview_col.addWidget(self.preview_tip)
            preview_col.addStretch()
            root.addLayout(preview_col, 0)

        # 右：花字预设 + 样式表单
        right = QVBoxLayout()
        right.setSpacing(8)

        preset_row = QHBoxLayout()
        preset_label = QLabel("花字预设")
        preset_label.setObjectName("appSubtitle")
        self.random_btn = QPushButton("🎲 随机花字")
        self.random_btn.setFixedWidth(90)
        self.random_btn.setStyleSheet(
            "QPushButton{background:#1E3A5F;border:1px solid #3B82F6;border-radius:6px;"
            "color:#F8FAFC;font-weight:700;padding:4px 8px;}"
            "QPushButton:hover{background:#1E40AF;}"
        )
        self.random_btn.clicked.connect(self._on_random)
        preset_row.addWidget(preset_label)
        preset_row.addStretch()
        preset_row.addWidget(self.random_btn)
        right.addLayout(preset_row)

        self._preset_buttons = []
        self.preset_scroll = QScrollArea()
        self.preset_scroll.setWidgetResizable(True)
        self.preset_scroll.setFrameShape(QFrame.NoFrame)
        self.preset_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.preset_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.preset_scroll.setFixedHeight(150)
        self.preset_scroll.setStyleSheet("QScrollArea{background:transparent;border:none;}")
        container = QWidget()
        grid = QGridLayout(container)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(6)
        cols = 3
        for idx, preset in enumerate(SUB_STYLES):
            btn = QPushButton()
            btn.setCheckable(True)
            btn.setToolTip(preset["name"])
            btn.setFixedSize(104, 46)
            swatch = render_style_swatch(preset)
            if swatch:
                btn.setIcon(QIcon(swatch))
                btn.setIconSize(QSize(100, 42))
            btn.setStyleSheet(
                "QPushButton{background:#1E293B;border:1px solid #475569;border-radius:6px;padding:0;}"
                "QPushButton:hover{border:2px solid #60A5FA;}"
                "QPushButton:checked{border:3px solid #FACC15;background:#1E3A5F;}"
            )
            btn.clicked.connect(lambda checked=False, p=preset, b=btn: self.apply_preset(p, b))
            grid.addWidget(btn, idx // cols, idx % cols)
            self._preset_buttons.append(btn)
        container.setLayout(grid)
        self.preset_scroll.setWidget(container)
        right.addWidget(self.preset_scroll)

        form_group = QFrame()
        form_group.setObjectName("card")
        form = QFormLayout(form_group)
        form.setContentsMargins(10, 12, 10, 10)
        form.setSpacing(6)

        self.font_combo = QComboBox()
        self.font_combo.addItem("微软雅黑")
        self.font_combo.addItems(sorted(self._system_fonts.keys()))
        form.addRow("字体:", self.font_combo)

        self.size_spin = QSpinBox()
        self.size_spin.setRange(24, 160)
        self.size_spin.setValue(self._default_size)
        form.addRow("字号:", self.size_spin)

        self.color_combo = QComboBox()
        self.color_combo.addItems([
            "#FFFFFF", "#FFFF00", "#00FFFF", "#FFD700",
            "#FF4500", "#00FF00", "#FF69B4", "#FF00FF",
        ])
        form.addRow("主色:", self.color_combo)

        self.border_w_spin = QSpinBox()
        self.border_w_spin.setRange(0, 20)
        self.border_w_spin.setValue(4)
        form.addRow("描边粗细:", self.border_w_spin)

        self.border_color_combo = QComboBox()
        self.border_color_combo.addItems(["#000000", "#FFFFFF", "#FF0000", "#003366"])
        form.addRow("描边颜色:", self.border_color_combo)

        self.margin_spin = QSpinBox()
        self.margin_spin.setRange(0, 900)
        self.margin_spin.setValue(120)
        form.addRow("底部边距:", self.margin_spin)

        for widget in (self.font_combo, self.color_combo, self.border_color_combo):
            widget.currentTextChanged.connect(self._on_param_changed)
        for widget in (self.size_spin, self.border_w_spin, self.margin_spin):
            widget.valueChanged.connect(self._on_param_changed)
        right.addWidget(form_group)
        right.addStretch()
        root.addLayout(right, 1)

    # ─────────────── 预设应用 ───────────────
    def apply_preset(self, preset, clicked_btn=None):
        self._apply_style_values(
            font=preset.get("font"),
            size=int(preset.get("size", self._default_size)),
            color=str(preset.get("color", "#FFFFFF")),
            border_w=int(preset.get("border_w", 4)),
            border_color=str(preset.get("border_color", "#000000")),
            margin_v=int(preset.get("margin_v", 120)),
            shadow=int(preset.get("shadow", 0) or 0),
            bold=bool(preset.get("bold", True)),
        )
        self._current_preset = dict(preset)
        for btn in self._preset_buttons:
            btn.setChecked(btn is clicked_btn)
        self.styleChanged.emit()

    def _on_random(self):
        preset = random_style()
        if not preset:
            return
        # 随机仅换颜色搭配，保留字号/描边粗细/边距/粗体
        self._apply_style_values(
            color=str(preset.get("color", "#FFFFFF")),
            border_color=str(preset.get("border_color", "#000000")),
        )
        self._style["color"] = str(preset.get("color", "#FFFFFF"))
        self._style["border_color"] = str(preset.get("border_color", "#000000"))
        self._current_preset = dict(preset)
        for btn in self._preset_buttons:
            btn.setChecked(False)
        self._update_preview()
        self.styleChanged.emit()

    def apply_random_style(self):
        return self._on_random()

    def _load_default_preset(self):
        if SUB_STYLES and self._preset_buttons:
            self.apply_preset(SUB_STYLES[0], self._preset_buttons[0])

    # ─────────────── 参数读写 ───────────────
    def _apply_style_values(self, **values):
        self.font_combo.blockSignals(True)
        font = values.get("font")
        if font:
            idx = self.font_combo.findText(str(font))
            if idx >= 0:
                self.font_combo.setCurrentIndex(idx)
        self.font_combo.blockSignals(False)

        if "size" in values:
            self.size_spin.blockSignals(True)
            self.size_spin.setValue(int(values["size"]))
            self.size_spin.blockSignals(False)
        if "color" in values:
            self.color_combo.blockSignals(True)
            idx = self.color_combo.findText(str(values["color"]))
            if idx < 0:
                self.color_combo.insertItem(2, str(values["color"]))
                idx = 2
            self.color_combo.setCurrentIndex(idx)
            self.color_combo.blockSignals(False)
        if "border_w" in values:
            self.border_w_spin.blockSignals(True)
            self.border_w_spin.setValue(int(values["border_w"]))
            self.border_w_spin.blockSignals(False)
        if "border_color" in values:
            self.border_color_combo.blockSignals(True)
            idx = self.border_color_combo.findText(str(values["border_color"]))
            if idx < 0:
                self.border_color_combo.insertItem(2, str(values["border_color"]))
                idx = 2
            self.border_color_combo.setCurrentIndex(idx)
            self.border_color_combo.blockSignals(False)
        if "margin_v" in values:
            self.margin_spin.blockSignals(True)
            self.margin_spin.setValue(int(values["margin_v"]))
            self.margin_spin.blockSignals(False)

        self._sync_style_from_controls()
        if "shadow" in values:
            self._style["shadow"] = int(values["shadow"] or 0)
        if "bold" in values:
            self._style["bold"] = -1 if values["bold"] else 0
        self._update_preview()

    def _sync_style_from_controls(self):
        self._style.update({
            "font_name": self.font_combo.currentText(),
            "size": self.size_spin.value(),
            "color": self.color_combo.currentText(),
            "border_w": self.border_w_spin.value(),
            "border_color": self.border_color_combo.currentText(),
            "margin_v": self.margin_spin.value(),
        })

    def _on_param_changed(self, *args):
        self._sync_style_from_controls()
        self._update_preview()
        self.styleChanged.emit()

    def get_style(self):
        """返回完整 sub_style 字典（与 AddSubtitleWorker 兼容）。"""
        style = dict(self._style)
        preset = getattr(self, "_current_preset", None) or {}
        style["shadow"] = int(preset.get("shadow", style.get("shadow", 0)) or 0)
        style["bold"] = int(-1 if preset.get("bold", True) else 0)
        style["play_res_w"] = int(style.get("play_res_w", 1080))
        style["play_res_h"] = int(style.get("play_res_h", 1920))
        return style

    def set_play_res(self, width, height):
        """已知视频分辨率时更新（影响预览比例）。"""
        self._style["play_res_w"] = int(width)
        self._style["play_res_h"] = int(height)
        self._update_preview()

    # ─────────────── 预览 ───────────────
    def set_video_frame(self, pixmap):
        """设置视频真实帧作为预览背景。"""
        self._frame_pixmap = pixmap
        self._update_preview()

    def _update_preview(self):
        if self.preview_canvas is None:
            return
        canvas = QPixmap(self.PREVIEW_W, self.PREVIEW_H)
        render_subtitle_preview(canvas, self._style, self._frame_pixmap)
        self.preview_canvas.setPixmap(canvas)


def render_subtitle_preview(canvas, style, frame_pixmap=None):
    """在 canvas(QPixmap) 上渲染字幕预览（面板小预览与独立大预览共用）。

    换算链与成片 _srt_to_ass 同源：字号/边距按 draw_h/1920（基准高），
    描边/阴影按 PlayRes 坐标直映，画面按视频实际比例等比完整显示。
    """
    from PyQt5.QtGui import QFontMetrics

    w, h = canvas.width(), canvas.height()
    canvas.fill(QColor("#111827"))
    play_w = max(2, int(style.get("play_res_w", 1080)))
    play_h = max(2, int(style.get("play_res_h", 1920)))
    frame_scale = w / play_w
    draw_h = int(play_h * frame_scale)
    y_off = max(0, (h - draw_h) // 2)

    painter = QPainter(canvas)
    painter.setRenderHint(QPainter.Antialiasing, True)
    if frame_pixmap is not None and not frame_pixmap.isNull():
        bg = frame_pixmap.scaled(
            w, draw_h, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation,
        )
        painter.drawPixmap(0, y_off, w, draw_h, bg)
    else:
        painter.fillRect(0, y_off, w, draw_h, QColor("#1F2937"))
        painter.setPen(QColor("#4B5563"))
        painter.drawRect(0, y_off, w - 1, draw_h - 1)

    # 字号/底部边距按基准高换算（与成片一致）；描边/阴影按 PlayRes 坐标直映
    scale = draw_h / 1920.0
    font_size = max(4, int(style.get("size", 72) * scale))
    margin_v = int(style.get("margin_v", 120) * scale)
    border_w = max(1, int(style.get("border_w", 4) * frame_scale))
    color = QColor(style.get("color", "#FFFFFF"))
    border_color = QColor(style.get("border_color", "#000000"))
    shadow = max(0, int(style.get("shadow", 0) * frame_scale))

    font = QFont(style.get("font_name", "微软雅黑"), font_size)
    font.setBold(bool(style.get("bold", -1)))
    painter.setFont(font)
    fm = QFontMetrics(font)
    text = "这是一句字幕示例"
    max_text_w = w - 2 * max(4, int(60 * frame_scale))
    if fm.horizontalAdvance(text) > max_text_w:
        cut = len(text) // 2
        all_lines = [text[:cut], text[cut:]]
    else:
        all_lines = [text]

    base_y = y_off + draw_h - margin_v
    line_height = int(fm.height() * 1.15)
    for li, line in enumerate(all_lines):
        tw = fm.horizontalAdvance(line)
        x = (w - tw) // 2
        y = base_y - (len(all_lines) - 1 - li) * line_height - fm.descent()
        if shadow > 0:
            painter.setPen(QColor(0, 0, 0, 180))
            painter.drawText(QPoint(x + shadow, y + shadow), line)
        if border_w > 0:
            painter.setPen(border_color)
            for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1),
                           (-1, -1), (1, -1), (-1, 1), (1, 1)):
                painter.drawText(QPoint(x + dx * border_w, y + dy * border_w), line)
        painter.setPen(color)
        painter.drawText(QPoint(x, y), line)
    painter.end()


class SubtitlePreviewWidget(QWidget):
    """独立的大尺寸字幕预览（与 SubtitleStylePanel 共享同一渲染与样式快照）。

    支持「画面放大」：缩放 50%~400%，放大后滚动查看细节，实时重绘——
    与批量混剪各预览沙盘的缩放体验一致。
    """

    def __init__(self, width=380, parent=None):
        super().__init__(parent)
        self._base_w = max(240, int(width))   # 100% 基准宽
        self._zoom = 1.0
        self._style = {
            "font_name": "微软雅黑",
            "size": 72,
            "color": "#FFFFFF",
            "border_w": 4,
            "border_color": "#000000",
            "margin_v": 120,
            "shadow": 0,
            "bold": -1,
            "play_res_w": 1080,
            "play_res_h": 1920,
        }
        self._frame_pixmap = None

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)

        # 顶部：标题 + 预览缩放
        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        title = QLabel("字幕预览（真实位置 · 不裁切）")
        title.setObjectName("appSubtitle")
        top.addWidget(title)
        top.addStretch(1)
        top.addWidget(QLabel("画面放大:"))
        self._zoom_spin = QSpinBox()
        self._zoom_spin.setRange(50, 400)
        self._zoom_spin.setValue(100)
        self._zoom_spin.setSuffix(" %")
        self._zoom_spin.setSingleStep(25)
        self._zoom_spin.setFixedWidth(88)
        self._zoom_spin.setToolTip(
            "放大预览画面查看字幕细节（不影响成片，仅放大预览显示）。\n"
            "放大后超出区域可滚动查看。"
        )
        self._zoom_spin.wheelEvent = lambda event: event.ignore()
        self._zoom_spin.valueChanged.connect(self._on_zoom_changed)
        top.addWidget(self._zoom_spin)
        lay.addLayout(top)

        # 滚动区承载可放大画布
        self.preview_canvas = QLabel()
        self.preview_canvas.setAlignment(Qt.AlignCenter)
        self.preview_canvas.setStyleSheet(
            "QLabel{background:#111827;border:1px solid #334155;border-radius:6px;}"
        )
        self.preview_scroll = QScrollArea()
        self.preview_scroll.setWidgetResizable(False)
        self.preview_scroll.setFrameShape(QFrame.NoFrame)
        self.preview_scroll.setAlignment(Qt.AlignHCenter | Qt.AlignTop)
        self.preview_scroll.setStyleSheet(
            "QScrollArea{background:#0B1120;border:1px solid #1E293B;border-radius:6px;}"
        )
        self.preview_scroll.setWidget(self.preview_canvas)
        self.preview_scroll.setMinimumHeight(420)
        lay.addWidget(self.preview_scroll, 1)
        self._update_preview()

    def _on_zoom_changed(self, value):
        self._zoom = max(0.5, min(4.0, int(value) / 100.0))
        self._update_preview()

    def set_zoom(self, percent):
        """外部程序化设置缩放（百分比）。"""
        self._zoom_spin.setValue(int(percent))
        # valueChanged 会触发 _on_zoom_changed

    def zoom_percent(self):
        return self._zoom_spin.value()

    @property
    def PREVIEW_W(self):
        return int(self._base_w * self._zoom)

    @property
    def PREVIEW_H(self):
        return int(self.PREVIEW_W * 16 / 9)

    def set_style(self, style):
        """全量套用样式快照（来自 SubtitleStylePanel.get_style()）。"""
        if isinstance(style, dict):
            self._style.update(style)
        self._update_preview()

    def set_video_frame(self, pixmap):
        self._frame_pixmap = pixmap
        self._update_preview()

    def set_play_res(self, width, height):
        self._style["play_res_w"] = max(2, int(width))
        self._style["play_res_h"] = max(2, int(height))
        self._update_preview()

    def _update_preview(self):
        w, h = self.PREVIEW_W, self.PREVIEW_H
        self.preview_canvas.setFixedSize(QSize(w, h))
        canvas = QPixmap(w, h)
        render_subtitle_preview(canvas, self._style, self._frame_pixmap)
        self.preview_canvas.setPixmap(canvas)
