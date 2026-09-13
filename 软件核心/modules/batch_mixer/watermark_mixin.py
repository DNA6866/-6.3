import os
import re

from PyQt5.QtCore import QEvent, Qt, QTimer
from PyQt5.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen, QPixmap
from PyQt5.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFormLayout,
    QScrollArea,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ui_components import DragPreviewCanvas, PlainPasteTextEdit, WATERMARK_PREVIEW_FONT_SCALE
from utils import ANIMATIONS


class WatermarkMixin:
    def open_text_popup(self):
        text, ok = QInputDialog.getMultiLineText(self, "多行水印输入框", "请输入水印内容：", self.wm_text.toPlainText())
        if ok: self.wm_text.setPlainText(text); self.draw_preview_canvas()

    def set_watermark_mode(self, mode, row=-1):
        if mode == "edit":
            self.mode_label.setText(f"编辑模式：第 {row + 1} 条水印")
            self.mode_label.setStyleSheet("color: #FDBA74; background:#2A1F0B; border:1px solid #B45309; border-radius:8px; font-weight:bold; font-size:13px; padding:5px 7px;")
            if hasattr(self, 'btn_modify_sel'):
                self.btn_modify_sel.setEnabled(True)
            if hasattr(self, 'btn_add_new'):
                self.btn_add_new.setText("另存新水印")
            return
        self.mode_label.setText("新增模式：当前内容会作为新水印")
        self.mode_label.setStyleSheet("color: #BBF7D0; background:#052E16; border:1px solid #22C55E; border-radius:8px; font-weight:bold; font-size:13px; padding:5px 7px;")
        if hasattr(self, 'btn_modify_sel'):
            self.btn_modify_sel.setEnabled(False)
        if hasattr(self, 'btn_add_new'):
            self.btn_add_new.setText("添加为新水印")

    def selected_watermark_row(self):
        if not hasattr(self, 'wm_table') or not self.wm_table.selectionModel():
            return -1
        selected = self.wm_table.selectionModel().selectedRows()
        if not selected:
            return -1
        row = selected[0].row()
        return row if 0 <= row < len(self.watermarks_data) else -1

    def reset_watermark_form(self, clear_inputs=False):
        if hasattr(self, 'wm_table'):
            self.wm_table.blockSignals(True)
            self.wm_table.clearSelection()
            self.wm_table.blockSignals(False)
        self.editing_wm_index = -1
        self.set_watermark_mode("new")
        if clear_inputs:
            self._wm_new_draft = {}
            self.apply_watermark_form_data({})
        else:
            draft = getattr(self, '_wm_new_draft', {})
            if draft:
                self.apply_watermark_form_data(draft)
            else:
                self.apply_watermark_form_data({})
        self.draw_preview_canvas()

    def _get_active_text(self): return self.wm_text.toPlainText()

    def split_watermark_text_pool(self, text):
        text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
        return [block.strip() for block in re.split(r"\n\s*\n+", text) if block.strip()]

    def watermark_display_text(self, data):
        if data.get('use_text_pool'):
            pool = self.split_watermark_text_pool(data.get('text_pool', ''))
            if pool:
                return pool[0]
            return ''
        return data.get('text', '')

    def watermark_table_text(self, data):
        display_text = self.watermark_display_text(data).replace('\n', '  ')
        if data.get('use_text_pool'):
            count = len(self.split_watermark_text_pool(data.get('text_pool', '')))
            return f"随机卖点池({count}组): {display_text}" if count else "随机卖点池(未填写)"
        return display_text

    def watermark_line_step(self, wm_data):
        size = float(wm_data.get('size', 60) or 60)
        line_spacing = float(wm_data.get('line_spacing', 130) or 130)
        return size * max(40.0, line_spacing) / 100.0

    def apply_preview_letter_spacing(self, font, wm_data, scale_ratio):
        spacing = float(wm_data.get('letter_spacing', 0) or 0)
        if spacing <= 0:
            return
        try:
            font.setLetterSpacing(QFont.AbsoluteSpacing, spacing * scale_ratio)
        except Exception:
            pass

    def schedule_watermark_preview_update(self, *args):
        if getattr(self, '_wm_preview_pending', False):
            return
        self._wm_preview_pending = True
        QTimer.singleShot(30, self._run_watermark_preview_update)

    def _run_watermark_preview_update(self):
        self._wm_preview_pending = False
        self.save_watermark_new_draft()
        focus_widget = QApplication.focusWidget()
        self.draw_preview_canvas()
        if focus_widget in (getattr(self, 'wm_text', None), getattr(self, 'wm_text_pool', None), getattr(self, 'av_wm_text', None)):
            focus_widget.setFocus()

    def _spinbox_live_value(self, spinbox):
        text = ""
        try:
            text = spinbox.lineEdit().text()
        except Exception:
            pass
        text = re.sub(r"[^\d-]", "", text or "")
        try:
            value = int(text)
        except Exception:
            value = spinbox.value()
        return max(spinbox.minimum(), min(spinbox.maximum(), value))

    def _get_current_wm_data(self):
        return {
            'text': self._get_active_text(), 'font_name': self.wm_font.currentText(), 'font_path': self.sys_fonts.get(self.wm_font.currentText(), ""), 
            'use_text_pool': self.wm_use_text_pool.isChecked() if hasattr(self, 'wm_use_text_pool') else False,
            'text_pool': self.wm_text_pool.toPlainText() if hasattr(self, 'wm_text_pool') else '',
            'size': self._spinbox_live_value(self.wm_size), 'color': self.wm_color.currentText(), 'align': self.wm_align.currentText(),
            'line_spacing': self._spinbox_live_value(self.wm_line_spacing) if hasattr(self, 'wm_line_spacing') else 130,
            'letter_spacing': self._spinbox_live_value(self.wm_letter_spacing) if hasattr(self, 'wm_letter_spacing') else 0,
            'border_w': self._spinbox_live_value(self.wm_border_w), 'border_color': self.wm_border_color.currentText(), 'is_bold': self.wm_is_bold.isChecked(),
            'shadow': int(getattr(self, '_current_wm_style_preset', {}).get('shadow', 0) or 0),
            'x': self._spinbox_live_value(self.wm_x), 'y': self._spinbox_live_value(self.wm_y), 'anim': self.wm_anim.currentText()
        }

    def watermark_form_widgets(self):
        return (self.wm_text, self.wm_use_text_pool, self.wm_text_pool, self.wm_font, self.wm_size, self.wm_color, self.wm_align, self.wm_is_bold,
                self.wm_line_spacing, self.wm_letter_spacing, self.wm_border_w, self.wm_border_color, self.wm_x, self.wm_y, self.wm_anim)

    def apply_watermark_form_data(self, wm):
        if not hasattr(self, 'wm_text'):
            return
        for widget in self.watermark_form_widgets():
            widget.blockSignals(True)
        self.wm_text.setPlainText(wm.get('text', ''))
        self.wm_use_text_pool.setChecked(bool(wm.get('use_text_pool', False)))
        self.wm_text_pool.setPlainText(wm.get('text_pool', ''))
        self.wm_font.setCurrentText(wm.get('font_name', '🎲 随机系统字体'))
        self.wm_size.setValue(int(wm.get('size', 60)))
        self.wm_color.setCurrentText(wm.get('color', 'white'))
        self.wm_align.setCurrentText(wm.get('align', '居中对齐'))
        self.wm_line_spacing.setValue(int(wm.get('line_spacing', 130)))
        self.wm_letter_spacing.setValue(int(wm.get('letter_spacing', 0)))
        self.wm_is_bold.setChecked(bool(wm.get('is_bold', False)))
        self.wm_border_w.setValue(int(wm.get('border_w', 2)))
        self.wm_border_color.setCurrentText(wm.get('border_color', 'black'))
        self.wm_x.setValue(int(wm.get('x', 540)))
        self.wm_y.setValue(int(wm.get('y', 960)))
        self.wm_anim.setCurrentText(wm.get('anim', '静态展示'))
        for widget in self.watermark_form_widgets():
            widget.blockSignals(False)

    def save_watermark_new_draft(self):
        if not hasattr(self, 'wm_text'):
            return
        data = self._get_current_wm_data()
        if data.get('text', '').strip() or data.get('text_pool', '').strip() or data.get('use_text_pool'):
            self._wm_new_draft = data
        else:
            self._wm_new_draft = {}

    def add_watermark_new(self):
        data = self._get_current_wm_data()
        if not self.watermark_display_text(data).strip(): return
        self.watermarks_data.append(data); self.add_wm_to_table(data)
        self.reset_watermark_form(clear_inputs=True); self.save_configuration(silent=True)
        
    def modify_selected_watermark(self):
        curr_row = self.selected_watermark_row()
        if curr_row < 0: return
        data = self._get_current_wm_data(); self.watermarks_data[curr_row] = data; self.update_wm_table_row(curr_row, data)
        self.reset_watermark_form(); self.save_configuration(silent=True)

    def on_wm_table_selection_changed(self):
        prev_editing = getattr(self, 'editing_wm_index', -1)
        self.save_watermark_new_draft()
        curr_row = self.selected_watermark_row()
        if curr_row < 0:
            was_editing = prev_editing >= 0
            self.editing_wm_index = -1
            self.set_watermark_mode("new")
            # 从编辑状态失焦 → 清空所有输入框（固定文字/随机卖点等）
            if was_editing:
                self._wm_new_draft = {}
                self.apply_watermark_form_data({})
            self.draw_preview_canvas()
            return
        # 同一行重复触发的选择变更（如失焦重选），保持当前编辑状态不覆盖
        if curr_row == prev_editing:
            self.draw_preview_canvas()
            return
        self.editing_wm_index = curr_row
        self.set_watermark_mode("edit", curr_row)
        wm = self.watermarks_data[curr_row]
        self.apply_watermark_form_data(wm)
        self.draw_preview_canvas()

    def add_wm_to_table(self, data):
        row = self.wm_table.rowCount(); self.wm_table.insertRow(row); self.update_wm_table_row(row, data)
        
    def update_wm_table_row(self, row, data):
        self.wm_table.setItem(row, 0, QTableWidgetItem(self.watermark_table_text(data))); self.wm_table.setItem(row, 1, QTableWidgetItem(data['font_name']))
        self.wm_table.setItem(row, 2, QTableWidgetItem(f"大小:{data['size']} 行距:{data.get('line_spacing', 130)}% 字距:{data.get('letter_spacing', 0)}")); self.wm_table.setItem(row, 3, QTableWidgetItem(f"{data['x']}, {data['y']}"))
        self.wm_table.setItem(row, 4, QTableWidgetItem(data['anim'].split(' ')[0]))
        
    def delete_selected_watermark(self):
        curr_row = self.selected_watermark_row()
        if curr_row < 0: return
        del self.watermarks_data[curr_row]; self.wm_table.removeRow(curr_row); self.reset_watermark_form(); self.save_configuration(silent=True)

    def current_watermark_design_size(self):
        res_combo = getattr(self, 'res_combo', None)
        res_text = res_combo.currentText() if res_combo else self.RES_PORTRAIT
        if "竖屏" in res_text:
            return 1080, 1920
        return 1920, 1080

    def set_watermark_canvas_size(self, canvas, popup=False):
        design_w, design_h = self.current_watermark_design_size()
        # 底图比例自适应：画布比例 = 底图比例，位置所见即所得
        design_w, design_h = self._preview_design_size_with_bg(
            design_w, design_h, getattr(self, 'preview_bg_path', '')
        )
        if popup and design_h >= design_w:
            canvas_w = 520 if popup else 260
            canvas_h = int(canvas_w * design_h / design_w)
        elif popup:
            canvas_w = 960 if popup else 300
            canvas_h = int(canvas_w * design_h / design_w)
        else:
            canvas_w, canvas_h = self._fit_preview_canvas_size(
                design_w,
                design_h,
                max_w=430 if design_h >= design_w else 430,
                max_h=760 if design_h >= design_w else 242,
                min_w=190 if design_h >= design_w else 260,
                reserved_h=70,
                reserved_w=20,
                # host_widget 传 None：画布尺寸只随主窗口，不随滚动容器
                # （画布包进 QScrollArea 后 parent 是 scroll，其尺寸又绑定画布，
                #   会形成"画布→scroll→画布"收缩循环，每点一次预览缩小一次）。
                host_widget=None,
            )
            # 预览缩放：放大查看细节（不影响成片，仅预览显示）
            zoom = self._preview_zoom_factor()
            canvas_w, canvas_h = max(1, int(canvas_w * zoom)), max(1, int(canvas_h * zoom))
        canvas.setFixedSize(canvas_w, canvas_h)
        if hasattr(canvas, 'set_design_size'):
            canvas.set_design_size(design_w, design_h)
        return design_w, design_h

    def render_watermark_canvas(self, canvas):
        for child in list(canvas.children()):
            child.setParent(None)
            child.deleteLater()
        design_w, design_h = self.set_watermark_canvas_size(canvas, popup=getattr(canvas, 'is_popup_preview', False))
        if not getattr(canvas, 'is_popup_preview', False):
            self._sync_preview_scroll_size(getattr(self, 'preview_scroll', None), canvas)
        scale_ratio = canvas.width() / design_w
        if self.preview_bg_path and os.path.exists(self.preview_bg_path):
            bg_lbl = QLabel(canvas); bg_lbl.setGeometry(0, 0, canvas.width(), canvas.height())
            bg_lbl.setAlignment(Qt.AlignCenter)
            bg_lbl.setAttribute(Qt.WA_TransparentForMouseEvents)
            bg_lbl.setPixmap(QPixmap(self.preview_bg_path).scaled(canvas.width(), canvas.height(), Qt.KeepAspectRatio, Qt.SmoothTransformation)); bg_lbl.show()
        for i, wm in enumerate(self.watermarks_data):
            if i == self.editing_wm_index: continue
            self.draw_text_lines(wm, scale_ratio, is_cursor=False, target_canvas=canvas)
        current_data = self._get_current_wm_data()
        if self.watermark_display_text(current_data).strip():
            self.draw_text_lines(current_data, scale_ratio, is_cursor=True, target_canvas=canvas)

    def draw_preview_canvas(self):
        if not hasattr(self, 'preview_canvas'):
            return
        self.render_watermark_canvas(self.preview_canvas)

    def draw_text_lines(self, wm_data, scale_ratio, is_cursor, target_canvas=None):
        canvas = target_canvas or self.preview_canvas
        lines = str(self.watermark_display_text(wm_data)).split('\n')
        prepared_lines = []
        min_x = min_y = 10**9
        max_x = max_y = -10**9
        for i, line in enumerate(lines):
            if not line.strip():
                continue
            color = "#FF1493" if wm_data['color'] == "🎲 随机颜色" else wm_data['color']
            border_w = wm_data.get('border_w', 0); border_color = "#00FFFF" if wm_data.get('border_color', 'black') == "🎲 随机颜色" else wm_data.get('border_color', 'black')
            font = QFont(canvas.font())
            family = self.preview_font_family(wm_data.get('font_name', ''))
            if family:
                font.setFamily(family)
            preview_font_px = max(1, int(float(wm_data['size']) * scale_ratio * WATERMARK_PREVIEW_FONT_SCALE))
            font.setPixelSize(preview_font_px)
            font.setBold(wm_data.get('is_bold', False))
            self.apply_preview_letter_spacing(font, wm_data, scale_ratio)
            metrics = QFontMetrics(font)
            base_x = wm_data['x'] * scale_ratio; line_y = (wm_data['y'] + i * self.watermark_line_step(wm_data)) * scale_ratio
            label_w = metrics.horizontalAdvance(line)
            label_h = metrics.height()
            if wm_data['align'] == "居中对齐": final_x = base_x - label_w / 2
            elif wm_data['align'] == "右对齐": final_x = base_x - label_w
            else: final_x = base_x
            prepared_lines.append((line, font, final_x, line_y, label_w, label_h, metrics.ascent(), color, border_w, border_color))
            min_x = min(min_x, final_x)
            min_y = min(min_y, line_y)
            max_x = max(max_x, final_x + label_w)
            max_y = max(max_y, line_y + label_h)

        if not prepared_lines:
            return

        text_pixmap = QPixmap(canvas.size())
        text_pixmap.fill(Qt.transparent)
        painter = QPainter(text_pixmap)
        painter.setRenderHint(QPainter.TextAntialiasing)
        painter.setRenderHint(QPainter.Antialiasing)
        for line, font, final_x, line_y, _label_w, _label_h, ascent, color, border_w, border_color in prepared_lines:
            painter.setFont(font)
            baseline_y = int(line_y + ascent)
            if border_w > 0:
                bw = max(1, int(border_w * scale_ratio))
                painter.setPen(QPen(QColor(border_color)))
                for dx, dy in [(-bw, -bw), (bw, -bw), (-bw, bw), (bw, bw), (0, -bw), (0, bw), (-bw, 0), (bw, 0)]:
                    painter.drawText(int(final_x) + dx, baseline_y + dy, line)
            painter.setPen(QPen(QColor(color)))
            painter.drawText(int(final_x), baseline_y, line)
        painter.end()

        layer = QLabel(canvas)
        layer.setObjectName("watermarkTextLayer")
        layer.setGeometry(0, 0, canvas.width(), canvas.height())
        layer.setStyleSheet("background: transparent; border: none;")
        layer.setPixmap(text_pixmap)
        layer.setAttribute(Qt.WA_TransparentForMouseEvents)
        layer.show()

        if is_cursor:
            padding = max(5, int(10 * scale_ratio))
            x = max(0, min(max(0, canvas.width() - 8), int(min_x) - padding))
            y = max(0, min(max(0, canvas.height() - 8), int(min_y) - padding))
            w = min(canvas.width() - x, int(max_x - min_x) + padding * 2)
            h = min(canvas.height() - y, int(max_y - min_y) + padding * 2)
            frame = QFrame(canvas)
            frame.setObjectName("watermarkEditFrame")
            frame.setGeometry(x, y, max(8, w), max(8, h))
            frame.setStyleSheet("background: transparent; border: 1px dashed #FBBF24; border-radius: 4px;")
            frame.setAttribute(Qt.WA_TransparentForMouseEvents)
            frame.show()

    def open_watermark_big_preview(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("文字水印大图预览")
        dialog.setModal(False)
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)
        preview = DragPreviewCanvas()
        preview.is_popup_preview = True
        preview.setObjectName("referencePreview")
        preview.setAttribute(Qt.WA_StyledBackground, True)
        preview.setStyleSheet("background-color:#111; border:1px solid #64748B; border-radius:8px;")
        layout.addWidget(preview, alignment=Qt.AlignCenter)
        close_btn = QPushButton("关闭")
        close_btn.setMaximumWidth(120)
        close_btn.clicked.connect(dialog.close)
        layout.addWidget(close_btn, alignment=Qt.AlignRight)
        self.render_watermark_canvas(preview)
        dialog.resize(preview.width() + 40, min(preview.height() + 90, 1060))
        dialog.show()
        self._watermark_preview_dialog = dialog

    def eventFilter(self, obj, event):
        if (
            obj in getattr(self, '_preview_action_buttons', ())
            and event.type() in (QEvent.FontChange, QEvent.StyleChange, QEvent.ApplicationFontChange)
        ):
            QTimer.singleShot(0, lambda button=obj: self._resize_preview_action_button(button))
        if event.type() == QEvent.Resize:
            if obj == getattr(self, 'wm_preview_group', None) and hasattr(self, 'preview_canvas'):
                if not getattr(self, '_wm_preview_resize_pending', False):
                    self._wm_preview_resize_pending = True
                    QTimer.singleShot(0, self._refresh_watermark_preview_after_resize)
            elif obj == getattr(self, 'sub_preview_group', None) and hasattr(self, 'sub_preview_canvas'):
                if not getattr(self, '_sub_preview_resize_pending', False):
                    self._sub_preview_resize_pending = True
                    QTimer.singleShot(0, self._refresh_subtitle_preview_after_resize)
            elif obj == getattr(self, 'wm_tab', None):
                if not getattr(self, '_wm_responsive_pending', False):
                    self._wm_responsive_pending = True
                    QTimer.singleShot(0, self._apply_watermark_responsive_layout)
        if hasattr(self, 'wm_table') and obj == self.wm_table.viewport() and event.type() == QEvent.MouseButtonPress:
            if self.wm_table.itemAt(event.pos()) is None:
                if getattr(self, 'editing_wm_index', -1) >= 0:
                    # 编辑模式下点击空白 → 清空所有输入框
                    self._wm_new_draft = {}
                    self.reset_watermark_form(clear_inputs=True)
                else:
                    # 新增模式下点击空白 → 保留当前表单状态
                    self.save_watermark_new_draft()
                    self.reset_watermark_form()
        return super().eventFilter(obj, event)

    def _refresh_watermark_preview_after_resize(self):
        self._wm_preview_resize_pending = False
        if hasattr(self, 'preview_canvas'):
            self.draw_preview_canvas()

    def _refresh_subtitle_preview_after_resize(self):
        self._sub_preview_resize_pending = False
        if hasattr(self, 'sub_preview_canvas'):
            self.draw_subtitle_preview()

    def _apply_watermark_responsive_layout(self):
        self._wm_responsive_pending = False
        required = ('wm_tab', 'wm_left_scroll', 'wm_left_v', 'wm_top_layout', 'wm_pool_box', 'wm_preview_group')
        if any(not hasattr(self, name) for name in required):
            return
        tab_w = self.wm_tab.width()
        viewport_w = self.wm_left_scroll.viewport().width()
        narrow = tab_w < 900 or viewport_w < 660
        if narrow == getattr(self, '_wm_pool_stacked', False):
            return
        self._wm_pool_stacked = narrow
        if narrow:
            self.wm_top_layout.removeWidget(self.wm_pool_box)
            self.wm_left_v.insertWidget(1, self.wm_pool_box)
            self.wm_pool_box.setMinimumWidth(190)
            self.wm_pool_box.setMaximumWidth(16777215)
            self.wm_pool_box.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            self.wm_preview_group.setMinimumWidth(240)
        else:
            self.wm_left_v.removeWidget(self.wm_pool_box)
            self.wm_top_layout.insertWidget(0, self.wm_pool_box)
            self.wm_pool_box.setFixedWidth(190)
            self.wm_pool_box.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Expanding)
            self.wm_preview_group.setMinimumWidth(360)
        self.draw_preview_canvas()

    def build_watermark_tab(self):
        tab = QWidget()
        self.wm_tab = tab
        tab.installEventFilter(self)
        tab.setObjectName("watermarkTab")
        tab.setStyleSheet(self._batch_tab_stylesheet(
            "watermarkTab",
            "QWidget#watermarkLeftContent{background:#0B1120;}"
        ))
        layout = QHBoxLayout(tab)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(10)

        left_content = QWidget()
        left_content.setObjectName("watermarkLeftContent")
        left_v = QVBoxLayout(left_content)
        self.wm_left_v = left_v
        left_v.setContentsMargins(0, 0, 4, 0)
        left_v.setSpacing(8)
        top_layout = QHBoxLayout()
        self.wm_top_layout = top_layout
        top_layout.setSpacing(10)

        self.mode_label = QLabel("新增模式：当前内容会作为新水印")
        self.mode_label.setWordWrap(True)
        self.mode_label.setMinimumHeight(38)
        self.mode_label.setStyleSheet("color: #BBF7D0; background:#052E16; border:1px solid #22C55E; border-radius:8px; font-weight:bold; font-size:13px; padding:5px 7px;")

        pool_box, pool_layout = self._make_tab_section("9:16 卖点随机池", margins=(12, 10, 12, 12), spacing=6)
        self.wm_pool_box = pool_box
        pool_box.setFixedWidth(190)
        pool_box.setMinimumHeight(360)
        pool_box.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Expanding)
        self.wm_use_text_pool = QCheckBox("启用随机卖点")
        pool_hint = QLabel("空行分组，组内换行保留")
        pool_hint.setObjectName("appSubtitle")
        self.wm_text_pool = PlainPasteTextEdit()
        self.wm_text_pool.setPlaceholderText("3 秒速干\n不挑肤色\n\n直播间同款\n今天限时福利\n\n买一送一\n拍下立减")
        self.wm_text_pool.setMinimumHeight(230)
        self.wm_text_pool.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        pool_layout.addWidget(self.wm_use_text_pool)
        pool_layout.addWidget(pool_hint)
        pool_layout.addWidget(self.wm_text_pool, stretch=1)

        controls_box, controls_v = self._make_tab_section("文字水印参数", margins=(12, 10, 12, 12), spacing=7)
        self.wm_controls_box = controls_box
        controls_box.setMinimumWidth(420)
        controls_box.setMinimumHeight(360)
        controls_box.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.setHorizontalSpacing(10)
        form.setVerticalSpacing(7)
        form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        form.setRowWrapPolicy(QFormLayout.WrapLongRows)
        input_layout = QVBoxLayout()
        input_layout.setSpacing(6)
        self.wm_text = PlainPasteTextEdit()
        self.wm_text.setPlaceholderText("普通固定水印文字，支持多行")
        self.wm_text.setMinimumHeight(64)
        self.wm_text.setMaximumHeight(86)
        self.wm_text.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        text_btn_layout = QHBoxLayout()
        text_btn_layout.setSpacing(6)
        btn_popup = QPushButton("弹窗输入"); btn_popup.setMaximumWidth(108); btn_popup.clicked.connect(self.open_text_popup)
        btn_reset = QPushButton("清空/退出编辑"); btn_reset.setMaximumWidth(140); btn_reset.clicked.connect(lambda: self.reset_watermark_form(clear_inputs=True))
        text_btn_layout.addWidget(btn_popup); text_btn_layout.addWidget(btn_reset); text_btn_layout.addStretch()
        input_layout.addWidget(self.wm_text); input_layout.addLayout(text_btn_layout)
        self.wm_font = QComboBox(); self.wm_font.addItem("🎲 随机系统字体"); self.wm_font.addItems(list(self.sys_fonts.keys()))
        self.wm_size = QSpinBox(); self.wm_size.setRange(20, 300); self.wm_size.setValue(60)
        self.wm_color = QComboBox(); self.populate_color_combo(self.wm_color); self.wm_color.addItem("🎲 随机颜色"); self.wm_color.setCurrentText("white")
        self.wm_color.activated.connect(lambda idx: self.handle_color_selection(idx, self.wm_color, self.draw_preview_canvas))
        self.wm_align = QComboBox(); self.wm_align.addItems(["左对齐", "居中对齐", "右对齐"]); self.wm_align.setCurrentText("居中对齐")
        spacing_layout = QHBoxLayout()
        spacing_layout.setSpacing(8)
        self.wm_line_spacing = QSpinBox(); self.wm_line_spacing.setRange(60, 300); self.wm_line_spacing.setValue(130); self.wm_line_spacing.setSuffix("%")
        self.wm_letter_spacing = QSpinBox(); self.wm_letter_spacing.setRange(0, 40); self.wm_letter_spacing.setValue(0); self.wm_letter_spacing.setSuffix("px")
        spacing_layout.addWidget(self._param_label("换行间隔")); spacing_layout.addWidget(self.wm_line_spacing, 0, Qt.AlignLeft)
        spacing_layout.addWidget(self._param_label("字间距")); spacing_layout.addWidget(self.wm_letter_spacing, 0, Qt.AlignLeft)
        spacing_layout.addStretch()
        format_layout = QHBoxLayout(); format_layout.setSpacing(8); self.wm_is_bold = QCheckBox("强制加粗")
        self.wm_border_w = QSpinBox(); self.wm_border_w.setRange(0, 10); self.wm_border_w.setValue(2)
        self.wm_border_color = QComboBox(); self.populate_color_combo(self.wm_border_color); self.wm_border_color.addItem("🎲 随机颜色"); self.wm_border_color.setCurrentText("black")
        self.wm_border_color.activated.connect(lambda idx: self.handle_color_selection(idx, self.wm_border_color, self.draw_preview_canvas))
        self._hide_combo_icons(self.wm_color, self.wm_border_color)
        format_layout.addWidget(self.wm_is_bold)
        format_layout.addWidget(self._param_label("描边粗细")); format_layout.addWidget(self.wm_border_w, 0, Qt.AlignLeft)
        format_layout.addStretch()
        self.wm_x = QSpinBox(); self.wm_x.setRange(0, 3840); self.wm_x.setValue(540); self.wm_y = QSpinBox(); self.wm_y.setRange(0, 3840); self.wm_y.setValue(960)
        self.wm_anim = QComboBox(); self.wm_anim.addItems(list(ANIMATIONS.keys()))

        self._style_compact_spinboxes(
            self.wm_size,
            self.wm_line_spacing,
            self.wm_letter_spacing,
            self.wm_border_w,
            self.wm_x,
            self.wm_y,
            chars=10,
        )
        combo_controls = (
            self.wm_font,
            self.wm_color,
            self.wm_align,
            self.wm_border_color,
            self.wm_anim,
        )
        for control in combo_controls:
            control.setMinimumHeight(36)
            control.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        self.wm_font.setMinimumWidth(160)
        self.wm_font.setMaximumWidth(240)
        self.wm_color.setMinimumWidth(96)
        self.wm_color.setMaximumWidth(122)
        self.wm_align.setMinimumWidth(104)
        self.wm_align.setMaximumWidth(126)
        self.wm_border_color.setMinimumWidth(96)
        self.wm_border_color.setMaximumWidth(122)
        self.wm_anim.setMinimumWidth(160)
        self.wm_anim.setMaximumWidth(240)
        
        # 花字预设选择器（水印/卖点随机词共用）
        wm_preset_cell = QWidget()
        wm_preset_cell_layout = QVBoxLayout(wm_preset_cell)
        wm_preset_cell_layout.setContentsMargins(0, 0, 0, 0)
        wm_preset_cell_layout.setSpacing(4)
        wm_preset_top = QHBoxLayout()
        wm_preset_top.addStretch()
        self.wm_style_random_btn = QPushButton("🎲 随机")
        self.wm_style_random_btn.setFixedWidth(70)
        self.wm_style_random_btn.setStyleSheet(
            "QPushButton{background:#1E3A5F;border:1px solid #3B82F6;border-radius:6px;"
            "color:#F8FAFC;font-weight:700;padding:4px 6px;}"
            "QPushButton:hover{background:#1E40AF;}"
        )
        self.wm_style_random_btn.clicked.connect(self._apply_wm_random_style)
        wm_preset_top.addWidget(self.wm_style_random_btn)
        wm_preset_cell_layout.addLayout(wm_preset_top)
        wm_preset_cell_layout.addWidget(self._build_wm_style_presets())
        form.addRow("花字预设:", wm_preset_cell)
        form.addRow(self.mode_label)
        form.addRow("固定文字:", input_layout)
        font_layout = QHBoxLayout()
        font_layout.setSpacing(8)
        font_layout.addWidget(self.wm_font, stretch=0, alignment=Qt.AlignLeft)
        font_layout.addWidget(self._param_label("字号", 52))
        font_layout.addWidget(self.wm_size, 0, Qt.AlignLeft)
        font_layout.addStretch()
        form.addRow("字体与大小:", font_layout); form.addRow("间距设置:", spacing_layout); form.addRow("格式强化:", format_layout)
        form.addRow("描边颜色:", self.wm_border_color)
        color_layout = QHBoxLayout()
        color_layout.setSpacing(8)
        color_layout.addWidget(self.wm_color)
        color_layout.addStretch()
        align_layout = QHBoxLayout()
        align_layout.setSpacing(8)
        align_layout.addWidget(self.wm_align)
        align_layout.addStretch()
        form.addRow("字体颜色:", color_layout)
        form.addRow("对齐方式:", align_layout)
        coord_layout = QHBoxLayout()
        coord_layout.setSpacing(8)
        coord_layout.addWidget(self._param_label("X 坐标"))
        coord_layout.addWidget(self.wm_x, 0, Qt.AlignLeft)
        coord_layout.addWidget(self._param_label("Y 坐标"))
        coord_layout.addWidget(self.wm_y, 0, Qt.AlignLeft)
        coord_layout.addStretch()
        form.addRow("位置坐标:", coord_layout); form.addRow("百变动画:", self.wm_anim)
        controls_v.addLayout(form)
        
        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(8)
        self.btn_add_new = QPushButton("添加为新水印"); self.btn_add_new.setMaximumWidth(140); self.btn_add_new.clicked.connect(self.add_watermark_new)
        self.btn_modify_sel = QPushButton("覆盖选中"); self.btn_modify_sel.setMaximumWidth(130); self.btn_modify_sel.clicked.connect(self.modify_selected_watermark); self.btn_modify_sel.setEnabled(False)
        del_one_btn = QPushButton("删除"); del_one_btn.setMaximumWidth(80); del_one_btn.clicked.connect(self.delete_selected_watermark)
        btn_layout.addWidget(self.btn_add_new); btn_layout.addWidget(self.btn_modify_sel); btn_layout.addWidget(del_one_btn); btn_layout.addStretch()
        controls_v.addLayout(btn_layout)
        top_layout.addWidget(pool_box, stretch=0)
        top_layout.addWidget(controls_box, stretch=1)
        left_v.addLayout(top_layout)
        
        self.wm_table = QTableWidget(0, 5); self.wm_table.setHorizontalHeaderLabels(['文本', '字体', '格式', '坐标', '特效'])
        self.wm_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch); self.wm_table.setSelectionBehavior(QTableWidget.SelectRows) 
        self.wm_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.wm_table.verticalHeader().setVisible(False)
        self.wm_table.setMinimumHeight(180)
        self.wm_table.itemSelectionChanged.connect(self.on_wm_table_selection_changed)
        self.wm_table.viewport().installEventFilter(self)
        left_v.addWidget(self.wm_table, stretch=1)
        
        preset_group, p_layout = self._make_tab_section("商业矩阵水印预设库", margins=(12, 10, 12, 12), spacing=8)
        r1 = QHBoxLayout(); r1.setSpacing(8);
        r1.addWidget(QLabel("保存名称:"))
        self.preset_name_input = QLineEdit("我的矩阵预设1")
        self.preset_name_input.setMaxLength(50)
        self.preset_name_input.setMinimumWidth(100)
        self.preset_name_input.setPlaceholderText("输入预设名称，最多50字")
        r1.addWidget(self.preset_name_input, stretch=1)
        btn_save_p = QPushButton("保存为预设"); btn_save_p.setMaximumWidth(120); btn_save_p.clicked.connect(self.save_current_as_preset)
        r1.addWidget(btn_save_p)
        r2 = QHBoxLayout(); r2.setSpacing(8);
        r2.addWidget(QLabel("选择预设:"))
        self.preset_combo = QComboBox()
        self.preset_combo.setSizeAdjustPolicy(QComboBox.AdjustToContents)
        self.preset_combo.setMinimumContentsLength(12)
        self.preset_combo.setMaxVisibleItems(12)
        self.preset_combo.setMinimumWidth(100)
        r2.addWidget(self.preset_combo, stretch=1)
        btn_load_p = QPushButton("加载"); btn_load_p.setMaximumWidth(80); btn_load_p.clicked.connect(self.apply_selected_preset)
        btn_del_p = QPushButton("删除"); btn_del_p.setMaximumWidth(80); btn_del_p.clicked.connect(self.delete_selected_preset)
        r2.addWidget(btn_load_p); r2.addWidget(btn_del_p)
        p_layout.addLayout(r1); p_layout.addLayout(r2); left_v.insertWidget(1, preset_group)
        left_scroll = QScrollArea()
        left_scroll.setWidgetResizable(True)
        left_scroll.setFrameShape(QFrame.NoFrame)
        left_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        left_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        left_scroll.setStyleSheet("QScrollArea{background:#0B1120;border:none;}")
        left_scroll.viewport().setStyleSheet("background:#0B1120;border:none;")
        left_scroll.setWidget(left_content)
        self.wm_left_scroll = left_scroll
        layout.addWidget(left_scroll, stretch=3)
        
        preview_group, right_v = self._make_tab_section("水印沙盘", margins=(8, 6, 8, 8), spacing=4)
        self.wm_preview_group = preview_group
        preview_group.installEventFilter(self)
        preview_group.setMinimumWidth(360)
        preview_group.setMaximumWidth(470)
        preview_group.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding)
        bg_btn_layout = QHBoxLayout(); bg_btn_layout.setContentsMargins(0, 0, 0, 0); bg_btn_layout.setSpacing(4); load_bg_btn = QPushButton("底图"); load_bg_btn.clicked.connect(lambda: self.load_preview_bg('watermark'))
        video_frame_btn = QPushButton("视频取帧"); video_frame_btn.clicked.connect(lambda: self.auto_load_watermark_reference_frame(silent=False, force=True))
        big_preview_btn = QPushButton("大图"); big_preview_btn.clicked.connect(self.open_watermark_big_preview)
        self._style_preview_action_buttons(load_bg_btn, video_frame_btn, big_preview_btn)
        bg_btn_layout.addStretch(); bg_btn_layout.addWidget(load_bg_btn); bg_btn_layout.addWidget(video_frame_btn); bg_btn_layout.addWidget(big_preview_btn); bg_btn_layout.addStretch(); right_v.addLayout(bg_btn_layout)
        right_v.addLayout(self._make_preview_zoom_spin(self.draw_preview_canvas))
        self.preview_canvas = DragPreviewCanvas(); self.preview_canvas.setObjectName("referencePreview"); self.preview_canvas.setAttribute(Qt.WA_StyledBackground, True); self.preview_canvas.setStyleSheet("background-color: #020617; border: 2px solid #38BDF8; border-radius: 8px;"); self.set_watermark_canvas_size(self.preview_canvas);         self.preview_canvas.pointChanged.connect(self.set_watermark_position_from_preview)
        self.preview_scroll = QScrollArea()
        self.preview_scroll.setWidgetResizable(False)
        self.preview_scroll.setFrameShape(QFrame.NoFrame)
        self.preview_scroll.setAlignment(Qt.AlignHCenter | Qt.AlignTop)
        self.preview_scroll.setStyleSheet("QScrollArea{background:transparent;border:none;}")
        self.preview_scroll.setWidget(self.preview_canvas)
        self._sync_preview_scroll_size(self.preview_scroll, self.preview_canvas)
        right_v.addWidget(self.preview_scroll, alignment=Qt.AlignHCenter | Qt.AlignTop); right_v.addStretch()
        layout.addWidget(preview_group, stretch=0)
        
        self.wm_size.setKeyboardTracking(True)
        self.wm_line_spacing.setKeyboardTracking(True)
        self.wm_letter_spacing.setKeyboardTracking(True)
        self.wm_border_w.setKeyboardTracking(True)
        self.wm_x.setKeyboardTracking(True)
        self.wm_y.setKeyboardTracking(True)
        for widget in (self.wm_text, self.wm_use_text_pool, self.wm_text_pool, self.wm_font, self.wm_size, self.wm_color, self.wm_align, self.wm_is_bold,
                       self.wm_line_spacing, self.wm_letter_spacing,
                       self.wm_border_w, self.wm_border_color, self.wm_x, self.wm_y, self.wm_anim):
            signal = (getattr(widget, 'textChanged', None)
                      or getattr(widget, 'valueChanged', None)
                      or getattr(widget, 'currentTextChanged', None)
                      or getattr(widget, 'stateChanged', None))
            if signal:
                signal.connect(self.schedule_watermark_preview_update)
        for spinbox in (self.wm_size, self.wm_line_spacing, self.wm_letter_spacing, self.wm_border_w, self.wm_x, self.wm_y):
            spinbox.lineEdit().textChanged.connect(self.schedule_watermark_preview_update)
        return tab

    def _build_wm_style_presets(self):
        """花字预设选择器（水印/卖点随机词共用），大预览格直接显示渲染效果。"""
        from PyQt5.QtCore import QSize
        from PyQt5.QtGui import QIcon
        from services.subtitle_presets import SUB_STYLES, render_style_swatch

        container = QWidget()
        grid = QGridLayout(container)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(6)
        self.wm_style_buttons = []
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
            btn.clicked.connect(lambda checked=False, p=preset, b=btn: self._apply_wm_style_preset(p, b))
            grid.addWidget(btn, idx // cols, idx % cols)
            self.wm_style_buttons.append(btn)
        container.setLayout(grid)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        scroll.setFixedHeight(150)
        scroll.setStyleSheet("QScrollArea{background:transparent;border:none;}")
        scroll.setWidget(container)
        return scroll

    def _apply_wm_style_preset(self, preset, clicked_btn):
        """点选花字预设：应用到水印表单，并联动预览。"""
        for btn in self.wm_style_buttons:
            btn.setChecked(btn is clicked_btn)
        self.wm_font.blockSignals(True)
        if preset.get("font"):
            idx = self.wm_font.findText(preset["font"])
            if idx >= 0:
                self.wm_font.setCurrentIndex(idx)
        self.wm_font.blockSignals(False)
        self.wm_size.blockSignals(True); self.wm_size.setValue(int(preset.get("size", 60))); self.wm_size.blockSignals(False)
        self.wm_color.blockSignals(True)
        cidx = self.wm_color.findText(preset.get("color", "white"))
        if cidx >= 0:
            self.wm_color.setCurrentIndex(cidx)
        self.wm_color.blockSignals(False)
        self.wm_border_color.blockSignals(True)
        bidx = self.wm_border_color.findText(preset.get("border_color", "black"))
        if bidx >= 0:
            self.wm_border_color.setCurrentIndex(bidx)
        self.wm_border_color.blockSignals(False)
        self.wm_border_w.blockSignals(True); self.wm_border_w.setValue(int(preset.get("border_w", 2))); self.wm_border_w.blockSignals(False)
        # 粗体
        self.wm_is_bold.blockSignals(True)
        self.wm_is_bold.setChecked(bool(preset.get("bold", True)))
        self.wm_is_bold.blockSignals(False)
        # 记录预设（shadow 用于引擎 ASS 生成）
        self._current_wm_style_preset = preset
        self.draw_preview_canvas()

    def _apply_wm_random_style(self):
        """随机花字：只换颜色搭配（主色+描边色），保留字号/描边粗细/边距/粗体。"""
        from services.subtitle_presets import random_style
        preset = random_style()
        if not preset:
            return
        from services.subtitle_presets import color_to_combo_item
        self.wm_color.blockSignals(True)
        cidx = color_to_combo_item(preset.get("color", "#FFFFFF"), self.wm_color)
        if cidx is not None:
            self.wm_color.setCurrentIndex(cidx)
        self.wm_color.blockSignals(False)
        self.wm_border_color.blockSignals(True)
        bidx = color_to_combo_item(preset.get("border_color", "#000000"), self.wm_border_color)
        if bidx is not None:
            self.wm_border_color.setCurrentIndex(bidx)
        self.wm_border_color.blockSignals(False)
        current = dict(getattr(self, "_current_wm_style_preset", {}) or {})
        current["color"] = str(preset.get("color", "#FFFFFF"))
        current["border_color"] = str(preset.get("border_color", "#000000"))
        self._current_wm_style_preset = current
        for btn in self.wm_style_buttons:
            btn.setChecked(False)
        self.draw_preview_canvas()
        self.update_log(f"[花字] 随机颜色搭配：{preset['name']}")

