import os
import multiprocessing
import re
import subprocess
from paths import tools_dir as default_tools_dir

# 扩展 50+ 种丰富的颜色预设
EXTENDED_COLORS = [
    "white", "black", "red", "yellow", "green", "blue", "gray", "pink", "purple", "orange", "cyan", "gold",
    "#FF5722", "#E91E63", "#9C27B0", "#673AB7", "#3F51B5", "#2196F3", "#03A9F4", "#00BCD4", "#009688", "#4CAF50",
    "#8BC34A", "#CDDC39", "#FFEB3B", "#FFC107", "#FF9800", "#FF5722", "#795548", "#9E9E9E", "#607D8B",
    "#F44336", "#E57373", "#F06292", "#BA68C8", "#9575CD", "#7986CB", "#64B5F6", "#4FC3F7", "#4DD0E1", "#4DB6AC",
    "#81C784", "#AED581", "#DCE775", "#FFF176", "#FFD54F", "#FFB74D", "#FF8A65", "#A1887F", "#E0E0E0", "#90A4AE"
]

def path_to_ffmpeg(path):
    p = str(path).replace('\\', '/')
    p = p.replace(':', '\\:')
    return p

def _hidden_startupinfo():
    if os.name != 'nt':
        return None
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    return si

def _run_probe(cmd, timeout=4):
    try:
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='ignore',
            startupinfo=_hidden_startupinfo(),
            timeout=timeout
        )
    except Exception:
        return None

def _get_ffmpeg_encoders(ffmpeg_path):
    if not os.path.exists(ffmpeg_path):
        return set()
    res = _run_probe([ffmpeg_path, '-hide_banner', '-encoders'], timeout=6)
    if not res or res.returncode != 0:
        return set()
    return set(re.findall(r'\b(h264_[a-z0-9_]+|hevc_[a-z0-9_]+)\b', res.stdout.lower()))

def detect_performance_profile(tools_dir=None):
    tools_dir = tools_dir or default_tools_dir()
    ffmpeg_path = os.path.join(tools_dir, 'ffmpeg.exe')
    encoders = _get_ffmpeg_encoders(ffmpeg_path)
    cpu_count = multiprocessing.cpu_count()
    recommended_threads = max(1, min(4, max(1, cpu_count - 2)))

    profile = {
        'gpu_mode': 'CPU 软解',
        'threads': recommended_threads,
        'summary': 'CPU 渲染模式',
        'details': [],
        'level': 'cpu'
    }

    if not os.path.exists(ffmpeg_path):
        profile['details'].append(f'未找到 ffmpeg.exe: {ffmpeg_path}')
        return profile

    nv_res = _run_probe(['nvidia-smi', '--query-gpu=name', '--format=csv,noheader'], timeout=3)
    nv_name = nv_res.stdout.strip().splitlines()[0].strip() if nv_res and nv_res.returncode == 0 and nv_res.stdout.strip() else ''
    if nv_name and 'h264_nvenc' in encoders:
        profile.update({
            'gpu_mode': 'Nvidia NVENC 加速',
            'threads': max(1, min(3, recommended_threads)),
            'summary': f'Nvidia NVENC: {nv_name}',
            'level': 'nvenc'
        })
        profile['details'].append('已验证 ffmpeg 支持 h264_nvenc')
        return profile
    if nv_name:
        profile['details'].append(f'检测到 Nvidia 显卡，但当前 ffmpeg 未启用 h264_nvenc: {nv_name}')

    amd_name = ''
    wmic_res = _run_probe(['wmic', 'path', 'win32_VideoController', 'get', 'name'], timeout=4)
    if wmic_res and wmic_res.stdout:
        lines = [line.strip() for line in wmic_res.stdout.splitlines() if line.strip() and line.strip().lower() != 'name']
        amd_name = next((line for line in lines if 'amd' in line.lower() or 'radeon' in line.lower()), '')

    if amd_name and 'h264_amf' in encoders:
        profile.update({
            'gpu_mode': 'AMD AMF 加速',
            'threads': max(1, min(3, recommended_threads)),
            'summary': f'AMD AMF: {amd_name}',
            'level': 'amf'
        })
        profile['details'].append('已验证 ffmpeg 支持 h264_amf')
        return profile
    if amd_name:
        profile['details'].append(f'检测到 AMD 显卡，但当前 ffmpeg 未启用 h264_amf: {amd_name}')

    if 'h264_nvenc' in encoders:
        profile['details'].append('ffmpeg 支持 NVENC，但未检测到可用 Nvidia 设备')
    if 'h264_amf' in encoders:
        profile['details'].append('ffmpeg 支持 AMF，但未检测到可用 AMD 设备')
    profile['details'].append(f'CPU 核心数: {cpu_count}，建议并发: {recommended_threads}')
    return profile

