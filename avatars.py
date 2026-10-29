"""Генерация двух премиальных аватарок 1024x1024 (суперсэмплинг x2 из 2048).

Стиль: тёмный неоновый Steam-scape (#0f141d, #1b2838, неон #66c0f4, лайм-изумруд #a4d007),
векторная геометрия, сетка, стеклянные блики, свечение.
"""
import math
import os

from PIL import Image, ImageDraw, ImageFilter

import fonts
from config import cfg

S = 2          # суперсэмплинг
FULL = 1024
SZ = FULL * S  # 2048

INK = (15, 20, 29)       # #0f141d
INK2 = (27, 40, 56)      # #1b2838
BLUE = (102, 192, 244)   # #66c0f4
LIME = (164, 208, 7)     # #a4d007
EMERALD = (46, 204, 113)
WHITE = (240, 246, 252)
MUTED = (120, 140, 160)


def _p(v):
    return int(v * S)


def _box(b):
    return [_p(b[0]), _p(b[1]), _p(b[2]), _p(b[3])]


def _grad_v(size, top, bottom):
    img = Image.new("RGBA", (size, size))
    d = ImageDraw.Draw(img)
    for y in range(size):
        t = y / max(1, size - 1)
        c = tuple(int(a + (b - a) * t) for a, b in zip(top, bottom)) + (255,)
        d.line([(0, y), (size, y)], fill=c)
    return img


