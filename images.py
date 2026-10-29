"""Премиальный генератор карточек Steam-скидок (Pillow).

- 1280x720, JPEG q=95, суперсэмплинг x2, только шрифты (без эмодзи в тексте)
- Всегда цена в ₽ (сконвертированная помечается "по курсу ₸")
- make_card — одиночная карточка; make_multi_card — сетка 3–4 игр
- Полностью русские подписи
"""
import os

from PIL import Image, ImageDraw, ImageFilter

import fonts
from config import cfg

K = 2

INK = (23, 26, 33)
INK2 = (27, 40, 56)
STEEL = (42, 71, 94)
BLUE = (102, 192, 244)
GREEN = (164, 208, 7)
GREEN_DARK = (76, 107, 34)
GOLD = (255, 196, 61)
EMERALD = (46, 204, 113)
WHITE = (245, 248, 252)
MUTED = (140, 156, 174)
MUTED2 = (110, 126, 144)
DANGER = (226, 92, 92)

REGIONS = [("RUB", "₽"), ("KZT", "₸"), ("UAH", "₴")]
REGION_NAME = {"RUB": "Россия", "KZT": "Казахстан", "UAH": "Украина"}
CUR_SYM = {"RUB": "₽", "KZT": "₸", "UAH": "₴", "USD": "$", "EUR": "€"}
KIND_LABEL = {
    "single": "СКИДКА В STEAM",
    "multi": "ВЫГОДНАЯ ПОДБОРКА",
    "series": "СКИДКА НА СЕРИЮ",
    "roundup": "ПОДБОРКА ДНЯ",
    "ending": "СКОРО ЗАКОНЧИТСЯ",
    "under300": "ХИТЫ ДО 300 ₽",
    "freebie": "ХАЛЯВА В STEAM",
    # новинки и релизы
    "releases": "НОВИНКИ И РЕЛИЗЫ",
    "upcoming": "СКОРО В STEAM",
    # сезонные события (events.py -> kind карточки)
    "halloween": "ХЭЛЛОУИН В STEAM",
    "autumn": "ОСЕННЯЯ РАСПРОДАЖА",
    "winter": "ЗИМНЯЯ РАСПРОДАЖА",
    "spring": "ВЕСЕННЯЯ РАСПРОДАЖА",
    "summer": "ЛЕТНЯЯ РАСПРОДАЖА",
}


class Canvas:
    def __init__(self, w, h, base=None):
        self.w, self.h = w, h
        if base is not None:
            self.img = base.convert("RGBA")
        else:
            self.img = Image.new("RGBA", (w * K, h * K), INK)

    def _box(self, b):
        return [int(b[0] * K), int(b[1] * K), int(b[2] * K), int(b[3] * K)]

    def rrect(self, box, radius, fill=None, outline=None, width=1):
        self.img_d().rounded_rectangle(self._box(box), radius=int(radius * K),
                                       fill=fill, outline=outline, width=max(1, int(width * K)))

    def ellipse(self, box, fill=None, outline=None, width=1):
        self.img_d().ellipse(self._box(box), fill=fill, outline=outline,
                             width=max(1, int(width * K)))

    def polygon(self, pts, fill=None, outline=None):
        self.img_d().polygon([(int(x * K), int(y * K)) for x, y in pts],
                             fill=fill, outline=outline)

    def line(self, pts, fill, width=1):
        self.img_d().line([(int(x * K), int(y * K)) for x, y in pts],
                          fill=fill, width=max(1, int(width * K)))

    def img_d(self):
        return ImageDraw.Draw(self.img)

    def text(self, xy, txt, weight, size, fill, anchor="la", spacing=8):
        f = fonts.font(weight, int(size * K))
        self.img_d().text((int(xy[0] * K), int(xy[1] * K)), txt, font=f, fill=fill,
                          anchor=anchor, spacing=int(spacing * K))

    def textlen(self, txt, weight, size):
        f = fonts.font(weight, int(size * K))
        return self.img_d().textlength(txt, font=f) / K

    def paste_rounded(self, small_img, box, radius):
        bw = int((box[2] - box[0]) * K)
        bh = int((box[3] - box[1]) * K)
        im = small_img.convert("RGBA").resize((bw, bh), Image.LANCZOS)
        mask = Image.new("L", (bw, bh), 0)
        ImageDraw.Draw(mask).rounded_rectangle([0, 0, bw - 1, bh - 1], radius=int(radius * K), fill=255)
        self.img.paste(im, (int(box[0] * K), int(box[1] * K)), mask)

    def save(self, path, quality=95):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.img.convert("RGB").resize((self.w, self.h), Image.LANCZOS).save(
            path, "JPEG", quality=quality, subsampling=0
        )


