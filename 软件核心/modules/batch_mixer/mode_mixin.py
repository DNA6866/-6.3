class ModeMixin:
    def switch_tab(self, index):
        self.stacked_widget.setCurrentIndex(index)
        name = self.tab_names[index] if 0 <= index < len(self.tab_names) else ""
        if hasattr(self, 'batch_settings_scroll'):
            self.batch_settings_scroll.setVisible(True)
            self._apply_batch_settings_width(name)
        if name == '文字水印':
            self.auto_load_watermark_reference_frame(silent=True)
        if name == '智能字幕':
            self.auto_load_subtitle_reference_frame(silent=True)

    def _apply_batch_settings_width(self, tab_name):
        if not hasattr(self, 'batch_settings_scroll'):
            return
        min_w, max_w = 390, 460
        self.batch_settings_scroll.setMinimumWidth(min_w)
        self.batch_settings_scroll.setMaximumWidth(max_w)
        card = getattr(self, 'batch_settings_card', None)
        if card is not None:
            card.setMinimumWidth(min_w)

    def is_first_track_mixer_mode(self):
        if not hasattr(self, 'mixer_mode_combo'):
            return False
        return self.mixer_mode_combo.currentData() in {'first_track', 'ai_intro_first_track'}

    def is_ai_intro_mixer_mode(self):
        return (
            hasattr(self, 'mixer_mode_combo')
            and self.mixer_mode_combo.currentData() in {'ai_intro_first_track', 'ai_intro_audio'}
        )

    def is_ai_intro_audio_mixer_mode(self):
        return (
            hasattr(self, 'mixer_mode_combo')
            and self.mixer_mode_combo.currentData() == 'ai_intro_audio'
        )

    def is_ordinary_mixer_mode(self):
        return (
            hasattr(self, 'mixer_mode_combo')
            and self.mixer_mode_combo.currentData() == 'ordinary'
        )

    def _set_mixer_mode(self, mode):
        if not hasattr(self, 'mixer_mode_combo'):
            return
        idx = self.mixer_mode_combo.findData(mode)
        if idx >= 0:
            self.mixer_mode_combo.setCurrentIndex(idx)
        self.apply_mixer_mode_ui()

    def _set_combo_data(self, combo, data):
        idx = combo.findData(data)
        if idx >= 0:
            combo.setCurrentIndex(idx)

    @staticmethod
    def _set_layout_items_visible(layout, visible):
        if layout is None:
            return
        for index in range(layout.count()):
            item = layout.itemAt(index)
            widget = item.widget()
            child_layout = item.layout()
            if widget is not None:
                widget.setVisible(visible)
            elif child_layout is not None:
                ModeMixin._set_layout_items_visible(child_layout, visible)

    def _set_mixer_parameter_visible(self, key, visible):
        fields = getattr(self, 'mixer_parameter_fields', {})
        form_layout = getattr(self, 'mixer_rule_layout', None)
        field = fields.get(key)
        if field is None or form_layout is None:
            return
        try:
            label = form_layout.labelForField(field)
        except TypeError:
            label = None
        if label is not None:
            label.setVisible(visible)
        if hasattr(field, 'setVisible'):
            field.setVisible(visible)
        else:
            self._set_layout_items_visible(field, visible)

    def _activate_mixer_tab(self, name):
        if not hasattr(self, 'tab_names') or name not in self.tab_names:
            return
        index = self.tab_names.index(name)
        self.stacked_widget.setCurrentIndex(index)
        button = getattr(self, 'tab_buttons', {}).get(name)
        if button is not None:
            button.setChecked(True)

    def apply_mixer_mode_ui(self):
        is_first_track = self.is_first_track_mixer_mode()
        is_ai_intro = self.is_ai_intro_mixer_mode()
        is_ordinary = self.is_ordinary_mixer_mode()
        is_ai_intro_audio = (
            hasattr(self, 'mixer_mode_combo')
            and self.mixer_mode_combo.currentData() == 'ai_intro_audio'
        )
        if hasattr(self, 'tab_buttons'):
            first_track_btn = self.tab_buttons.get('第一轨道')
            if first_track_btn:
                first_track_btn.setVisible(is_first_track)
            second_segment_btn = self.tab_buttons.get('第二段指定素材')
            if second_segment_btn:
                second_segment_btn.setVisible(not is_first_track)
            ai_intro_btn = self.tab_buttons.get('AI开场视频')
            if ai_intro_btn:
                ai_intro_btn.setVisible(is_ai_intro)
            hook_btn = self.tab_buttons.get('钩子素材')
            if hook_btn:
                hook_btn.setVisible(True)
                hook_btn.setToolTip(
                    "AI完整开场模式下，钩子素材会加在AI视频播放完之后的第一个镜头。"
                    if is_ai_intro else
                    "选择用于普通模式或第一轨道模式开头覆盖的钩子素材。"
                )
        if hasattr(self, 'stacked_widget') and hasattr(self, 'tab_names'):
            current_index = self.stacked_widget.currentIndex()
            current_name = self.tab_names[current_index] if 0 <= current_index < len(self.tab_names) else ""
            if current_name == '第一轨道' and not is_first_track:
                self._activate_mixer_tab('第二段指定素材')
            elif current_name == '第二段指定素材' and is_first_track:
                self._activate_mixer_tab('第一轨道')
            elif current_name == 'AI开场视频' and not is_ai_intro:
                target_name = '第一轨道' if is_first_track else '第二段指定素材'
                self._activate_mixer_tab(target_name)
        for key in ('strong_structure', 'avatar', 'product', 'avatar_count', 'overlay_gap'):
            self._set_mixer_parameter_visible(key, is_first_track)
        # AI开场模式下钩子参数同样可用（钩子叠加在AI视频播放完后的主内容开头）
        self._set_mixer_parameter_visible('hook', True)

        rule_layout = getattr(self, 'mixer_rule_layout', None)
        fields = getattr(self, 'mixer_parameter_fields', {})
        if rule_layout is not None:
            rhythm_label = rule_layout.labelForField(fields.get('rhythm'))
            clip_label = rule_layout.labelForField(fields.get('clip_duration'))
            audio_label = rule_layout.labelForField(fields.get('audio'))
            if rhythm_label is not None:
                rhythm_label.setText('覆盖节奏:' if is_first_track else '拼接节奏:')
            if clip_label is not None:
                clip_label.setText('产品切片:' if is_first_track else '片段时长:')
            if audio_label is not None:
                audio_label.setText('混音倍率:')
        if hasattr(self, 'main_vol_text_label'):
            self.main_vol_text_label.setVisible(True)
        if hasattr(self, 'main_vol_spin'):
            self.main_vol_spin.setVisible(True)
        if hasattr(self, 'mixer_rule_group'):
            if is_ordinary:
                self.mixer_rule_group.setTitle('1. 普通拼接参数')
            elif is_first_track:
                self.mixer_rule_group.setTitle('1. 第一轨道与覆盖裁剪')
            else:
                self.mixer_rule_group.setTitle('1. 音频驱动拼接参数')
        if hasattr(self, 'mixer_summary_label'):
            if is_ai_intro_audio:
                text = "AI开场+音频拼接模式：音频素材提供声音和总时长；AI视频完整播放并保留原声，随后钩子素材作为第一镜头，之后由产品素材随机拼接。"
            elif is_ai_intro:
                text = "AI完整开场模式：AI视频完整播放并保留原声，钩子素材加在AI视频之后的第一个镜头，随后执行第一轨道数字人和产品混剪。"
            elif is_first_track:
                text = "数字人口播第一轨道模式：第一轨道提供声音和总时长；钩子素材可覆盖开头，强结构模式会让数字人短露出，后段由产品素材接管。"
            elif is_ordinary:
                text = "普通视频拼接模式：音频素材提供声音和总时长；钩子作为第一段，第二段可从指定目录抽取，后续从视频素材内部随机位置取片拼接。"
            else:
                text = "传统音频混剪模式：钩子作为第一段，第二段有指定素材时必定使用，后续由视频素材内部随机位置取片拼接。"
            self.mixer_summary_label.setText(text)

    def apply_clip_rhythm_preset(self):
        if not hasattr(self, 'clip_rhythm_combo'):
            return
        mode = self.clip_rhythm_combo.currentData()
        presets = {
            'fast': (1.2, 2.4, 0.5, 0.8, 1.8),
            'balanced': (2.0, 4.5, 0.8, 1.5, 3.5),
            'slow': (3.5, 7.0, 1.2, 3.0, 6.0),
            'mixed': (1.2, 7.0, 1.0, 0.8, 5.0),
        }
        if mode not in presets:
            return
        min_clip, max_clip, jitter, gap_min, gap_max = presets[mode]
        self.min_clip_spin.setValue(min_clip)
        self.max_clip_spin.setValue(max_clip)
        self.clip_jitter_spin.setValue(jitter)
        self.overlay_gap_min_spin.setValue(gap_min)
        self.overlay_gap_max_spin.setValue(gap_max)

