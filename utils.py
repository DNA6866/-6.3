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

def _probe_message(res):
    if not res:
        return "命令无法执行或超时"
    text = ((res.stderr or "") + "\n" + (res.stdout or "")).strip()
    text = re.sub(r"\s+", " ", text)
    if not text:
        text = f"退出码 {res.returncode}"
    return text[:240]

def _get_ffmpeg_encoders(ffmpeg_path):
    if not os.path.exists(ffmpeg_path):
        return set()
    res = _run_probe([ffmpeg_path, '-hide_banner', '-encoders'], timeout=6)
    if not res or res.returncode != 0:
        return set()
    return set(re.findall(r'\b(h264_[a-z0-9_]+|hevc_[a-z0-9_]+)\b', res.stdout.lower()))

def _candidate_nvidia_smi_paths():
    candidates = ['nvidia-smi']
    for base in (
        os.environ.get('ProgramFiles'),
        os.environ.get('ProgramW6432'),
        r'C:\Program Files',
    ):
        if base:
            candidates.append(os.path.join(base, 'NVIDIA Corporation', 'NVSMI', 'nvidia-smi.exe'))
    system_root = os.environ.get('SystemRoot', r'C:\Windows')
    candidates.append(os.path.join(system_root, 'System32', 'nvidia-smi.exe'))

    seen = set()
    result = []
    for path in candidates:
        key = os.path.normcase(path)
        if key in seen:
            continue
        seen.add(key)
        if path == 'nvidia-smi' or os.path.exists(path):
            result.append(path)
    return result

def _query_nvidia_smi_name():
    for exe in _candidate_nvidia_smi_paths():
        res = _run_probe([exe, '--query-gpu=name', '--format=csv,noheader'], timeout=4)
        if res and res.returncode == 0 and res.stdout.strip():
            return res.stdout.strip().splitlines()[0].strip(), exe
    return '', ''