def _background():
    img = _grad_v(SZ, INK, INK2)
    # неоновая сетка
    grid = Image.new("RGBA", (SZ, SZ), (0, 0, 0, 0))
    gd = ImageDraw.Draw(grid)
    step = _p(64)
    for x in range(0, SZ, step):
        gd.line([(x, 0), (x, SZ)], fill=(102, 192, 244, 16), width=1)
    for y in range(0, SZ, step):
        gd.line([(0, y), (SZ, y)], fill=(102, 192, 244, 16), width=1)
    img = Image.alpha_composite(img, grid)
    # свечение (blobs)
    glow = Image.new("RGBA", (SZ, SZ), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.ellipse(_box((80, 120, 640, 680)), fill=(46, 204, 113, 60))
    gd.ellipse(_box((520, 520, 1040, 1040)), fill=(102, 192, 244, 70))
    gd.ellipse(_box((260, 640, 820, 1000)), fill=(164, 208, 7, 40))
    glow = glow.filter(ImageFilter.GaussianBlur(_p(120)))
    img = Image.alpha_composite(img, glow)
    # виньетка
    vig = Image.new("L", (SZ, SZ), 0)
    vd = ImageDraw.Draw(vig)
    vd.ellipse(_box((-260, -260, 1284, 1284)), fill=255)
    vig = vig.filter(ImageFilter.GaussianBlur(_p(60)))
    dark = Image.new("RGBA", (SZ, SZ), (0, 0, 0, 150))
    img = Image.composite(img, Image.alpha_composite(img, dark), vig)
    return img


def _neon_ring(img, cx, cy, r, color, w):
    layer = Image.new("RGBA", (SZ, SZ), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    d.ellipse(_box((cx - r, cy - r, cx + r, cy + r)), outline=color + (255,), width=_p(w))
    img.alpha_composite(layer)
    glow = layer.filter(ImageFilter.GaussianBlur(_p(16)))
    img.alpha_composite(glow)
    return img


def _shield_points(cx, cy, w, h):
    top = cy - h / 2
    bot = cy + h / 2
    return [
        (cx - w / 2, top + h * 0.06),
        (cx, top),
        (cx + w / 2, top + h * 0.06),
        (cx + w / 2, cy + h * 0.16),
        (cx, bot),
        (cx - w / 2, cy + h * 0.16),
    ]


def _draw_shield(img, cx, cy, w, h):
    pts = [( _p(x), _p(y)) for x, y in _shield_points(cx, cy, w, h)]
    # тень/свечение
    glow = Image.new("RGBA", (SZ, SZ), (0, 0, 0, 0))
    ImageDraw.Draw(glow).polygon(pts, fill=(102, 192, 244, 150))
    img.alpha_composite(glow.filter(ImageFilter.GaussianBlur(_p(28))))
    # заливка щита (вертикальный градиент)
    grad = _grad_v(_p(h), (42, 71, 94), (20, 30, 46)).resize((_p(w), _p(h)))
    mask = Image.new("L", (SZ, SZ), 0)
    ImageDraw.Draw(mask).polygon(pts, fill=255)
    fill_layer = Image.new("RGBA", (SZ, SZ), (0, 0, 0, 0))
    fill_layer.paste(grad, (_p(cx - w / 2), _p(cy - h / 2)))
    img.paste(fill_layer, (0, 0), mask)
    # контур
    d = ImageDraw.Draw(img)
    d.polygon(pts, outline=(102, 192, 244, 255))
    d.line(pts + [pts[0]], fill=(102, 192, 244, 255), width=_p(6))
    # внутренний контур
    inner = [( _p(cx + (x / S - cx) * 0.86), _p(cy + (y / S - cy) * 0.86)) for x, y in pts]
    d.line(inner + [inner[0]], fill=(164, 208, 7, 180), width=_p(3))


def _steam_wisps(d, cx, cy):
    # стилизованный пар: три изогнутые линии с точками
    for i, off in enumerate((-70, 0, 70)):
        pts = []
        for t in range(0, 101, 5):
            yy = cy - t * 0.9
            xx = cx + off * (1 - t / 130.0) + math.sin(t / 16.0 + i) * 6
            pts.append((_p(xx), _p(yy)))
        col = (102, 192, 244, 160 - i * 30)
        d.line(pts, fill=col, width=_p(5), joint="curve")


def _gamepad(img, cx, cy, w, h, color):
    # стилизованный геймпад
    d = ImageDraw.Draw(img)
    body = [_p(cx - w / 2), _p(cy - h / 2), _p(cx + w / 2), _p(cy + h / 2)]
    d.rounded_rectangle(body, radius=_p(h / 2), fill=color + (255,))
    # крестовина
    c1 = (_p(cx - w * 0.22), _p(cy - h * 0.12))
    d.rounded_rectangle([c1[0] - _p(4), c1[1] - _p(16), c1[0] + _p(4), c1[1] + _p(16)], radius=_p(3), fill=(15, 20, 29, 255))
    d.rounded_rectangle([c1[0] - _p(16), c1[1] - _p(4), c1[0] + _p(16), c1[1] + _p(4)], radius=_p(3), fill=(15, 20, 29, 255))
    # кнопки
    for dx, dy in ((w * 0.22, -h * 0.12), (w * 0.32, 0.0), (w * 0.22, h * 0.12), (w * 0.12, 0.0)):
        bx, by = _p(cx + dx), _p(cy + dy)
        d.ellipse([bx - _p(7), by - _p(7), bx + _p(7), by + _p(7)], fill=(15, 20, 29, 255))


def _plate(img, cx, cy, w, h, fill, text, weight, tsize, tcol):
    layer = Image.new("RGBA", (SZ, SZ), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    d.rounded_rectangle(_box((cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2)),
                        radius=_p(h / 2), fill=fill + (255,))
    # стеклянный блик сверху
    d.rounded_rectangle(_box((cx - w / 2 + 6, cy - h / 2 + 6, cx + w / 2 - 6, cy - h / 2 + h * 0.42)),
                        radius=_p(h * 0.4), fill=(255, 255, 255, 40))
    img.alpha_composite(layer)
    # свечение
    gl = layer.filter(ImageFilter.GaussianBlur(_p(14)))
    img.alpha_composite(gl)
    # текст
    f = fonts.font(weight, _p(tsize))
    dd = ImageDraw.Draw(img)
    dd.text((_p(cx), _p(cy)), text, font=f, fill=tcol + (255,), anchor="mm")


def _text(img, xy, txt, weight, size, color, anchor="mm"):
    dd = ImageDraw.Draw(img)
    f = fonts.font(weight, _p(size))
    dd.text((_p(xy[0]), _p(xy[1])), txt, font=f, fill=color + (255,), anchor=anchor)


def _finish(img, path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    img.convert("RGB").resize((FULL, FULL), Image.LANCZOS).save(path, "PNG")


# ---------------------------------------------------------------- channel avatar

def avatar_channel(path=None):
    path = path or os.path.join(cfg.img_dir, "avatar_channel.png")
    fonts.ensure_fonts()
    img = _background()

    # верх: STEAM
    _text(img, (512, 150), "STEAM", "black", 104, WHITE)

    # щит по центру
    cx, cy, w, h = 512, 520, 430, 470
    _draw_shield(img, cx, cy, w, h)

    # "%" крупно внутри щита
    dd = ImageDraw.Draw(img)
    f = fonts.font("black", _p(230))
    dd.text((_p(cx), _p(cy - 48)), "%", font=f, fill=LIME + (255,), anchor="mm")
    # подсветка %
    glow = Image.new("RGBA", (SZ, SZ), (0, 0, 0, 0))
    ImageDraw.Draw(glow).text((_p(cx), _p(cy - 48)), "%", font=f, fill=(164, 208, 7, 180), anchor="mm")
    img.alpha_composite(glow.filter(ImageFilter.GaussianBlur(_p(20))))

    # маленький геймпад внизу щита
    _gamepad(img, cx, cy + 168, 150, 74, (102, 192, 244))

    # нижняя плашка СКИДКИ
    _plate(img, 512, 858, 470, 128, LIME, "СКИДКИ", "nbold", 78, (18, 24, 12))

    _finish(img, path)
    return path


# ---------------------------------------------------------------- support avatar

def _headset(img, cx, cy, w, h):
    d = ImageDraw.Draw(img)
    # дуга наушников
    d.arc(_box((cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2)), start=200, end=340,
          fill=BLUE + (255,), width=_p(22))
    # амбушюры
    for sx in (-1, 1):
        ex = cx + sx * w / 2 * 0.98
        d.rounded_rectangle(_box((ex - 34, cy - 120, ex + 34, cy + 90)), radius=_p(30), fill=(30, 44, 66, 255),
                            outline=BLUE + (255,), width=_p(6))
    # микрофон
    d.line([( _p(cx - w / 2 * 0.98), _p(cy + 60)), (_p(cx - 60), _p(cy + 190))], fill=BLUE + (255,), width=_p(10))
    d.ellipse(_box((cx - 86, cy + 168, cx - 34, cy + 220)), fill=LIME + (255,))


def avatar_support(path=None):
    path = path or os.path.join(cfg.img_dir, "avatar_support.png")
    fonts.ensure_fonts()
    img = _background()

    _text(img, (512, 150), "SUPPORT", "black", 92, WHITE)

    cx, cy, w, h = 512, 520, 430, 470
    _draw_shield(img, cx, cy, w, h)
    _headset(img, cx, cy - 10, 300, 300)
    # бейдж помощи "+"
    d = ImageDraw.Draw(img)
    d.ellipse(_box((cx + 150, cy + 120, cx + 270, cy + 240)), fill=LIME + (255,))
    _text(img, (cx + 210, cy + 180), "+", "black", 84, (18, 24, 12))

    _plate(img, 512, 858, 620, 120, (42, 71, 94), "ПОДДЕРЖКА", "nbold", 64, WHITE)

    _finish(img, path)
    return path


if __name__ == "__main__":
    cfg.ensure_dirs()
    print(avatar_channel())
    print(avatar_support())
