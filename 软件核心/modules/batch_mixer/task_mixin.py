import os
import copy
import subprocess
import time

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QApplication, QMessageBox, QTableWidgetItem

from engine import SynthesisEngine
from paths import APP_ROOT, logs_dir, models_dir, resource_root_candidates, tools_dir
from services.batch_project_service import safe_output_component


class TaskMixin:
    def on_task_done(self):
        self._completed_mix_tasks += 1
        expected = max(1, self._expected_mix_tasks)
        completed = min(self._completed_mix_tasks, expected)
        self.task_progress.setRange(0, expected)
        self.task_progress.setValue(completed)
        if completed < expected:
            self.task_progress.setFormat(f"已完成 {completed}/{expected}，剩余任务仍在渲染")
        else:
            self.task_progress.setFormat(f"已完成 {completed}/{expected}")
        if getattr(self, '_active_queue_task_id', ''):
            self.update_active_queue_progress(completed)
    
    def on_synthesis_finished(self, queue_task_id=""):
        self.update_log("[任务控制] 收到混剪完成信号，正在恢复按钮与刷新输出目录。")
        engine = getattr(self, 'engine_thread', None)
        self.engine_thread = None
        if engine is not None:
            engine.deleteLater()
        if self._expected_mix_tasks:
            self.task_progress.setRange(0, self._expected_mix_tasks)
            completed = min(self._completed_mix_tasks, self._expected_mix_tasks)
            self.task_progress.setValue(completed)
            self.task_progress.setFormat(f"任务结束：成功 {completed}/{self._expected_mix_tasks}")
        handled_queue = False
        if queue_task_id and hasattr(self, 'finish_active_queue_task'):
            handled_queue = self.finish_active_queue_task(
                self._completed_mix_tasks,
                self._expected_mix_tasks,
            )
        if not handled_queue:
            self._restore_mix_start_controls()
            self.refresh_directory_async('保存视频', silent=True)
        self.update_log("[任务控制] 混剪收尾完成，窗口保持运行。")

    def _restore_mix_start_controls(self):
        self.start_btn.setEnabled(True)
        self.start_btn.setText("开始执行任务")
        if hasattr(self, 'batch_project_combo'):
            self.batch_project_combo.setEnabled(True)
        if hasattr(self, 'preview_sample_btn'):
            self.preview_sample_btn.setEnabled(True)

    def _write_mixer_start_trace(self, message, reset=False):
        try:
            os.makedirs(logs_dir(), exist_ok=True)
            path = os.path.join(logs_dir(), "mixer_start_latest.log")
            mode = "w" if reset else "a"
            with open(path, mode, encoding="utf-8") as handle:
                handle.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {message}\n")
        except Exception:
            pass

    def get_selected_mixer_assets(self, tab):
        res = []
        tbl = self.tables.get(tab)
        if not tbl:
            return res
        for r in range(tbl.rowCount()):
            if self._asset_row_checked(tbl, r):
                item = tbl.item(r, 2)
                if not item:
                    continue
                if tab in ['音频素材']:
                    res.append({'path': item.text(), 'row': r})
                else:
                    res.append({'path': item.text()})
        return res

    def _mixer_output_dir(self, project_context=None):
        base_dir = os.path.abspath(self.tab_folders['保存视频'])
        context = project_context
        if context is None and hasattr(self, 'current_batch_project_context'):
            context = self.current_batch_project_context()
        if isinstance(context, dict) and context.get('id'):
            base_dir = os.path.join(
                base_dir,
                safe_output_component(context.get('shop_name'), "未命名店铺"),
                safe_output_component(context.get('product_name'), "未命名产品"),
                time.strftime("%Y-%m-%d"),
            )
        else:
            base_dir = os.path.join(base_dir, time.strftime("%Y-%m-%d"))
        os.makedirs(base_dir, exist_ok=True)
        return base_dir

    def apply_mix_workflow_preset(self):
        name = self.mix_preset_combo.currentText() if hasattr(self, 'mix_preset_combo') else ""
        presets = {
            "抖音竖屏带字幕": {
                "min_clip": 2.0, "max_clip": 3.5, "main_vol": 2.0, "bgm_vol": 0.18,
                "trans": "🎲 随机特效 (防限流神器)", "trans_dur": 0.45,
                "res": "720x1280 (竖屏)", "anti": True, "subtitle": True, "loops": 1,
            },
            "快手轻量差异化": {
                "min_clip": 1.8, "max_clip": 3.0, "main_vol": 2.0, "bgm_vol": 0.12,
                "trans": "叠化溶解", "trans_dur": 0.35,
                "res": "720x1280 (竖屏)", "anti": True, "subtitle": False, "loops": 1,
            },
            "快手强结构防重": {
                "min_clip": 1.4, "max_clip": 3.2, "main_vol": 2.0, "bgm_vol": 0.12,
                "trans": "🎲 随机特效 (防限流神器)", "trans_dur": 0.35,
                "res": "720x1280 (竖屏)", "anti": True, "subtitle": True, "loops": 1,
                "strong": True, "hook_dur": 2.0, "avatar_dur": 2.5, "product_dur": 3.0, "avatar_count": 3,
            },
            "低配电脑稳定": {
                "min_clip": 2.5, "max_clip": 4.0, "main_vol": 1.8, "bgm_vol": 0.15,
                "trans": "不使用转场", "trans_dur": 0.3,
                "res": "720x1280 (竖屏)", "anti": False, "subtitle": False, "loops": 1,
                "gpu": "CPU 软解", "threads": 1,
            },
            "高性能批量生产": {
                "min_clip": 1.5, "max_clip": 3.0, "main_vol": 2.0, "bgm_vol": 0.18,
                "trans": "🎲 随机特效 (防限流神器)", "trans_dur": 0.45,
                "res": "720x1280 (竖屏)", "anti": True, "subtitle": True, "loops": 3,
                "threads": 3,
            },
            "直播切片样片": {
                "min_clip": 4.0, "max_clip": 7.0, "main_vol": 2.0, "bgm_vol": 0.08,
                "trans": "不使用转场", "trans_dur": 0.3,
                "res": "720x1280 (竖屏)", "anti": True, "subtitle": True, "loops": 1,
            },
        }
        preset = presets.get(name)
        if not preset:
            return
        self.min_clip_spin.setValue(preset["min_clip"])
        self.max_clip_spin.setValue(preset["max_clip"])
        self.main_vol_spin.setValue(preset["main_vol"])
        self.bgm_vol_spin.setValue(preset["bgm_vol"])
        self.trans_combo.setCurrentText(preset["trans"])
        self.trans_duration.setValue(preset["trans_dur"])
        self.res_combo.setCurrentText(preset["res"])
        self.anti_dedup_chk.setChecked(preset["anti"])
        self.sub_enable_chk.setChecked(preset["subtitle"])
        self.loop_spin.setValue(preset["loops"])
        self.strong_structure_chk.setChecked(bool(preset.get("strong", False)))
        if "hook_dur" in preset:
            self.hook_duration_spin.setValue(preset["hook_dur"])
        if "avatar_dur" in preset:
            self.strong_avatar_duration_spin.setValue(preset["avatar_dur"])
        if "product_dur" in preset:
            self.strong_product_duration_spin.setValue(preset["product_dur"])
        if "avatar_count" in preset:
            self.strong_avatar_count_spin.setValue(preset["avatar_count"])
        if "gpu" in preset:
            self.gpu_combo.setCurrentText(preset["gpu"])
        if "threads" in preset:
            self.thread_spin.setValue(preset["threads"])
        self.update_log(f"[批量混剪] 已应用生产预设：{name}")
        self.run_mixer_preflight(show_dialog=False)

    def _probe_media_duration_for_check(self, path):
        ffprobe = os.path.abspath(os.path.join(tools_dir(), 'ffprobe.exe'))
        if not os.path.exists(ffprobe) or not os.path.exists(path):
            return 0.0
        try:
            res = subprocess.run(
                [ffprobe, '-v', 'error', '-show_entries', 'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1', path],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding='utf-8',
                errors='ignore',
                startupinfo=self.get_si(),
                timeout=8,
            )
            return max(0.0, float((res.stdout or "0").strip()))
        except Exception:
            return 0.0

    def run_mixer_preflight(self, show_dialog=True):
        selected_audios = self.get_selected_mixer_assets('音频素材')
        selected_vids = self.get_selected_mixer_assets('视频素材')
        selected_ai_intros = self.get_selected_mixer_assets('AI开场视频')
        selected_hooks = self.get_selected_mixer_assets('钩子素材')
        selected_covers = self.get_selected_mixer_assets('封面图片')
        selected_bgms = self.get_selected_mixer_assets('背景音乐')
        selected_first_track = self.get_selected_mixer_assets('第一轨道')
        selected_second_segment = self.get_selected_mixer_assets('第二段指定素材')
        use_first_track_audio = self.is_first_track_mixer_mode()
        ai_intro_mode = self.is_ai_intro_mixer_mode()
        ordinary_mode = self.is_ordinary_mixer_mode()
        strong_structure = self.strong_structure_chk.isChecked() and use_first_track_audio

        errors = []
        warnings = []
        ok = []
        ffmpeg = os.path.abspath(os.path.join(tools_dir(), 'ffmpeg.exe'))
        ffprobe = os.path.abspath(os.path.join(tools_dir(), 'ffprobe.exe'))
        if os.path.exists(ffmpeg) and os.path.exists(ffprobe):
            ok.append("ffmpeg / ffprobe 已就绪")
        else:
            errors.append("缺少 ffmpeg.exe 或 ffprobe.exe，请检查网盘资源包/tools")

        if not selected_audios and not use_first_track_audio:
            errors.append("未勾选音频素材")
        elif selected_audios:
            ok.append(f"已选择音频 {len(selected_audios)} 条")
        if not selected_vids:
            errors.append("未勾选视频素材")
        else:
            ok.append(f"已选择视频素材 {len(selected_vids)} 条")
        if ai_intro_mode:
            if not selected_ai_intros:
                errors.append("AI完整开场模式需要勾选至少一条AI开场视频")
            else:
                ok.append(f"AI开场视频：{len(selected_ai_intros)} 条，将按均衡循环完整播放")
            if selected_hooks:
                ok.append(f"钩子素材 {len(selected_hooks)} 条，将加在AI视频播放完之后的第一个镜头")
        elif selected_hooks:
            ok.append(f"钩子素材 {len(selected_hooks)} 条")
        elif strong_structure:
            warnings.append("已开启强结构模式，但未勾选钩子素材；开头将用视频素材兜底，效果会弱一些")
        if selected_covers:
            ok.append(f"封面图片 {len(selected_covers)} 张")
        if selected_bgms:
            ok.append(f"背景音乐 {len(selected_bgms)} 条")
        if selected_first_track and use_first_track_audio:
            ok.append(f"第一轨道：{len(selected_first_track)} 条")
        elif use_first_track_audio:
            errors.append("数字人口播模式需要勾选第一轨道素材")
        if use_first_track_audio:
            ok.append("声音来源：使用第一轨道原声")
        if selected_second_segment and not use_first_track_audio:
            ok.append(f"第二段指定素材：{len(selected_second_segment)} 条，将固定用于第二段")
            if not selected_hooks:
                warnings.append("已指定第二段素材但未选择钩子，第一段将从普通视频素材补位")

        bad_ai_intros = []
        unusual_ai_intros = []
        if ai_intro_mode:
            for item in selected_ai_intros[:20]:
                path = item.get('path', '')
                duration = self._probe_media_duration_for_check(path)
                if duration <= 0:
                    bad_ai_intros.append(os.path.basename(path))
                elif duration < 5.0 or duration > 30.0:
                    unusual_ai_intros.append(f"{os.path.basename(path)}({duration:.1f}s)")
            if bad_ai_intros:
                errors.append("部分AI开场视频无法读取：" + "、".join(bad_ai_intros[:5]))
            if unusual_ai_intros:
                warnings.append("部分AI开场时长不在常见的5～30秒范围：" + "、".join(unusual_ai_intros[:5]))

        if self.min_clip_spin.value() > self.max_clip_spin.value():
            errors.append("最小裁剪秒数不能大于最大裁剪秒数")
        if use_first_track_audio and self.overlay_gap_min_spin.value() > self.overlay_gap_max_spin.value():
            errors.append("覆盖间隔的最小秒数不能大于最大秒数")
        if self.trans_combo.currentText() != "不使用转场" and self.trans_duration.value() >= self.min_clip_spin.value():
            warnings.append("转场时长接近或超过最短片段，可能导致转场失败，建议降低转场时长")

        bad_files = []
        short_videos = []
        for item in selected_vids[:12]:
            path = item.get('path', '')
            if not os.path.exists(path):
                bad_files.append(os.path.basename(path))
                continue
            duration = self._probe_media_duration_for_check(path)
            if duration <= 0:
                bad_files.append(os.path.basename(path))
            elif duration < max(1.2, self.min_clip_spin.value() + 0.8):
                short_videos.append(f"{os.path.basename(path)}({duration:.1f}s)")
        if bad_files:
            errors.append("部分视频无法读取：" + "、".join(bad_files[:5]))
        if short_videos:
            warnings.append("部分视频过短，可能被跳过：" + "、".join(short_videos[:5]))

        bad_audios = []
        total_audio_seconds = 0.0
        duration_sources = selected_first_track if use_first_track_audio else selected_audios
        for item in duration_sources[:20]:
            path = item.get('path', '')
            duration = self._probe_media_duration_for_check(path)
            if duration <= 0:
                bad_audios.append(os.path.basename(path))
            total_audio_seconds += duration
        if bad_audios:
            label = "第一轨道音频" if use_first_track_audio else "音频"
            errors.append(f"部分{label}无法读取：" + "、".join(bad_audios[:5]))

        if self.sub_enable_chk.isChecked():
            sensevoice_dir = os.path.abspath(os.path.join(models_dir(), 'SenseVoiceSmall'))
            whisper_exists = any(name.endswith('.bin') for name in os.listdir(tools_dir())) if os.path.isdir(tools_dir()) else False
            if self.sub_word_timestamps_chk.isChecked() and whisper_exists:
                ok.append("智能字幕：精准词级时间轴已启用")
            elif self.sub_word_timestamps_chk.isChecked():
                warnings.append("精准词级时间轴缺少 Whisper 模型，将自动回退普通字幕")
            if os.path.exists(os.path.join(sensevoice_dir, 'model.pt')):
                ok.append("智能字幕：SenseVoiceSmall 可用")
            elif whisper_exists:
                warnings.append("智能字幕将回退 Whisper，速度可能较慢")
            else:
                errors.append("已开启智能字幕，但未找到 SenseVoiceSmall 或 Whisper 模型")

        try:
            out_dir = self._mixer_output_dir()
            test_path = os.path.join(out_dir, ".write_test")
            with open(test_path, "w", encoding="utf-8") as f:
                f.write("ok")
            os.remove(test_path)
            ok.append(f"输出目录可写：{out_dir}")
        except Exception as exc:
            errors.append(f"输出目录不可写：{exc}")

        drive_count = len(selected_first_track) if use_first_track_audio else len(selected_audios)
        task_count = drive_count * max(1, self.loop_spin.value())
        estimate = ""
        if total_audio_seconds > 0 and task_count > 0:
            estimate = f"预计生成 {task_count} 条，音频总时长约 {total_audio_seconds * max(1, self.loop_spin.value()) / 60:.1f} 分钟"
        else:
            estimate = f"预计生成 {task_count} 条"
        source_label = (
            "第一轨道原声"
            if use_first_track_audio else
            ("普通拼接音频素材" if ordinary_mode else "音频素材")
        )
        structure_label = "普通随机拼接" if ordinary_mode else ("强结构开启" if strong_structure else "常规结构")
        opening_label = (
            f"AI完整开场 {len(selected_ai_intros)} 条，钩子 {len(selected_hooks)}"
            if ai_intro_mode else
            f"钩子 {len(selected_hooks)}"
        )
        summary = f"任务概览：{source_label}驱动，{estimate}；{structure_label}；{opening_label}，视频 {len(selected_vids)}，封面 {len(selected_covers)}，BGM {len(selected_bgms)}；输出 {self._mixer_output_dir()}"
        if hasattr(self, 'mixer_summary_label'):
            self.mixer_summary_label.setText(summary)

        lines = ["【开始前检查】", summary, ""]
        if ok:
            lines.extend(["通过："] + [f"  √ {item}" for item in ok])
        if warnings:
            lines.extend(["", "提醒："] + [f"  ! {item}" for item in warnings])
        if errors:
            lines.extend(["", "需要处理："] + [f"  × {item}" for item in errors])
        report = "\n".join(lines)
        self.update_log(report.replace("\n", " | "))
        if show_dialog:
            if errors:
                QMessageBox.warning(self, "开始前检查未通过", report)
            elif warnings:
                QMessageBox.information(self, "开始前检查有提醒", report)
            else:
                QMessageBox.information(self, "开始前检查通过", report)
        return not errors, report

    def start_preview_synthesis(self):
        self.start_synthesis(preview_only=True)

    def _show_task_prepare_error(self, title, message):
        self.update_log(f"[任务控制] {message.replace(chr(10), ' | ')}")
        self._write_mixer_start_trace(message)
        QMessageBox.warning(self, title, message)

    def prepare_synthesis_task(self, preview_only=False, for_queue=False):
        resolved_tools = os.path.abspath(tools_dir())
        ffmpeg = os.path.join(resolved_tools, "ffmpeg.exe")
        ffprobe = os.path.join(resolved_tools, "ffprobe.exe")
        if not os.path.isfile(ffmpeg) or not os.path.isfile(ffprobe):
            ffmpeg_state = "存在" if os.path.isfile(ffmpeg) else "缺失"
            ffprobe_state = "存在" if os.path.isfile(ffprobe) else "缺失"
            searched = "\n".join(f"  - {path}" for path in resource_root_candidates())
            message = (
                "未找到完整的 FFmpeg 工具，任务尚未启动。\n\n"
                f"软件目录：{APP_ROOT}\n"
                f"tools 解析结果：{resolved_tools}\n"
                f"ffmpeg.exe：{ffmpeg_state}\n"
                f"ffprobe.exe：{ffprobe_state}\n\n"
                f"已检查以下资源包位置：\n{searched}\n\n"
                "请确认主程序 EXE 与“网盘资源包”处于同一层，并检查网盘资源包\\tools。"
            )
            self._show_task_prepare_error("FFmpeg 工具缺失", message)
            return None

        if for_queue:
            passed, report = self.run_mixer_preflight(show_dialog=False)
            if not passed:
                QMessageBox.warning(self, "加入队列前检查未通过", report)
                return None

        # 合成前确保字体字典非空，不在这里触发全量字体扫描。
        if not self.sys_fonts:
            self.sys_fonts = {
                "微软雅黑 (默认)": "C:/Windows/Fonts/msyh.ttc",
                "黑体": "C:/Windows/Fonts/simhei.ttf",
                "宋体": "C:/Windows/Fonts/simsun.ttc",
                "楷体": "C:/Windows/Fonts/simkai.ttf",
            }
        selected_audios = self.get_selected_mixer_assets('音频素材')
        selected_first_track = self.get_selected_mixer_assets('第一轨道')
        selected_second_segment = self.get_selected_mixer_assets('第二段指定素材')
        selected_vids = self.get_selected_mixer_assets('视频素材')
        selected_ai_intros = self.get_selected_mixer_assets('AI开场视频')
        selected_hooks = self.get_selected_mixer_assets('钩子素材')
        use_first_track_audio = self.is_first_track_mixer_mode()
        ai_intro_mode = self.is_ai_intro_mixer_mode()
        ordinary_mode = self.is_ordinary_mixer_mode()
        self.update_log(
            f"[任务控制] 已读取素材：音频 {len(selected_audios)}，第一轨道 {len(selected_first_track)}，"
            f"第二段指定 {len(selected_second_segment)}，视频 {len(selected_vids)}，"
            f"AI开场 {len(selected_ai_intros)}，钩子 {len(selected_hooks)}。"
        )
        if use_first_track_audio:
            if not selected_first_track or not selected_vids:
                self._show_task_prepare_error(
                    "素材缺失",
                    "数字人口播模式请至少勾选：第一轨道、视频素材。",
                )
                return None
            selected_audios = [
                {
                    'path': item['path'],
                    'row': -1,
                    'first_track_path': item['path'],
                    'track_index': index,
                }
                for index, item in enumerate(selected_first_track)
            ]
            selected_first_track_for_task = selected_first_track
            if ai_intro_mode and not selected_ai_intros:
                self._show_task_prepare_error(
                    "素材缺失",
                    "AI完整开场模式请至少勾选一条AI开场视频。",
                )
                return None
        elif self.is_ai_intro_audio_mixer_mode():
            if not selected_audios or not selected_vids:
                self._show_task_prepare_error(
                    "素材缺失",
                    "AI开场+音频拼接模式请至少勾选：音频素材、视频素材。",
                )
                return None
            if not selected_ai_intros:
                self._show_task_prepare_error(
                    "素材缺失",
                    "AI开场+音频拼接模式请至少勾选一条AI开场视频。",
                )
                return None
            selected_first_track_for_task = []
        elif ordinary_mode:
            if not selected_audios or not selected_vids:
                self._show_task_prepare_error(
                    "素材缺失",
                    "普通视频拼接模式请至少勾选：音频素材、视频素材。",
                )
                return None
            selected_first_track_for_task = []
        elif not selected_audios or not selected_vids:
            self._show_task_prepare_error(
                "素材缺失",
                "传统音频混剪模式请至少勾选：音频素材、视频素材。",
            )
            return None
        else:
            selected_first_track_for_task = []

        if self.overlay_zoom_min_spin.value() > self.overlay_zoom_max_spin.value():
            self._show_task_prepare_error(
                "参数错误",
                "覆盖素材放大倍率的最小值不能大于最大值。",
            )
            return None
        if (
            use_first_track_audio
            and self.overlay_gap_min_spin.value() > self.overlay_gap_max_spin.value()
        ):
            self._show_task_prepare_error(
                "参数错误",
                "覆盖间隔的最小秒数不能大于最大秒数。",
            )
            return None

        if self.auto_perf_chk.isChecked():
            profile = getattr(self, "performance_profile", {}) or {
                "gpu_mode": self.gpu_combo.currentText(),
                "threads": self.thread_spin.value(),
                "summary": "使用当前界面中的硬件配置，后台引擎将再次验证",
                "details": [],
            }
            detail = "；".join(profile.get("details", [])) or profile.get("summary", "")
            self.update_log(
                f"[性能检测] 使用启动时缓存结果："
                f"{profile.get('gpu_mode', self.gpu_combo.currentText())} / "
                f"{profile.get('threads', self.thread_spin.value())} 线程。{detail}"
            )
        else:
            self.update_log(
                f"[性能检测] 使用手动选择：{self.gpu_combo.currentText()} / "
                f"{self.thread_spin.value()} 线程。"
            )
        if preview_only:
            selected_audios = selected_audios[:1]
        if not use_first_track_audio and not for_queue:
            for audio in selected_audios:
                self.tables['音频素材'].setItem(
                    audio['row'],
                    3,
                    QTableWidgetItem("排队中..."),
                )
        self.save_configuration(silent=True)

        task_data = {
            'audios': copy.deepcopy(selected_audios),
            'videos': copy.deepcopy(selected_vids),
            'second_segment_videos': copy.deepcopy(
                selected_second_segment if not use_first_track_audio else []
            ),
            'ai_intro_videos': copy.deepcopy(selected_ai_intros if ai_intro_mode else []),
            'ai_intro_mode': ai_intro_mode,
            'mixer_mode': self.mixer_mode_combo.currentData(),
            'hook_videos': copy.deepcopy(selected_hooks),
            'covers': copy.deepcopy(self.get_selected_mixer_assets('封面图片')),
            'bgms': copy.deepcopy(self.get_selected_mixer_assets('背景音乐')),
            'first_track_videos': copy.deepcopy(selected_first_track_for_task),
            'watermarks': copy.deepcopy(self.watermarks_data),
            'duration_cache': self.duration_cache,
            'asset_scope': os.path.abspath(self.tab_folders.get('视频素材', '') or ''),
            'min_clip': self.min_clip_spin.value(),
            'max_clip': self.max_clip_spin.value(),
            'strong_structure': self.strong_structure_chk.isChecked() and use_first_track_audio,
            'hook_duration': self.hook_duration_spin.value(),
            'strong_avatar_duration': self.strong_avatar_duration_spin.value(),
            'strong_avatar_smart_tail': self.strong_avatar_smart_tail_chk.isChecked(),
            'strong_product_duration': self.strong_product_duration_spin.value(),
            'strong_avatar_count': self.strong_avatar_count_spin.value(),
            'clip_rhythm': self.clip_rhythm_combo.currentData(),
            'clip_jitter': self.clip_jitter_spin.value(),
            'overlay_gap_min': self.overlay_gap_min_spin.value(),
            'overlay_gap_max': self.overlay_gap_max_spin.value(),
            'overlay_offset_strength': self.overlay_offset_spin.value(),
            'overlay_zoom_mode': self.overlay_zoom_mode_combo.currentData(),
            'overlay_zoom_min': self.overlay_zoom_min_spin.value(),
            'overlay_zoom_max': self.overlay_zoom_max_spin.value(),
            'use_first_track_audio': use_first_track_audio,
            'trans_type': self.trans_combo.currentText(),
            'trans_dur': self.trans_duration.value(),
            'lut_style': self.lut_combo.currentData(),
            'main_vol': self.main_vol_spin.value(),
            'bgm_vol': self.bgm_vol_spin.value(),
            'smart_sfx_enabled': self.smart_sfx_chk.isChecked(),
            'smart_sfx_strength': self.smart_sfx_strength_combo.currentData(),
            'resolution': self.res_combo.currentText(),
            'gpu_mode': self.gpu_combo.currentText(),
            'threads': 1 if preview_only else self.thread_spin.value(),
            'loop_count': 1 if preview_only else self.loop_spin.value(),
            'cache_dir': os.path.abspath(self.cache_dir),
            'output_dir': self._mixer_output_dir(),
            'enable_standard_video_cache': True,
            'anti_dedup': self.anti_dedup_chk.isChecked(),
            'preview_only': preview_only,
            'sys_fonts': copy.deepcopy(self.sys_fonts),
            'sub_config': {
                'enable': self.sub_enable_chk.isChecked(),
                'word_timestamps': self.sub_word_timestamps_chk.isChecked(),
                'font': self.sub_font.currentText(),
                'size': self.sub_size.value(),
                'color': self.get_hex_from_combo(self.sub_color),
                'border_w': self.sub_border_w.value(),
                'border_color': self.get_hex_from_combo(self.sub_border_color),
                'anim': self.sub_anim.currentText(),
                'margin_v': self.sub_margin_v.value(),
                # 花字预设扩展：shadow/bold（引擎 ASS 生成支持）
                'shadow': int(getattr(self, '_current_sub_style_preset', {}).get('shadow', 0) or 0),
                'bold': int(-1 if getattr(self, '_current_sub_style_preset', {}).get('bold', True) else 0),
            },
            'cover_config': {
                'text': self.cover_text_input.text().strip(),
                'font': self.cover_font.currentText(),
                'style': self.cover_style.currentText(),
            },
        }
        expected = len(selected_audios) * int(task_data['loop_count'])
        return task_data, expected

    def launch_synthesis_task(
        self,
        task_data,
        expected,
        preview_only=False,
        queue_task_id="",
        initial_completed=0,
    ):
        self._completed_mix_tasks = max(0, int(initial_completed or 0))
        self._expected_mix_tasks = max(1, int(expected or 0))
        self.task_progress.setRange(0, self._expected_mix_tasks)
        self.task_progress.setValue(
            min(self._completed_mix_tasks, self._expected_mix_tasks)
        )
        self.task_progress.setFormat(
            f"已完成 {self._completed_mix_tasks}/{self._expected_mix_tasks}，正在渲染"
        )
        self.start_btn.setEnabled(False)
        if queue_task_id:
            self.start_btn.setText("产品队列执行中...")
        else:
            self.start_btn.setText(
                "正在生成预览样片..." if preview_only else "合成阵列执行中..."
            )
            # 直接执行会把进度写回当前素材表，运行期间不允许切换产品。
            if hasattr(self, 'batch_project_combo'):
                self.batch_project_combo.setEnabled(False)
        if hasattr(self, 'preview_sample_btn'):
            self.preview_sample_btn.setEnabled(False)
        engine = SynthesisEngine(task_data)
        self.engine_thread = engine
        engine.log_signal.connect(self.update_log, Qt.QueuedConnection)
        engine.task_done_signal.connect(self.on_task_done, Qt.QueuedConnection)
        if queue_task_id and hasattr(self, 'update_active_queue_status'):
            engine.status_update_signal.connect(
                lambda _row, text, _color: self.update_active_queue_status(text),
                Qt.QueuedConnection,
            )
        else:
            engine.status_update_signal.connect(
                self.update_table_status,
                Qt.QueuedConnection,
            )
        # 使用 QThread 自带 finished，确保旧线程真正退出后再启动队列下一项。
        engine.finished.connect(
            lambda qid=queue_task_id: self.on_synthesis_finished(qid),
            Qt.QueuedConnection,
        )
        self._write_mixer_start_trace(
            f"即将启动后台线程：任务 {self._expected_mix_tasks} 条，"
            f"GPU {task_data.get('gpu_mode', '')}，并发 {task_data.get('threads', 1)}"
        )
        engine.start()
        self.update_log("[任务控制] 后台合成线程已启动。")
        self._write_mixer_start_trace("后台合成线程 start() 已调用")

    def start_synthesis(self, preview_only=False):
        try:
            engine = getattr(self, 'engine_thread', None)
            if engine is not None and engine.isRunning():
                self.update_log("[任务控制] 已有混剪任务正在运行，本次点击已忽略。")
                return
            start_label = "预览样片" if preview_only else "正式混剪任务"
            self._write_mixer_start_trace(
                f"开始按钮已触发：{start_label}",
                reset=True,
            )
            self.update_log(f"[任务控制] 开始按钮已触发，正在准备{start_label}。")
            self.start_btn.setEnabled(False)
            self.start_btn.setText("正在检查任务...")
            if hasattr(self, 'preview_sample_btn'):
                self.preview_sample_btn.setEnabled(False)
            self.start_btn.repaint()
            QApplication.processEvents()
            prepared = self.prepare_synthesis_task(
                preview_only=preview_only,
                for_queue=False,
            )
            if not prepared:
                self._restore_mix_start_controls()
                return
            task_data, expected = prepared
            self.launch_synthesis_task(
                task_data,
                expected,
                preview_only=preview_only,
            )
        except Exception as exc:
            import traceback
            error_text = traceback.format_exc()
            print(error_text, flush=True)
            self.update_log(f"[任务控制] 合成启动失败：{exc}")
            self._write_mixer_start_trace(f"合成启动失败：{exc}\n{error_text}")
            try:
                self.write_feedback_report(extra_error=error_text)
            except Exception:
                pass
            self._restore_mix_start_controls()
            QMessageBox.critical(
                self,
                "合成启动失败",
                f"开始执行任务时发生错误：\n\n{str(exc)}\n\n"
                "请点击【复制反馈日志】发给开发者排查。",
            )
