import os
import json

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QFontMetrics
from PyQt5.QtWidgets import QMessageBox

from paths import workspace_path
from ui_components import resolve_network_path


def _config_value(mapping, key, default):
    if not isinstance(mapping, dict):
        return default
    value = mapping.get(key, default)
    return default if value is None else value


class ConfigMixin:
    def watermark_draft_for_config(self):
        if not hasattr(self, 'wm_text'):
            return getattr(self, '_wm_new_draft', {})
        if getattr(self, 'editing_wm_index', -1) >= 0:
            return getattr(self, '_wm_new_draft', {})
        data = self._get_current_wm_data()
        if data.get('text', '').strip() or data.get('text_pool', '').strip() or data.get('use_text_pool'):
            self._wm_new_draft = data
            return data
        self._wm_new_draft = {}
        return {}

    def save_configuration(self, silent=False):
        config = {
            'watermarks': self.watermarks_data,
            'watermark_draft': self.watermark_draft_for_config(),
            'rules': {
                'min_clip': self.min_clip_spin.value(), 'max_clip': self.max_clip_spin.value(),
                'mixer_mode': self.mixer_mode_combo.currentData(),
                'ai_intro_mode': self.is_ai_intro_mixer_mode(),
                'strong_structure': self.strong_structure_chk.isChecked(),
                'hook_duration': self.hook_duration_spin.value(),
                'strong_avatar_duration': self.strong_avatar_duration_spin.value(),
                'strong_avatar_smart_tail': self.strong_avatar_smart_tail_chk.isChecked(),
                'strong_product_duration': self.strong_product_duration_spin.value(),
                'strong_avatar_count': self.strong_avatar_count_spin.value(),
                'clip_rhythm': self.clip_rhythm_combo.currentData(),
                'clip_jitter': self.clip_jitter_spin.value(),
                'overlay_gap_min': self.overlay_gap_min_spin.value(), 'overlay_gap_max': self.overlay_gap_max_spin.value(),
                'overlay_offset_strength': self.overlay_offset_spin.value(),
                'overlay_zoom_mode': self.overlay_zoom_mode_combo.currentData(),
                'overlay_zoom_min': self.overlay_zoom_min_spin.value(), 'overlay_zoom_max': self.overlay_zoom_max_spin.value(),
                'overlay_zoom_profile_version': 2,
                'use_first_track_audio': self.is_first_track_mixer_mode(),
                'trans_type': self.trans_combo.currentText(), 'trans_dur': self.trans_duration.value(),
                'lut_style': self.lut_combo.currentData(),
                'main_vol': self.main_vol_spin.value(), 'bgm_vol': self.bgm_vol_spin.value(),
                'smart_sfx_enabled': self.smart_sfx_chk.isChecked(),
                'smart_sfx_strength': self.smart_sfx_strength_combo.currentData(),
                'resolution': self.res_combo.currentText(), 'gpu': self.gpu_combo.currentText(),
                'loop_count': self.loop_spin.value(), 'threads': self.thread_spin.value(),
                'anti_dedup': self.anti_dedup_chk.isChecked(),
                'auto_perf': self.auto_perf_chk.isChecked(),
                'output_date_folder': True
            },
            'subtitle': {
                'style_version': 3,
                'enable': self.sub_enable_chk.isChecked(),
                'word_timestamps': self.sub_word_timestamps_chk.isChecked(),
                'font': self.sub_font.currentText(),
                'size': self.sub_size.value(), 'color': self.sub_color.currentText(),
                'border_w': self.sub_border_w.value(), 'border_color': self.sub_border_color.currentText(),
                'anim': self.sub_anim.currentText(), 'margin_v': self.sub_margin_v.value()
            },
            'cover': {
                'text': self.cover_text_input.text(), 'font': self.cover_font.currentText(),
                'style': self.cover_style.currentText()
            },
            'custom_dirs': {k: v for k, v in self.tab_folders.items() if v != workspace_path('素材', k) and v != workspace_path('输出', k)},
            'last_file_dialog_dir': self._last_file_dialog_dir
        }
        try:
            with open(self.config_file, 'w', encoding='utf-8') as f: json.dump(config, f, indent=4, ensure_ascii=False)
            if not silent: self.update_log("💾 [系统配置] 全局配置已自动保存。")
        except Exception:
            print(f"[警告] 配置保存失败: {self.config_file}", flush=True)

    def load_configuration(self):
        if not os.path.exists(self.config_file): return
        try:
            with open(self.config_file, 'r', encoding='utf-8') as f: config = json.load(f)
            if not isinstance(config, dict):
                raise ValueError("配置文件根节点必须是对象")
            rules = config.get('rules', {})
            rules = rules if isinstance(rules, dict) else {}
            if 'clip_rhythm' in rules: self._set_combo_data(self.clip_rhythm_combo, _config_value(rules, 'clip_rhythm', self.clip_rhythm_combo.currentData()))
            if 'strong_structure' in rules: self.strong_structure_chk.setChecked(bool(_config_value(rules, 'strong_structure', self.strong_structure_chk.isChecked())))
            if 'hook_duration' in rules: self.hook_duration_spin.setValue(_config_value(rules, 'hook_duration', self.hook_duration_spin.value()))
            if 'strong_avatar_duration' in rules: self.strong_avatar_duration_spin.setValue(_config_value(rules, 'strong_avatar_duration', self.strong_avatar_duration_spin.value()))
            if 'strong_avatar_smart_tail' in rules: self.strong_avatar_smart_tail_chk.setChecked(bool(_config_value(rules, 'strong_avatar_smart_tail', self.strong_avatar_smart_tail_chk.isChecked())))
            if 'strong_product_duration' in rules: self.strong_product_duration_spin.setValue(_config_value(rules, 'strong_product_duration', self.strong_product_duration_spin.value()))
            if 'strong_avatar_count' in rules: self.strong_avatar_count_spin.setValue(_config_value(rules, 'strong_avatar_count', self.strong_avatar_count_spin.value()))
            if 'min_clip' in rules: self.min_clip_spin.setValue(_config_value(rules, 'min_clip', self.min_clip_spin.value()))
            if 'max_clip' in rules: self.max_clip_spin.setValue(_config_value(rules, 'max_clip', self.max_clip_spin.value()))
            if 'clip_jitter' in rules: self.clip_jitter_spin.setValue(_config_value(rules, 'clip_jitter', self.clip_jitter_spin.value()))
            if 'overlay_gap_min' in rules: self.overlay_gap_min_spin.setValue(_config_value(rules, 'overlay_gap_min', self.overlay_gap_min_spin.value()))
            if 'overlay_gap_max' in rules: self.overlay_gap_max_spin.setValue(_config_value(rules, 'overlay_gap_max', self.overlay_gap_max_spin.value()))
            if 'overlay_offset_strength' in rules:
                self.overlay_offset_spin.setValue(_config_value(rules, 'overlay_offset_strength', self.overlay_offset_spin.value()))
            elif 'overlay_offset' in rules:
                self.overlay_offset_spin.setValue(max(0, min(100, int(float(rules.get('overlay_offset', 0)) * 2.5))))
            if 'mixer_mode' in rules: self._set_mixer_mode(_config_value(rules, 'mixer_mode', self.mixer_mode_combo.currentData()))
            elif rules.get('use_first_track_audio'): self._set_mixer_mode('first_track')
            if 'overlay_zoom_mode' in rules: self._set_combo_data(self.overlay_zoom_mode_combo, _config_value(rules, 'overlay_zoom_mode', self.overlay_zoom_mode_combo.currentData()))
            if 'overlay_zoom_min' in rules: self.overlay_zoom_min_spin.setValue(_config_value(rules, 'overlay_zoom_min', self.overlay_zoom_min_spin.value()))
            if 'overlay_zoom_max' in rules:
                overlay_zoom_max = _config_value(rules, 'overlay_zoom_max', self.overlay_zoom_max_spin.value())
                try:
                    legacy_zoom_profile = int(rules.get('overlay_zoom_profile_version', 0) or 0) < 2
                    slow_push_mode = (rules.get('overlay_zoom_mode') or self.overlay_zoom_mode_combo.currentData()) == 'slow_push_in'
                    if legacy_zoom_profile and slow_push_mode and float(overlay_zoom_max) <= 1.04:
                        overlay_zoom_max = 1.10
                except Exception:
                    pass
                self.overlay_zoom_max_spin.setValue(overlay_zoom_max)
            if 'trans_type' in rules: self.trans_combo.setCurrentText(_config_value(rules, 'trans_type', self.trans_combo.currentText()))
            if 'trans_dur' in rules: self.trans_duration.setValue(_config_value(rules, 'trans_dur', self.trans_duration.value()))
            if 'lut_style' in rules:
                self._set_combo_data(
                    self.lut_combo,
                    _config_value(rules, 'lut_style', self.lut_combo.currentData()),
                )
            if 'main_vol' in rules: self.main_vol_spin.setValue(_config_value(rules, 'main_vol', self.main_vol_spin.value()))
            if 'bgm_vol' in rules: self.bgm_vol_spin.setValue(_config_value(rules, 'bgm_vol', self.bgm_vol_spin.value()))
            if 'smart_sfx_enabled' in rules:
                self.smart_sfx_chk.setChecked(bool(_config_value(
                    rules,
                    'smart_sfx_enabled',
                    self.smart_sfx_chk.isChecked(),
                )))
            if 'smart_sfx_strength' in rules:
                self._set_combo_data(
                    self.smart_sfx_strength_combo,
                    _config_value(
                        rules,
                        'smart_sfx_strength',
                        self.smart_sfx_strength_combo.currentData(),
                    ),
                )
            self.smart_sfx_strength_combo.setEnabled(
                self.smart_sfx_chk.isChecked()
            )
            if 'resolution' in rules: self.res_combo.setCurrentText(_config_value(rules, 'resolution', self.res_combo.currentText()))
            if 'gpu' in rules: self.gpu_combo.setCurrentText(_config_value(rules, 'gpu', self.gpu_combo.currentText()))
            if 'loop_count' in rules: self.loop_spin.setValue(_config_value(rules, 'loop_count', self.loop_spin.value()))
            if 'threads' in rules: self.thread_spin.setValue(_config_value(rules, 'threads', self.thread_spin.value()))
            if 'anti_dedup' in rules: self.anti_dedup_chk.setChecked(bool(_config_value(rules, 'anti_dedup', self.anti_dedup_chk.isChecked())))
            if 'auto_perf' in rules: self.auto_perf_chk.setChecked(bool(_config_value(rules, 'auto_perf', self.auto_perf_chk.isChecked())))

            sub = config.get('subtitle', {})
            sub = sub if isinstance(sub, dict) else {}
            if 'enable' in sub: self.sub_enable_chk.setChecked(bool(_config_value(sub, 'enable', self.sub_enable_chk.isChecked())))
            if 'word_timestamps' in sub:
                self.sub_word_timestamps_chk.setChecked(bool(_config_value(
                    sub,
                    'word_timestamps',
                    self.sub_word_timestamps_chk.isChecked(),
                )))
            if 'font' in sub: self.sub_font.setCurrentText(_config_value(sub, 'font', self.sub_font.currentText()))
            if 'size' in sub: self.sub_size.setValue(_config_value(sub, 'size', self.sub_size.value()))
            if 'color' in sub: self.sub_color.setCurrentText(_config_value(sub, 'color', self.sub_color.currentText()))
            if 'border_w' in sub: self.sub_border_w.setValue(_config_value(sub, 'border_w', self.sub_border_w.value()))
            if 'border_color' in sub: self.sub_border_color.setCurrentText(_config_value(sub, 'border_color', self.sub_border_color.currentText()))
            if 'anim' in sub: self.sub_anim.setCurrentText(_config_value(sub, 'anim', self.sub_anim.currentText()))
            if 'margin_v' in sub: self.sub_margin_v.setValue(_config_value(sub, 'margin_v', self.sub_margin_v.value()))

            cov = config.get('cover', {})
            cov = cov if isinstance(cov, dict) else {}
            if 'text' in cov: self.cover_text_input.setText(_config_value(cov, 'text', self.cover_text_input.text()))
            if 'font' in cov: self.cover_font.setCurrentText(_config_value(cov, 'font', self.cover_font.currentText()))
            if 'style' in cov: self.cover_style.setCurrentText(_config_value(cov, 'style', self.cover_style.currentText()))

            watermarks = config.get('watermarks', [])
            self.watermarks_data = watermarks if isinstance(watermarks, list) else []
            for wm in self.watermarks_data:
                if not isinstance(wm, dict):
                    continue
                font_name = wm.get('font_name', '')
                if font_name in self.sys_fonts: wm['font_path'] = self.sys_fonts.get(font_name, '')
                self.add_wm_to_table(wm)
            draft = config.get('watermark_draft', {})
            self._wm_new_draft = draft if isinstance(draft, dict) else {}
            if self._wm_new_draft:
                self.apply_watermark_form_data(self._wm_new_draft)
            self.draw_preview_canvas(); self.draw_subtitle_preview()
            # 恢复自定义产品目录
            custom_dirs = config.get('custom_dirs', {})
            custom_dirs = custom_dirs if isinstance(custom_dirs, dict) else {}
            if '第二段指定素材' not in custom_dirs and custom_dirs.get('首段优选素材'):
                custom_dirs['第二段指定素材'] = custom_dirs.get('首段优选素材')
            for tab, path in custom_dirs.items():
                if tab not in self.tab_folders or not str(path or "").strip():
                    continue
                resolved_path = resolve_network_path(path)
                self.tab_folders[tab] = resolved_path
            # 恢复上次文件对话框目录
            last_dir = config.get('last_file_dialog_dir')
            if last_dir:
                resolved_last_dir = resolve_network_path(last_dir)
                if os.path.isdir(resolved_last_dir):
                    self._last_file_dialog_dir = resolved_last_dir
        except Exception:
            print("[警告] 配置加载失败，使用默认设置。", flush=True)

    def load_preset_list(self):
        if os.path.exists(self.preset_file):
            try:
                with open(self.preset_file, 'r', encoding='utf-8') as f: self.wm_presets = json.load(f)
            except Exception: self.wm_presets = {}
        else: self.wm_presets = {}
        self.preset_combo.clear(); self.preset_combo.addItems(list(self.wm_presets.keys()))
        # 为每个预设项设置 tooltip，确保即使显示截断也能通过鼠标悬停查看完整名称
        for idx, preset_name in enumerate(self.wm_presets.keys()):
            self.preset_combo.setItemData(idx, preset_name, Qt.ToolTipRole)

    def save_current_as_preset(self):
        name = self.preset_name_input.text().strip()
        if not name: return
        # 输入验证：检测名称过长或超出可视区域
        max_chars = 50
        if len(name) > max_chars:
            QMessageBox.warning(self, "名称过长", f"预设名称不能超过{max_chars}个字符，当前{len(name)}个字符。")
            return
        # 使用字体度量检测文本是否超出 combo 显示区域
        fm = QFontMetrics(self.preset_combo.font())
        text_width = fm.horizontalAdvance(name)
        view_width = self.preset_combo.view().width() if self.preset_combo.view() else 200
        max_display_width = max(view_width, self.preset_combo.width() - 30)
        if text_width > max_display_width:
            reply = QMessageBox.question(self, "名称可能显示不全",
                f'预设名称"{name}"较长，在列表中可能无法完整显示。\n\n是否仍要保存？',
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if reply != QMessageBox.Yes:
                return
        self.wm_presets[name] = self.watermarks_data.copy()
        try:
            with open(self.preset_file, 'w', encoding='utf-8') as f: json.dump(self.wm_presets, f, indent=4, ensure_ascii=False)
            self.load_preset_list(); self.preset_combo.setCurrentText(name); QMessageBox.information(self, "成功", f"预设【{name}】已保存！")
        except Exception as exc:
            print(f"[水印预设] 保存失败：{exc}", flush=True)
            self.update_log(f"[水印预设] 保存失败：{exc}")

    def apply_selected_preset(self):
        name = self.preset_combo.currentText()
        if not name or name not in self.wm_presets: return
        self._wm_new_draft = {}
        self.watermarks_data = self.wm_presets.get(name, []).copy()
        self.wm_table.setRowCount(0)
        for wm in self.watermarks_data: self.add_wm_to_table(wm)
        self.reset_watermark_form(clear_inputs=True); self.save_configuration(silent=True)

    def delete_selected_preset(self):
        name = self.preset_combo.currentText()
        if not name or name not in self.wm_presets: return
        del self.wm_presets[name]
        try:
            with open(self.preset_file, 'w', encoding='utf-8') as f: json.dump(self.wm_presets, f, indent=4, ensure_ascii=False)
            self.load_preset_list()
        except Exception as exc:
            print(f"[水印预设] 删除失败：{exc}", flush=True)
            self.update_log(f"[水印预设] 删除失败：{exc}")

