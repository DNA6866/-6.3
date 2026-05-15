import os
import json
import re
import time
import html
from urllib.parse import urlparse

import requests


class LiveParseError(Exception):
    pass


DESKTOP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

KS_QUALITY_HEIGHT = {
    "蓝光": 1080,
    "超清": 720,
    "原画": 900,
    "高清": 540,
    "标清": 360,
}

DY_QUALITY_HEIGHT = {
    "FULL_HD": 1080,
    "FULL_HD1": 1080,
    "ORIGIN": 1080,
    "HD": 720,
    "HD1": 720,
    "SD": 540,
    "SD1": 540,
    "SD2": 540,
    "LD": 360,
    "LD1": 360,
}


def extract_first_url(text):
    match = re.search(r"https?://[^\s，。；,]+", text or "")
    if not match:
        raise LiveParseError("未识别到链接，请粘贴手机分享出来的完整直播间链接。")
    return match.group(0).strip()


def detect_platform(url):
    host = urlparse(url).netloc.lower()
    if "douyin" in host or "iesdouyin" in host or "amemv" in host:
        return "抖音"
    if "kuaishou" in host or "gifshow" in host:
        return "快手"
    return "未知平台"


def expand_share_url(url):
    try:
        res = requests.get(url, headers=DESKTOP_HEADERS, allow_redirects=True, timeout=12)
        return res.url or url
    except Exception:
        return url


def _format_height(fmt):
    height = fmt.get("height")
    if height:
        return int(height)
    note = fmt.get("format_note") or fmt.get("format") or ""
    match = re.search(r"(\d{3,4})p", str(note))
    return int(match.group(1)) if match else 0


def _kuaishou_height(name, level=0):
    text = str(name or "")
    for key, height in KS_QUALITY_HEIGHT.items():
        if key in text:
            return height
    try:
        level_value = int(level or 0)
    except Exception:
        level_value = 0
    if level_value >= 60:
        return 1080
    if level_value >= 50:
        return 720
    if level_value >= 40:
        return 540
    if level_value > 0:
        return 360
    return 0


def _extract_kuaishou_state(html):
    match = re.search(r"window\.__INITIAL_STATE__=(.*?);\(function", html, re.S)
    if not match:
        match = re.search(r"__INITIAL_STATE__=(.*?);\(function", html, re.S)
    if not match:
        raise LiveParseError("未在快手直播页中找到直播数据，可能页面结构已变化或需要重新分享链接。")

    state_text = match.group(1).strip()
    # 快手页面里偶尔会混入 JS 的 undefined，不是合法 JSON；转成 null 后再解析。
    state_text = re.sub(r":\s*undefined(?=,|})", ":null", state_text)
    try:
        return json.loads(state_text)
    except Exception as exc:
        raise LiveParseError(f"快手直播数据解析失败，请重新复制分享链接后再试。底层信息：{exc}")


