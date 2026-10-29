"""Кроссплатформенные шрифты: автоскачивание с GitHub (Google Fonts) при первом запуске.

Montserrat — латиница/цифры/брендинг. Noto Sans — кириллица (названия игр на русском).
Никаких C:\\Windows\\Fonts. На Linux/Docker работает так же.
"""
import os

import requests
from PIL import ImageFont

from config import cfg

_FONT_DIR = os.path.join(cfg.assets_dir, "fonts")

# (имя файла, url)
_FONTS = {
    # Montserrat (static, JulietaUla mirror of Google Fonts)
    "black": ("Montserrat-ExtraBold.ttf",
              "https://github.com/JulietaUla/Montserrat/raw/master/fonts/ttf/Montserrat-ExtraBold.ttf"),
    "bold": ("Montserrat-Bold.ttf",
             "https://github.com/JulietaUla/Montserrat/raw/master/fonts/ttf/Montserrat-Bold.ttf"),
    "semi": ("Montserrat-SemiBold.ttf",
             "https://github.com/JulietaUla/Montserrat/raw/master/fonts/ttf/Montserrat-SemiBold.ttf"),
    "reg": ("Montserrat-Regular.ttf",
            "https://github.com/JulietaUla/Montserrat/raw/master/fonts/ttf/Montserrat-Regular.ttf"),
    # Noto Sans (кириллица)
    "nbold": ("NotoSans-Bold.ttf",
              "https://github.com/googlefonts/noto-fonts/raw/main/hinted/ttf/NotoSans/NotoSans-Bold.ttf"),
    "nsemi": ("NotoSans-SemiBold.ttf",
              "https://github.com/googlefonts/noto-fonts/raw/main/hinted/ttf/NotoSans/NotoSans-SemiBold.ttf"),
    "nreg": ("NotoSans-Regular.ttf",
             "https://github.com/googlefonts/noto-fonts/raw/main/hinted/ttf/NotoSans/NotoSans-Regular.ttf"),
}

_cache: dict[tuple, ImageFont.FreeTypeFont] = {}
_downloaded = False


def _download_all():
    global _downloaded
    if _downloaded:
        return
    os.makedirs(_FONT_DIR, exist_ok=True)
    for name, (fname, url) in _FONTS.items():
        path = os.path.join(_FONT_DIR, fname)
        if os.path.exists(path) and os.path.getsize(path) > 20000:
            continue
        try:
            r = requests.get(url, timeout=60, allow_redirects=True)
            if r.status_code == 200 and len(r.content) > 20000:
                with open(path, "wb") as f:
                    f.write(r.content)
        except requests.RequestException:
            pass
    _downloaded = True


def _safe_load(path: str, size: int):
    if os.path.exists(path):
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            pass
    return None


def font(weight: str, size: int) -> ImageFont.FreeTypeFont:
    """weight: black|bold|semi|reg|nbold|nsemi|nreg. Возвращает шрифт нужного размера."""
    _download_all()
    key = (weight, size)
    if key in _cache:
        return _cache[key]
    fname, _ = _FONTS.get(weight, _FONTS["bold"])
    f = _safe_load(os.path.join(_FONT_DIR, fname), size)
    if f is None:
        # фолбэк: любой доступный шрифт с кириллицей из системы
        for cand in (
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            r"C:\Windows\Fonts\arialbd.ttf",
            r"C:\Windows\Fonts\arial.ttf",
        ):
            f = _safe_load(cand, size)
            if f is not None:
                break
    if f is None:
        f = ImageFont.load_default()
    _cache[key] = f
    return f


def ensure_fonts():
    _download_all()
