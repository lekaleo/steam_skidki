# -*- coding: utf-8 -*-
"""Проверка новостного поста БЕЗ публикации (для GitHub Actions и локально).

Показывает: сколько статей собрано из RSS, какая выбрана, текст подписи,
скачивается ли картинка и собирается ли карточка. В канал ничего не уходит.
Код возврата 1, если что-то из этого не работает.
"""
import logging
import os
import sys

import images
import news
import templates
from config import cfg
from poster import _storage

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")


def main() -> int:
    cfg.ensure_dirs()
    st = _storage()
    items = news.fetch_all()
    print(f"[1/5] RSS: статей собрано {len(items)}; с картинкой "
          f"{sum(1 for a in items if a['image'])}")
    if not items:
        print("ОШИБКА: ни одна лента не отдала новости")
        return 1

    art = news.pick_article(st.is_news_posted)
    if not art:
        print("ОШИБКА: нет неопубликованной новости")
        return 1
    print(f"[2/5] выбрана: [{art['source']}] {art['title'][:90]}")
    print(f"      url: {art['url'][:110]}")

    caption = templates.news_post(art)
    ok_html = templates.html_balanced(caption)
    ok_len = templates.vlen(caption) <= cfg.caption_limit
    print(f"[3/5] подпись: {templates.vlen(caption)} символов, html_ok={ok_html}, в лимите={ok_len}")
    print("-----")
    print(caption)
    print("-----")

    img = news.image_path(art)
    print(f"[4/5] картинка: {'скачана ' + str(os.path.getsize(img)) + ' Б' if img else 'НЕ скачалась'}")
    if not img:
        return 1

    out = os.path.join(cfg.img_dir, "preview_news.jpg")
    made = images.make_news_card(img, out, cfg.channel_handle,
                                 art.get("title", ""), art.get("source", ""))
    print(f"[5/5] карточка: {made}, {os.path.getsize(out) if made else 0} Б")

    ok = bool(made) and ok_html and ok_len
    print("ИТОГ:", "новостной пост готов к публикации ✅" if ok else "есть проблемы ❌")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
