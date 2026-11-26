"""共用 Obsidian Canvas 浅色配色。"""

import colorsys


ROOT_COLOR = "#b8b4ad"
_BRANCH_COLORS = (
    "#8faec5",  # 雾蓝
    "#a6b78f",  # 淡苔绿
    "#c4a28b",  # 暖陶
    "#ae9fc3",  # 灰紫
    "#92b8b2",  # 灰青
    "#c5a0ab",  # 灰粉
    "#c2b485",  # 亚麻黄
    "#a1a9cb",  # 淡靛
)


def branch_color(index):
    if index < len(_BRANCH_COLORS):
        return _BRANCH_COLORS[index]
    # 额外章节继续按色相分散，保持亮度和饱和度一致。
    hue = (0.10 + (index - len(_BRANCH_COLORS)) * 0.61803398875) % 1
    rgb = colorsys.hls_to_rgb(hue, 0.70, 0.24)
    return "#" + "".join(f"{round(component * 255):02x}" for component in rgb)