def _iter_dicts(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _iter_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _iter_dicts(child)


def _find_kuaishou_play_item(state):
    liveroom = state.get("liveroom") if isinstance(state, dict) else {}
    for item in (liveroom or {}).get("playList") or []:
        if isinstance(item, dict) and item.get("liveStream"):
            return item
    for item in _iter_dicts(state):
        if isinstance(item.get("liveStream"), dict):
            return item
    return None


def _add_kuaishou_play_urls(formats, live_stream):
    play_urls = live_stream.get("playUrls") or {}
    if not isinstance(play_urls, dict):
        return
    for codec_name, codec_data in play_urls.items():
        adaptation = (codec_data or {}).get("adaptationSet") or {}
        representations = adaptation.get("representation") or []
        if isinstance(representations, dict):
            representations = [representations]
        for index, rep in enumerate(representations):
            if not isinstance(rep, dict):
                continue
            url = rep.get("url")
            if not url:
                continue
            name = rep.get("name") or rep.get("shortName") or rep.get("qualityType") or "直播流"
            bitrate = rep.get("bitrate") or 0
            level = rep.get("level") or 0
            formats.append({
                "format_id": f"ks-{codec_name}-{index}",
                "ext": "flv" if ".flv" in urlparse(url).path.lower() else "mp4",
                "protocol": "http-flv" if ".flv" in urlparse(url).path.lower() else "https",
                "height": _kuaishou_height(name, level),
                "tbr": bitrate,
                "url": url,
                "note": f"{name} {codec_name}".strip(),
            })


def _add_kuaishou_multi_urls(formats, live_stream):
    multi_urls = live_stream.get("multiResolutionPlayUrls") or []
    if isinstance(multi_urls, dict):
        multi_urls = list(multi_urls.values())
    for index, item in enumerate(multi_urls):
        if not isinstance(item, dict):
            continue
        url = item.get("url") or item.get("playUrl") or item.get("adaptationUrl")
        if not url:
            continue
        name = item.get("name") or item.get("quality") or item.get("qualityType") or "直播流"
        formats.append({
            "format_id": f"ks-multi-{index}",
            "ext": "flv" if ".flv" in urlparse(url).path.lower() else "mp4",
            "protocol": "http-flv" if ".flv" in urlparse(url).path.lower() else "https",
            "height": _kuaishou_height(name, item.get("level")),
            "tbr": item.get("bitrate") or 0,
            "url": url,
            "note": str(name),
        })


def _parse_kuaishou_live(url, source_url=None):
    try:
        response = requests.get(url, headers=DESKTOP_HEADERS, timeout=15)
        response.raise_for_status()
    except Exception as exc:
        raise LiveParseError(f"快手直播页访问失败，请确认链接可公开访问。底层信息：{exc}")

    state = _extract_kuaishou_state(response.text)
    play_item = _find_kuaishou_play_item(state)
    if not play_item:
        raise LiveParseError("未在快手页面中找到直播间信息，请确认主播正在直播。")

    live_stream = play_item.get("liveStream") or {}
    author = play_item.get("author") or {}
    formats = []
    _add_kuaishou_play_urls(formats, live_stream)
    _add_kuaishou_multi_urls(formats, live_stream)

    hls_url = live_stream.get("hlsPlayUrl")
    if hls_url:
        formats.append({
            "format_id": "ks-hls",
            "ext": "mp4",
            "protocol": "m3u8",
            "height": 1,
            "tbr": 0,
            "url": hls_url,
            "note": "HLS备用线路",
        })

    unique_formats = []
    seen = set()
    for fmt in formats:
        stream_url = fmt.get("url")
        if stream_url and stream_url not in seen:
            unique_formats.append(fmt)
            seen.add(stream_url)
    if not unique_formats:
        raise LiveParseError("已找到快手直播间，但没有解析到可录制的播放地址。")

    unique_formats.sort(key=lambda item: (item.get("height", 0), item.get("tbr", 0)), reverse=True)
    is_living = bool(play_item.get("isLiving") or live_stream.get("live") or live_stream.get("type") == "live")
    title = (
        live_stream.get("caption")
        or live_stream.get("id")
        or author.get("name")
        or "快手直播间录制"
    )
    return {
        "source_url": source_url or url,
        "expanded_url": response.url or url,
        "platform": "快手",
        "title": title,
        "uploader": author.get("name") or author.get("id") or "",
        "is_live": is_living,
        "formats": unique_formats,
        "parsed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


def _fix_douyin_text(value):
    value = value or ""
    try:
        return value.encode("latin1").decode("utf-8")
    except Exception:
        return value


def _extract_douyin_text(html_text, key):
    match = re.search(rf'\\"{re.escape(key)}\\":\\"(.*?)\\"', html_text)
    if not match:
        match = re.search(rf'"{re.escape(key)}":"(.*?)"', html_text)
    if not match:
        return ""
    text = match.group(1)
    text = text.replace("\\/", "/").replace("\\u0026", "&")
    if "\\u" in text:
        try:
            text = text.encode("utf-8").decode("unicode_escape")
        except Exception:
            pass
    return _fix_douyin_text(text)


def _douyin_quality_from_url(url):
    text = url.lower()
    if "_or4" in text or "origin" in text or "full_hd" in text:
        return "蓝光", 1080
    if "_hd" in text:
        return "超清", 720
    if "_sd" in text:
        return "高清", 540
    if "_ld" in text:
        return "标清", 360
    return "默认线路", 0


def _add_douyin_urls(formats, html_text, ext):
    text = html.unescape(html_text)
    text = text.replace("\\u0026", "&").replace("\\/", "/")
    pattern = r'https?://[^"\'<>\s\\]+?\.' + re.escape(ext) + r'[^"\'<>\s\\]*'
    for index, match in enumerate(re.finditer(pattern, text)):
        url = match.group(0)
        name, height = _douyin_quality_from_url(url)
        protocol = "http-flv" if ext == "flv" else "m3u8"
        formats.append({
            "format_id": f"dy-{ext}-{index}",
            "ext": "flv" if ext == "flv" else "mp4",
            "protocol": protocol,
            "height": height,
            "tbr": 0,
            "url": url,
            "note": f"{name} {protocol}",
        })


def _parse_douyin_live(url, source_url=None):
    try:
        response = requests.get(url, headers=DESKTOP_HEADERS, timeout=15)
        response.raise_for_status()
    except Exception as exc:
        raise LiveParseError(f"抖音直播页访问失败，请确认链接可公开访问。底层信息：{exc}")

    formats = []
    _add_douyin_urls(formats, response.text, "flv")
    _add_douyin_urls(formats, response.text, "m3u8")

    unique_formats = []
    seen = set()
    for fmt in formats:
        stream_url = fmt.get("url")
        if stream_url and stream_url not in seen:
            unique_formats.append(fmt)
            seen.add(stream_url)
    if not unique_formats:
        raise LiveParseError("未从抖音直播页解析到可录制的播放地址，可能直播已结束或页面触发了风控。")

    title = _extract_douyin_text(response.text, "title") or "抖音直播间录制"
    uploader = _extract_douyin_text(response.text, "nickname")
    unique_formats.sort(key=lambda item: (item.get("height", 0), 1 if item.get("protocol") == "http-flv" else 0), reverse=True)
    return {
        "source_url": source_url or url,
        "expanded_url": response.url or url,
        "platform": "抖音",
        "title": title,
        "uploader": uploader,
        "is_live": True,
        "formats": unique_formats,
        "parsed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


def parse_live_url(raw_text):
    source_url = extract_first_url(raw_text)
    expanded_url = expand_share_url(source_url)
    platform = detect_platform(expanded_url or source_url)

    if platform == "快手":
        return _parse_kuaishou_live(expanded_url, source_url)
    if platform == "抖音" and "amemv" in urlparse(expanded_url).netloc.lower():
        return _parse_douyin_live(expanded_url, source_url)

    try:
        from yt_dlp import YoutubeDL
    except Exception as exc:
        raise LiveParseError(f"缺少 yt-dlp 解析组件，请先安装依赖。底层信息：{exc}")

    options = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "socket_timeout": 15,
        "extractor_args": {},
    }
    try:
        with YoutubeDL(options) as ydl:
            info = ydl.extract_info(expanded_url, download=False)
    except Exception as exc:
        raise LiveParseError(
            "直播间解析失败。请确认直播间正在直播、链接是公开可访问的，并且没有需要登录/风控验证。"
            f"底层信息：{exc}"
        )

    formats = []
    for fmt in info.get("formats") or []:
        url = fmt.get("url")
        if not url:
            continue
        protocols = str(fmt.get("protocol") or "")
        if any(key in protocols for key in ("m3u8", "http", "https")) or url.startswith("http"):
            formats.append({
                "format_id": fmt.get("format_id") or "",
                "ext": fmt.get("ext") or "mp4",
                "protocol": protocols or "http",
                "height": _format_height(fmt),
                "tbr": fmt.get("tbr") or 0,
                "url": url,
                "note": fmt.get("format_note") or fmt.get("format") or "",
            })
    if not formats and info.get("url"):
        formats.append({
            "format_id": info.get("format_id") or "default",
            "ext": info.get("ext") or "mp4",
            "protocol": info.get("protocol") or "http",
            "height": _format_height(info),
            "tbr": info.get("tbr") or 0,
            "url": info["url"],
            "note": info.get("format_note") or "默认线路",
        })
    if not formats:
        raise LiveParseError("未从直播间解析到可下载的直播流地址。")

    formats.sort(key=lambda item: (item.get("height", 0), item.get("tbr", 0)), reverse=True)
    return {
        "source_url": source_url,
        "expanded_url": expanded_url,
        "platform": platform,
        "title": info.get("title") or "直播间录制",
        "uploader": info.get("uploader") or info.get("creator") or "",
        "is_live": bool(info.get("is_live")),
        "formats": formats,
        "parsed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


def select_stream_format(formats, quality="best"):
    if not formats:
        raise LiveParseError("没有可用直播流。")
    ordered = sorted(formats, key=lambda item: (item.get("height", 0), item.get("tbr", 0)), reverse=True)
    quality = quality or "best"
    if quality == "lowest":
        return ordered[-1]
    if quality == "high" and len(ordered) >= 2:
        return ordered[1]
    if quality == "medium" and len(ordered) >= 3:
        return ordered[min(2, len(ordered) - 1)]
    if quality == "low" and len(ordered) >= 4:
        return ordered[min(3, len(ordered) - 1)]
    return ordered[0]


def safe_filename(text):
    text = re.sub(r"[\\/:*?\"<>|]+", "_", text or "")
    text = re.sub(r"\s+", "_", text).strip("_")
    return text[:80] or "live_record"


def build_output_path(output_dir, title, ext="mp4"):
    os.makedirs(output_dir, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    return os.path.join(output_dir, f"{safe_filename(title)}_{stamp}.{ext}")


def build_segment_output_pattern(output_dir, title, ext="mp4"):
    os.makedirs(output_dir, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    prefix = os.path.join(output_dir, f"{safe_filename(title)}_{stamp}")
    return f"{prefix}_%03d.{ext}", prefix
