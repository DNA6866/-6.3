"""花字字幕预设库（50+ 种）。

每项是一个 ASS 样式参数组合，批量混剪字幕与「视频加字幕」共用。
字段说明：
  name         预设中文名（UI 显示）
  font         字体名（可留空表示默认）
  size         字号（1080x1920 设计稿基准）
  color        主色（#RRGGBB）
  border_color 描边色（#RRGGBB）
  border_w     描边宽度
  margin_v     底部边距
  shadow       阴影偏移（0=无）
  bold         True=粗体 False=细体
  swatch       缩略色块主色（UI 预览用）
"""

SUB_STYLES = [
    # ── 经典款 ──
    {"name": "白字黑边 经典", "font": "微软雅黑", "size": 85, "color": "#FFFFFF", "border_color": "#000000", "border_w": 5, "margin_v": 180, "shadow": 0, "bold": True, "swatch": "#FFFFFF"},
    {"name": "白字蓝边 清爽", "font": "微软雅黑", "size": 85, "color": "#FFFFFF", "border_color": "#0055CC", "border_w": 5, "margin_v": 180, "shadow": 0, "bold": True, "swatch": "#FFFFFF"},
    {"name": "白字黑边 粗描", "font": "微软雅黑", "size": 90, "color": "#FFFFFF", "border_color": "#000000", "border_w": 9, "margin_v": 180, "shadow": 0, "bold": True, "swatch": "#FFFFFF"},
    {"name": "白字黑边 投影", "font": "微软雅黑", "size": 85, "color": "#FFFFFF", "border_color": "#000000", "border_w": 4, "margin_v": 180, "shadow": 4, "bold": True, "swatch": "#FFFFFF"},
    {"name": "黑字白边 雅致", "font": "微软雅黑", "size": 85, "color": "#000000", "border_color": "#FFFFFF", "border_w": 4, "margin_v": 180, "shadow": 0, "bold": True, "swatch": "#000000"},
    {"name": "黑字黄边 复古", "font": "微软雅黑", "size": 85, "color": "#000000", "border_color": "#FFD700", "border_w": 5, "margin_v": 180, "shadow": 0, "bold": True, "swatch": "#000000"},
    # ── 撞色款 ──
    {"name": "黄字红边 醒目", "font": "微软雅黑", "size": 90, "color": "#FFFF00", "border_color": "#FF0000", "border_w": 5, "margin_v": 180, "shadow": 0, "bold": True, "swatch": "#FFFF00"},
    {"name": "黄字红边 粗描", "font": "微软雅黑", "size": 95, "color": "#FFFF00", "border_color": "#FF0000", "border_w": 9, "margin_v": 180, "shadow": 0, "bold": True, "swatch": "#FFFF00"},
    {"name": "黄字黑边 抖音风", "font": "微软雅黑", "size": 90, "color": "#FFFF00", "border_color": "#000000", "border_w": 6, "margin_v": 180, "shadow": 0, "bold": True, "swatch": "#FFFF00"},
    {"name": "青字紫边 潮流", "font": "微软雅黑", "size": 90, "color": "#00FFFF", "border_color": "#800080", "border_w": 5, "margin_v": 180, "shadow": 0, "bold": True, "swatch": "#00FFFF"},
    {"name": "青字蓝边 科技", "font": "微软雅黑", "size": 85, "color": "#00FFFF", "border_color": "#0000FF", "border_w": 5, "margin_v": 180, "shadow": 0, "bold": True, "swatch": "#00FFFF"},
    {"name": "粉字紫边 甜美", "font": "微软雅黑", "size": 85, "color": "#FF69B4", "border_color": "#800080", "border_w": 5, "margin_v": 180, "shadow": 0, "bold": True, "swatch": "#FF69B4"},
    {"name": "粉字红边 俏皮", "font": "微软雅黑", "size": 85, "color": "#FF69B4", "border_color": "#FF0000", "border_w": 5, "margin_v": 180, "shadow": 0, "bold": True, "swatch": "#FF69B4"},
    {"name": "橙字蓝边 活力", "font": "微软雅黑", "size": 85, "color": "#FF8C00", "border_color": "#003366", "border_w": 5, "margin_v": 180, "shadow": 0, "bold": True, "swatch": "#FF8C00"},
    {"name": "橙字白边 明亮", "font": "微软雅黑", "size": 85, "color": "#FF8C00", "border_color": "#FFFFFF", "border_w": 4, "margin_v": 180, "shadow": 0, "bold": True, "swatch": "#FF8C00"},
    {"name": "绿字白边 清新", "font": "微软雅黑", "size": 85, "color": "#00FF00", "border_color": "#FFFFFF", "border_w": 4, "margin_v": 180, "shadow": 0, "bold": True, "swatch": "#00FF00"},
    {"name": "绿字黑边 自然", "font": "微软雅黑", "size": 85, "color": "#00FF00", "border_color": "#000000", "border_w": 5, "margin_v": 180, "shadow": 0, "bold": True, "swatch": "#00FF00"},
    {"name": "金字黑边 贵气", "font": "微软雅黑", "size": 90, "color": "#FFD700", "border_color": "#000000", "border_w": 5, "margin_v": 180, "shadow": 0, "bold": True, "swatch": "#FFD700"},
    {"name": "金字红边 喜庆", "font": "微软雅黑", "size": 90, "color": "#FFD700", "border_color": "#FF0000", "border_w": 5, "margin_v": 180, "shadow": 0, "bold": True, "swatch": "#FFD700"},
    {"name": "蓝字白边 稳重", "font": "微软雅黑", "size": 85, "color": "#00A5FF", "border_color": "#FFFFFF", "border_w": 4, "margin_v": 180, "shadow": 0, "bold": True, "swatch": "#00A5FF"},
    {"name": "蓝字黑边 深邃", "font": "微软雅黑", "size": 85, "color": "#00A5FF", "border_color": "#000000", "border_w": 5, "margin_v": 180, "shadow": 0, "bold": True, "swatch": "#00A5FF"},
    {"name": "红字白边 热烈", "font": "微软雅黑", "size": 85, "color": "#FF0000", "border_color": "#FFFFFF", "border_w": 4, "margin_v": 180, "shadow": 0, "bold": True, "swatch": "#FF0000"},
    {"name": "玫红白边 时尚", "font": "微软雅黑", "size": 85, "color": "#E91E63", "border_color": "#FFFFFF", "border_w": 4, "margin_v": 180, "shadow": 0, "bold": True, "swatch": "#E91E63"},
    {"name": "紫字黄边 创意", "font": "微软雅黑", "size": 85, "color": "#FF00FF", "border_color": "#FFD700", "border_w": 5, "margin_v": 180, "shadow": 0, "bold": True, "swatch": "#FF00FF"},
    # ── 底框款（粗描边模拟底框） ──
    {"name": "白字黑底 字幕条", "font": "微软雅黑", "size": 88, "color": "#FFFFFF", "border_color": "#000000", "border_w": 16, "margin_v": 190, "shadow": 0, "bold": True, "swatch": "#000000"},
    {"name": "黄字黑底 标题条", "font": "微软雅黑", "size": 95, "color": "#FFFF00", "border_color": "#000000", "border_w": 16, "margin_v": 190, "shadow": 0, "bold": True, "swatch": "#000000"},
    {"name": "青字黑底 科技条", "font": "微软雅黑", "size": 88, "color": "#00FFFF", "border_color": "#000000", "border_w": 15, "margin_v": 190, "shadow": 0, "bold": True, "swatch": "#000000"},
    {"name": "粉字黑底 卡哇伊", "font": "微软雅黑", "size": 88, "color": "#FF69B4", "border_color": "#000000", "border_w": 15, "margin_v": 190, "shadow": 0, "bold": True, "swatch": "#000000"},
    {"name": "白字红底 综艺", "font": "微软雅黑", "size": 90, "color": "#FFFFFF", "border_color": "#FF0000", "border_w": 15, "margin_v": 190, "shadow": 0, "bold": True, "swatch": "#FF0000"},
    {"name": "黄字红底 促销", "font": "微软雅黑", "size": 95, "color": "#FFFF00", "border_color": "#FF0000", "border_w": 16, "margin_v": 190, "shadow": 0, "bold": True, "swatch": "#FF0000"},
    {"name": "黑字黄底 醒目条", "font": "微软雅黑", "size": 90, "color": "#000000", "border_color": "#FFD700", "border_w": 16, "margin_v": 190, "shadow": 0, "bold": True, "swatch": "#FFD700"},
    {"name": "白字蓝底 冷色条", "font": "微软雅黑", "size": 88, "color": "#FFFFFF", "border_color": "#003366", "border_w": 15, "margin_v": 190, "shadow": 0, "bold": True, "swatch": "#003366"},
    # ── 大字款 ──
    {"name": "超大白字 冲击", "font": "微软雅黑", "size": 110, "color": "#FFFFFF", "border_color": "#000000", "border_w": 7, "margin_v": 190, "shadow": 0, "bold": True, "swatch": "#FFFFFF"},
    {"name": "超大黄字 醒目", "font": "微软雅黑", "size": 115, "color": "#FFFF00", "border_color": "#FF0000", "border_w": 8, "margin_v": 190, "shadow": 0, "bold": True, "swatch": "#FFFF00"},
    {"name": "超大金字 高端", "font": "微软雅黑", "size": 110, "color": "#FFD700", "border_color": "#000000", "border_w": 6, "margin_v": 190, "shadow": 3, "bold": True, "swatch": "#FFD700"},
    {"name": "超大青字 科技感", "font": "微软雅黑", "size": 110, "color": "#00FFFF", "border_color": "#003366", "border_w": 7, "margin_v": 190, "shadow": 0, "bold": True, "swatch": "#00FFFF"},
    {"name": "超大粉字 少女感", "font": "微软雅黑", "size": 110, "color": "#FF69B4", "border_color": "#FFFFFF", "border_w": 6, "margin_v": 190, "shadow": 3, "bold": True, "swatch": "#FF69B4"},
    # ── 细体款 ──
    {"name": "细白字 极简", "font": "微软雅黑", "size": 75, "color": "#FFFFFF", "border_color": "#000000", "border_w": 2, "margin_v": 160, "shadow": 0, "bold": False, "swatch": "#FFFFFF"},
    {"name": "细黄字 文艺", "font": "微软雅黑", "size": 75, "color": "#FFFF00", "border_color": "#000000", "border_w": 2, "margin_v": 160, "shadow": 0, "bold": False, "swatch": "#FFFF00"},
    {"name": "细青字 高级灰感", "font": "微软雅黑", "size": 75, "color": "#00FFFF", "border_color": "#333333", "border_w": 2, "margin_v": 160, "shadow": 0, "bold": False, "swatch": "#00FFFF"},
    {"name": "细白字 轻投影", "font": "微软雅黑", "size": 75, "color": "#FFFFFF", "border_color": "#000000", "border_w": 2, "margin_v": 160, "shadow": 3, "bold": False, "swatch": "#FFFFFF"},
    # ── 渐变感（双色描边模拟） ──
    {"name": "白字青边 清新渐变", "font": "微软雅黑", "size": 85, "color": "#FFFFFF", "border_color": "#00A5FF", "border_w": 6, "margin_v": 180, "shadow": 0, "bold": True, "swatch": "#00A5FF"},
    {"name": "白字粉边 梦幻", "font": "微软雅黑", "size": 85, "color": "#FFFFFF", "border_color": "#FF69B4", "border_w": 6, "margin_v": 180, "shadow": 0, "bold": True, "swatch": "#FF69B4"},
    {"name": "黄字橙边 暖阳", "font": "微软雅黑", "size": 90, "color": "#FFFF00", "border_color": "#FF8C00", "border_w": 6, "margin_v": 180, "shadow": 0, "bold": True, "swatch": "#FF8C00"},
    {"name": "粉字橙边 活力渐", "font": "微软雅黑", "size": 85, "color": "#FF69B4", "border_color": "#FF8C00", "border_w": 6, "margin_v": 180, "shadow": 0, "bold": True, "swatch": "#FF69B4"},
    {"name": "青字绿边 森林", "font": "微软雅黑", "size": 85, "color": "#00FFFF", "border_color": "#00FF00", "border_w": 6, "margin_v": 180, "shadow": 0, "bold": True, "swatch": "#00FFFF"},
    # ── 描边特粗款 ──
    {"name": "特粗白边 海报", "font": "微软雅黑", "size": 95, "color": "#FFFFFF", "border_color": "#000000", "border_w": 12, "margin_v": 185, "shadow": 0, "bold": True, "swatch": "#FFFFFF"},
    {"name": "特粗黄边 贴纸", "font": "微软雅黑", "size": 95, "color": "#FFFF00", "border_color": "#FF0000", "border_w": 12, "margin_v": 185, "shadow": 0, "bold": True, "swatch": "#FFFF00"},
    {"name": "特粗金边 高端贴", "font": "微软雅黑", "size": 95, "color": "#FFD700", "border_color": "#8B0000", "border_w": 12, "margin_v": 185, "shadow": 0, "bold": True, "swatch": "#FFD700"},
    {"name": "特粗青边 霓虹", "font": "微软雅黑", "size": 95, "color": "#00FFFF", "border_color": "#FF00FF", "border_w": 11, "margin_v": 185, "shadow": 0, "bold": True, "swatch": "#00FFFF"},
    # ── 阴影款 ──
    {"name": "白字黑阴影 立体", "font": "微软雅黑", "size": 90, "color": "#FFFFFF", "border_color": "#000000", "border_w": 4, "margin_v": 185, "shadow": 8, "bold": True, "swatch": "#FFFFFF"},
    {"name": "黄字黑阴影 立体醒", "font": "微软雅黑", "size": 95, "color": "#FFFF00", "border_color": "#000000", "border_w": 4, "margin_v": 185, "shadow": 8, "bold": True, "swatch": "#FFFF00"},
    {"name": "青字深蓝影 悬浮", "font": "微软雅黑", "size": 90, "color": "#00FFFF", "border_color": "#003366", "border_w": 4, "margin_v": 185, "shadow": 7, "bold": True, "swatch": "#00FFFF"},
    {"name": "粉字紫影 温柔", "font": "微软雅黑", "size": 90, "color": "#FF69B4", "border_color": "#800080", "border_w": 4, "margin_v": 185, "shadow": 7, "bold": True, "swatch": "#FF69B4"},
    # ── 高位置款（适合中上部显示） ──
    {"name": "白字黑边 高位", "font": "微软雅黑", "size": 85, "color": "#FFFFFF", "border_color": "#000000", "border_w": 5, "margin_v": 800, "shadow": 0, "bold": True, "swatch": "#FFFFFF"},
    {"name": "黄字红边 高位", "font": "微软雅黑", "size": 90, "color": "#FFFF00", "border_color": "#FF0000", "border_w": 5, "margin_v": 800, "shadow": 0, "bold": True, "swatch": "#FFFF00"},
]

