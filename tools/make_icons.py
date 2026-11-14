#!/usr/bin/env python3
"""Генерирует fiptui/icons.py из assets/icons/*.svg.

Терминал не умеет рисовать SVG, поэтому векторы один раз превращаются в «Брайль-графику»
(2x4 точки на клетку). Результат кладётся в репозиторий, пользователям ничего ставить не нужно.

Поддерживаются иконки в стиле Feather (контур, сетка 24x24): polygon, polyline, line, rect, circle.
Запуск:  python3 tools/make_icons.py            (нужен Pillow)
         python3 tools/make_icons.py --preview  (показать иконки в консоли, файл не писать)
"""
from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "assets" / "icons"
OUT = ROOT / "fiptui" / "icons.py"

NAMES = ("shuffle", "prev", "back", "play", "pause", "fwd", "next", "repeat", "voldn", "volup")
# размер в точках (ширина = высота): клеток = точки/2 по ширине, точки/4 по высоте
SIZES = {"s": 12, "l": 16}
# иконки-«кнопки» рисуем залитыми: контур на 12-16 точках плохо читается. Остальные — линейные.
FILLED = {"prev", "back", "play", "pause", "fwd", "next", "voldn", "volup"}
THRESHOLD = 0.34          # доля закрашенной площади точки, при которой она «горит»
SCALE = 16                # супersample: пикселей на единицу сетки 24x24

_BRAILLE_BITS = ((0x01, 0x08), (0x02, 0x10), (0x04, 0x20), (0x40, 0x80))   # [строка][колонка]


def _tag(el) -> str:
    return el.tag.rsplit("}", 1)[-1]


def _nums(text: str) -> list[float]:
    return [float(x) for x in text.replace(",", " ").split()]


def rasterize(svg_path: Path, px: int, filled: bool = False) -> Image.Image:
    """Рисует контур иконки в градациях серого (0..255), размер px x px."""
    root = ET.parse(svg_path).getroot()
    vb = _nums(root.get("viewBox", "0 0 24 24"))
    if len(vb) != 4 or vb[2] != vb[3]:
        raise ValueError(f"{svg_path.name}: нужен квадратный viewBox")
    k = px / vb[2]
    stroke = float(root.get("stroke-width", "2")) * k
    img = Image.new("L", (px, px), 0)
    d = ImageDraw.Draw(img)

    def pt(x, y):
        return ((x - vb[0]) * k, (y - vb[1]) * k)

    def line(points, closed=False):
        pts = [pt(*p) for p in points] + ([pt(*points[0])] if closed else [])
        for a, b in zip(pts, pts[1:]):
            d.line([a, b], fill=255, width=round(stroke))
        for x, y in pts:                                     # круглые стыки и концы
            r = stroke / 2
            d.ellipse([x - r, y - r, x + r, y + r], fill=255)

    for el in root.iter():
        t = _tag(el)
        if t in ("polygon", "polyline"):
            n = _nums(el.get("points", ""))
            pts = list(zip(n[0::2], n[1::2]))
            if filled and t == "polygon":
                d.polygon([pt(*p) for p in pts], fill=255)
            line(pts, closed=(t == "polygon"))
        elif t == "line":
            line([(float(el.get("x1")), float(el.get("y1"))), (float(el.get("x2")), float(el.get("y2")))])
        elif t == "rect":
            x, y, w, h = (float(el.get(a)) for a in ("x", "y", "width", "height"))
            if filled:
                d.rectangle([pt(x, y), pt(x + w, y + h)], fill=255)
            line([(x, y), (x + w, y), (x + w, y + h), (x, y + h)], closed=True)
        elif t == "circle":
            cx, cy, r = (float(el.get(a)) for a in ("cx", "cy", "r"))
            (x0, y0), (x1, y1) = pt(cx - r, cy - r), pt(cx + r, cy + r)
            d.ellipse([x0, y0, x1, y1], outline=255, width=round(stroke))
        elif t not in ("svg", "g", "title", "desc", "defs"):
            raise ValueError(f"{svg_path.name}: элемент <{t}> не поддерживается (только polygon/polyline/line/rect/circle)")
    return img


def to_braille(img: Image.Image, dots: int, threshold: float = THRESHOLD) -> list[str]:
    """dots x dots точек -> строки из символов Брайля. Пустые клетки — обычный пробел."""
    small = img.resize((dots, dots), Image.BOX)            # BOX = доля закрашенной площади
    px = small.load()
    rows = []
    for cy in range(0, dots, 4):
        line = ""
        for cx in range(0, dots, 2):
            bits = 0
            for dy in range(4):
                for dx in range(2):
                    x, y = cx + dx, cy + dy
                    if x < dots and y < dots and px[x, y] / 255 >= threshold:
                        bits |= _BRAILLE_BITS[dy][dx]
            line += chr(0x2800 + bits) if bits else " "
        rows.append(line)
    return rows


def build() -> dict[str, dict[str, list[str]]]:
    data: dict[str, dict[str, list[str]]] = {}
    for name in NAMES:
        path = SRC / f"{name}.svg"
        if not path.exists():
            raise SystemExit(f"нет файла {path}")
        data[name] = {}
        for key, dots in SIZES.items():
            data[name][key] = to_braille(rasterize(path, dots * SCALE, filled=name in FILLED), dots)
    return data


def render_module(data) -> str:
    out = ['"""Иконки кнопок в виде Брайль-графики.',
           "",
           "АВТОГЕНЕРАЦИЯ: python3 tools/make_icons.py (из assets/icons/*.svg). Руками не править.",
           '"""',
           "",
           "BRAILLE: dict[str, dict[str, tuple[str, ...]]] = {"]
    for name, sizes in data.items():
        out.append(f'    "{name}": {{')
        for key, rows in sizes.items():
            out.append(f'        "{key}": (')
            out += [f"            {r!r}," for r in rows]
            out.append("        ),")
        out.append("    },")
    out.append("}")
    return "\n".join(out) + "\n"


if __name__ == "__main__":
    data = build()
    if "--preview" in sys.argv:
        for name, sizes in data.items():
            print(f"── {name}")
            for key, rows in sizes.items():
                print(f"[{key}]")
                print("\n".join("|" + r + "|" for r in rows))
    else:
        OUT.write_text(render_module(data), encoding="utf-8")
        print(f"записано {OUT.relative_to(ROOT)}")