# ---------------------------------------------------------------- helpers

def _cover(im, w, h):
    ratio = max(w / im.width, h / im.height)
    nw, nh = int(im.width * ratio + 0.5), int(im.height * ratio + 0.5)
    im = im.resize((nw, nh), Image.LANCZOS)
    left, top = (nw - w) // 2, (nh - h) // 2
    return im.crop((left, top, left + w, top + h))


def _vgrad(w, h, top_rgba, bot_rgba):
    img = Image.new("RGBA", (int(w * K), int(h * K)))
    d = ImageDraw.Draw(img)
    H = int(h * K)
    for y in range(H):
        t = y / max(1, H - 1)
        c = tuple(int(a + (b - a) * t) for a, b in zip(top_rgba, bot_rgba))
        d.line([(0, y), (img.width, y)], fill=c)
    return img


def _flag(code, w=52, h=36):
    w, h = int(w * K), int(h * K)
    im = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    if code == "RUB":
        d.rectangle([0, 0, w, h // 3], fill=(255, 255, 255, 255))
        d.rectangle([0, h // 3, w, 2 * h // 3], fill=(0, 57, 166, 255))
        d.rectangle([0, 2 * h // 3, w, h], fill=(213, 43, 30, 255))
    elif code == "KZT":
        d.rectangle([0, 0, w, h], fill=(0, 171, 216, 255))
        d.ellipse([w * 0.36, h * 0.16, w * 0.64, h * 0.44], fill=(250, 205, 53, 255))
    elif code == "UAH":
        d.rectangle([0, 0, w, h // 2], fill=(0, 87, 183, 255))
        d.rectangle([0, h // 2, w, h], fill=(255, 215, 0, 255))
    else:
        d.rectangle([0, 0, w, h], fill=(60, 60, 60, 255))
    out = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    out.paste(im, (0, 0))
    ImageDraw.Draw(out).rounded_rectangle([0, 0, w - 1, h - 1], radius=int(6 * K),
                                          outline=(255, 255, 255, 60), width=max(1, K // 2))
    return out


def _telegram_glyph(c, cx, cy, r, color):
    c.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(41, 161, 226))
    s = r * 0.9
    c.polygon([(cx - s * 0.55, cy + s * 0.02), (cx + s * 0.62, cy - s * 0.42),
               (cx + s * 0.18, cy + s * 0.52), (cx + s * 0.1, cy + s * 0.16)], fill=color)
    c.polygon([(cx + s * 0.1, cy + s * 0.16), (cx + s * 0.62, cy - s * 0.42),
               (cx + s * 0.2, cy + s * 0.3)], fill=(230, 242, 252))


def _fmt(n):
    if n is None:
        return None
    try:
        n = float(n)
    except (TypeError, ValueError):
        return None
    if n >= 100:
        return f"{round(n):,}".replace(",", " ")
    return f"{n:g}"


def _strike(c, x, y, txt, weight, size, color):
    c.text((x, y), txt, weight, size, color)
    w = c.textlen(txt, weight, size)
    mid = y + size * 0.52
    c.line([(x, mid), (x + w, mid)], color, width=max(1, size * 0.06))
    return w


def _wrap(c, text, weight, size, max_w, max_lines=2):
    words = text.split()
    lines, cur = [], ""
    for word in words:
        test = (cur + " " + word).strip()
        if c.textlen(test, weight, size) <= max_w or not cur:
            cur = test
        else:
            lines.append(cur)
            cur = word
            if len(lines) >= max_lines:
                break
    if cur and len(lines) < max_lines:
        lines.append(cur)
    if lines and c.textlen(lines[-1], weight, size) > max_w:
        while lines[-1] and c.textlen(lines[-1] + "…", weight, size) > max_w:
            lines[-1] = lines[-1][:-1]
        lines[-1] = lines[-1].rstrip() + "…"
    return lines


def _rub_display(price: dict | None):
    """Возвращает (main_text, note) для цены в ₽. note — 'по курсу ₸' если сконвертировано."""
    if not price or price.get("final") is None:
        return None, None
    if float(price["final"]) <= 0:
        return "БЕСПЛАТНО", None
    main = f"{_fmt(price['final'])}₽"
    if price.get("converted"):
        src = (price.get("converted_from") or "").upper()
        sym = CUR_SYM.get(src, "")
        main = f"~{_fmt(price['final'])}₽"
        note = f"по курсу {sym}" if sym else "по курсу"
        return main, note
    return main, None


def _alt_prices(price_kz: dict | None = None, price_ua: dict | None = None,
                price_us: dict | None = None) -> str:
    """Компактная строка остальных валют: '2 900₸ · 299₴ · $14.99'."""
    parts = []
    for pr, sym, fmt in ((price_kz, "₸", _fmt), (price_ua, "₴", _fmt)):
        if pr and pr.get("final") is not None and float(pr["final"]) > 0:
            parts.append(f"{fmt(pr['final'])}{sym}")
    if price_us and price_us.get("final") is not None and float(price_us["final"]) > 0:
        parts.append(f"${float(price_us['final']):.2f}")
    return " · ".join(parts)


# ---------------------------------------------------------------- single card

def make_card(header_path, out_path, handle, discount, prices=None, title="",
              kind="single", genres=None):
    W, H = cfg.card_w, cfg.card_h
    fonts.ensure_fonts()
    prices = prices or {}
    genres = genres or []

    art = None
    if header_path and os.path.exists(header_path):
        try:
            art = Image.open(header_path).convert("RGB")
        except OSError:
            art = None
    if art is None:
        art = Image.new("RGB", (460, 215), (30, 40, 55))

    bg = _cover(art, W * K, H * K).filter(ImageFilter.GaussianBlur(40))
    dark = Image.new("RGBA", (W * K, H * K), (10, 12, 18, 225))
    bg = Image.alpha_composite(bg.convert("RGBA"), dark)
    bottom = _vgrad(W, H, (0, 0, 0, 0), (16, 20, 30, 245))
    bg.alpha_composite(bottom, (0, int(H * 0.40 * K)))
    c = Canvas(W, H, base=bg)

    # верхняя лента рубрики
    label = KIND_LABEL.get(kind, "СКИДКА В STEAM")
    pill_w = c.textlen(label, "semi", 22) + 56
    c.rrect([56, 22, 56 + pill_w, 62], 14, fill=(30, 43, 63, 230), outline=(102, 192, 244, 120), width=2)
    c.text((56 + 28, 42), label, "semi", 22, BLUE, anchor="lm")
    if kind == "ending":
        hx = 56 + pill_w + 18
        c.line([(hx, 26), (hx + 14, 26)], GOLD, width=3)
        c.line([(hx, 58), (hx + 14, 58)], GOLD, width=3)
        c.line([(hx + 1, 27), (hx + 13, 57)], GOLD, width=3)
        c.line([(hx + 13, 27), (hx + 1, 57)], GOLD, width=3)

    # чёткий арт
    box = (56, 88, 600, 400)
    c.paste_rounded(art, box, 18)
    c.rrect(box, 18, outline=(102, 192, 244, 70), width=2)

    # правый блок
    rx = 632
    eyebrow = (" · ".join(genres[:2]) if genres else "МАГАЗИН STEAM").upper()
    c.text((rx, 96), eyebrow, "semi", 22, GREEN)
    lines = _wrap(c, title or "Игра в Steam", "nbold", 46, 592, max_lines=3)
    ty = 130
    for ln in lines:
        c.text((rx, ty), ln, "nbold", 46, WHITE)
        ty += 58
    c.text((rx, ty + 20), "Выгода", "nreg", 24, MUTED2)
    c.text((rx + 108, ty + 10), f"−{discount}%", "black", 40, GREEN)

    # доллар США — четвёртая валюта на карточке
    usd = prices.get("USD") or {}
    if usd.get("final") is not None and ty + 64 < 400:
        usd_txt = f"$ {float(usd['final']):.2f}"
        if usd.get("initial") and float(usd["initial"]) > float(usd["final"]):
            _strike(c, rx, ty + 64, f"$ {float(usd['initial']):.2f}", "nreg", 20, MUTED2)
            c.text((rx + 92, ty + 62), usd_txt, "semi", 24, MUTED)
        else:
            c.text((rx, ty + 62), usd_txt, "semi", 24, MUTED)

    # бейдж скидки (русские подписи)
    bd = "ХИТ СКИДКА" if discount >= 70 else "СКИДКА"
    bx0, by0, bx1, by1 = 440, 262, 596, 386
    if discount >= 70:
        _paste_grad_round(c, _vgrad(bx1 - bx0, by1 - by0, (26, 154, 96, 255), (16, 120, 74, 255)),
                          [bx0, by0, bx1, by1], 16)
        c.rrect([bx0, by0, bx1, by1], 16, outline=(255, 220, 120, 200), width=2)
        bcol = (255, 236, 170)
    else:
        _paste_grad_round(c, _vgrad(bx1 - bx0, by1 - by0, (164, 208, 7, 255), (110, 150, 10, 255)),
                          [bx0, by0, bx1, by1], 16)
        c.rrect([bx0, by0, bx1, by1], 16, outline=(210, 240, 130, 180), width=2)
        bcol = WHITE
    cx = (bx0 + bx1) / 2
    c.text((cx, by0 + 40), f"−{discount}%", "black", 40, bcol, anchor="ma")
    c.text((cx, by1 - 30), bd, "bold", 17, bcol, anchor="ma")

    # цены
    c.text((56, 436), "ЦЕНЫ В РЕГИОНАХ", "semi", 21, MUTED)
    chip_y0, chip_y1 = 468, 626
    gap = 24
    chip_w = (1224 - 56 - 2 * gap) / 3
    for i, (code, sym) in enumerate(REGIONS):
        x0 = 56 + i * (chip_w + gap)
        x1 = x0 + chip_w
        c.rrect([x0, chip_y0, x1, chip_y1], 18, fill=(22, 30, 46, 175),
                outline=(102, 192, 244, 90), width=2)
        fl = _flag(code)
        c.img.paste(fl, (int((x0 + 24) * K), int((chip_y0 + 24) * K)), fl)
        region = REGION_NAME[code]
        c.text((x0 + 88, chip_y0 + 36), region, "nsemi", 22, WHITE, anchor="lm")
        c.text((x0 + 88, chip_y0 + 60), code, "bold", 15, MUTED2, anchor="lm")

        pr = prices.get(code) or {}
        final, initial = pr.get("final"), pr.get("initial")
        if final is not None and initial and initial > final:
            old = f"{_fmt(initial)}{sym}"
            _strike(c, x1 - 24 - c.textlen(old, "nreg", 20), chip_y0 + 30, old, "nreg", 20, MUTED2)

        if final is None:
            c.text((x0 + 24, chip_y1 - 34), "Недоступно", "nreg", 26, MUTED2, anchor="lm")
        else:
            new = f"{_fmt(final)}{sym}"
            if pr.get("converted"):
                new = f"~{_fmt(final)}{sym}"
            col = GREEN if discount >= 70 else BLUE
            c.text((x0 + 24, chip_y1 - 46), new, "nbold", 42, col, anchor="lm")
            if pr.get("converted"):
                src = (pr.get("converted_from") or "").upper()
                note = f"по курсу {CUR_SYM.get(src, '')}".strip()
                c.text((x0 + 24, chip_y1 - 16), note, "nreg", 15, MUTED2, anchor="lm")

    # футер
    fy = 656
    _telegram_glyph(c, 74, fy + 20, 20, (255, 255, 255))
    c.text((104, fy + 20), "@" + handle, "bold", 26, WHITE, anchor="lm")
    c.text((1224, fy + 20), "Steam-скидки · ежедневно", "nreg", 22, MUTED2, anchor="rm")

    c.save(out_path, quality=95)
    return True


# ---------------------------------------------------------------- multi card

def make_multi_card(games_data, out_path, handle, kind="multi"):
    """Сетка 2–4 игр на одной карточке 1280x720.
    games_data: список dict {header, title, discount, price_ru}."""
    W, H = cfg.card_w, cfg.card_h
    fonts.ensure_fonts()
    items = [g for g in games_data if g][:4]
    if not items:
        return False
    n = len(items)

    # фон: размытый коллаж из всех артов
    arts = []
    for it in items:
        p = it.get("header")
        if p and os.path.exists(p):
            try:
                arts.append(Image.open(p).convert("RGB"))
            except OSError:
                pass
    if arts:
        canvas_bg = Image.new("RGB", (W * K // 4, H * K // 4), (14, 18, 26))
        cols = 2
        rows = 2
        cw = canvas_bg.width // cols
        ch = canvas_bg.height // rows
        for idx, a in enumerate(arts[:4]):
            r, col = divmod(idx, cols)
            canvas_bg.paste(_cover(a, cw, ch), (col * cw, r * ch))
        bg = canvas_bg.resize((W * K, H * K), Image.LANCZOS).filter(ImageFilter.GaussianBlur(30))
    else:
        bg = Image.new("RGBA", (W * K, H * K), (14, 18, 26))
    dark = Image.new("RGBA", (W * K, H * K), (10, 12, 18, 215))
    bg = Image.alpha_composite(bg.convert("RGBA"), dark)
    bg.alpha_composite(_vgrad(W, H, (0, 0, 0, 0), (14, 18, 27, 250)), (0, int(H * 0.30 * K)))
    c = Canvas(W, H, base=bg)

    # верхняя лента
    label = KIND_LABEL.get(kind, "ВЫГОДНАЯ ПОДБОРКА")
    pill_w = c.textlen(label, "semi", 22) + 56
    c.rrect([56, 20, 56 + pill_w, 60], 14, fill=(30, 43, 63, 230), outline=(102, 192, 244, 120), width=2)
    c.text((56 + 28, 40), label, "semi", 22, BLUE, anchor="lm")

    # сетка
    margin = 46
    top = 84
    bottom = 636
    gap = 20
    if n == 1:
        cols, rows = 1, 1
    elif n == 2:
        cols, rows = 2, 1
    elif n == 3:
        cols, rows = 3, 1
    else:
        cols, rows = 2, 2
    cell_w = (W - 2 * margin - (cols - 1) * gap) / cols
    cell_h = (bottom - top - (rows - 1) * gap) / rows

    for idx, it in enumerate(items):
        r, col = divmod(idx, cols)
        x0 = margin + col * (cell_w + gap)
        y0 = top + r * (cell_h + gap)
        x1, y1 = x0 + cell_w, y0 + cell_h
        # арт
        art = None
        p = it.get("header")
        if p and os.path.exists(p):
            try:
                art = Image.open(p).convert("RGB")
            except OSError:
                art = None
        if art is None:
            art = Image.new("RGB", (460, 215), (32, 42, 58))
        c.paste_rounded(art, (x0, y0, x1, y1), 16)
        c.rrect((x0, y0, x1, y1), 16, outline=(102, 192, 244, 80), width=2)
        # нижний затемняющий градиент внутри ячейки
        gh = cell_h * 0.55
        c.img.alpha_composite(
            _vgrad(cell_w, gh, (0, 0, 0, 0), (8, 10, 16, 240)),
            (int(x0 * K), int((y1 - gh) * K)),
        )
        # бейдж скидки (для новинок/релизов без скидки бейдж не рисуем)
        disc = int(it.get("discount") or 0)
        if disc > 0:
            btxt = f"−{disc}%"
            bw = c.textlen(btxt, "black", 26) + 26
            bx1, by1 = x1 - 14, y0 + 14 + 44
            c.rrect([bx1 - bw, y0 + 14, bx1, by1], 12,
                    fill=(26, 154, 96, 245) if disc >= 70 else (120, 160, 20, 245),
                    outline=(255, 255, 255, 90), width=1)
            c.text(((bx1 - bw / 2), (y0 + 14 + 22)), btxt, "black", 26, (255, 255, 255), anchor="mm")
        # названия НЕ рисуем: они уже есть на обложке и в подписи к посту
        main, note = _rub_display(it.get("price_ru"))
        if main:
            c.text((x0 + 16, y1 - 54), main, "black", 34, GREEN if disc >= 70 else BLUE, anchor="ls")
            if note:
                nw = c.textlen(main, "black", 34)
                c.text((x0 + 16 + nw + 8, y1 - 58), note, "nreg", 14, MUTED2, anchor="ls")
        else:
            c.text((x0 + 16, y1 - 54), "Недоступно", "nreg", 20, MUTED2, anchor="ls")
        # остальные валюты: ₸ · ₴ · $
        alt = _alt_prices(it.get("price_kz"), it.get("price_ua"), it.get("price_us"))
        if alt:
            c.text((x0 + 16, y1 - 24), alt, "nreg", 17, MUTED, anchor="ls")

    # футер
    fy = 658
    _telegram_glyph(c, 74, fy + 18, 19, (255, 255, 255))
    c.text((102, fy + 18), "@" + handle, "bold", 24, WHITE, anchor="lm")
    c.text((1224, fy + 18), "Steam-скидки · ежедневно", "nreg", 21, MUTED2, anchor="rm")

    c.save(out_path, quality=95)
    return True


def _paste_grad_round(c: Canvas, grad_img, box, radius):
    bw = int((box[2] - box[0]) * K)
    bh = int((box[3] - box[1]) * K)
    g = grad_img.resize((bw, bh), Image.LANCZOS)
    mask = Image.new("L", (bw, bh), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, bw - 1, bh - 1], radius=int(radius * K), fill=255)
    c.img.paste(g, (int(box[0] * K), int(box[1] * K)), mask)