# 名 → 预设映射，方便查找
SUB_STYLE_MAP = {item["name"]: item for item in SUB_STYLES}

# UI 默认选中第一个
DEFAULT_SUB_STYLE = SUB_STYLES[0]["name"] if SUB_STYLES else ""


def render_style_swatch(preset, size=(100, 42)):
    """渲染"花字"两个字的预设效果图，作为 UI 预览缩略图。

    用 QPixmap + QPainter 直接绘制，描边用偏移绘制模拟，阴影/底框按预设渲染，
    让客户在点选前就能直观看到该花字效果。
    """
    try:
        from PyQt5.QtCore import QPoint, QRect, Qt
        from PyQt5.QtGui import QColor, QFont, QFontMetrics, QPainter, QPixmap
    except Exception:
        return None

    w, h = size
    pix = QPixmap(w, h)
    pix.fill(QColor("#0B1120"))
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.Antialiasing, True)

    color = str(preset.get("color", "#FFFFFF"))
    border_color = str(preset.get("border_color", "#000000"))
    border_w = max(1, int(preset.get("border_w", 4) or 4) // 2)
    shadow = int(preset.get("shadow", 0) or 0)
    bold = bool(preset.get("bold", True))
    font_name = str(preset.get("font", "微软雅黑") or "微软雅黑")

    font = QFont(font_name, 16)
    font.setBold(bold)
    painter.setFont(font)

    text = "花字"
    fm = QFontMetrics(font)
    tw = fm.horizontalAdvance(text)
    th = fm.height()
    x = (w - tw) // 2
    y = (h + th) // 2 - fm.descent()

    # 阴影
    if shadow > 0:
        painter.setPen(QColor("#000000"))
        painter.drawText(QPoint(x + shadow, y + shadow), text)

    # 描边（8 方向偏移模拟）
    if border_w > 0:
        bc = QColor(border_color)
        painter.setPen(bc)
        for dx, dy in [(-border_w, 0), (border_w, 0), (0, -border_w), (0, border_w),
                       (-border_w, -border_w), (border_w, -border_w),
                       (-border_w, border_w), (border_w, border_w)]:
            painter.drawText(QPoint(x + dx, y + dy), text)

    # 主色
    painter.setPen(QColor(color))
    painter.drawText(QPoint(x, y), text)
    painter.end()
    return pix
