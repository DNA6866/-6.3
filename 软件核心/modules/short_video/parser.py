import json
import os
import re
import subprocess
import time
from urllib.parse import urlparse

import requests


class ShortVideoError(Exception):
    pass


HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9",
}

MOBILE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_2 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
    ),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Referer": "https://www.douyin.com/",
}


def extract_first_url(text):
    match = re.search(r"https?://[^\s，。；,，。；]+", text or "")
    if not match:
        raise ShortVideoError("未识别到链接，请粘贴抖音或快手分享出来的完整文本。")
    return match.group(0).strip().rstrip("，。；,;)")


def caption_from_share_text(text):
    """平台分享文案解析失败时，先尽量保留用户真正需要复制的文案。"""
    value = str(text or "").strip()
    if not value:
        return ""
    value = re.sub(r"https?://[^\s，。；,，。；]+", "", value)
    value = re.sub(r"[0-9a-zA-Z]+@[0-9a-zA-Z.]+\\s*:.*$", "", value).strip()
    noise_patterns = [
        r"复制下方链接.*$",
        r"打开【?抖音】?.*$",
        r"打开【?快手】?.*$",
        r"长按复制此条消息.*$",
        r"来和我一起支持Ta吧[。！!]*",
        r"直接观看.*$",
    ]
    for pattern in noise_patterns:
        value = re.sub(pattern, "", value).strip()
    value = value.strip("# \n\r\t，。；,;:：")
    return value


def expand_share_url(url):
    try:
        res = requests.get(url, headers=HEADERS, allow_redirects=True, timeout=12)
        return res.url or url
    except Exception as exc:
        raise ShortVideoError(f"链接跳转失败，请确认网络可访问该分享链接。底层信息：{exc}")


def try_expand_share_url(url):
    try:
        return expand_share_url(url)
    except Exception:
        return url


def detect_platform(url):
    host = urlparse(url).netloc.lower()
    if "douyin" in host or "iesdouyin" in host or "amemv" in host:
        return "抖音"
    if "kuaishou" in host or "gifshow" in host:
        return "快手"
    return "未知平台"


def _video_id_from_url(url):
    parsed = urlparse(url)
    path = parsed.path.strip("/")
    parts = [item for item in path.split("/") if item]
    for index, item in enumerate(parts):
        if item in {"video", "note"} and index + 1 < len(parts):
            match = re.search(r"\d{10,}", parts[index + 1])
            if match:
                return match.group(0)
    for item in reversed(parts):
        match = re.search(r"\d{10,}", item)
        if match:
            return match.group(0)
    return ""


def _extract_router_data(html_text):
    match = re.search(r"window\._ROUTER_DATA\s*=\s*(.*?)</script>", html_text, re.S)
    if not match:
        match = re.search(r'<script id="RENDER_DATA" type="application/json">(.*?)</script>', html_text, re.S)
        if match:
            from urllib.parse import unquote
            return json.loads(unquote(match.group(1)))
    if not match:
        raise ShortVideoError("抖音分享页中没有找到视频数据。")
    return json.loads(match.group(1).strip())


def _find_douyin_item(router_data):
    loader = router_data.get("loaderData") or {}
    for key in ("video_(id)/page", "note_(id)/page"):
        try:
            items = loader[key]["videoInfoRes"]["item_list"]
            if items:
                return items[0]
        except Exception:
            pass
    for value in loader.values():
        try:
            items = value["videoInfoRes"]["item_list"]
            if items:
                return items[0]
        except Exception:
            continue
    raise ShortVideoError("抖音页面数据里没有找到作品信息。")


