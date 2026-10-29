"""Безопасность: маскировка BOT_TOKEN в логах и сообщениях об ошибках."""
import logging

from config import cfg


class TokenMaskFilter(logging.Filter):
    """Заменяет токен бота на ***TOKEN*** в сообщениях логов."""

    def filter(self, record: logging.LogRecord) -> bool:
        token = cfg.bot_token
        if not token:
            return True
        try:
            if isinstance(record.msg, str) and token in record.msg:
                record.msg = record.msg.replace(token, "***TOKEN***")
            if record.args:
                new_args = []
                for a in record.args:
                    if isinstance(a, str) and token in a:
                        new_args.append(a.replace(token, "***TOKEN***"))
                    else:
                        new_args.append(a)
                record.args = tuple(new_args)
        except (TypeError, ValueError):
            pass
        return True


_installed = False


def install():
    """Ставит фильтр на корневой логгер и все его хендлеры (идемпотентно)."""
    global _installed
    if _installed:
        return
    f = TokenMaskFilter()
    root = logging.getLogger()
    root.addFilter(f)
    for h in root.handlers:
        h.addFilter(f)
    _installed = True