def get_system_fonts():
    # 核心保底中文字体
    fonts = {
        "微软雅黑 (默认)": "C:/Windows/Fonts/msyh.ttc",
        "微软雅黑 (粗)": "C:/Windows/Fonts/msyhbd.ttc",
        "黑体": "C:/Windows/Fonts/simhei.ttf",
        "楷体": "C:/Windows/Fonts/simkai.ttf",
        "宋体": "C:/Windows/Fonts/simsun.ttc",
        "仿宋": "C:/Windows/Fonts/simfang.ttf",
    }
    font_dir = "C:/Windows/Fonts"
    if os.path.exists(font_dir):
        for f in os.listdir(font_dir):
            if f.lower().endswith(('.ttf', '.ttc')):
                # 【中文字体过滤器】：只加载明确支持中文的系统字体，屏蔽纯英文导致的方框乱码
                if any(x in f.lower() for x in ['msyh', 'sim', 'st', 'ms', 'deng', 'fz']):
                    name = f.split('.')[0]
                    # 给系统字体起个友好的名字
                    friendly_name = name.replace('msyh', '雅黑').replace('simhei', '黑体').replace('simsun', '宋体')
                    if friendly_name not in fonts.keys() and len(fonts) < 50:
                        fonts[f"系统字体: {friendly_name}"] = os.path.join(font_dir, f)
    return fonts
def get_animations():
    anims = {"静态展示": ("(base_x)", "(line_y)", "1.0"), "🎲 随机动态特效": "random"}
    speeds = [50, 100, 150, 200, 300]
    for s in speeds:
        anims[f"➡️ 向右匀速滚动 (速{s})"] = (f"TIME_VAR*{s}-tw+(offset_x)", "(line_y)", "1.0")
        anims[f"⬅️ 向左匀速滚动 (速{s})"] = (f"w-TIME_VAR*{s}+(offset_x)", "(line_y)", "1.0")
        anims[f"⬆️ 向上匀速滚动 (速{s})"] = ("(base_x)", f"h-TIME_VAR*{s}+(offset_y)", "1.0")
        anims[f"⬇️ 向下匀速滚动 (速{s})"] = ("(base_x)", f"TIME_VAR*{s}-th+(offset_y)", "1.0")

    amps = [10, 30, 50, 80]
    freqs = [1, 2, 3, 5]
    for a in amps:
        for f in freqs:
            anims[f"🌊 上下平滑呼吸 (幅{a}_频{f})"] = ("(base_x)", f"(line_y)+{a}*sin(TIME_VAR*{f})", "1.0")
            anims[f"⛵ 左右平滑摇摆 (幅{a}_频{f})"] = (f"(base_x)+{a}*sin(TIME_VAR*{f})", "(line_y)", "1.0")
            anims[f"💫 斜向漂浮转圈 (幅{a}_频{f})"] = (f"(base_x)+{a}*sin(TIME_VAR*{f})", f"(line_y)+{a}*cos(TIME_VAR*{f})", "1.0")

    for f in [1, 2, 3, 5, 8, 10]:
        anims[f"💡 平滑呼吸闪烁 (频{f})"] = ("(base_x)", "(line_y)", f"0.5+0.5*sin(TIME_VAR*{f})")
        anims[f"🚨 急促警报闪烁 (频{f})"] = ("(base_x)", "(line_y)", f"abs(sin(TIME_VAR*{f}))")

    for s in [50, 100, 200]:
        for f in [2, 4, 6]:
            anims[f"☄️ 向左滚动+闪烁 (速{s}_频{f})"] = (f"w-TIME_VAR*{s}+(offset_x)", "(line_y)", f"0.5+0.5*sin(TIME_VAR*{f})")
            anims[f"☄️ 向右滚动+闪烁 (速{s}_频{f})"] = (f"TIME_VAR*{s}-tw+(offset_x)", "(line_y)", f"0.5+0.5*sin(TIME_VAR*{f})")

    return anims

ANIMATIONS = get_animations()

TRANS_MAP = {
    "不使用转场": "none", "🎲 随机特效 (防限流神器)": "random",
    "叠化溶解": "fade", "黑场过渡": "fadeblack", "白场过渡": "fadewhite",
    "标准溶解": "dissolve", "像素化": "pixelize", "径向扇形": "radial",
    "向左推移": "slideleft", "向右推移": "slideright", "向上推移": "slideup", "向下推移": "slidedown",
    "平滑左推": "smoothleft", "平滑右推": "smoothright", "平滑上推": "smoothup", "平滑下推": "smoothdown",
    "向左擦除": "wipeleft", "向右擦除": "wiperight", "向上擦除": "wipeup", "向下擦除": "wipedown",
    "左上对角擦除": "wipetl", "右上对角擦除": "wipetr", "左下对角擦除": "wipebl", "右下对角擦除": "wipebr",
    "水平切片": "hlslice", "垂直切片": "vuslice",
    "圆形展开": "circleopen", "圆形收缩": "circleclose",
    "矩形展开": "rectcrop", "中心放大": "zoomin",
    "水平挤压": "squeezeh", "垂直挤压": "squeezev"
}
