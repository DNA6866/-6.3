import os

from PyQt5.QtCore import Qt, QSize
from PyQt5.QtGui import QColor, QIcon, QPixmap
from PyQt5.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QScrollArea,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QWidget,
)

from services.asr_service import layout_subtitle_text, split_text_for_subtitle
from services.subtitle_presets import SUB_STYLES
from utils import SUBTITLE_ANIMATIONS


class SubtitleMixin:
    def build_subtitle_tab(self):
        tab = QWidget()
        tab.setObjectName("subtitleTab")
        tab.setStyleSheet(self._batch_tab_stylesheet("subtitleTab"))
        layout = QHBoxLayout(tab)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(12)

        control_group, left_v = self._make_tab_section("智能字幕参数")

        info_label = QLabel("本地智能字幕：按自然语句显示，宽度不足时自动排成双行")
        info_label.setObjectName("appSubtitle")
        info_label.setWordWrap(True)
        left_v.addWidget(info_label)

        # 花字预设选择器（大预览格，直接显示渲染效果）
        preset_row = QHBoxLayout()
        preset_label = QLabel("花字预设")
        preset_label.setObjectName("appSubtitle")
        self.sub_style_random_btn = QPushButton("🎲 随机")
        self.sub_style_random_btn.setFixedWidth(70)
        self.sub_style_random_btn.setStyleSheet(
            "QPushButton{background:#1E3A5F;border:1px solid #3B82F6;border-radius:6px;"
            "color:#F8FAFC;font-weight:700;padding:4px 6px;}"
            "QPushButton:hover{background:#1E40AF;}"
        )
        self.sub_style_random_btn.clicked.connect(self._apply_random_sub_style)
        preset_row.addWidget(preset_label)
        preset_row.addStretch()
        preset_row.addWidget(self.sub_style_random_btn)
        left_v.addLayout(preset_row)
        self.sub_style_buttons = []
        self.sub_style_scroll = QScrollArea()
        self.sub_style_scroll.setWidgetResizable(True)
        self.sub_style_scroll.setFrameShape(QFrame.NoFrame)
        self.sub_style_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.sub_style_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.sub_style_scroll.setFixedHeight(150)
        self.sub_style_scroll.setStyleSheet("QScrollArea{background:transparent;border:none;}")
        style_container = QWidget()
        style_grid = QGridLayout(style_container)
        style_grid.setContentsMargins(0, 0, 0, 0)
        style_grid.setSpacing(6)
        cols = 3
        for idx, preset in enumerate(SUB_STYLES):
            btn = QPushButton()
            btn.setCheckable(True)
            btn.setToolTip(preset["name"])
            btn.setFixedSize(104, 46)
            # 直接渲染"花字"两个字的效果图作为按钮图标
            from services.subtitle_presets import render_style_swatch
            btn.setIcon(QIcon(render_style_swatch(preset)))
            btn.setIconSize(QSize(100, 42))
            btn.setStyleSheet(
                "QPushButton{background:#1E293B;border:1px solid #475569;border-radius:6px;padding:0;}"
                "QPushButton:hover{border:2px solid #60A5FA;}"
                "QPushButton:checked{border:3px solid #FACC15;background:#1E3A5F;}"
            )
            btn.clicked.connect(lambda checked=False, p=preset, b=btn: self._apply_sub_style_preset(p, b))
            style_grid.addWidget(btn, idx // cols, idx % cols)
            self.sub_style_buttons.append(btn)
        style_container.setLayout(style_grid)
        self.sub_style_scroll.setWidget(style_container)
        left_v.addWidget(self.sub_style_scroll)

        self.sub_enable_chk = QCheckBox("启用本地 AI 模型极速听写")
        self.sub_enable_chk.setChecked(True)
        left_v.addWidget(self.sub_enable_chk)
        self.sub_word_timestamps_chk = QCheckBox("精准词级时间轴（口型更准，处理稍慢）")
        self.sub_word_timestamps_chk.setChecked(False)
        self.sub_word_timestamps_chk.setToolTip(
            "调用软件自带 Whisper 生成真实停顿时间轴；失败时自动回退普通字幕，不影响成片。"
        )
        left_v.addWidget(self.sub_word_timestamps_chk)

        self.sub_font = QComboBox(); self.sub_font.addItem("🎲 随机系统字体"); self.sub_font.addItems(list(self.sys_fonts.keys()))
        self.sub_size = QSpinBox(); self.sub_size.setRange(30, 200); self.sub_size.setValue(85)
        self.sub_color = QComboBox(); self.populate_color_combo(self.sub_color); self.sub_color.addItem("🎲 随机颜色"); self.sub_color.setCurrentText("🎲 随机颜色")
        self.sub_color.activated.connect(lambda idx: self.handle_color_selection(idx, self.sub_color, self.draw_subtitle_preview))
        self.sub_border_color = QComboBox(); self.populate_color_combo(self.sub_border_color); self.sub_border_color.addItem("🎲 随机颜色"); self.sub_border_color.setCurrentText("black")
        self.sub_border_color.activated.connect(lambda idx: self.handle_color_selection(idx, self.sub_border_color, self.draw_subtitle_preview))
        self._hide_combo_icons(self.sub_color, self.sub_border_color)
        self.sub_border_w = QSpinBox(); self.sub_border_w.setRange(0, 15); self.sub_border_w.setValue(5)
        self.sub_margin_v = QSpinBox(); self.sub_margin_v.setRange(10, 1500); self.sub_margin_v.setValue(180)
        self.sub_anim = QComboBox()
        self.sub_anim.addItems(list(SUBTITLE_ANIMATIONS.keys()))
        self.sub_anim.setCurrentText("🎲 随机动效")
        self.sub_test_input = QLineEdit("老板你好，这是一条用来测试智能双排防溢出功能的测试字幕哦！")

        self._style_compact_spinboxes(self.sub_size, self.sub_border_w, self.sub_margin_v, chars=10)
        for control in (self.sub_font, self.sub_color, self.sub_border_color, self.sub_anim, self.sub_test_input):
            control.setMinimumHeight(34)
            control.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(10)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(3, 1)
        row = 0
        grid.addWidget(self._param_label("字体"), row, 0)
        grid.addWidget(self.sub_font, row, 1)
        grid.addWidget(self._param_label("字号"), row, 2)
        grid.addWidget(self.sub_size, row, 3, alignment=Qt.AlignLeft)
        row += 1
        grid.addWidget(self._param_label("主色"), row, 0)
        grid.addWidget(self.sub_color, row, 1)
        grid.addWidget(self._param_label("描边色"), row, 2)
        grid.addWidget(self.sub_border_color, row, 3)
        grid.addWidget(self._param_label("粗细", 52), row, 4)
        grid.addWidget(self.sub_border_w, row, 5, alignment=Qt.AlignLeft)
        row += 1
        grid.addWidget(self._param_label("底边距"), row, 0)
        grid.addWidget(self.sub_margin_v, row, 1, alignment=Qt.AlignLeft)
        layout_hint = QLabel("整句显示 / 超宽自动双行")
        layout_hint.setObjectName("appSubtitle")
        grid.addWidget(self._param_label("排版"), row, 2)
        grid.addWidget(layout_hint, row, 3, 1, 3)
        row += 1
        grid.addWidget(self._param_label("动效"), row, 0)
        grid.addWidget(self.sub_anim, row, 1, 1, 5)
        row += 1
        grid.addWidget(self._param_label("测试文本"), row, 0)
        grid.addWidget(self.sub_test_input, row, 1, 1, 5)
        left_v.addLayout(grid)
        left_v.addStretch()

        left_scroll = QScrollArea()
        left_scroll.setObjectName("subtitleLeftScroll")
        left_scroll.setWidgetResizable(True)
        left_scroll.setFrameShape(QFrame.NoFrame)
        left_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        left_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        left_scroll.setStyleSheet("QScrollArea#subtitleLeftScroll{background:#0B1120;border:none;}")
        left_scroll.viewport().setStyleSheet("background:#0B1120;border:none;")
        left_scroll.setWidget(control_group)
        layout.addWidget(left_scroll, stretch=2)

        preview_group, right_v = self._make_tab_section("字幕沙盘", margins=(8, 6, 8, 8), spacing=4)
        self.sub_preview_group = preview_group
        preview_group.installEventFilter(self)
        preview_group.setMinimumWidth(360)
        preview_group.setMaximumWidth(470)
        preview_group.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding)
        bg_btn_layout = QHBoxLayout()
        bg_btn_layout.setContentsMargins(0, 0, 0, 0)
        bg_btn_layout.setSpacing(4)
        load_bg_btn = QPushButton("底图")
        load_bg_btn.clicked.connect(lambda: self.load_preview_bg('subtitle'))
        video_frame_btn = QPushButton("视频取帧")
        video_frame_btn.clicked.connect(lambda: self.auto_load_subtitle_reference_frame(silent=False, force=True))
        self._style_preview_action_buttons(load_bg_btn, video_frame_btn)
        bg_btn_layout.addStretch()
        bg_btn_layout.addWidget(load_bg_btn)
        bg_btn_layout.addWidget(video_frame_btn)
        bg_btn_layout.addStretch()
        zoom_row = self._make_preview_zoom_spin(self.draw_subtitle_preview)
        right_v.addLayout(zoom_row)
        right_v.addLayout(bg_btn_layout)
        self.sub_preview_canvas = QWidget()
        self.sub_preview_canvas.setObjectName("referencePreview")
        self.sub_preview_canvas.setAttribute(Qt.WA_StyledBackground, True)
        self.sub_preview_canvas.setStyleSheet("background-color:#111; border:2px solid #38BDF8; border-radius:8px;")
        self.sub_preview_canvas.setFixedSize(*self._subtitle_preview_size())
        self.sub_preview_scroll = QScrollArea()
        self.sub_preview_scroll.setWidgetResizable(False)
        self.sub_preview_scroll.setFrameShape(QFrame.NoFrame)
        self.sub_preview_scroll.setAlignment(Qt.AlignHCenter | Qt.AlignTop)
        self.sub_preview_scroll.setStyleSheet("QScrollArea{background:transparent;border:none;}")
        self.sub_preview_scroll.setWidget(self.sub_preview_canvas)
        self._sync_preview_scroll_size(self.sub_preview_scroll, self.sub_preview_canvas)
        right_v.addWidget(self.sub_preview_scroll, alignment=Qt.AlignHCenter | Qt.AlignTop)
        right_v.addStretch()
        layout.addWidget(preview_group, stretch=1)
        
        self.sub_size.valueChanged.connect(self.draw_subtitle_preview); self.sub_margin_v.valueChanged.connect(self.draw_subtitle_preview)
        self.sub_test_input.textChanged.connect(self.draw_subtitle_preview)
        self.sub_border_w.valueChanged.connect(self.draw_subtitle_preview)
        self.sub_font.currentTextChanged.connect(self.draw_subtitle_preview)
        self.sub_color.currentTextChanged.connect(self.draw_subtitle_preview)
        self.sub_border_color.currentTextChanged.connect(self.draw_subtitle_preview)
        self.sub_anim.currentTextChanged.connect(self.draw_subtitle_preview)
        return tab

    def _apply_sub_style_preset(self, preset, clicked_btn):
        """点选花字预设：更新参数控件并高亮当前按钮。"""
        # 高亮当前按钮
        for btn in self.sub_style_buttons:
            btn.setChecked(btn is clicked_btn)
        # 应用参数（blockSignals 避免多次刷新）
        self.sub_font.blockSignals(True)
        if preset.get("font"):
            idx = self.sub_font.findText(preset["font"])
            if idx >= 0:
                self.sub_font.setCurrentIndex(idx)
        self.sub_font.blockSignals(False)
        self.sub_size.blockSignals(True); self.sub_size.setValue(int(preset.get("size", 85))); self.sub_size.blockSignals(False)
        self.sub_color.blockSignals(True)
        color_idx = self.sub_color.findText(preset.get("color", "#FFFFFF"))
        if color_idx >= 0:
            self.sub_color.setCurrentIndex(color_idx)
        self.sub_color.blockSignals(False)
        self.sub_border_color.blockSignals(True)
        bcolor_idx = self.sub_border_color.findText(preset.get("border_color", "#000000"))
        if bcolor_idx >= 0:
            self.sub_border_color.setCurrentIndex(bcolor_idx)
        self.sub_border_color.blockSignals(False)
        self.sub_border_w.blockSignals(True); self.sub_border_w.setValue(int(preset.get("border_w", 5))); self.sub_border_w.blockSignals(False)
        self.sub_margin_v.blockSignals(True); self.sub_margin_v.setValue(int(preset.get("margin_v", 180))); self.sub_margin_v.blockSignals(False)
        # shadow/bold 不直接映射到批量混剪现有控件，记录到属性供 config 保存
        self._current_sub_style_preset = preset
        self.draw_subtitle_preview()

    def _apply_random_sub_style(self):
        """随机花字：只换颜色搭配（主色+描边色），保留字号/描边粗细/边距/粗体。"""
        from services.subtitle_presets import random_style
        preset = random_style()
        if not preset:
            return
        # 只更新颜色搭配（用映射函数兼容 hex/色名）
        from services.subtitle_presets import color_to_combo_item
        self.sub_color.blockSignals(True)
        cidx = color_to_combo_item(preset.get("color", "#FFFFFF"), self.sub_color)
        if cidx is not None:
            self.sub_color.setCurrentIndex(cidx)
        self.sub_color.blockSignals(False)
        self.sub_border_color.blockSignals(True)
        bidx = color_to_combo_item(preset.get("border_color", "#000000"), self.sub_border_color)
        if bidx is not None:
            self.sub_border_color.setCurrentIndex(bidx)
        self.sub_border_color.blockSignals(False)
        # 记录当前预设（仅颜色字段生效，字号/描边等保留用户设置）
        current = dict(getattr(self, "_current_sub_style_preset", {}) or {})
        current["color"] = str(preset.get("color", "#FFFFFF"))
        current["border_color"] = str(preset.get("border_color", "#000000"))
        self._current_sub_style_preset = current
        # 不高亮任何预设格（当前是混合参数）
        for btn in self.sub_style_buttons:
            btn.setChecked(False)
        self.draw_subtitle_preview()
        self.update_log(f"[花字] 随机颜色搭配：{preset['name']}")

    def _subtitle_preview_size(self):
        res_combo = getattr(self, 'res_combo', None)
        res_text = res_combo.currentText() if res_combo else self.RES_PORTRAIT
        if "竖屏" in res_text:
            design_w, design_h = 1080, 1920
            max_w, max_h, min_w = 430, 760, 190
        else:
            design_w, design_h = 1920, 1080
            max_w, max_h, min_w = 430, 242, 260
        # 底图比例自适应：画布比例 = 底图比例，位置所见即所得
        design_w, design_h = self._preview_design_size_with_bg(
            design_w, design_h, getattr(self, 'sub_preview_bg_path', '')
        )
        base_w, base_h = self._fit_preview_canvas_size(
            design_w, design_h, max_w=max_w, max_h=max_h, min_w=min_w,
            reserved_h=70, reserved_w=20,
            # 不随滚动容器自适应（画布包进 QScrollArea 后其尺寸绑定画布，
            # 会形成收缩循环导致每次重绘画布变小）
            host_widget=None,
        )
        # 预览缩放：放大查看细节（不影响成片，仅预览显示）
        zoom = self._preview_zoom_factor()
        return max(1, int(base_w * zoom)), max(1, int(base_h * zoom))

    def draw_subtitle_preview(self):
        for child in self.sub_preview_canvas.children(): child.deleteLater()
        res_combo = getattr(self, 'res_combo', None)
        res_text = res_combo.currentText() if res_combo else self.RES_PORTRAIT
        if "竖屏" in res_text:
            self.sub_preview_canvas.setFixedSize(*self._subtitle_preview_size())
            design_w = 1080
        else:
            self.sub_preview_canvas.setFixedSize(*self._subtitle_preview_size())
            design_w = 1920
        self._sync_preview_scroll_size(getattr(self, 'sub_preview_scroll', None), self.sub_preview_canvas)
        scale_ratio = self.sub_preview_canvas.width() / design_w
        if self.sub_preview_bg_path and os.path.exists(self.sub_preview_bg_path):
            bg_lbl = QLabel(self.sub_preview_canvas); bg_lbl.setGeometry(0, 0, self.sub_preview_canvas.width(), self.sub_preview_canvas.height())
            bg_lbl.setAlignment(Qt.AlignCenter)
            bg_lbl.setAttribute(Qt.WA_TransparentForMouseEvents)
            bg_lbl.setPixmap(QPixmap(self.sub_preview_bg_path).scaled(self.sub_preview_canvas.width(), self.sub_preview_canvas.height(), Qt.KeepAspectRatio, Qt.SmoothTransformation)); bg_lbl.show()
            
        text = self.sub_test_input.text().strip()
        if not text: return
        
        preview_parts = split_text_for_subtitle(text)
        preview_text = preview_parts[0] if preview_parts else text
        preview_text, fitted_size = layout_subtitle_text(
            preview_text,
            font_size=self.sub_size.value(),
            play_res_x=design_w,
            margin_side=80,
        )
        lines = preview_text.split(r"\N")

        color = "#00FFFF" if self.sub_color.currentText() == "🎲 随机颜色" else self.sub_color.currentText()
        border_color = "black" if self.sub_border_color.currentText() == "🎲 随机颜色" else self.sub_border_color.currentText()
        bw = max(1, int(self.sub_border_w.value() * scale_ratio))
        font_size = max(1, int(fitted_size * scale_ratio))
        canvas_h = self.sub_preview_canvas.height(); canvas_w = self.sub_preview_canvas.width()
        margin_v_scaled = int(self.sub_margin_v.value() * scale_ratio)
        # 花字预设：粗体/阴影跟随当前预设
        preset_bold = bool(getattr(self, '_current_sub_style_preset', {}).get('bold', True))
        preset_shadow = int(getattr(self, '_current_sub_style_preset', {}).get('shadow', 0) or 0)
        shadow_scaled = max(1, int(preset_shadow * scale_ratio)) if preset_shadow else 0
        
        for i, line in enumerate(lines):
            temp_lbl = QLabel(line); font = temp_lbl.font()
            family = self.preview_font_family(self.sub_font.currentText())
            if family:
                font.setFamily(family)
            font.setPixelSize(font_size); font.setBold(preset_bold); temp_lbl.setFont(font); temp_lbl.adjustSize()
            line_y = canvas_h - margin_v_scaled - (len(lines) - i) * font_size * 1.3
            final_x = (canvas_w - temp_lbl.width()) / 2
            temp_lbl.deleteLater()
            # 阴影：先绘制深色偏移层
            if shadow_scaled > 0:
                sh_lbl = QLabel(line, self.sub_preview_canvas)
                sh_lbl.setStyleSheet("color: #000000; background: transparent;")
                sh_lbl.setFont(font); sh_lbl.adjustSize()
                sh_lbl.move(int(final_x) + shadow_scaled, int(line_y) + shadow_scaled); sh_lbl.show()
                sh_lbl.setAttribute(Qt.WA_TransparentForMouseEvents)
            if self.sub_border_w.value() > 0:
                for dx, dy in [(-bw,-bw), (bw,-bw), (-bw,bw), (bw,bw), (0,-bw), (0,bw), (-bw,0), (bw,0)]:
                    bg_lbl = QLabel(line, self.sub_preview_canvas); bg_lbl.setStyleSheet(f"color: {border_color}; background: transparent;"); bg_lbl.setFont(font); bg_lbl.adjustSize(); bg_lbl.move(int(final_x) + dx, int(line_y) + dy); bg_lbl.show()
                    bg_lbl.setAttribute(Qt.WA_TransparentForMouseEvents)
            lbl = QLabel(line, self.sub_preview_canvas)
            lbl.setStyleSheet(f"color: {color}; background: transparent;"); lbl.setFont(font); lbl.adjustSize(); lbl.move(int(final_x), int(line_y)); lbl.show()
            lbl.setAttribute(Qt.WA_TransparentForMouseEvents)

