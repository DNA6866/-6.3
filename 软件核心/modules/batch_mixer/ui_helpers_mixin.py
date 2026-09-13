from PyQt5.QtCore import Qt, QSize
from PyQt5.QtGui import QColor, QIcon, QPixmap
from PyQt5.QtWidgets import (
    QAbstractSpinBox,
    QColorDialog,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QSpinBox,
    QFrame,
    QVBoxLayout,
)

from utils import EXTENDED_COLORS


class UIHelpersMixin:
    # ───────── 预览共用：底图比例自适应 + 缩放 ─────────
    def _preview_design_size_with_bg(self, design_w, design_h, bg_path):
        """有参考底图时按底图比例调整设计尺寸。

        预览画布比例 = 底图比例 → 底图铺满画布无黑边，
        文字/水印位置相对底图所见即所得（底图=视频帧时与成片完全一致）。
        """
        import os
        try:
            if bg_path and os.path.exists(bg_path):
                pm = QPixmap(bg_path)
                if pm.width() > 0 and pm.height() > 0 and pm.width() != pm.height():
                    return design_w, max(1, int(round(design_w * pm.height() / pm.width())))
        except Exception:
            pass
        return design_w, design_h

    def _preview_zoom_factor(self):
        """当前预览缩放系数（1.0 = 100%）。"""
        try:
            return max(0.5, min(4.0, float(getattr(self, '_preview_zoom', 1.0) or 1.0)))
        except (TypeError, ValueError):
            return 1.0

    def _make_preview_zoom_spin(self, on_change):
        """创建「预览缩放」百分比控件（50%~400%，默认100%），放大查看细节。"""
        import os
        row = QHBoxLayout()
        label = QLabel("预览缩放:")
        zoom_spin = QSpinBox()
        zoom_spin.setRange(50, 400)
        zoom_spin.setValue(100)
        zoom_spin.setSuffix(" %")
        zoom_spin.setSingleStep(25)
        zoom_spin.setFixedWidth(86)
        zoom_spin.setToolTip(
            "放大预览画面查看细节（不影响成片，仅放大预览显示）。\n"
            "100% = 适配面板的默认大小。"
        )
        zoom_spin.wheelEvent = lambda event: event.ignore()

        def _apply(value):
            self._preview_zoom = max(0.5, min(4.0, value / 100.0))
            on_change()

        zoom_spin.valueChanged.connect(_apply)
        row.addWidget(label)
        row.addWidget(zoom_spin)
        row.addStretch(1)
        return row

    def _sync_preview_scroll_size(self, scroll, canvas, pad=6):
        """滚动容器尺寸跟随画布（缩放后可滚动查看放大区域）。"""
        if scroll is None or canvas is None:
            return
        try:
            scroll.setFixedSize(
                min(760, canvas.width() + pad),
                min(900, canvas.height() + pad),
            )
        except Exception:
            pass

    def populate_color_combo(self, combo):
        combo.clear(); combo.addItem("🎨 自定义取色盘...")
        for c in EXTENDED_COLORS:
            pixmap = QPixmap(16, 16); pixmap.fill(QColor(c)); combo.addItem(QIcon(pixmap), c)

    def handle_color_selection(self, index, combo, update_func):
        text = combo.itemText(index)
        if text == "🎨 自定义取色盘...":
            color = QColorDialog.getColor()
            if color.isValid():
                hex_color = color.name().upper(); pixmap = QPixmap(16, 16); pixmap.fill(color)
                combo.insertItem(1, QIcon(pixmap), hex_color); combo.setCurrentIndex(1)
            else: combo.setCurrentIndex(2) 
        update_func()

    def get_hex_from_combo(self, combo):
        text = combo.currentText()
        if text == "🎲 随机颜色": return text
        if text == "🎨 自定义取色盘...": return "#FFFFFF"
        return QColor(text).name().upper()

    def preview_font_family(self, font_name):
        name = str(font_name or "")
        if name in getattr(self, 'font_display_families', {}):
            return self.font_display_families[name]
        if "微软雅黑" in name or "雅黑" in name:
            return "Microsoft YaHei"
        if "黑体" in name:
            return "SimHei"
        if "宋体" in name:
            return "SimSun"
        if "楷体" in name:
            return "KaiTi"
        if "仿宋" in name:
            return "FangSong"
        if name.startswith("系统字体:"):
            return name.split(":", 1)[1].strip()
        return ""

    def _compact_input_width(self, chars=10):
        return 116

    def _style_compact_spinboxes(self, *spinboxes, chars=10):
        width = self._compact_input_width(chars)
        for spinbox in spinboxes:
            spinbox.setFixedWidth(width)
            spinbox.setMinimumHeight(34)
            spinbox.setAlignment(Qt.AlignRight)
            spinbox.setButtonSymbols(QAbstractSpinBox.NoButtons)
            spinbox.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
            line_edit = spinbox.lineEdit()
            if line_edit:
                line_edit.setAlignment(Qt.AlignRight)

    def _param_label(self, text, width=72):
        label = QLabel(text)
        label.setFixedWidth(width)
        label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        return label

    def _hide_combo_icons(self, *combos):
        for combo in combos:
            combo.setIconSize(QSize(16, 16))

    def _style_preview_action_buttons(self, *buttons):
        """按当前字体计算沙盘工具按钮尺寸，避免高 DPI 下文字被固定高度裁掉。"""
        for button in buttons:
            button.setObjectName("previewActionButton")
            button.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Fixed)
            self._resize_preview_action_button(button)
            tracked = getattr(self, '_preview_action_buttons', None)
            if tracked is None:
                tracked = []
                self._preview_action_buttons = tracked
            if button not in tracked:
                tracked.append(button)
                button.installEventFilter(self)

    @staticmethod
    def _resize_preview_action_button(button):
        metrics = button.fontMetrics()
        button.setMinimumWidth(max(72, metrics.horizontalAdvance(button.text()) + 30))
        button.setMinimumHeight(max(36, metrics.height() + 18))

    def _fit_preview_canvas_size(self, design_w, design_h, max_w=300, max_h=533, min_w=180, reserved_h=130, reserved_w=36, host_widget=None):
        """Return a preview size that keeps the full design aspect visible."""
        aspect = design_w / design_h
        try:
            host_h = host_widget.height() if host_widget is not None else 0
            host_w = host_widget.width() if host_widget is not None else 0
            if host_h > 260:
                available_h = min(max_h, max(180, host_h - reserved_h))
                available_w = max_w
                if host_w > 0:
                    available_w = min(max_w, max(1, host_w - reserved_w))
                target_w = min(available_w, int(available_h * aspect))
                if target_w < min_w and available_w >= min_w and int(min_w * design_h / design_w) <= available_h:
                    target_w = min_w
                target_h = int(target_w * design_h / design_w)
                if target_h > available_h:
                    target_h = available_h
                    target_w = int(target_h * aspect)
                return max(1, target_w), max(1, target_h)
        except Exception:
            pass

        candidate_h = 0
        try:
            stack_h = self.stacked_widget.height()
            if stack_h > 260:
                candidate_h = max(candidate_h, max(220, stack_h - reserved_h))
        except Exception:
            pass
        try:
            window_h = self.height()
            if window_h > 360:
                candidate_h = max(candidate_h, max(220, window_h - 360))
        except Exception:
            pass
        available_h = min(max_h, candidate_h) if candidate_h > 0 else max_h

        target_w = min(max_w, int(available_h * aspect))
        if target_w < min_w and int(min_w * design_h / design_w) <= available_h:
            target_w = min_w
        target_h = int(target_w * design_h / design_w)
        if target_h > available_h:
            target_h = available_h
            target_w = int(target_h * aspect)
        return max(1, target_w), max(1, target_h)

    def _batch_tab_stylesheet(self, root_name, extra_rules=""):
        return f"""
QWidget#{root_name} {{
    background: #0B1120;
}}
{extra_rules}
QFrame#tabSectionFrame {{
    background: #111827;
    border: 1px solid #4B5E78;
    border-radius: 10px;
}}
QLabel#tabSectionTitle {{
    background: transparent;
    border: none;
    color: #F8FAFC;
    font-weight: 700;
    padding: 0 0 6px 0;
}}
QFrame#tabSectionFrame QLabel {{
    background: transparent;
    border: none;
}}
QFrame#tabSectionFrame QLineEdit,
QFrame#tabSectionFrame QPlainTextEdit,
QFrame#tabSectionFrame QTextEdit,
QFrame#tabSectionFrame QComboBox,
QFrame#tabSectionFrame QSpinBox,
QFrame#tabSectionFrame QDoubleSpinBox {{
    background: #020617;
    border: 1px solid #64748B;
    border-radius: 8px;
    color: #F8FAFC;
    padding: 7px;
    selection-background-color: #2563EB;
}}
QFrame#tabSectionFrame QLineEdit:focus,
QFrame#tabSectionFrame QPlainTextEdit:focus,
QFrame#tabSectionFrame QTextEdit:focus,
QFrame#tabSectionFrame QComboBox:focus,
QFrame#tabSectionFrame QSpinBox:focus,
QFrame#tabSectionFrame QDoubleSpinBox:focus {{
    border: 1px solid #60A5FA;
}}
QFrame#tabSectionFrame QComboBox::drop-down {{
    border: none;
    width: 0;
}}
QFrame#tabSectionFrame QComboBox::down-arrow {{
    image: none;
    width: 0;
    height: 0;
    border: none;
    margin: 0;
}}
"""

    def _make_tab_section(self, title, margins=(14, 12, 14, 14), spacing=10):
        frame = QFrame()
        frame.setObjectName("tabSectionFrame")
        frame.setFrameShape(QFrame.StyledPanel)
        frame.setStyleSheet("""
QFrame#tabSectionFrame {
    background: #111827;
    border: 1px solid #4B5E78;
    border-radius: 10px;
}
QLabel#tabSectionTitle {
    background: transparent;
    border: none;
    color: #F8FAFC;
    font-weight: 700;
    padding: 0 0 6px 0;
}
QFrame#tabSectionFrame QLabel {
    background: transparent;
    border: none;
}
QFrame#tabSectionFrame QLineEdit,
QFrame#tabSectionFrame QPlainTextEdit,
QFrame#tabSectionFrame QTextEdit,
QFrame#tabSectionFrame QComboBox,
QFrame#tabSectionFrame QSpinBox,
QFrame#tabSectionFrame QDoubleSpinBox {
    background: #020617;
    border: 1px solid #64748B;
    border-radius: 8px;
    color: #F8FAFC;
    padding: 7px;
    selection-background-color: #2563EB;
}
QFrame#tabSectionFrame QLineEdit:focus,
QFrame#tabSectionFrame QPlainTextEdit:focus,
QFrame#tabSectionFrame QTextEdit:focus,
QFrame#tabSectionFrame QComboBox:focus,
QFrame#tabSectionFrame QSpinBox:focus,
QFrame#tabSectionFrame QDoubleSpinBox:focus {
    border: 1px solid #60A5FA;
}
QFrame#tabSectionFrame QComboBox::drop-down {
    border: none;
    width: 0;
}
QFrame#tabSectionFrame QComboBox::down-arrow {
    image: none;
    width: 0;
    height: 0;
    border: none;
    margin: 0;
}
""")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(*margins)
        layout.setSpacing(spacing)
        title_label = QLabel(title)
        title_label.setObjectName("tabSectionTitle")
        layout.addWidget(title_label)
        return frame, layout

