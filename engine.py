import warnings
warnings.filterwarnings("ignore", module="zhconv")

import os
import subprocess
import random
import shutil
import time
import re  
import ctypes
import multiprocessing 
from concurrent.futures import ThreadPoolExecutor, as_completed
from PyQt5.QtCore import QThread, pyqtSignal
from services.asr_service import sensevoice_model_ready, transcribe_with_sensevoice, write_srt_from_text
from utils import path_to_ffmpeg, detect_performance_profile, ANIMATIONS, TRANS_MAP
from paths import models_dir, tools_dir

try:
    import zhconv
    HAS_ZHCONV = True
except ImportError:
    HAS_ZHCONV = False

class SynthesisEngine(QThread):
    log_signal = pyqtSignal(str)
    finished_signal = pyqtSignal()
    task_done_signal = pyqtSignal() 
    status_update_signal = pyqtSignal(int, str, str) 

    def __init__(self, task_data):
        super().__init__()
        self.task_data = task_data
        self.tools_dir = tools_dir()
        self.ffmpeg_exe = os.path.join(self.tools_dir, 'ffmpeg.exe')
        self.ffprobe_exe = os.path.join(self.tools_dir, 'ffprobe.exe')
        self.sensevoice_model_dir = os.path.join(models_dir(), 'SenseVoiceSmall')
        self.is_running = True
        
        total_cores = multiprocessing.cpu_count()
        self.optimal_threads = str(max(4, total_cores - 2)) 

    def stop(self):
        self.is_running = False

    def _get_short_path(self, path):
        if os.name == 'nt':
            try:
                buf_size = ctypes.windll.kernel32.GetShortPathNameW(path, None, 0)
                if buf_size > 0:
                    buf = ctypes.create_unicode_buffer(buf_size)
                    ctypes.windll.kernel32.GetShortPathNameW(path, buf, buf_size)
                    return buf.value
            except: pass
        return path

    def _get_whisper_exe(self, gpu_mode):
        if "NVENC" in gpu_mode:
            target = os.path.join(self.tools_dir, "nv", "whisper.exe")
        elif "AMF" in gpu_mode:
            target = os.path.join(self.tools_dir, "amd", "whisper.exe")
        else:
            target = os.path.join(self.tools_dir, "cpu", "whisper.exe")
            
        if os.path.exists(target): return target
        
        for fallback in ["whisper_cpu.exe", "whisper_nv.exe", "whisper_amd.exe", "whisper.exe"]:
            fallback_path = os.path.join(self.tools_dir, fallback)
            if os.path.exists(fallback_path): return fallback_path
            
        return None

    def check_env(self):
        if not os.path.exists(self.ffmpeg_exe) or not os.path.exists(self.ffprobe_exe):
            self.log_signal.emit("[致命错误] ❌ 未在 tools 目录下找到 ffmpeg.exe 或 ffprobe.exe！")
            return False

        profile = detect_performance_profile(self.tools_dir)
        selected_gpu = self.task_data.get('gpu_mode', 'CPU 软解')
        if "NVENC" in selected_gpu and profile.get('level') != 'nvenc':
            self.log_signal.emit(f"[性能兜底] 当前环境无法稳定启用 NVENC，已切换为 {profile['gpu_mode']}。")
            self.task_data['gpu_mode'] = profile['gpu_mode']
            self.task_data['threads'] = profile['threads']
        elif "AMF" in selected_gpu and profile.get('level') != 'amf':
            self.log_signal.emit(f"[性能兜底] 当前环境无法稳定启用 AMF，已切换为 {profile['gpu_mode']}。")
            self.task_data['gpu_mode'] = profile['gpu_mode']
            self.task_data['threads'] = profile['threads']
            
        if self.task_data.get('sub_config', {}).get('enable'):
            if sensevoice_model_ready(self.sensevoice_model_dir):
                return True
            whisper_exe = self._get_whisper_exe(self.task_data.get('gpu_mode', 'CPU'))
            models = [f for f in os.listdir(self.tools_dir) if f.endswith('.bin')]
            if not whisper_exe:
                self.log_signal.emit("[AI 警告] ⚠️ 未找到 SenseVoiceSmall 或 whisper.exe，无法生成智能字幕。")
                return False
            if not models:
                self.log_signal.emit("[AI 警告] ⚠️ 未找到 SenseVoiceSmall 或 Whisper .bin 模型，无法生成智能字幕。")
                return False
                
        return True

    def run_ffmpeg(self, cmd, cwd=None):
        try:
            # 🚀 关闭 X 光机：直接使用原版命令，遇到非致命警告不再大惊小怪
            res = subprocess.run(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, 
                                 text=True, encoding='utf-8', errors='ignore', startupinfo=self._get_si())
            if res.returncode != 0:
                out_text = res.stdout if res.stdout else ""
                err_lines = [line for line in out_text.split('\n') if line.strip() and "Fontconfig" not in line]
                err_msg = " | ".join(err_lines[-5:]) if err_lines else "底层警告(已触发安全兜底机制)"
                # 改为更温和的警告提示
                self.log_signal.emit(f"⚠️ 节点重试: {err_msg}")
                return False
            return True
        except Exception as e:
            self.log_signal.emit(f"❌ 进程异常: {e}")
            return False

    def get_duration(self, file_path):
        cache = self.task_data['duration_cache']
        if file_path in cache: return cache[file_path]
        try:
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            cmd = [self.ffprobe_exe, '-v', 'error', '-show_entries', 
                   'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1', file_path]
            result = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='ignore', startupinfo=startupinfo)
            dur = float(result.stdout.strip())
            cache[file_path] = dur 
            return dur
        except Exception: return 0.0

    def generate_watermark_filter(self, resolved_watermarks, cache_dir, unique_id, time_offset=0.0, is_first_clip=False):
        if not resolved_watermarks: return ""
        wm_str = ""
        t_expr = f"(t+{time_offset:.3f})"
        delay_filter = "enable='gt(t\\, 0.2)':" if is_first_clip else ""
        
        for idx, r_wm in enumerate(resolved_watermarks):
            wm = r_wm['original']
            lines = str(wm['text']).split('\n')
            safe_font_path = path_to_ffmpeg(r_wm['actual_font_path'])
            size = wm['size']; align = wm['align']
            color = r_wm['actual_color']
            border_w = wm.get('border_w', 0); border_color = r_wm['actual_border_color']; is_bold = wm.get('is_bold', False)
            
            offset_x = r_wm['offset_x']; offset_y = r_wm['offset_y']; anim_key = r_wm['anim_key']
            base_x = wm['x'] + offset_x; base_y = wm['y'] + offset_y
            x_tpl, y_tpl, alpha_tpl = ANIMATIONS.get(anim_key, ANIMATIONS["静态展示"])
            
            for i, line in enumerate(lines):
                if not line.strip(): continue 
                txt_file = os.path.join(cache_dir, f"wm_{unique_id}_{idx}_l{i}.txt")
                with open(txt_file, 'w', encoding='utf-8') as f: f.write(line)
                safe_txt_file = path_to_ffmpeg(txt_file)
                
                line_spacing = i * size * 1.3; line_y_str = f"({base_y}) + {line_spacing}"
                if align == "居中对齐": x_base_str = f"({base_x}) - tw/2"
                elif align == "右对齐": x_base_str = f"({base_x}) - tw"
                else: x_base_str = f"({base_x})"
                    
                x_expr = x_tpl.replace("(base_x)", x_base_str).replace("(offset_x)", str(offset_x)).replace("TIME_VAR", t_expr)
                y_expr = y_tpl.replace("(line_y)", line_y_str).replace("(offset_y)", f"({offset_y} + {line_spacing})").replace("TIME_VAR", t_expr)
                alpha = alpha_tpl.replace("TIME_VAR", t_expr)
                
                font_cmd = f"fontfile='{safe_font_path}':" if safe_font_path else ""
                actual_bw = border_w; actual_bc = border_color
                if is_bold and border_w == 0: actual_bw = 2; actual_bc = color 
                elif is_bold and border_w > 0: actual_bw = border_w + 1 
                border_cmd = f"borderw={actual_bw}:bordercolor={actual_bc}:" if actual_bw > 0 else ""
                wm_str += f",drawtext={delay_filter}{font_cmd}textfile='{safe_txt_file}':fontsize={size}:fontcolor={color}:{border_cmd}x={x_expr}:y={y_expr}:alpha='{alpha}'"
        return wm_str

    def process_asr_and_color(self, audio_path, cache_dir, audio_name, sub_config, sys_fonts, has_cover, gpu_mode):
        ass_out_path = os.path.join(cache_dir, f"{audio_name}.ass")
        if os.path.exists(ass_out_path): return ass_out_path 
        
        engine_type = "N卡加速" if "NVENC" in gpu_mode else ("A卡加速" if "AMF" in gpu_mode else "CPU满载")
        self.log_signal.emit(f"🎧 [AI 引擎] 正在开启 SenseVoice 本地识别生成字幕...")
        if HAS_ZHCONV: self.log_signal.emit("✨ 已启用 zhconv 引擎，确保 100% 输出纯正简体中文。")
        
        temp_wav_path = ""
        temp_srt_path = os.path.join(cache_dir, f"sensevoice_{random.randint(10000, 99999)}.srt")
        actual_srt = None

        if sensevoice_model_ready(self.sensevoice_model_dir):
            try:
                text = transcribe_with_sensevoice(audio_path, self.sensevoice_model_dir)
                audio_dur = self.get_duration(audio_path)
                write_srt_from_text(text, audio_dur, temp_srt_path, max_chars=sub_config.get('max_len', 14))
                actual_srt = temp_srt_path
                self.log_signal.emit("✅ SenseVoice 字幕识别完成。")
            except Exception as e:
                self.log_signal.emit(f"⚠️ SenseVoice 识别失败，尝试 Whisper 回退: {e}")

        if not actual_srt:
            whisper_exe = self._get_whisper_exe(gpu_mode)
            if not whisper_exe: return None
            models = [f for f in os.listdir(self.tools_dir) if f.endswith('.bin')]
            if not models: return None
            model_filename = models[0]

            self.log_signal.emit(f"🎧 [AI 引擎] 正在回退 Whisper {engine_type} 听写...")
            temp_wav_name = f"temp_whisper_{random.randint(10000, 99999)}.wav"
            temp_wav_path = os.path.join(self.tools_dir, temp_wav_name)
            temp_srt_path = temp_wav_path + ".srt"

            cmd_wav = [self.ffmpeg_exe, '-y', '-hide_banner', '-loglevel', 'error', '-i', audio_path, '-ar', '16000', '-ac', '1', '-c:a', 'pcm_s16le', temp_wav_path]
            subprocess.run(cmd_wav, startupinfo=self._get_si())

            if not os.path.exists(temp_wav_path):
                self.log_signal.emit("❌ 提取音频给 AI 失败！")
                return None

            cmd_whisper = [
                whisper_exe, '-m', model_filename, '-f', temp_wav_name,
                '-osrt', '-l', 'zh', '--prompt', '简体中文',
                '-t', self.optimal_threads,
                '-ml', '16'
            ]
            res = subprocess.run(cmd_whisper, cwd=self.tools_dir, startupinfo=self._get_si(), capture_output=True, text=True, encoding='utf-8', errors='ignore')

            if res.returncode != 0:
                err_text = res.stderr.strip() if res.stderr else res.stdout.strip()
                err_line = " | ".join(err_text.split('\n')[-3:]) if err_text else "程序闪退(请检查是否放置了对应的硬件版 whisper 及其 dll)"
                self.log_signal.emit(f"❌ Whisper 崩溃: {err_line}")

            actual_srt = temp_srt_path if os.path.exists(temp_srt_path) else None
        
        if not actual_srt or os.path.getsize(actual_srt) == 0: 
            self.log_signal.emit("⚠️ 未能生成有效字幕，跳过字幕压制。")
            if os.path.exists(temp_wav_path): os.remove(temp_wav_path)
            return None 

        def hex_to_ass(h):
            h = h.strip('#')
            if len(h) != 6: return "&H00FFFFFF&"
            return f"&H00{h[4:6]}{h[2:4]}{h[0:2]}&"

        vibrant_colors = ["#00FFFF", "#FFFF00", "#00FF00", "#FF00FF", "#00A5FF", "#FFFFFF", "#FF5722", "#E91E63"]
        actual_color = random.choice(vibrant_colors) if sub_config['color'] == "🎲 随机颜色" else sub_config['color']
        actual_border_color = random.choice(["#000000", "#FFFFFF", "#FF0000"]) if sub_config['border_color'] == "🎲 随机颜色" else sub_config['border_color']
        
        ass_c = hex_to_ass(actual_color); ass_bc = hex_to_ass(actual_border_color)
        font_name = sub_config['font']
        if font_name == "🎲 随机系统字体": font_name = random.choice(list(sys_fonts.keys())) if sys_fonts else "Arial"

        anim_key = sub_config['anim']
        anims = {
            "无动效": "", "柔和淡入淡出": "{\\fad(200,200)}",
            "字号弹性弹出": "{\\fscx80\\fscy80\\t(0,250,\\fscx115\\fscy115)\\t(250,500,\\fscx100\\fscy100)}",
            "持续缓慢放大": "{\\t(0,2000,\\fscx115\\fscy115)}",
            "字间距呼吸展宽": "{\\fsp-3\\t(0,1000,\\fsp2)}",
            "Y轴3D翻转入场": "{\\fry90\\t(0,400,\\fry0)}",
            "X轴3D翻转入场": "{\\frx90\\t(0,400,\\frx0)}",
            "微倾斜摇摆": "{\\frz-5\\t(0,200,\\frz5)\\t(200,400,\\frz0)}",
            "高斯模糊聚焦入场": "{\\blur10\\t(0,300,\\blur0)}",
            "颜色渐变(白转青)": "{\\c&HFFFFFF&\\t(0,500,\\c&HFFFF00&)}",
            "描边粗细脉冲": "{\\bord0\\t(0,300,\\bord8)\\t(300,600,\\bord4)}",
            "字符压扁复原": "{\\fscy200\\t(0,300,\\fscy100)}",
            "幽灵漂浮显影": "{\\alpha&HFF&\\fsp10\\t(0,500,\\alpha&H00&\\fsp0)}",
            "暴击震动": "{\\frz20\\fscx130\\fscy130\\t(0,100,\\frz-15\\fscx80\\fscy80)\\t(100,200,\\frz0\\fscx100\\fscy100)}"
        }
        if anim_key == "🎲 随机动效": anim_key = random.choice(list(anims.keys())[1:])
        anim_str = anims.get(anim_key, "")
        
        margin_v = sub_config.get('margin_v', 180); margin_side = 80 ; max_len = sub_config.get('max_len', 14)

        header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{font_name},{sub_config['size']},{ass_c},&H000000FF,{ass_bc},&H00000000,-1,0,0,0,100,100,0,0,1,{sub_config['border_w']},0,2,{margin_side},{margin_side},{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
        t2s = str.maketrans("說這們麼會個裡後為來對過去發機與樣學當覺點實種聲網著經視聽畫貝車買賣嗎無體國學問網動萬裡東風", "说这们么会个里后为来对过去发机与样学当觉点实种声网着经视听画贝车买卖吗无体国学问网动万里东风")
        
        try:
            with open(actual_srt, 'r', encoding='utf-8') as f: lines = f.readlines()
            events = []; i = 0
            while i < len(lines):
                line = lines[i].strip()
                if '-->' in line:
                    times = line.split('-->')
                    def fix_t(t):
                        h, m, s = t.strip().replace(',', '.').split(':')
                        total_sec = int(h) * 3600 + int(m) * 60 + float(s)
                        if has_cover: total_sec += 0.1 
                        new_h = int(total_sec // 3600); new_m = int((total_sec % 3600) // 60); new_s = total_sec % 60
                        return f"{new_h:01d}:{new_m:02d}:{new_s:05.2f}"
                        
                    start = fix_t(times[0]); end = fix_t(times[1]); i += 1
                    text_lines = []
                    while i < len(lines) and lines[i].strip() != "":
                        clean_text = lines[i].strip()
                        if not (clean_text.startswith('[') and clean_text.endswith(']')):
                            if HAS_ZHCONV: clean_text = zhconv.convert(clean_text, 'zh-cn')
                            else: clean_text = clean_text.translate(t2s)
                            
                            clean_text = clean_text.strip("。，、？！. ,?!\n\t")
                            if len(clean_text) == 0 or len(clean_text) > 35:
                                i += 1; continue
                            if "字幕" in clean_text or "翻译" in clean_text or "未经允许" in clean_text:
                                i += 1; continue
                            
                            if len(clean_text) > max_len:
                                half = (len(clean_text) + 1) // 2
                                clean_text = clean_text[:half] + r"\N" + clean_text[half:]
                            text_lines.append(clean_text)
                        i += 1
                    text = r"\N".join(text_lines)
                    if text: events.append(f"Dialogue: 0,{start},{end},Default,,0,0,0,,{anim_str}{text}")
                i += 1
            with open(ass_out_path, 'w', encoding='utf-8') as f: f.write(header + "\n".join(events) + "\n")
            
            if os.path.exists(temp_wav_path): os.remove(temp_wav_path)
            if os.path.exists(temp_srt_path): os.remove(temp_srt_path)
            
            return ass_out_path
        except Exception as e:
            self.log_signal.emit(f"⚠️ 字幕处理失败: {str(e)}")
            return None

    def process_single_audio(self, audio_dict, video_pool, bgm_pool, min_clip, max_clip, gpu_mode, 
                             target_res, trans_type, trans_dur, cache_dir, output_dir, covers, watermarks, 
                             round_idx, total_rounds, main_vol, bgm_vol):
        if not self.is_running: return 
                             
        audio_path = audio_dict['path']
        audio_row = audio_dict['row']
        audio_name = os.path.basename(audio_path).split('.')[0]
        
        salt = f"{random.randint(10000, 99999)}"
        unique_id = f"{audio_name}_Row{audio_row}_R{round_idx}_{salt}"
        
        self.log_signal.emit(f"[{unique_id}] ▶ 第 {round_idx} 轮混剪开始...")
        self.status_update_signal.emit(audio_row, "处理中...", "#60A5FA")
        
        final_ass_path = ""
        sub_config = self.task_data.get('sub_config', {})
        if sub_config.get('enable'):
            sys_fonts = self.task_data.get('sys_fonts', {})
            has_cover = bool(covers)
            final_ass_path = self.process_asr_and_color(audio_path, cache_dir, audio_name, sub_config, sys_fonts, has_cover, gpu_mode)
            
        audio_dur = self.get_duration(audio_path)
        use_trans = trans_type != "不使用转场"
        
        if "NVENC" in gpu_mode:
            pix_fmt = "yuv420p"
            enc_args = ['-c:v', 'h264_nvenc', '-preset', 'fast', '-b:v', '5M']
            final_enc_args = ['-c:v', 'h264_nvenc', '-preset', 'fast', '-b:v', '5M', '-profile:v', 'high', '-level', '4.1']
        elif "AMF" in gpu_mode:
            pix_fmt = "nv12" 
            enc_args = ['-c:v', 'h264_amf', '-b:v', '5M'] 
            final_enc_args = ['-c:v', 'h264_amf', '-b:v', '5M']
        else:
            pix_fmt = "yuv420p"
            enc_args = ['-c:v', 'libx264', '-preset', 'fast', '-b:v', '5M']
            final_enc_args = ['-c:v', 'libx264', '-preset', 'fast', '-b:v', '5M', '-profile:v', 'high', '-level', '4.1']
            
        anti_dedup = self.task_data.get('anti_dedup', True)
        seq1_videos = self.task_data.get('seq1_videos', [])
        seq2_videos = self.task_data.get('seq2_videos', [])
        sys_fonts_list = list(self.task_data.get('sys_fonts', {}).values())

        target_w, target_h = target_res.split(':')

        resolved_watermarks = []
        for idx, wm in enumerate(watermarks):
            anim_key = wm['anim']
            if anim_key == "从右向左滚动": anim_key = "➡️ 向左匀速滚动 (速100)"
            elif anim_key == "上下浮动呼吸": anim_key = "🌊 上下平滑呼吸 (幅30_频3)"
            elif anim_key == "透明度闪烁": anim_key = "💡 平滑呼吸闪烁 (频4)"
            if anim_key == "🎲 随机动态特效": anim_key = random.choice([k for k in ANIMATIONS.keys() if k not in ["静态展示", "🎲 随机动态特效"]])
            
            actual_font_path = wm['font_path']
            if wm['font_name'] == "🎲 随机系统字体" and sys_fonts_list: actual_font_path = random.choice(sys_fonts_list)
            actual_color = f"#{random.randint(0, 0xFFFFFF):06x}" if wm['color'] == "🎲 随机颜色" else wm['color']
            actual_border_color = f"#{random.randint(0, 0xFFFFFF):06x}" if wm.get('border_color', 'black') == "🎲 随机颜色" else wm.get('border_color', 'black')

            resolved_watermarks.append({
                'original': wm, 'anim_key': anim_key, 'actual_font_path': actual_font_path,
                'actual_color': actual_color, 'actual_border_color': actual_border_color,
                'offset_x': random.randint(-15, 15), 'offset_y': random.randint(-15, 15)
            })
        
        current_time = 0.0; clip_plan = []
        while current_time < audio_dur:
            if not self.is_running: return
            clip_idx = len(clip_plan)
            if clip_idx == 0 and seq1_videos: valid_vid = random.choice(seq1_videos)['path']
            elif clip_idx == 1 and seq2_videos: valid_vid = random.choice(seq2_videos)['path']
            else: valid_vid = random.choice(video_pool)
                
            raw_dur = self.get_duration(valid_vid); valid_dur = raw_dur - 0.8 
            if valid_dur < 1.0: continue
            actual_max = min(max_clip, valid_dur); actual_min = min(min_clip, actual_max - 0.1) 
            clip_dur = random.uniform(actual_min, actual_max); overlap = trans_dur if (use_trans and len(clip_plan) > 0) else 0.0
            progress = clip_dur - overlap; gap = audio_dur - current_time
            fade_out = False
            if progress >= gap: clip_dur = gap + overlap; fade_out = True
            clip_plan.append({'video': valid_vid, 'start': random.uniform(0, valid_dur - clip_dur), 'duration': clip_dur, 'fade_out': fade_out, 'global_start': current_time })
            current_time += (clip_dur - overlap)

        concat_list = []
        for j, plan in enumerate(clip_plan):
            if not self.is_running: return 
            temp_clip = os.path.join(cache_dir, f"temp_{unique_id}_clip_{j}.ts"); concat_list.append(temp_clip)
            base_vf = f"scale={target_w}:{target_h}:force_original_aspect_ratio=decrease,pad={target_w}:{target_h}:(ow-iw)/2:(oh-ih)/2:color=black,setparams=color_primaries=bt709:color_trc=bt709:colorspace=bt709"
            vid_speed = 1.0; vid_start = plan['start']; vid_dur = plan['duration']
            
            if anti_dedup:
                vid_speed = round(random.uniform(0.95, 1.05), 3)
                base_vf += f",setpts={1.0/vid_speed}*PTS"
                zoom = round(random.uniform(1.01, 1.04), 3)
                base_vf += f",scale=iw*{zoom}:ih*{zoom},crop={target_w}:{target_h}"
                eq_b = round(random.uniform(-0.03, 0.03), 3); eq_c = round(random.uniform(0.97, 1.03), 3); eq_s = round(random.uniform(0.97, 1.03), 3)
                base_vf += f",eq=brightness={eq_b}:contrast={eq_c}:saturation={eq_s}"

            if plan['fade_out'] and not use_trans and plan['duration'] > 1.5: 
                base_vf += f",fade=t=out:st={plan['duration']-0.5:.3f}:d=0.5"
            
            base_vf += f",format={pix_fmt},fps=30,settb=1/90000"
            is_first = (j == 0)
            wm_filter_str = self.generate_watermark_filter(resolved_watermarks, cache_dir, unique_id, time_offset=plan['global_start'], is_first_clip=is_first)
            vf_filter = base_vf + wm_filter_str
            
            cmd = [
                self.ffmpeg_exe, '-y', '-hide_banner', '-loglevel', 'error', 
                '-ss', f"{vid_start:.3f}", '-i', plan['video'], '-t', f"{vid_dur * vid_speed:.3f}", 
                '-vf', vf_filter, *enc_args, '-an', '-r', '30', '-pix_fmt', pix_fmt, temp_clip
            ]
            if not self.run_ffmpeg(cmd, cwd=cache_dir): return

        if not self.is_running: return 
        
        main_v_no_audio = os.path.join(cache_dir, f"main_v_{unique_id}.ts")
        real_trans_map = {k: v for k, v in TRANS_MAP.items() if "不使用" not in k and "随机" not in k}
        real_trans_keys = list(real_trans_map.keys())

        fallback_to_concat = False 
        if use_trans and len(concat_list) > 1:
            filter_complex_final = ""; inputs = []; current_offset = 0.0; last_out = "0:v"
            for clip in concat_list: inputs.extend(['-i', clip])
            for k in range(1, len(concat_list)):
                if not self.is_running: return 
                if "随机" in trans_type and real_trans_keys: actual_trans = real_trans_map[random.choice(real_trans_keys)]
                else: actual_trans = TRANS_MAP.get(trans_type, "fade")
                if actual_trans == "none" or actual_trans == "random": actual_trans = "fade"
                
                real_prev_dur = self.get_duration(concat_list[k-1])
                current_offset += (real_prev_dur - trans_dur)
                in1 = last_out if k == 1 else f"v{k}"; in2 = f"{k}:v"; out = f"v{k+1}"
                filter_complex_final += f"[{in1}][{in2}]xfade=transition={actual_trans}:duration={trans_dur}:offset={current_offset:.3f}[{out}];"
                last_out = out
            filter_complex_final = filter_complex_final.rstrip(';')
            trans_temp_video = os.path.join(cache_dir, f"trans_{unique_id}.ts")
            
            trans_cmd = [self.ffmpeg_exe, '-y', '-hide_banner', '-loglevel', 'error'] + inputs + [
                '-filter_complex', filter_complex_final, '-map', f"[{last_out}]",
                *enc_args, '-an', '-r', '30', '-pix_fmt', pix_fmt, trans_temp_video]
            
            if self.run_ffmpeg(trans_cmd, cwd=cache_dir) and os.path.exists(trans_temp_video) and os.path.getsize(trans_temp_video) > 1024:
                concat_cmd = [self.ffmpeg_exe, '-y', '-hide_banner', '-loglevel', 'error', '-i', trans_temp_video, '-c:v', 'copy', main_v_no_audio]
                if not self.run_ffmpeg(concat_cmd, cwd=cache_dir): fallback_to_concat = True
            else: fallback_to_concat = True
        else: fallback_to_concat = True 

        if not self.is_running: return 

        if fallback_to_concat:
            concat_txt_path = os.path.join(cache_dir, f"concat_{unique_id}.txt")
            with open(concat_txt_path, 'w', encoding='utf-8') as f:
                for clip in concat_list: 
                    safe_clip_path = str(clip).replace('\\', '/')
                    f.write(f"file '{safe_clip_path}'\n")
            concat_cmd = [self.ffmpeg_exe, '-y', '-hide_banner', '-loglevel', 'error', '-f', 'concat', '-safe', '0', '-i', concat_txt_path, '-c:v', 'copy', main_v_no_audio]
            if not self.run_ffmpeg(concat_cmd, cwd=cache_dir): return

        if not self.is_running: return 

        final_v_with_cover = main_v_no_audio
        if covers:
            chosen_cover_path = random.choice(covers)
            cover_v = os.path.join(cache_dir, f"cover_v_{unique_id}.ts")
            
            cover_cfg = self.task_data.get('cover_config', {})
            c_text = cover_cfg.get('text', '').strip()
            c_style = cover_cfg.get('style', '无特效 (仅原图)')
            
            vf_cover_clean = f"scale={target_w}:{target_h}:force_original_aspect_ratio=decrease,pad={target_w}:{target_h}:(ow-iw)/2:(oh-ih)/2:color=black,setparams=color_primaries=bt709:color_trc=bt709:colorspace=bt709,format={pix_fmt},fps=30,settb=1/90000"
            
            if c_text and c_style != "无特效 (仅原图)":
                c_font = cover_cfg.get('font', '🎲 随机系统字体')
                if c_font == "🎲 随机系统字体" and sys_fonts_list: c_font = random.choice(sys_fonts_list)
                safe_f = path_to_ffmpeg(c_font)
                if c_style == "🎲 随机静态风格": c_style = random.choice(["经典白字黑边", "高亮霓虹发光", "综艺黑字黄底", "复古牛皮纸色"])
                    
                if c_style == "经典白字黑边":
                    draw_cmd = f"drawtext=fontfile='{safe_f}':text='{c_text}':fontsize=120:fontcolor=white:x=(w-tw)/2:y=(h-th)/2:borderw=4:bordercolor=black"
                elif c_style == "高亮霓虹发光":
                    draw_cmd = f"drawtext=fontfile='{safe_f}':text='{c_text}':fontsize=130:fontcolor=0x00FFFF:x=(w-tw)/2:y=(h-th)/2:borderw=8:bordercolor=0xFF00FF"
                elif c_style == "综艺黑字黄底":
                    draw_cmd = f"drawtext=fontfile='{safe_f}':text='{c_text}':fontsize=100:fontcolor=black:x=(w-tw)/2:y=(h-th)/2+200:box=1:boxcolor=yellow@0.9:boxborderw=15:shadowx=6:shadowy=6:shadowcolor=red"
                elif c_style == "复古牛皮纸色":
                    draw_cmd = f"drawtext=fontfile='{safe_f}':text='{c_text}':fontsize=140:fontcolor=0xD2B48C:x=(w-tw)/2:y=(h-th)/2:borderw=5:bordercolor=0x5D4037"
                else: draw_cmd = ""
                if draw_cmd: vf_cover_clean += f",{draw_cmd}"

            cover_cmd = [self.ffmpeg_exe, '-y', '-hide_banner', '-loglevel', 'error', '-loop', '1', '-framerate', '30', '-i', chosen_cover_path, '-frames:v', '2', '-vf', vf_cover_clean, *enc_args, '-an', '-r', '30', '-pix_fmt', pix_fmt, cover_v]
            
            if self.run_ffmpeg(cover_cmd, cwd=cache_dir) and os.path.exists(cover_v) and os.path.getsize(cover_v) > 256:
                cover_concat_txt = os.path.join(cache_dir, f"c_concat_{unique_id}.txt")
                with open(cover_concat_txt, 'w', encoding='utf-8') as f:
                    safe_cover_path = str(cover_v).replace('\\', '/')
                    safe_main_path = str(main_v_no_audio).replace('\\', '/')
                    f.write(f"file '{safe_cover_path}'\n")
                    f.write(f"file '{safe_main_path}'\n")
                temp_final = os.path.join(cache_dir, f"cover_final_{unique_id}.ts")
                merge_cmd = [self.ffmpeg_exe, '-y', '-hide_banner', '-loglevel', 'error', '-f', 'concat', '-safe', '0', '-i', cover_concat_txt, '-c:v', 'copy', temp_final]
                if self.run_ffmpeg(merge_cmd, cwd=cache_dir) and os.path.exists(temp_final) and os.path.getsize(temp_final) > 256:
                    final_v_with_cover = temp_final

        if not self.is_running: return 

        if self.task_data.get('preview_only'):
            final_output_name = f"预览样片_{audio_name}_{salt}.mp4"
        else:
            final_output_name = f"{audio_name}_回{round_idx}_{salt}.mp4" if total_rounds > 1 else f"{audio_name}_{salt}.mp4"
        final_output = os.path.join(output_dir, final_output_name)
        temp_out_mp4 = os.path.join(cache_dir, f"temp_out_{unique_id}.mp4")

        idx_a_main = 1; mix_inputs = ['-i', final_v_with_cover, '-i', audio_path]; current_input_count = 2; idx_bgm = -1
        if bgm_pool:
            mix_inputs.extend(['-stream_loop', '-1', '-i', random.choice(bgm_pool)])
            idx_bgm = current_input_count; current_input_count += 1

        mix_cmd = [self.ffmpeg_exe, '-y', '-hide_banner', '-loglevel', 'error'] + mix_inputs

        if idx_bgm != -1:
            filter_complex_audio = f"[{idx_a_main}:a]volume={main_vol}[a1];[{idx_bgm}:a]volume={bgm_vol}[a2];[a1][a2]amix=inputs=2:duration=first:dropout_transition=3[aout]"
            mix_cmd.extend(['-filter_complex', filter_complex_audio, '-map', '0:v:0', '-map', '[aout]'])
        else:
            mix_cmd.extend(['-af', f"volume={main_vol}", '-map', '0:v:0', '-map', f'{idx_a_main}:a:0'])

        if final_ass_path and os.path.exists(final_ass_path):
            safe_ass = path_to_ffmpeg(final_ass_path)
            mix_cmd.extend(['-vf', f"subtitles='{safe_ass}'"])

        mix_cmd.extend([
            *final_enc_args, '-pix_fmt', pix_fmt,
            '-c:a', 'aac', '-b:a', '128k', '-ar', '44100', '-ac', '2',
            '-shortest', '-avoid_negative_ts', 'make_zero', '-movflags', '+faststart', temp_out_mp4
        ])
        
        if self.run_ffmpeg(mix_cmd, cwd=cache_dir):
            if os.path.exists(temp_out_mp4) and os.path.getsize(temp_out_mp4) > 1024:
                shutil.move(temp_out_mp4, final_output)
                self.log_signal.emit(f"[{unique_id}] ✅ 已成功封装输出：{final_output}\n")
                self.task_done_signal.emit()
                if round_idx == total_rounds: self.status_update_signal.emit(audio_row, "✅ 全部完成", "green")
            else:
                self.status_update_signal.emit(audio_row, "❌ 生成异常", "red")
                self.log_signal.emit(f"[{unique_id}] ❌ 封装失败：文件生成异常！")
        else:
            self.status_update_signal.emit(audio_row, "❌ 封装失败", "red")
            self.log_signal.emit(f"[{unique_id}] ❌ 混合封装过程发生崩溃！")

        safe_audio_name = re.sub(r'[^a-zA-Z0-9\u4e00-\u9fa5]', '_', audio_name)
        trash_files = concat_list + [
            os.path.join(cache_dir, f"trans_{unique_id}.ts"), os.path.join(cache_dir, f"main_v_{unique_id}.ts"),
            os.path.join(cache_dir, f"concat_{unique_id}.txt"), os.path.join(cache_dir, f"cover_v_{unique_id}.ts"),
            os.path.join(cache_dir, f"c_concat_{unique_id}.txt"), os.path.join(cache_dir, f"cover_final_{unique_id}.ts"),
            os.path.join(cache_dir, f"asr_{safe_audio_name}_16k.wav"), 
            os.path.join(cache_dir, f"asr_{safe_audio_name}_16k.wav.srt"), os.path.join(cache_dir, f"asr_{safe_audio_name}_16k.srt")
        ]
        for idx in range(len(watermarks)):
            for i in range(20): 
                tf = os.path.join(cache_dir, f"wm_{unique_id}_{idx}_l{i}.txt")
                if os.path.exists(tf): trash_files.append(tf)
        for tf in trash_files:
            if os.path.exists(tf): os.remove(tf)

    def run(self):
        if not self.check_env(): self.finished_signal.emit(); return
        videos = [v['path'] for v in self.task_data['videos']]; covers = [c['path'] for c in self.task_data['covers']]
        bgms = [b['path'] for b in self.task_data['bgms']]; audios = self.task_data['audios'] 
        watermarks = self.task_data['watermarks']; min_clip = self.task_data['min_clip']; max_clip = self.task_data['max_clip']
        gpu_mode = self.task_data['gpu_mode']; w, h = self.task_data['resolution'].split(' ')[0].split('x')
        target_res = f"{w}:{h}"; cache_dir = self.task_data['cache_dir']; output_dir = self.task_data['output_dir']
        trans_type = self.task_data['trans_type']; trans_dur = self.task_data['trans_dur']
        loop_count = self.task_data['loop_count']; main_vol = self.task_data['main_vol']; bgm_vol = self.task_data['bgm_vol']

        if self.task_data.get('preview_only'):
            audios = audios[:1]
            loop_count = 1
            self.log_signal.emit("[引擎管线] 预览样片模式：只生成第一条音频的一条样片。\n")
        self.log_signal.emit(f"[引擎管线] 共需循环 {loop_count} 轮，启动并发任务排队...\n")
        with ThreadPoolExecutor(max_workers=self.task_data['threads']) as executor:
            futures = []
            for round_idx in range(1, loop_count + 1):
                if not self.is_running: break 
                for audio_dict in audios:
                    future = executor.submit(self.process_single_audio, audio_dict, videos, bgms, min_clip, max_clip, gpu_mode, target_res, trans_type, trans_dur, cache_dir, output_dir, covers, watermarks, round_idx, loop_count, main_vol, bgm_vol)
                    futures.append(future)
            for future in as_completed(futures):
                try: future.result() 
                except Exception as e: self.log_signal.emit(f"[引擎错误] 并发管线发生异常: {str(e)}")
        if self.is_running:        
            self.log_signal.emit("🎉 全部混剪任务完毕！硬件引擎进入休眠。")
            self.finished_signal.emit()

    def _get_si(self):
        si = subprocess.STARTUPINFO(); si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        return si