def _query_video_controller_names():
    names = []
    wmic_res = _run_probe(['wmic', 'path', 'win32_VideoController', 'get', 'name'], timeout=5)
    if wmic_res and wmic_res.stdout:
        names.extend(
            line.strip()
            for line in wmic_res.stdout.splitlines()
            if line.strip() and line.strip().lower() != 'name'
        )

    if not names:
        ps_cmd = (
            "$OutputEncoding=[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
            "(Get-CimInstance Win32_VideoController | ForEach-Object {$_.Name}) -join \"`n\""
        )
        ps_res = _run_probe(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', ps_cmd], timeout=6)
        if ps_res and ps_res.stdout:
            names.extend(line.strip() for line in ps_res.stdout.splitlines() if line.strip())

    deduped = []
    seen = set()
    for name in names:
        key = name.lower()
        if key not in seen:
            seen.add(key)
            deduped.append(name)
    return deduped

def _first_controller_name(names, keywords):
    keywords = tuple(k.lower() for k in keywords)
    return next((name for name in names if any(k in name.lower() for k in keywords)), '')

def _probe_video_encoder_runtime(ffmpeg_path, encoder):
    cmd = [
        ffmpeg_path,
        '-hide_banner',
        '-loglevel', 'error',
        '-y',
        '-f', 'lavfi',
        '-i', 'color=c=black:s=320x240:r=1:d=0.2',
        '-frames:v', '1',
        '-an',
        '-c:v', encoder,
        '-f', 'null',
        '-',
    ]
    res = _run_probe(cmd, timeout=12)
    if res and res.returncode == 0:
        return True, '实际编码探针通过'
    return False, _probe_message(res)

def _looks_like_rtx_50_series(name):
    return bool(re.search(r'\b(geforce\s+)?rtx\s*50\d{2}\b|\b50\d{2}\s*ti\b', name or '', re.IGNORECASE))

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

    video_names = _query_video_controller_names()
    nv_name, nv_source = _query_nvidia_smi_name()
    if not nv_name:
        nv_name = _first_controller_name(video_names, ['nvidia', 'geforce', 'rtx'])
        if nv_name:
            nv_source = 'Windows 显卡信息'

    if nv_name and 'h264_nvenc' in encoders:
        nvenc_ok, nvenc_msg = _probe_video_encoder_runtime(ffmpeg_path, 'h264_nvenc')
        if nvenc_ok:
            profile.update({
                'gpu_mode': 'Nvidia NVENC 加速',
                'threads': max(1, min(3, recommended_threads)),
                'summary': f'Nvidia NVENC: {nv_name}',
                'level': 'nvenc'
            })
            source_label = f'（来源：{nv_source}）' if nv_source else ''
            profile['details'].append(f'检测到 Nvidia 显卡: {nv_name}{source_label}')
            profile['details'].append('ffmpeg 已列出 h264_nvenc，且 NVENC 实际编码探针通过')
            return profile
        profile['details'].append(f'检测到 Nvidia 显卡: {nv_name}，但 NVENC 实际编码测试失败: {nvenc_msg}')
        if _looks_like_rtx_50_series(nv_name):
            profile['details'].append('RTX 50 系显卡建议安装 R570 或更高版本 NVIDIA 驱动，并使用新版网盘资源包中的 ffmpeg')
    if nv_name:
        if 'h264_nvenc' not in encoders:
            profile['details'].append(f'检测到 Nvidia 显卡，但当前 ffmpeg 未启用 h264_nvenc: {nv_name}')
    elif 'h264_nvenc' in encoders:
        nvenc_ok, nvenc_msg = _probe_video_encoder_runtime(ffmpeg_path, 'h264_nvenc')
        if nvenc_ok:
            profile.update({
                'gpu_mode': 'Nvidia NVENC 加速',
                'threads': max(1, min(3, recommended_threads)),
                'summary': 'Nvidia NVENC: 编码器可用（显卡名称未返回）',
                'level': 'nvenc'
            })
            profile['details'].append('ffmpeg 已列出 h264_nvenc，且 NVENC 实际编码探针通过；Windows 未返回显卡名称')
            return profile

    amd_name = _first_controller_name(video_names, ['amd', 'radeon'])

    if amd_name and 'h264_amf' in encoders:
        amf_ok, amf_msg = _probe_video_encoder_runtime(ffmpeg_path, 'h264_amf')
        if amf_ok:
            profile.update({
                'gpu_mode': 'AMD AMF 加速',
                'threads': max(1, min(3, recommended_threads)),
                'summary': f'AMD AMF: {amd_name}',
                'level': 'amf'
            })
            profile['details'].append('ffmpeg 已列出 h264_amf，且 AMF 实际编码探针通过')
            return profile
        profile['details'].append(f'检测到 AMD 显卡，但 AMF 实际编码测试失败: {amf_msg}')
    if amd_name:
        if 'h264_amf' not in encoders:
            profile['details'].append(f'检测到 AMD 显卡，但当前 ffmpeg 未启用 h264_amf: {amd_name}')

    if 'h264_nvenc' in encoders:
        profile['details'].append('ffmpeg 支持 NVENC，但未检测到可用 Nvidia 设备或实际编码测试未通过')
    if 'h264_amf' in encoders:
        profile['details'].append('ffmpeg 支持 AMF，但未检测到可用 AMD 设备或实际编码测试未通过')
    if video_names:
        profile['details'].append('Windows 显卡列表: ' + ' / '.join(video_names[:4]))
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
                file_name = f.lower()
                # 宋体扩展 B/G 只覆盖扩展字符，烧录常用中文时会显示方框。
                if file_name == 'simsunb.ttf' or (
                    file_name.startswith('simsun') and 'ext' in file_name
                ):
                    continue
                # 【中文字体过滤器】：只加载明确支持中文的系统字体，屏蔽纯英文导致的方框乱码
                if any(x in file_name for x in ['msyh', 'sim', 'st', 'ms', 'deng', 'fz']):
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
        anims[f"➡️ 向右匀速滚动 (速{s})"] = (f"mod(TIME_VAR*{s}\\,w+tw)-tw+(offset_x)", "(line_y)", "1.0")
        anims[f"⬅️ 向左匀速滚动 (速{s})"] = (f"w-mod(TIME_VAR*{s}\\,w+tw)+(offset_x)", "(line_y)", "1.0")
        anims[f"⬆️ 向上匀速滚动 (速{s})"] = ("(base_x)", f"h-mod(TIME_VAR*{s}\\,h+th)+(offset_y)", "1.0")
        anims[f"⬇️ 向下匀速滚动 (速{s})"] = ("(base_x)", f"mod(TIME_VAR*{s}\\,h+th)-th+(offset_y)", "1.0")

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
            anims[f"☄️ 向左滚动+闪烁 (速{s}_频{f})"] = (f"w-mod(TIME_VAR*{s}\\,w+tw)+(offset_x)", "(line_y)", f"0.5+0.5*sin(TIME_VAR*{f})")
            anims[f"☄️ 向右滚动+闪烁 (速{s}_频{f})"] = (f"mod(TIME_VAR*{s}\\,w+tw)-tw+(offset_x)", "(line_y)", f"0.5+0.5*sin(TIME_VAR*{f})")

    return anims

ANIMATIONS = get_animations()

def get_subtitle_animations():
    anims = {
        "无动效": "",
        "🎲 随机动效": "random",
        "柔和淡入淡出": "{\\fad(200,200)}",
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
        "暴击震动": "{\\frz20\\fscx130\\fscy130\\t(0,100,\\frz-15\\fscx80\\fscy80)\\t(100,200,\\frz0\\fscx100\\fscy100)}",
    }

    variants = [
        ("电影淡入", "{\\alpha&HFF&\\t(0,%d,\\alpha&H00&)}", [120, 200, 320, 480]),
        ("电影淡出", "{\\t(%d,%d,\\alpha&HFF&)}", [(500, 900), (800, 1300), (1200, 1800), (1600, 2300)]),
        ("弹性放大", "{\\fscx70\\fscy70\\t(0,%d,\\fscx120\\fscy120)\\t(%d,%d,\\fscx100\\fscy100)}", [(180, 180, 360), (240, 240, 520), (320, 320, 700)]),
        ("轻微缩放", "{\\fscx105\\fscy105\\t(0,%d,\\fscx100\\fscy100)}", [500, 900, 1400, 2000]),
        ("慢速推近", "{\\t(0,%d,\\fscx118\\fscy118)}", [1200, 1800, 2400, 3200]),
        ("慢速拉远", "{\\fscx118\\fscy118\\t(0,%d,\\fscx100\\fscy100)}", [1200, 1800, 2400, 3200]),
        ("左倾回正", "{\\frz-8\\t(0,%d,\\frz0)}", [220, 360, 520, 760]),
        ("右倾回正", "{\\frz8\\t(0,%d,\\frz0)}", [220, 360, 520, 760]),
        ("上下压扁弹回", "{\\fscy70\\t(0,%d,\\fscy120)\\t(%d,%d,\\fscy100)}", [(180, 180, 360), (260, 260, 520), (360, 360, 720)]),
        ("左右拉伸弹回", "{\\fscx135\\t(0,%d,\\fscx90)\\t(%d,%d,\\fscx100)}", [(180, 180, 360), (260, 260, 520), (360, 360, 720)]),
        ("锐利聚焦", "{\\blur8\\alpha&H40&\\t(0,%d,\\blur0\\alpha&H00&)}", [220, 360, 520, 760]),
        ("柔焦显影", "{\\blur16\\alpha&H80&\\t(0,%d,\\blur1\\alpha&H00&)}", [320, 520, 760, 1000]),
        ("描边生长", "{\\bord0\\t(0,%d,\\bord6)}", [220, 360, 520, 760]),
        ("描边回落", "{\\bord10\\t(0,%d,\\bord4)}", [220, 360, 520, 760]),
        ("阴影扫过", "{\\shad0\\t(0,%d,\\shad6)}", [260, 420, 680, 900]),
        ("字距打开", "{\\fsp-5\\t(0,%d,\\fsp2)}", [300, 520, 760, 1000]),
        ("字距收紧", "{\\fsp8\\t(0,%d,\\fsp0)}", [300, 520, 760, 1000]),
        ("白到黄", "{\\c&HFFFFFF&\\t(0,%d,\\c&H00FFFF&)}", [300, 520, 760]),
        ("白到青", "{\\c&HFFFFFF&\\t(0,%d,\\c&HFFFF00&)}", [300, 520, 760]),
        ("白到粉", "{\\c&HFFFFFF&\\t(0,%d,\\c&HFF66FF&)}", [300, 520, 760]),
        ("蓝光入场", "{\\c&HFFFFFF&\\3c&HFFAA00&\\t(0,%d,\\3c&HFFFFFF&)}", [300, 520, 760]),
        ("红光入场", "{\\c&HFFFFFF&\\3c&H3333FF&\\t(0,%d,\\3c&H000000&)}", [300, 520, 760]),
        ("轻微左摇", "{\\frz-4\\t(0,180,\\frz4)\\t(180,%d,\\frz0)}", [360, 520, 760]),
        ("轻微右摇", "{\\frz4\\t(0,180,\\frz-4)\\t(180,%d,\\frz0)}", [360, 520, 760]),
        ("横向翻转", "{\\fry90\\t(0,%d,\\fry0)}", [220, 360, 520, 760]),
        ("纵向翻转", "{\\frx90\\t(0,%d,\\frx0)}", [220, 360, 520, 760]),
        ("透视翻入", "{\\frx50\\fry-30\\alpha&H80&\\t(0,%d,\\frx0\\fry0\\alpha&H00&)}", [300, 520, 760]),
        ("轻微呼吸", "{\\t(0,600,\\fscx106\\fscy106)\\t(600,%d,\\fscx100\\fscy100)}", [900, 1200, 1600]),
        ("重击放大", "{\\fscx150\\fscy150\\alpha&H60&\\t(0,%d,\\fscx100\\fscy100\\alpha&H00&)}", [140, 220, 320]),
        ("闪白入场", "{\\c&HFFFFFF&\\alpha&H80&\\t(0,%d,\\alpha&H00&)}", [100, 180, 260]),
        ("霓虹闪烁", "{\\alpha&H80&\\t(0,120,\\alpha&H00&)\\t(120,240,\\alpha&H60&)\\t(240,%d,\\alpha&H00&)}", [420, 620, 820]),
    ]

    for family, template, values in variants:
        for index, value in enumerate(values, 1):
            if isinstance(value, tuple):
                anims[f"{family} {index}"] = template % value
            else:
                anims[f"{family} {index}"] = template % value
            if len(anims) >= 100:
                return anims
    return anims

SUBTITLE_ANIMATIONS = get_subtitle_animations()

TRANS_MAP = {
    "不使用转场": "none",
    "🎲 随机特效 (防限流神器)": "random",
    "叠化溶解": "fade",
    "向左擦除": "wipeleft",
    "向右擦除": "wiperight",
    "向上擦除": "wipeup",
    "向下擦除": "wipedown",
    "向左推移": "slideleft",
    "向右推移": "slideright",
    "向上推移": "slideup",
    "向下推移": "slidedown",
    "圆形裁切": "circlecrop",
    "矩形裁切": "rectcrop",
    "距离扩散": "distance",
    "黑场过渡": "fadeblack",
    "白场过渡": "fadewhite",
    "径向扇形": "radial",
    "平滑左推": "smoothleft",
    "平滑右推": "smoothright",
    "平滑上推": "smoothup",
    "平滑下推": "smoothdown",
    "圆形展开": "circleopen",
    "圆形收缩": "circleclose",
    "垂直打开": "vertopen",
    "垂直闭合": "vertclose",
    "水平打开": "horzopen",
    "水平闭合": "horzclose",
    "标准溶解": "dissolve",
    "像素化": "pixelize",
    "左上对角": "diagtl",
    "右上对角": "diagtr",
    "左下对角": "diagbl",
    "右下对角": "diagbr",
    "水平左切片": "hlslice",
    "水平右切片": "hrslice",
    "垂直上切片": "vuslice",
    "垂直下切片": "vdslice",
    "水平模糊": "hblur",
    "灰度淡化": "fadegrays",
    "左上擦除": "wipetl",
    "右上擦除": "wipetr",
    "左下擦除": "wipebl",
    "右下擦除": "wipebr",
    "水平挤压": "squeezeh",
    "垂直挤压": "squeezev",
    "中心放大": "zoomin",
    "快速淡入": "fadefast",
    "慢速淡入": "fadeslow",
    "水平左风吹": "hlwind",
    "水平右风吹": "hrwind",
    "垂直上风吹": "vuwind",
    "垂直下风吹": "vdwind",
    "左侧覆盖": "coverleft",
    "右侧覆盖": "coverright",
    "上方覆盖": "coverup",
    "下方覆盖": "coverdown",
    "左侧揭开": "revealleft",
    "右侧揭开": "revealright",
    "上方揭开": "revealup",
    "下方揭开": "revealdown",
}

COVER_RANDOM_STATIC_STYLES = [
    "经典白字黑边",
    "高亮霓虹发光",
    "综艺黑字黄底",
    "复古牛皮纸色",
    "冲击红字白边",
    "蓝黄电商条幅",
    "黑金质感",
    "清新绿白底",
    "紫粉发光",
    "橙色爆款标签",
    "白字红底横幅",
    "深蓝科技风",
    "粉色少女风",
    "金色描边大字",
    "黑字白底清晰款",
    "青色霓虹描边",
    "红黄直播间款",
    "白字阴影款",
    "绿色价格牌",
    "银灰高级感",
]

COVER_STATIC_STYLES = ["无特效 (仅原图)"] + COVER_RANDOM_STATIC_STYLES + ["🎲 随机静态风格"]