def parse_douyin_ies(raw_text, source_url, expanded_url):
    video_id = _video_id_from_url(expanded_url) or _video_id_from_url(source_url)
    if not video_id:
        raise ShortVideoError("未能从抖音链接里识别作品 ID。")
    share_page_url = f"https://www.iesdouyin.com/share/video/{video_id}"
    response = requests.get(share_page_url, headers=MOBILE_HEADERS, timeout=15)
    response.raise_for_status()
    item = _find_douyin_item(_extract_router_data(response.text))
    video = item.get("video") or {}
    play_addr = video.get("play_addr") or {}
    url_list = play_addr.get("url_list") or []
    if not url_list:
        raise ShortVideoError("抖音作品没有返回可下载视频地址。")
    download_url = str(url_list[0]).replace("playwm", "play")
    desc = _clean_caption(item.get("desc")) or f"douyin_{video_id}"
    author = item.get("author") or {}
    statistics = item.get("statistics") or {}
    cover = ((video.get("cover") or {}).get("url_list") or [""])[0]
    share_caption = caption_from_share_text(raw_text)
    return {
        "source_url": source_url,
        "expanded_url": expanded_url,
        "platform": "抖音",
        "title": desc,
        "caption": share_caption or desc,
        "description": desc,
        "share_caption": share_caption,
        "uploader": author.get("nickname") or author.get("unique_id") or "",
        "video_id": video_id,
        "webpage_url": share_page_url,
        "direct_download_url": download_url,
        "cover": cover,
        "format_summary": (
            f"抖音直链解析成功"
            f" | 点赞 {statistics.get('digg_count', 0)}"
            f" | 评论 {statistics.get('comment_count', 0)}"
            f" | 分享 {statistics.get('share_count', 0)}"
        ),
        "parsed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


def looks_like_live_link(*values):
    text = " ".join(str(item or "").lower() for item in values)
    live_keys = ("live", "webcast", "liveroom", "livestream", "正在直播", "直播间")
    return any(key in text for key in live_keys)


def safe_filename(text):
    text = re.sub(r"[\\/:*?\"<>|]+", "_", text or "")
    text = re.sub(r"\s+", "_", text).strip("_")
    return text[:80] or "short_video"


def _clean_caption(text):
    text = str(text or "").strip()
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _best_caption(info):
    candidates = [
        info.get("description"),
        info.get("fulltitle"),
        info.get("title"),
        info.get("alt_title"),
    ]
    for value in candidates:
        caption = _clean_caption(value)
        if caption and caption.lower() not in {"none", "null"}:
            return caption
    return ""


def _format_summary(info):
    formats = info.get("formats") or []
    video_count = sum(1 for item in formats if item.get("vcodec") and item.get("vcodec") != "none")
    audio_count = sum(1 for item in formats if item.get("acodec") and item.get("acodec") != "none")
    return f"视频流 {video_count} 个，音频流 {audio_count} 个"


def _apply_cookie_options(options, cookie_options=None):
    cookie_options = cookie_options or {}
    mode = str(cookie_options.get("mode") or "none").lower()
    cookie_file = str(cookie_options.get("cookie_file") or "").strip()
    if mode in {"chrome", "edge", "firefox"}:
        options["cookiesfrombrowser"] = (mode,)
    elif mode == "file" and cookie_file:
        if not os.path.exists(cookie_file):
            raise ShortVideoError("cookies.txt 文件不存在，请重新选择。")
        options["cookiefile"] = cookie_file
    return options


def _cookie_hint(cookie_options=None):
    cookie_options = cookie_options or {}
    mode = str(cookie_options.get("mode") or "none").lower()
    if mode == "none":
        return "。这类链接通常需要 Cookie，请在本页 Cookie 来源选择 Chrome 或 Edge 后重试。"
    if mode in {"chrome", "edge", "firefox"}:
        return f"。已尝试读取 {mode} Cookie，如果仍失败，请先用该浏览器打开一次抖音/快手再重试。"
    if mode == "file":
        return "。已使用 cookies.txt，如果仍失败，请重新导出最新 Cookie。"
    return ""


def humanize_ytdlp_error(error, cookie_options=None):
    text = str(error)
    lower = text.lower()
    if "could not copy chrome cookie database" in lower:
        return (
            "Chrome Cookie 数据库被占用，软件暂时读不到 Cookie。请先完全关闭 Chrome 浏览器，"
            "包括右下角托盘和任务管理器里的 chrome.exe 后再解析；也可以切换 Edge Cookie，"
            "或导入最新的 cookies.txt。"
        )
    if "could not copy edge cookie database" in lower:
        return (
            "Edge Cookie 数据库被占用，软件暂时读不到 Cookie。请先完全关闭 Edge 浏览器，"
            "包括右下角托盘和任务管理器里的 msedge.exe 后再解析；也可以切换 Chrome Cookie，"
            "或导入最新的 cookies.txt。"
        )
    if "fresh cookies" in lower or "cookies" in lower and "needed" in lower:
        return (
            "平台要求使用最新 Cookie。请先用所选浏览器打开一次抖音/快手视频页，确认能正常观看，"
            "然后回到软件重新解析。"
        )
    if "unsupported url" in lower:
        return "当前链接类型 yt-dlp 暂不支持，请确认粘贴的是单条短视频作品链接，不是直播间、主页或合集链接。"
    return text + _cookie_hint(cookie_options)


def parse_short_video(raw_text, cookie_options=None):
    source_url = extract_first_url(raw_text)
    expanded_url = try_expand_share_url(source_url)
    platform = detect_platform(expanded_url or source_url)
    share_caption = caption_from_share_text(raw_text)
    if looks_like_live_link(raw_text, source_url, expanded_url):
        if share_caption:
            return {
                "source_url": source_url,
                "expanded_url": expanded_url,
                "platform": platform,
                "title": share_caption[:60] or "直播间分享",
                "caption": share_caption,
                "uploader": "",
                "video_id": "",
                "webpage_url": expanded_url or source_url,
                "format_summary": "检测到直播间链接；本页只解析单条短视频，直播请使用“直播间解析下载”功能区",
                "parsed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "partial": True,
                "parse_error": "检测到直播间链接，不是短视频作品链接。",
            }
        raise ShortVideoError("检测到这是直播间链接。请使用左侧“直播间解析下载”功能区；本页只处理单条短视频作品链接。")

    if platform == "抖音":
        try:
            return parse_douyin_ies(raw_text, source_url, expanded_url)
        except Exception as exc:
            douyin_direct_error = humanize_ytdlp_error(exc, cookie_options)
    else:
        douyin_direct_error = ""

    try:
        from yt_dlp import YoutubeDL
    except Exception as exc:
        if share_caption:
            return {
                "source_url": source_url,
                "expanded_url": expanded_url,
                "platform": platform,
                "title": share_caption[:60] or "短视频",
                "caption": share_caption,
                "uploader": "",
                "video_id": "",
                "webpage_url": expanded_url or source_url,
                "format_summary": "仅提取到分享文案；当前 Python 环境缺少 yt-dlp，无法解析下载",
                "parsed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "partial": True,
                "parse_error": f"缺少 yt-dlp：{exc}",
            }
        raise ShortVideoError(f"缺少 yt-dlp 解析组件，请先安装依赖。底层信息：{exc}")

    options = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "socket_timeout": 20,
        "http_headers": HEADERS,
        "extractor_retries": 2,
        "retries": 2,
    }
    options = _apply_cookie_options(options, cookie_options)
    errors = []
    candidates = []
    for item in (expanded_url, source_url):
        if item and item not in candidates:
            candidates.append(item)

    info = None
    for candidate in candidates:
        try:
            with YoutubeDL(options) as ydl:
                info = ydl.extract_info(candidate, download=False)
            break
        except Exception as exc:
            errors.append(f"{candidate} -> {humanize_ytdlp_error(exc, cookie_options)}")
            info = None

    if info is None and share_caption:
        if douyin_direct_error:
            errors.insert(0, f"抖音直链解析 -> {douyin_direct_error}")
        return {
            "source_url": source_url,
            "expanded_url": expanded_url,
            "platform": platform,
            "title": share_caption[:60] or "短视频",
            "caption": share_caption,
            "uploader": "",
            "video_id": "",
            "webpage_url": expanded_url or source_url,
            "format_summary": "仅提取到分享文案，未解析到可下载视频流",
            "parsed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "partial": True,
            "parse_error": "；".join(errors[-2:]),
        }

    if info is None:
        detail = "；".join(errors[-2:]) or "未知错误"
        if douyin_direct_error:
            detail = f"抖音直链解析 -> {douyin_direct_error}；{detail}"
        raise ShortVideoError(
            "短视频解析失败。请确认链接公开可访问、视频未删除，或重新从手机端复制分享链接。"
            f"平台：{platform}；原始链接：{source_url}；跳转链接：{expanded_url}；底层信息：{detail}"
        )

    if isinstance(info, dict) and info.get("_type") == "playlist" and info.get("entries"):
        info = next((item for item in info.get("entries") or [] if item), info)
    if not isinstance(info, dict):
        raise ShortVideoError("解析结果为空，请重新复制分享链接后再试。")

    title = _clean_caption(info.get("title")) or "短视频"
    caption = _best_caption(info) or share_caption or title
    return {
        "source_url": source_url,
        "expanded_url": expanded_url,
        "platform": platform,
        "title": title,
        "caption": caption,
        "description": _best_caption(info) or title,
        "share_caption": share_caption,
        "uploader": info.get("uploader") or info.get("creator") or info.get("channel") or "",
        "video_id": info.get("id") or "",
        "webpage_url": info.get("webpage_url") or expanded_url or source_url,
        "format_summary": _format_summary(info),
        "parsed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


def _snapshot_files(output_dir):
    if not os.path.isdir(output_dir):
        return {}
    return {
        os.path.abspath(os.path.join(output_dir, name)): os.path.getmtime(os.path.join(output_dir, name))
        for name in os.listdir(output_dir)
        if os.path.isfile(os.path.join(output_dir, name))
    }


def _newest_created_file(output_dir, before):
    after = _snapshot_files(output_dir)
    new_files = [path for path in after if path not in before or after[path] > before.get(path, 0)]
    if not new_files:
        return ""
    return max(new_files, key=lambda path: os.path.getmtime(path))


def _direct_download(url, output_path):
    with requests.get(url, headers=MOBILE_HEADERS, stream=True, timeout=60) as res:
        res.raise_for_status()
        with open(output_path, "wb") as f:
            for chunk in res.iter_content(chunk_size=1024 * 256):
                if chunk:
                    f.write(chunk)
    return output_path


def _run_ffmpeg(cmd):
    startupinfo = None
    if os.name == "nt":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="ignore", startupinfo=startupinfo)
    if proc.returncode != 0:
        raise ShortVideoError((proc.stderr or proc.stdout or "ffmpeg 执行失败").strip())


