import random

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from utils import COVER_STATIC_STYLES, TRANS_MAP
from services.lut_service import LUT_OPTIONS


class WorkbenchMixin:
    @staticmethod
    def _randomized_spin_value(spin, unit):
        current = float(spin.value())
        amount = random.randint(1, 5) * float(unit)
        directions = [-1, 1]
        random.shuffle(directions)
        for direction in directions:
            candidate = round(current + direction * amount, spin.decimals())
            if spin.minimum() <= candidate <= spin.maximum() and candidate != current:
                return candidate
        fallback = current + unit if current + unit <= spin.maximum() else current - unit
        return round(max(spin.minimum(), min(spin.maximum(), fallback)), spin.decimals())

    def _randomized_spin_pair(self, minimum_spin, maximum_spin, unit):
        previous_pair = (minimum_spin.value(), maximum_spin.value())
        minimum_value = self._randomized_spin_value(minimum_spin, unit)
        maximum_value = self._randomized_spin_value(maximum_spin, unit)
        if minimum_value > maximum_value:
            minimum_value, maximum_value = maximum_value, minimum_value
        if maximum_value - minimum_value < unit:
            if minimum_value + unit <= maximum_spin.maximum():
                maximum_value = minimum_value + unit
            else:
                minimum_value = maximum_value - unit
        if (minimum_value, maximum_value) == previous_pair:
            if (
                minimum_value + unit <= minimum_spin.maximum()
                and maximum_value + unit <= maximum_spin.maximum()
            ):
                minimum_value += unit
                maximum_value += unit
            elif (
                minimum_value - unit >= minimum_spin.minimum()
                and maximum_value - unit >= maximum_spin.minimum()
            ):
                minimum_value -= unit
                maximum_value -= unit
        minimum_spin.setValue(minimum_value)
        maximum_spin.setValue(maximum_value)

    def randomize_core_parameters(self):
        """在当前生产参数上做小幅随机变化，同时保持所有区间合法。"""
        rhythm_index = self.clip_rhythm_combo.findData("custom")
        if rhythm_index >= 0:
            self.clip_rhythm_combo.setCurrentIndex(rhythm_index)

        self._randomized_spin_pair(self.min_clip_spin, self.max_clip_spin, 0.1)
        self._randomized_spin_pair(
            self.overlay_gap_min_spin,
            self.overlay_gap_max_spin,
            0.1,
        )
        self._randomized_spin_pair(
            self.overlay_zoom_min_spin,
            self.overlay_zoom_max_spin,
            0.01,
        )

        for spin in (
            self.clip_jitter_spin,
            self.strong_product_duration_spin,
            self.strong_avatar_duration_spin,
            self.hook_duration_spin,
        ):
            spin.setValue(self._randomized_spin_value(spin, 0.1))

        previous_transition = self.trans_duration.value()
        transition_value = self._randomized_spin_value(self.trans_duration, 0.1)
        transition_ceiling = max(
            self.trans_duration.minimum(),
            self.min_clip_spin.value() - 0.1,
        )
        transition_value = min(transition_value, transition_ceiling)
        if transition_value == previous_transition:
            if transition_value + 0.1 <= transition_ceiling:
                transition_value += 0.1
            elif transition_value - 0.1 >= self.trans_duration.minimum():
                transition_value -= 0.1
        self.trans_duration.setValue(transition_value)

        self.save_configuration(silent=True)
        self.update_log(
            "[核心参数] 一键随机微调完成："
            f"转场 {self.trans_duration.value():.1f}s，"
            f"覆盖间隔 {self.overlay_gap_min_spin.value():.1f}-"
            f"{self.overlay_gap_max_spin.value():.1f}s，"
            f"随机浮动 {self.clip_jitter_spin.value():.1f}s，"
            f"产品切片 {self.min_clip_spin.value():.1f}-"
            f"{self.max_clip_spin.value():.1f}s，"
            f"产品穿插 {self.strong_product_duration_spin.value():.1f}s，"
            f"数字人 {self.strong_avatar_duration_spin.value():.1f}s，"
            f"开头覆盖 {self.hook_duration_spin.value():.1f}s，"
            f"缓慢拉近 {self.overlay_zoom_min_spin.value():.2f}-"
            f"{self.overlay_zoom_max_spin.value():.2f}倍。"
        )

    def build_task_settings_widget(self):
        container = QWidget()
        form_layout = QFormLayout(container)
        form_layout.setContentsMargins(0, 0, 0, 0)
        form_layout.setSpacing(12)
        form_layout.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        form_layout.setRowWrapPolicy(QFormLayout.WrapLongRows)
        form_layout.setLabelAlignment(Qt.AlignLeft | Qt.AlignVCenter)

        self.mixer_summary_label = QLabel("传统音频混剪模式：音频素材提供声音和总时长，视频素材按随机裁剪时长拼接。")
        self.mixer_summary_label.setObjectName("appSubtitle")
        self.mixer_summary_label.setWordWrap(True)
        form_layout.addRow(self.mixer_summary_label)

        rule_group = QGroupBox("1. 轨道与覆盖裁剪"); r_layout = QFormLayout()
        r_layout.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        r_layout.setRowWrapPolicy(QFormLayout.WrapLongRows)
        r_layout.setLabelAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.mixer_mode_combo = QComboBox()
        self.mixer_mode_combo.addItem("传统音频混剪（音频素材 + 视频素材）", "traditional")
        # 说明：原「普通视频拼接(ordinary)」与「传统音频混剪(traditional)」引擎行为完全一致，已合并为传统模式（旧存档自动兼容）。
        self.mixer_mode_combo.addItem("数字人口播第一轨道（第一轨道原声 + 视频覆盖）", "first_track")
        self.mixer_mode_combo.addItem("AI完整开场（AI视频 + 第一轨道 + 原有混剪）", "ai_intro_first_track")
        self.mixer_mode_combo.addItem("AI开场+音频拼接（AI视频 + 钩子 + 产品随机）", "ai_intro_audio")
        self.mixer_mode_combo.currentIndexChanged.connect(self.apply_mixer_mode_ui)
        self.mixer_mode_combo.wheelEvent = lambda e: e.ignore()
        r_layout.addRow("混剪模式:", self.mixer_mode_combo)

        self.strong_structure_chk = QCheckBox("启用强结构模式")
        self.strong_structure_chk.setChecked(False)
        self.strong_structure_chk.setToolTip(
            "第一轨道始终作为原声底轨；数字人露出次数会随机分散到整条主音频，"
            "其余时间由产品素材覆盖。AI完整开场模式下，钩子素材会加在AI视频播放完之后的第一个镜头。"
        )
        r_layout.addRow("强结构:", self.strong_structure_chk)

        hook_layout = QHBoxLayout()
        self.hook_duration_spin = QDoubleSpinBox()
        self.hook_duration_spin.setRange(0.5, 8.0)
        self.hook_duration_spin.setDecimals(1)
        self.hook_duration_spin.setSingleStep(0.1)
        self.hook_duration_spin.setValue(2.0)
        hook_layout.addWidget(QLabel("开头覆盖"))
        hook_layout.addWidget(self.hook_duration_spin)
        hook_layout.addWidget(QLabel("秒"))
        hook_layout.addStretch(1)
        r_layout.addRow("钩子素材:", hook_layout)

        self.strong_avatar_duration_spin = QDoubleSpinBox()
        self.strong_avatar_duration_spin.setRange(0.8, 8.0)
        self.strong_avatar_duration_spin.setDecimals(1)
        self.strong_avatar_duration_spin.setSingleStep(0.1)
        self.strong_avatar_duration_spin.setValue(2.5)
        self.strong_avatar_duration_spin.setSuffix(" 秒/次")
        self.strong_avatar_duration_spin.setToolTip("每一次数字人画面至少露出的时间。开启智能补尾后，会尽量等到停顿或多给一点尾巴再切。")
        self.strong_avatar_smart_tail_chk = QCheckBox("智能补尾")
        self.strong_avatar_smart_tail_chk.setChecked(True)
        self.strong_avatar_smart_tail_chk.setToolTip("到达设定秒数后，优先找后面短停顿切画面；找不到停顿时最多多露出约半秒，减少一句话差几个字被切断。")
        self.strong_product_duration_spin = QDoubleSpinBox()
        self.strong_product_duration_spin.setRange(0.8, 10.0)
        self.strong_product_duration_spin.setDecimals(1)
        self.strong_product_duration_spin.setSingleStep(0.1)
        self.strong_product_duration_spin.setValue(3.0)
        self.strong_product_duration_spin.setSuffix(" 秒/段")
        self.strong_product_duration_spin.setToolTip(
            "强结构随机分布时，两次数字人露出之间至少保留的产品画面时间。"
        )
        self.strong_avatar_count_spin = QSpinBox()
        self.strong_avatar_count_spin.setRange(1, 5)
        self.strong_avatar_count_spin.setValue(3)
        self.strong_avatar_count_spin.setSuffix(" 次")
        self.strong_avatar_count_spin.setToolTip("整条主音频中数字人底轨随机露出的次数。")
        avatar_layout = QHBoxLayout()
        avatar_layout.addWidget(self.strong_avatar_duration_spin)
        avatar_layout.addWidget(self.strong_avatar_smart_tail_chk)
        avatar_layout.addStretch(1)
        r_layout.addRow("数字人露出:", avatar_layout)
        r_layout.addRow("产品穿插:", self.strong_product_duration_spin)
        r_layout.addRow("露出次数:", self.strong_avatar_count_spin)

        self.clip_rhythm_combo = QComboBox()
        self.clip_rhythm_combo.addItem("标准节奏（自然混剪）", "balanced")
        self.clip_rhythm_combo.addItem("快节奏（密集种草）", "fast")
        self.clip_rhythm_combo.addItem("慢节奏（细节展示）", "slow")
        self.clip_rhythm_combo.addItem("随机节奏（长短混合）", "mixed")
        self.clip_rhythm_combo.addItem("自定义", "custom")
        self.clip_rhythm_combo.currentIndexChanged.connect(self.apply_clip_rhythm_preset)
        r_layout.addRow("覆盖节奏:", self.clip_rhythm_combo)
        clip_layout = QHBoxLayout(); self.min_clip_spin = QDoubleSpinBox(); self.min_clip_spin.setRange(0.5, 10.0); self.min_clip_spin.setValue(2.0); self.max_clip_spin = QDoubleSpinBox(); self.max_clip_spin.setRange(0.5, 20.0); self.max_clip_spin.setValue(4.5)
        self.min_clip_spin.setToolTip("常规混剪的产品片段时长；强结构后半段产品接管时也用这个范围。")
        self.max_clip_spin.setToolTip("常规混剪的产品片段时长；强结构后半段产品接管时也用这个范围。")
        clip_layout.addWidget(QLabel("从")); clip_layout.addWidget(self.min_clip_spin); clip_layout.addWidget(QLabel("到")); clip_layout.addWidget(self.max_clip_spin); clip_layout.addWidget(QLabel("秒"))
        r_layout.addRow("产品切片:", clip_layout)
        jitter_layout = QHBoxLayout()
        self.clip_jitter_spin = QDoubleSpinBox()
        self.clip_jitter_spin.setRange(0.0, 3.0)
        self.clip_jitter_spin.setSingleStep(0.1)
        self.clip_jitter_spin.setValue(0.8)
        jitter_layout.addWidget(QLabel("每段额外")); jitter_layout.addWidget(self.clip_jitter_spin); jitter_layout.addWidget(QLabel("秒随机浮动"))
        r_layout.addRow("随机浮动:", jitter_layout)
        gap_layout = QHBoxLayout()
        self.overlay_gap_min_spin = QDoubleSpinBox()
        self.overlay_gap_min_spin.setRange(0.0, 20.0)
        self.overlay_gap_min_spin.setValue(1.5)
        self.overlay_gap_max_spin = QDoubleSpinBox()
        self.overlay_gap_max_spin.setRange(0.0, 30.0)
        self.overlay_gap_max_spin.setValue(3.5)
        gap_layout.addWidget(QLabel("从")); gap_layout.addWidget(self.overlay_gap_min_spin)
        gap_layout.addWidget(QLabel("到")); gap_layout.addWidget(self.overlay_gap_max_spin)
        gap_layout.addWidget(QLabel("秒"))
        r_layout.addRow("覆盖间隔:", gap_layout)
        offset_layout = QHBoxLayout()
        self.overlay_offset_spin = QSpinBox()
        self.overlay_offset_spin.setRange(0, 100)
        self.overlay_offset_spin.setSingleStep(5)
        self.overlay_offset_spin.setSuffix("%")
        self.overlay_offset_spin.setValue(60)
        self.overlay_offset_spin.setToolTip("放大后的画面随机移动强度。0=不移动，100=在不露黑边范围内尽量移动。")
        offset_layout.addWidget(QLabel("强度"))
        offset_layout.addWidget(self.overlay_offset_spin)
        offset_layout.addWidget(QLabel("不露黑边"))
        r_layout.addRow("素材偏移:", offset_layout)
        zoom_layout = QHBoxLayout()
        self.overlay_zoom_mode_combo = QComboBox()
        self.overlay_zoom_mode_combo.addItem("缓慢拉近", "slow_push_in")
        self.overlay_zoom_mode_combo.addItem("固定随机放大", "static")
        self.overlay_zoom_mode_combo.setToolTip("缓慢拉近会在每段产品素材内逐步推近；固定随机放大保持旧版一次性放大效果。")
        self.overlay_zoom_min_spin = QDoubleSpinBox()
        self.overlay_zoom_min_spin.setRange(1.00, 2.50)
        self.overlay_zoom_min_spin.setDecimals(2)
        self.overlay_zoom_min_spin.setSingleStep(0.01)
        self.overlay_zoom_min_spin.setValue(1.01)
        self.overlay_zoom_max_spin = QDoubleSpinBox()
        self.overlay_zoom_max_spin.setRange(1.00, 2.50)
        self.overlay_zoom_max_spin.setDecimals(2)
        self.overlay_zoom_max_spin.setSingleStep(0.01)
        self.overlay_zoom_max_spin.setValue(1.10)
        zoom_layout.addWidget(self.overlay_zoom_mode_combo)
        zoom_layout.addWidget(QLabel("从")); zoom_layout.addWidget(self.overlay_zoom_min_spin)
        zoom_layout.addWidget(QLabel("到")); zoom_layout.addWidget(self.overlay_zoom_max_spin)
        zoom_layout.addWidget(QLabel("倍"))
        r_layout.addRow("缩放动画:", zoom_layout)
        audio_layout = QHBoxLayout(); self.main_vol_spin = QDoubleSpinBox(); self.main_vol_spin.setValue(2.0); self.bgm_vol_spin = QDoubleSpinBox(); self.bgm_vol_spin.setValue(0.2)
        self.main_vol_text_label = QLabel("主音:")
        self.bgm_vol_text_label = QLabel("BGM:")
        audio_layout.addWidget(self.main_vol_text_label); audio_layout.addWidget(self.main_vol_spin); audio_layout.addWidget(self.bgm_vol_text_label); audio_layout.addWidget(self.bgm_vol_spin)
        r_layout.addRow("混音倍率:", audio_layout)
        smart_sfx_layout = QHBoxLayout()
        self.smart_sfx_chk = QCheckBox("启用")
        self.smart_sfx_chk.setChecked(False)
        self.smart_sfx_chk.setToolTip(
            "按片头、钩子、产品切换、转场和收尾匹配真实短音效；"
            "会自动避开连续重复并为人声留出空间。"
        )
        self.smart_sfx_strength_combo = QComboBox()
        self.smart_sfx_strength_combo.addItem("轻量（少而克制）", "light")
        self.smart_sfx_strength_combo.addItem("标准（推荐）", "standard")
        self.smart_sfx_strength_combo.addItem("强化（更密集）", "strong")
        self.smart_sfx_strength_combo.setCurrentIndex(1)
        self.smart_sfx_strength_combo.setEnabled(False)
        self.smart_sfx_strength_combo.setToolTip(
            "优先使用本机剪映已经下载的音效；强度影响密度和响度。"
            "也会读取“用户工作区\\素材\\智能音效”内的自定义音效。"
        )
        self.smart_sfx_strength_combo.wheelEvent = lambda event: event.ignore()
        self.smart_sfx_chk.toggled.connect(
            self.smart_sfx_strength_combo.setEnabled
        )
        smart_sfx_layout.addWidget(self.smart_sfx_chk)
        smart_sfx_layout.addWidget(self.smart_sfx_strength_combo, 1)
        r_layout.addRow("智能音效:", smart_sfx_layout)
        self.mixer_rule_group = rule_group
        self.mixer_rule_layout = r_layout
        self.mixer_parameter_fields = {
            'strong_structure': self.strong_structure_chk,
            'hook': hook_layout,
            'avatar': avatar_layout,
            'product': self.strong_product_duration_spin,
            'avatar_count': self.strong_avatar_count_spin,
            'rhythm': self.clip_rhythm_combo,
            'clip_duration': clip_layout,
            'jitter': jitter_layout,
            'overlay_gap': gap_layout,
            'offset': offset_layout,
            'zoom': zoom_layout,
            'audio': audio_layout,
            'smart_sfx': smart_sfx_layout,
        }
        rule_group.setLayout(r_layout); form_layout.addRow(rule_group)

        vis_group = QGroupBox("2. 视觉特效防重"); v_layout = QFormLayout()
        v_layout.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        v_layout.setRowWrapPolicy(QFormLayout.WrapLongRows)
        v_layout.setLabelAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        trans_layout = QHBoxLayout(); self.trans_combo = QComboBox(); self.trans_combo.addItems(list(TRANS_MAP.keys())); self.trans_combo.setCurrentText("🎲 随机特效 (防限流神器)")
        self.trans_duration = QDoubleSpinBox(); self.trans_duration.setRange(0.1, 2.0); self.trans_duration.setValue(0.5)
        trans_layout.addWidget(self.trans_combo); trans_layout.addWidget(QLabel("时长:")); trans_layout.addWidget(self.trans_duration); v_layout.addRow("转场库:", trans_layout)

        self.lut_combo = QComboBox()
        for lut_label, lut_key in LUT_OPTIONS:
            self.lut_combo.addItem(lut_label, lut_key)
        self.lut_combo.setCurrentIndex(0)
        self.lut_combo.setToolTip(
            "LUT 会在 HDR 转 SDR 和画面缩放后应用；随机模式每条成片只选择一种风格。"
        )
        self.lut_combo.wheelEvent = lambda event: event.ignore()
        v_layout.addRow("LUT滤镜:", self.lut_combo)

        self.anti_dedup_chk = QCheckBox("启用【微变速/调色】防重"); self.anti_dedup_chk.setChecked(True); v_layout.addRow("平台过审:", self.anti_dedup_chk)
        # 参数防重抖动：让开头时长/强结构节点/转场时长/音量在 ±% 内每条成片随机浮动
        jitter_layout = QHBoxLayout()
        self.param_jitter_spin = QSpinBox()
        self.param_jitter_spin.setRange(0, 40)
        self.param_jitter_spin.setValue(15)
        self.param_jitter_spin.setSuffix(" %")
        self.param_jitter_spin.setSingleStep(5)
        self.param_jitter_spin.setToolTip(
            "把「开头覆盖 / 数字人露出 / 产品穿插 / 露出次数 / 转场时长 / 音量」这些固定参数，\n"
            "在 ±范围内每条成片随机浮动，避免同一批视频的时间轴骨架完全一致被平台判重复。\n"
            "0% = 关闭（所有视频共用你设定的固定值）。"
        )
        self.param_jitter_spin.wheelEvent = lambda event: event.ignore()
        jitter_layout.addWidget(self.param_jitter_spin)
        jitter_layout.addStretch(1)
        v_layout.addRow("参数防重抖动:", jitter_layout)
        output_layout = QHBoxLayout(); self.res_combo = QComboBox(); self.res_combo.addItems(["720x1280 (竖屏)"]); self.res_combo.currentTextChanged.connect(self.draw_preview_canvas); self.res_combo.currentTextChanged.connect(self.draw_subtitle_preview)
        output_layout.addWidget(self.res_combo); v_layout.addRow("画面比例:", output_layout); vis_group.setLayout(v_layout); form_layout.addRow(vis_group)
        
        perf_group = QGroupBox("3. 渲染与硬件自适应"); p_layout = QFormLayout()
        p_layout.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        p_layout.setRowWrapPolicy(QFormLayout.WrapLongRows)
        p_layout.setLabelAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        loop_layout = QHBoxLayout(); self.loop_spin = QSpinBox(); self.loop_spin.setRange(1, 999); self.loop_spin.setValue(1); self.thread_spin = QSpinBox(); self.thread_spin.setRange(1, 16); self.thread_spin.setValue(2) 
        loop_layout.addWidget(QLabel("执行")); loop_layout.addWidget(self.loop_spin); loop_layout.addWidget(QLabel("轮,")); loop_layout.addWidget(self.thread_spin); loop_layout.addWidget(QLabel("线程"))
        p_layout.addRow("任务列队:", loop_layout)
        self.auto_perf_chk = QCheckBox("自动选择最快档位"); self.auto_perf_chk.setChecked(True)
        p_layout.addRow("自动适配:", self.auto_perf_chk)
        gpu_layout = QHBoxLayout(); self.gpu_combo = QComboBox(); self.gpu_combo.addItems(["Nvidia NVENC 加速", "AMD AMF 加速", "CPU 软解"]); self.detect_gpu_btn = QPushButton("重新检测"); self.detect_gpu_btn.clicked.connect(self.detect_system_gpu) 
        gpu_layout.addWidget(self.gpu_combo); gpu_layout.addWidget(self.detect_gpu_btn); p_layout.addRow("引擎:", gpu_layout)
        status_layout = QVBoxLayout()
        status_layout.setSpacing(4)
        status_title = QLabel("当前档位")
        status_title.setObjectName("hardwareStatusTitle")
        self.hardware_status_label = QLabel("等待硬件检测")
        self.hardware_status_label.setObjectName("hardwareStatus")
        self.hardware_status_label.setWordWrap(True)
        self.hardware_status_label.setMinimumHeight(44)
        status_layout.addWidget(status_title)
        status_layout.addWidget(self.hardware_status_label)
        p_layout.addRow(status_layout)
        perf_group.setLayout(p_layout); form_layout.addRow(perf_group)

        # 🚀 核心新增：专属封面包装区
        cover_group = QGroupBox("4. 封面静态大字包装 (避开字幕)")
        c_layout = QFormLayout()
        c_layout.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        c_layout.setRowWrapPolicy(QFormLayout.WrapLongRows)
        c_layout.setLabelAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.cover_text_input = QLineEdit()
        self.cover_text_input.setPlaceholderText("在此输入封面主标题 (留空则不加文字)")
        self.cover_font = QComboBox()
        self.cover_font.addItem("🎲 随机系统字体")
        self.cover_font.addItems(list(self.sys_fonts.keys()))
        self.cover_style = QComboBox()
        self.cover_style.addItems(COVER_STATIC_STYLES)
        self.cover_style.setCurrentText("经典白字黑边")
        
        c_layout.addRow("封面大标题:", self.cover_text_input)
        font_style_layout = QHBoxLayout()
        font_style_layout.addWidget(self.cover_font, stretch=1)
        font_style_layout.addWidget(self.cover_style, stretch=1)
        c_layout.addRow("字体与特效:", font_style_layout)
        cover_group.setLayout(c_layout)
        form_layout.addRow(cover_group)

        action_layout = QHBoxLayout()
        self.start_btn = QPushButton("开始执行任务"); self.start_btn.setObjectName("startButton"); self.start_btn.setMinimumHeight(52); self.start_btn.clicked.connect(lambda checked=False: self.start_synthesis(preview_only=False))
        self.stop_btn = QPushButton("终止任务"); self.stop_btn.setObjectName("stopButton"); self.stop_btn.setMinimumHeight(52); self.stop_btn.clicked.connect(self.stop_and_clean)
        action_layout.addWidget(self.start_btn, stretch=2); action_layout.addWidget(self.stop_btn, stretch=1); form_layout.addRow(action_layout)

        self.task_progress = QProgressBar()
        self.task_progress.setRange(0, 100)
        self.task_progress.setValue(0)
        self.task_progress.setFormat("等待任务")
        form_layout.addRow("任务进度:", self.task_progress)
        return container

    def build_task_settings_page(self):
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.build_card_page("任务控制", self.build_task_settings_widget()))
        return scroll

    def build_batch_mixer_page(self):
        page = QWidget()
        layout = QHBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)

        workspace = QFrame()
        workspace.setObjectName("card")
        workspace_layout = QVBoxLayout(workspace)
        workspace_layout.setContentsMargins(16, 14, 16, 16)
        workspace_layout.setSpacing(12)

        title = QLabel("批量混剪工作台")
        title.setObjectName("appTitle")
        workspace_layout.addWidget(title)

        workspace_layout.addWidget(self.build_batch_project_bar())

        sub_nav = QGridLayout()
        sub_nav.setSpacing(8)
        self.stacked_widget = QStackedWidget()
        self.tab_btn_group = QButtonGroup()
        self.tables = {}
        self.tab_buttons = {}

        row, col = 0, 0
        first_track_nav_slot = None
        for i, name in enumerate(self.tab_names):
            btn = QPushButton(name)
            btn.setCheckable(True)
            btn.setProperty("navButton", True)
            btn.setMinimumHeight(38)
            if i == 0:
                btn.setChecked(True)
            self.tab_btn_group.addButton(btn, i)
            self.tab_buttons[name] = btn
            advance_nav_slot = name != '第二段指定素材'
            if name == '第二段指定素材' and first_track_nav_slot is not None:
                button_row, button_col = first_track_nav_slot
            else:
                button_row, button_col = row, col
                if name == '第一轨道':
                    first_track_nav_slot = (button_row, button_col)
            sub_nav.addWidget(btn, button_row, button_col)

            if name == '文字水印':
                tab = self.build_watermark_tab()
            elif name == '智能字幕':
                tab = self.build_subtitle_tab()
            else:
                tab = self.build_file_tab(name)
            self.stacked_widget.addWidget(tab)

            if advance_nav_slot:
                col += 1
                if col >= 5:
                    col = 0
                    row += 1

        self.tab_btn_group.idClicked.connect(self.switch_tab)
        workspace_layout.addLayout(sub_nav)
        workspace_layout.addWidget(self.stacked_widget, stretch=1)

        settings_card = QFrame()
        settings_card.setObjectName("card")
        settings_card.setMinimumWidth(390)
        settings_layout = QVBoxLayout(settings_card)
        settings_layout.setContentsMargins(16, 14, 16, 16)
        settings_layout.setSpacing(12)
        settings_title = QLabel("核心参数")
        settings_title.setObjectName("appTitle")
        settings_header = QHBoxLayout()
        settings_header.setContentsMargins(0, 0, 0, 0)
        settings_header.setSpacing(8)
        settings_header.addWidget(settings_title)
        settings_header.addStretch(1)
        self.randomize_core_params_btn = QPushButton("一键随机微调")
        self.randomize_core_params_btn.setToolTip(
            "以当前参数为基准随机微调：时长增减0.1-0.5秒，"
            "缓慢拉近倍率增减0.01-0.05；不会改变素材、模式和任务数量。"
        )
        self.randomize_core_params_btn.clicked.connect(self.randomize_core_parameters)
        settings_header.addWidget(self.randomize_core_params_btn)
        settings_layout.addLayout(settings_header)
        settings_layout.addWidget(self.build_task_settings_widget())
        self.apply_mixer_mode_ui()

        settings_scroll = QScrollArea()
        settings_scroll.setWidgetResizable(True)
        settings_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        settings_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        settings_scroll.setMinimumWidth(390)
        settings_scroll.setMaximumWidth(460)
        settings_scroll.setWidget(settings_card)
        self.batch_settings_card = settings_card
        self.batch_settings_scroll = settings_scroll

        layout.addWidget(workspace, stretch=1)
        layout.addWidget(settings_scroll, stretch=0)
        return page