def download_short_video(info, output_dir, ffmpeg_location="", kind="video", cookie_options=None):
    try:
        from yt_dlp import YoutubeDL
    except Exception as exc:
        raise ShortVideoError(f"缺少 yt-dlp 下载组件，请先安装依赖。底层信息：{exc}")

    os.makedirs(output_dir, exist_ok=True)
    url = info.get("webpage_url") or info.get("expanded_url") or info.get("source_url")
    direct_url = info.get("direct_download_url") or ""
    if not url:
        raise ShortVideoError("缺少可下载链接，请先解析视频。")

    title = safe_filename(info.get("caption") or info.get("title") or "短视频")
    stamp = time.strftime("%Y%m%d_%H%M%S")
    if direct_url:
        if kind == "video":
            output_path = os.path.join(output_dir, f"{title}_{stamp}.mp4")
            return _direct_download(direct_url, output_path)
        if not ffmpeg_location or not os.path.exists(ffmpeg_location):
            raise ShortVideoError("缺少 ffmpeg，无法从抖音直链提取音频。")
        output_path = os.path.join(output_dir, f"{title}_{stamp}.mp3")
        _run_ffmpeg([
            ffmpeg_location,
            "-y", "-hide_banner", "-loglevel", "error",
            "-headers", "User-Agent: " + MOBILE_HEADERS["User-Agent"] + "\r\nReferer: https://www.douyin.com/\r\n",
            "-i", direct_url,
            "-vn", "-c:a", "libmp3lame", "-b:a", "192k",
            output_path,
        ])
        return output_path

    outtmpl = os.path.join(output_dir, f"{title}_{stamp}.%(ext)s")
    before = _snapshot_files(output_dir)

    common = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "outtmpl": outtmpl,
        "socket_timeout": 30,
    }
    common = _apply_cookie_options(common, cookie_options)
    if ffmpeg_location and os.path.exists(ffmpeg_location):
        common["ffmpeg_location"] = os.path.dirname(ffmpeg_location)

    if kind == "audio":
        common.update({
            "format": "bestaudio/best",
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }],
        })
    else:
        common.update({
            "format": "bv*+ba/best",
            "merge_output_format": "mp4",
        })

    try:
        with YoutubeDL(common) as ydl:
            ydl.extract_info(url, download=True)
    except Exception as exc:
        if kind == "video":
            raise ShortVideoError(f"原视频下载失败，请重新解析后再试。底层信息：{humanize_ytdlp_error(exc, cookie_options)}")
        raise ShortVideoError(f"音频下载失败，请重新解析后再试。底层信息：{humanize_ytdlp_error(exc, cookie_options)}")

    output = _newest_created_file(output_dir, before)
    if not output or not os.path.exists(output):
        raise ShortVideoError("下载完成但未找到输出文件，请检查输出目录权限。")
    return output
