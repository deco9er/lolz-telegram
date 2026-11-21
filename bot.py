import argparse
import getpass
import logging
import re
import time
import warnings
from pathlib import Path
from threading import Thread

from api import LOLZ_TOKEN, TG_TOKEN, APIError, Lolz, Telegram, parse_int
from appearance import ICONS, compose
from monitor import (
    Monitor,
    check_group,
    forum_id,
    forum_title,
    is_forum,
    title_key,
)
from render import format_moscow_time
from store import DATA, Store, single_instance

LOG = logging.getLogger("bot")
VERSION = "1.3.1"
THREAD_REFERENCE = re.compile(
    r"(?:https?://[^/\s]+/threads/(?:[^/\s]*\.)?)?([0-9]{1,15})/?(?:[?#][^\s]*)?"
)
SECTION_PAGE_SIZE = 10


def button(text, action, icon):
    return {
        "text": text,
        "callback_data": action,
        "icon_custom_emoji_id": ICONS[icon][1],
    }


def bot_identity(telegram):
    identity = telegram.call("getMe")
    if identity.get("id") != parse_int(telegram.token.split(":", 1)[0]):
        raise APIError("Telegram: токен принадлежит другому боту.")
    return identity


class Admin:
    def __init__(self, monitor):
        self.monitor = monitor
        self.store = monitor.store
        self.input = None
        self.section_query = ""
        self.selected_only = False

    def menu(self, telegram, admin_id, prefix=""):
        settings = self.store.settings()
        chat = settings.get("chat")
        counts, delivery_error = self.store.stats(chat)
        sections = self.monitor.active_sections
        status = (
            ("pause", "Пауза")
            if settings.get("paused")
            else ("check", "Мониторинг включён")
        )
        lines = [
            prefix,
            ("bot", "Мониторинг Lolzteam"),
            status,
            ("group", f"Группа · {chat or 'не привязана'}"),
            ("lock", f"Telegram · @{settings.get('tg_username', 'бот')} · токен задан"),
            (
                "lock",
                f"Lolzteam · токен {'задан' if settings.get('lolz_token') else 'не задан'}",
            ),
            ("clock", f"Интервал · {settings.get('interval', 60)} с"),
        ]
        if len(sections) > 3:
            lines.append(("sections", f"Подключено разделов · {len(sections)}"))
        else:
            for section in sections.values():
                topic = (
                    self.store.topic(chat, section.id, legacy_name=section.legacy_name)
                    if chat
                    else None
                )
                label = f"{section.title[:120]} · форум {section.id}" + (
                    f" · топик {topic}" if topic else ""
                )
                lines.append(("sections", label))
        lines.extend(
            [
                f"В очереди {counts.get('pending', 0)} · отправлено {counts.get('sent', 0)}"
                f" · недоступно {counts.get('skipped', 0)}",
                self.monitor.note[:1300],
            ]
        )
        if delivery_error:
            lines.append("Последняя ошибка доставки · " + delivery_error)
        pause = (
            button("Возобновить", "resume", "resume")
            if settings.get("paused")
            else button("Пауза", "pause", "pause")
        )
        sound = (
            button("Со звуком", "loud", "sound")
            if settings.get("silent")
            else button("Без звука", "silent", "mute")
        )
        keyboard = [
            [
                button("Токен Lolzteam", "lolz_token", "lock"),
                button("Токен Telegram", "tg_token", "bot"),
            ],
            [
                button("Группа", "group", "group"),
                button("Разделы", "sections", "sections"),
            ],
            [
                button("Проверить API", "check", "check"),
                button("Проверить сейчас", "poll", "test"),
            ],
            [
                button("Интервал", "interval", "clock"),
                button("Тест в группу", "test", "info"),
            ],
            [pause, sound],
            [
                button("Проверить тему", "check_thread", "link"),
                button("Обновить статус", "menu", "refresh"),
            ],
            [button("Статистика API Lolzteam", "api_stats", "info")],
        ]
        telegram.send(admin_id, keyboard=keyboard, **compose(lines))

    def api_stats(self, telegram, admin_id, settings):
        token = settings.get("lolz_token")
        if not token:
            raise ValueError("Сначала задайте токен Lolzteam.")
        stats = Lolz(token).statistics()
        lines = [("info", "Статистика API Lolzteam")]
        if quota := stats["quota"]:
            lines.append(f"Квота · {quota.bucket}")
            if quota.limit is not None:
                lines.append(f"Лимит окна · {quota.limit}")
            if quota.remaining is not None:
                lines.append(f"Осталось по последнему ответу · {quota.remaining}")
                if quota.limit is not None:
                    lines.append(
                        f"Израсходовано в окне · {max(0, quota.limit - quota.remaining)}"
                    )
            if quota.reset is not None:
                lines.append(("clock", f"Сброс · {format_moscow_time(quota.reset)}"))
                if quota.expires_at <= time.monotonic():
                    lines.append(
                        "Это окно истекло. Остаток обновится после следующего запроса."
                    )
            lines.append(f"Данные получены · {format_moscow_time(quota.observed_at)}")
        else:
            lines.append("Данные о квоте ещё не получены.")
        if stats["wait"]:
            lines.append(("pause", f"Запросы отложены · {stats['wait']} с"))
        endpoints = stats["endpoints"]
        lines.extend(
            [
                "",
                f"Запросы бота с запуска · {sum(endpoints.values())}",
                f"Успешных · {stats['successes']} · ошибок · {stats['errors']}",
                f"Ответов HTTP 429 · {stats['limited']} · локально отложено · {stats['deferred']}",
            ]
        )
        lines.extend(f"GET {path} · {count}" for path, count in endpoints.items())
        rows = [
            [button("Обновить статистику", "api_stats", "refresh")],
            [button("В админку", "menu", "back")],
        ]
        telegram.send(admin_id, keyboard=rows, **compose(lines))

    def bind(self, telegram, message, settings):
        chat = message["chat"]["id"]
        admin_id = settings["admin_id"]
        checked = check_group(
            telegram, chat, parse_int(telegram.token.split(":", 1)[0]), admin_id
        )
        self.monitor.save(chat=chat)
        self.menu(telegram, admin_id, f"Привязана группа «{checked['title']}».")

    def section_menu(self, telegram, admin_id, settings, page=1, prefix=""):
        if not settings.get("lolz_token"):
            raise ValueError("Сначала задайте токен Lolzteam.")
        nodes = self.monitor.forums(settings)
        active = self.monitor.sections(settings)
        chosen = set(settings.get("forum_ids", active))
        query = title_key(self.section_query)
        entries = {
            forum_id(node): " ".join(forum_title(node).split())
            for node in nodes
            if is_forum(node)
            and forum_id(node) > 0
            and (not self.selected_only or forum_id(node) in chosen)
            and (not query or query in title_key(forum_title(node)))
        }
        if self.selected_only and not query:
            entries.update(
                {node: "Форум недоступен" for node in chosen.difference(active)}
            )
        entries = sorted(
            entries.items(), key=lambda item: (title_key(item[1]), item[0])
        )
        pages = max(1, (len(entries) + SECTION_PAGE_SIZE - 1) // SECTION_PAGE_SIZE)
        page = min(max(1, page), pages)
        rows = []
        for node, title in entries[
            (page - 1) * SECTION_PAGE_SIZE : page * SECTION_PAGE_SIZE
        ]:
            selected = "✓ " if node in chosen else ""
            label = f"{selected}{title[:56] or 'Форум'} · {node}"
            rows.append([button(label, f"section_toggle:{node}:{page}", "sections")])
        navigation = []
        if page > 1:
            navigation.append(button("Назад", f"section_page:{page - 1}", "back"))
        if page < pages:
            navigation.append(button("Дальше", f"section_page:{page + 1}", "resume"))
        if navigation:
            rows.append(navigation)
        view = "Все доступные" if self.selected_only else "Только выбранные"
        rows.extend(
            [
                [
                    button(view, "section_view", "sections"),
                    button("Стандартные", "auto_sections", "refresh"),
                ],
                [button("В админку", "menu", "back")],
            ]
        )
        lines = [
            prefix,
            ("sections", "Разделы Lolzteam"),
            f"Выбрано · {len(chosen)} · страница {page} из {pages}",
            f"Поиск · {self.section_query}" if self.section_query else "",
            "Нажмите на раздел, чтобы включить или отключить его.",
            "Для поиска пришлите название. Для добавления нескольких разделов пришлите их ID через пробел.",
        ]
        if not entries:
            lines.append("В этом списке нет разделов.")
        self.input = ("sections", time.monotonic() + 300)
        telegram.send(admin_id, keyboard=rows, **compose(lines))

    def action(self, telegram, action, settings):
        admin_id = settings["admin_id"]
        self.input = None
        if action in {"pause", "resume", "silent", "loud"}:
            if action in {"pause", "resume"}:
                self.monitor.save(paused=action == "pause")
            else:
                self.monitor.save(silent=action == "silent")
        elif action in {"lolz_token", "tg_token", "interval"}:
            prompts = {
                "lolz_token": "Пришлите токен API форума Lolzteam с правом read.",
                "tg_token": "Пришлите новый токен Telegram. Если меняете бота, сначала откройте его и нажмите Start.",
                "interval": "Пришлите интервал проверки в секундах от 30 до 900.",
            }
            self.input = (action, time.monotonic() + 300)
            icon = "clock" if action == "interval" else "lock"
            telegram.send(
                admin_id,
                keyboard=[[button("Отмена", "menu", "back")]],
                **compose([(icon, prompts[action])]),
            )
            return
        elif action == "group":
            rows = [[button("Назад", "menu", "back")]]
            if settings.get("chat"):
                rows.insert(0, [button("Отвязать группу", "unbind", "remove")])
            telegram.send(
                admin_id,
                keyboard=rows,
                **compose(
                    [
                        ("group", "Привязка группы"),
                        "Добавьте бота в приватную супергруппу с темами. "
                        "Дайте ему права администратора и «Управление темами». "
                        "Затем отправьте /bind в этой группе со своего аккаунта.",
                    ]
                ),
            )
            return
        elif action == "unbind":
            self.monitor.save(chat=None)
            self.menu(telegram, admin_id, "Группа отвязана.")
            return
        elif action == "sections":
            self.section_query = ""
            self.selected_only = False
            self.section_menu(telegram, admin_id, settings)
            return
        elif action == "section_view":
            self.section_query = ""
            self.selected_only = not self.selected_only
            self.section_menu(telegram, admin_id, settings)
            return
        elif match := re.fullmatch(r"section_page:([0-9]{1,8})", action or ""):
            self.section_menu(telegram, admin_id, settings, page=int(match[1]))
            return
        elif match := re.fullmatch(
            r"section_toggle:([0-9]{1,15}):([0-9]{1,8})", action or ""
        ):
            node, page = int(match[1]), int(match[2])
            nodes = self.monitor.forums(settings)
            active = self.monitor.sections(settings)
            chosen = list(settings.get("forum_ids", active))
            if node in chosen:
                chosen.remove(node)
            elif any(is_forum(forum) and forum_id(forum) == node for forum in nodes):
                chosen.append(node)
            else:
                raise ValueError("Раздел больше не доступен в каталоге.")
            self.monitor.save(forum_ids=chosen)
            self.section_menu(telegram, admin_id, self.store.settings(), page=page)
            return
        elif action == "check_thread":
            if not settings.get("lolz_token"):
                raise ValueError("Сначала задайте токен Lolzteam.")
            self.input = ("check_thread", time.monotonic() + 300)
            telegram.send(
                admin_id,
                "Пришлите ссылку на тему Lolzteam или её числовой ID.",
                keyboard=[[button("Отмена", "menu", "back")]],
            )
            return
        elif action == "auto_sections":
            self.monitor.use_auto_sections()
            self.selected_only = False
            self.section_query = ""
            self.section_menu(
                telegram,
                admin_id,
                self.store.settings(),
                prefix="Включены стандартные разделы.",
            )
            return
        elif action == "poll":
            if settings.get("paused"):
                raise ValueError(
                    "Мониторинг на паузе. Для проверки нажмите «Возобновить»."
                )
            self.monitor.wake.set()
            self.menu(
                telegram, admin_id, "Запрошена проверка. Результат появится в статусе."
            )
            return
        elif action == "api_stats":
            self.api_stats(telegram, admin_id, settings)
            return
        elif action == "check":
            lines = [f"Telegram · @{bot_identity(telegram)['username']}"]
            if chat := settings.get("chat"):
                check_group(
                    telegram, chat, parse_int(telegram.token.split(":", 1)[0]), admin_id
                )
                lines.append("Группа · доступ и права подтверждены")
            if settings.get("lolz_token"):
                matches = self.monitor.sections(settings, refresh=True)
                lines.append("Lolzteam · доступ подтверждён")
                lines.append(f"Подключено разделов · {len(matches)}")
                lines.extend(self.monitor.section_warnings)
            self.menu(telegram, admin_id, "\n".join(lines))
            return
        elif action == "test":
            self.monitor.test_send(settings)
            self.menu(
                telegram,
                admin_id,
                "Тестовое сообщение отправлено в каждый настроенный топик.",
            )
            return
        self.menu(telegram, admin_id)

    def receive_input(self, telegram, message, settings, action):
        admin_id = settings["admin_id"]
        value = message["text"].strip()
        if action == "check_thread":
            match = THREAD_REFERENCE.fullmatch(value)
            if not match or parse_int(match[1]) <= 0:
                raise ValueError("Пришлите ссылку на тему Lolzteam или её числовой ID.")
            thread, _ = Lolz(settings["lolz_token"]).thread(
                parse_int(match[1]), with_post=False
            )
            sections = self.monitor.sections(settings, refresh=True)
            node, title, section = self.monitor.destination(thread, settings, sections)
            lines = [
                ("info", f"Тема {thread['thread_id']}"),
                (
                    "sections",
                    f"Lolzteam · {title or 'раздел без названия'} · ID {node}",
                ),
            ]
            if section:
                chat = settings.get("chat")
                topic = (
                    self.store.topic(chat, section.id, legacy_name=section.legacy_name)
                    if chat
                    else None
                )
                label = f"Telegram · {section.topic_name}"
                if topic:
                    label += f" · ID {topic}"
                elif not chat:
                    label += " · группа ещё не привязана"
                else:
                    label += " · топик ещё не создан"
                lines.append(("group", label))
                if topic and (channel := -chat - 10**12) > 0:
                    lines.append(f"https://t.me/c/{channel}/{topic}?thread={topic}")
            else:
                lines.append("Раздел не подключён к публикации.")
            self.input = None
            telegram.send(
                admin_id, keyboard=[[button("Назад", "menu", "back")]], **compose(lines)
            )
            return
        if action == "sections":
            if re.fullmatch(r"[0-9,\s]+", value) and re.search(r"[0-9]", value):
                nodes = self.monitor.forums(settings)
                active = self.monitor.sections(settings)
                available = {forum_id(node) for node in nodes if is_forum(node)}
                ids = [parse_int(part) for part in re.findall(r"[0-9]+", value)]
                unknown = list(
                    dict.fromkeys(
                        node for node in ids if node <= 0 or node not in available
                    )
                )
                if unknown:
                    preview = ", ".join(map(str, unknown[:10]))
                    raise ValueError(f"ID отсутствуют в каталоге · {preview}.")
                chosen = list(dict.fromkeys([*settings.get("forum_ids", active), *ids]))
                self.monitor.save(forum_ids=chosen)
                self.selected_only = True
                self.section_query = ""
                self.section_menu(
                    telegram,
                    admin_id,
                    self.store.settings(),
                    prefix="Разделы добавлены.",
                )
            else:
                self.section_query = value[:120]
                self.section_menu(telegram, admin_id, settings)
            return
        deletion = ""
        if action in {"lolz_token", "tg_token"}:
            try:
                telegram.call(
                    "deleteMessage", chat_id=admin_id, message_id=message["message_id"]
                )
            except APIError:
                deletion = "Telegram не подтвердил удаление сообщения с токеном. Удалите его вручную."
        try:
            if action == "lolz_token":
                if not LOLZ_TOKEN.fullmatch(value):
                    raise ValueError(
                        "Введите токен Lolzteam без пробелов и префикса Bearer."
                    )
                Lolz(value).forums()
                self.monitor.save(lolz_token=value)
            elif action == "tg_token":
                if not TG_TOKEN.fullmatch(value):
                    raise ValueError("Введите токен от @BotFather.")
                replacement_telegram = Telegram(value)
                ident = bot_identity(replacement_telegram)
                if replacement_telegram.call("getWebhookInfo").get("url"):
                    raise ValueError(
                        "У этого Telegram-бота настроен webhook. Для запуска нужен режим getUpdates."
                    )
                if chat := settings.get("chat"):
                    check_group(replacement_telegram, chat, ident["id"], admin_id)
                replacement_telegram.send(
                    admin_id, "Токен проверен. Откройте /start у этого бота."
                )
                self.monitor.save(tg_token=value, tg_username=ident["username"])
            elif action == "interval":
                if (
                    not value.isascii()
                    or not value.isdigit()
                    or not 30 <= parse_int(value) <= 900
                ):
                    raise ValueError("Введите целое число от 30 до 900.")
                self.monitor.save(interval=parse_int(value))
        except (ValueError, APIError):
            if deletion:
                telegram.send(admin_id, deletion)
            raise
        self.input = None
        target = Telegram(value) if action == "tg_token" else telegram
        self.menu(
            target,
            admin_id,
            "\n".join(filter(None, ["Настройки сохранены.", deletion])),
        )

    def handle(self, telegram, update, settings):
        admin_id = settings["admin_id"]
        if query := update.get("callback_query"):
            if (
                query["from"]["id"] != admin_id
                or query.get("message", {}).get("chat", {}).get("type") != "private"
                or query["message"]["chat"]["id"] != admin_id
            ):
                return
            telegram.call("answerCallbackQuery", callback_query_id=query["id"])
            self.action(telegram, query.get("data"), settings)
            return
        message = update.get("message") or {}
        if message.get("from", {}).get("id") != admin_id or not isinstance(
            message.get("text"), str
        ):
            return
        chat = message["chat"]
        words = message["text"].split(maxsplit=1)
        first = words[0] if words else ""
        command, _, username = first.partition("@")
        if (
            command.startswith("/")
            and username
            and username.casefold() != settings.get("tg_username", "").casefold()
        ):
            return
        if chat["type"] != "private":
            if command == "/bind" and chat["type"] == "supergroup":
                self.bind(telegram, message, settings)
            return
        if chat["id"] != admin_id:
            return
        if command in {"/start", "/admin", "/status", "/cancel"}:
            self.input = None
            self.menu(telegram, admin_id)
        elif self.input and time.monotonic() <= self.input[1]:
            self.receive_input(telegram, message, settings, self.input[0])


def load_env_file(path):
    values = {}
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except FileNotFoundError:
        return values
    for index, line in enumerate(lines, 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise ValueError(
                f"Проверьте строку {index} в .env. Ожидается запись ИМЯ=значение."
            )
        if len(value) >= 2 and value[0] in "'\"" and value[0] == value[-1]:
            value = value[1:-1]
        values[key] = value
    return values


def setup(store, group_id=None, env=None):
    settings = store.settings()
    env = env or {}
    previous = settings.get("admin_id")
    label = (
        f"Ваш числовой Telegram ID [{previous}]: "
        if previous
        else "Ваш числовой Telegram ID: "
    )
    admin_id = (
        env.get("TELEGRAM_ADMIN_ID") or input(label).strip() or str(previous or "")
    )
    if not re.fullmatch(r"[1-9][0-9]{0,15}", admin_id):
        raise SystemExit("Укажите числовой ID своего аккаунта Telegram.")
    token = env.get("TELEGRAM_BOT_TOKEN")
    if not token:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", getpass.GetPassWarning)
                token = getpass.getpass(
                    "Токен Telegram от @BotFather (скрытый ввод): "
                ).strip()
        except getpass.GetPassWarning:
            raise SystemExit(
                "Запустите настройку в интерактивном терминале со скрытым вводом."
            ) from None
    if not TG_TOKEN.fullmatch(token):
        raise SystemExit("Проверьте формат токена Telegram.")
    telegram = Telegram(token)
    identity = bot_identity(telegram)
    if telegram.call("getWebhookInfo").get("url"):
        raise ValueError("У бота настроен webhook. Для запуска нужен режим getUpdates.")
    admin_id = int(admin_id)
    initial = {
        "admin_id": admin_id,
        "tg_token": token,
        "tg_username": identity["username"],
    }
    if group_id is None and env.get("TELEGRAM_GROUP_ID"):
        group_id = parse_int(env["TELEGRAM_GROUP_ID"])
        if not -(10**16) < group_id < 0:
            raise ValueError(
                "В TELEGRAM_GROUP_ID укажите отрицательный числовой ID супергруппы."
            )
    if group_id is not None:
        check_group(telegram, group_id, identity["id"], admin_id)
        initial["chat"] = group_id
    if lolz_token := env.get("LOLZTEAM_TOKEN"):
        if not LOLZ_TOKEN.fullmatch(lolz_token):
            raise ValueError(
                "В LOLZTEAM_TOKEN укажите токен Lolzteam без префикса Bearer."
            )
        Lolz(lolz_token).forums()
        initial["lolz_token"] = lolz_token
    store.save(**initial)
    print(f"Откройте @{identity['username']} и отправьте /start.")


def updates(store, monitor):
    admin = Admin(monitor)
    token, offset = None, None
    while not monitor.stop.is_set():
        settings = store.settings()
        if token != settings["tg_token"]:
            admin.input = None
            if (
                token is None
                or token.split(":")[0] != settings["tg_token"].split(":")[0]
            ):
                offset = None
            token = settings["tg_token"]
        telegram = Telegram(token)
        try:
            options = {"timeout": 25, "allowed_updates": ["message", "callback_query"]}
            if offset is not None:
                options["offset"] = offset
            batch = telegram.call("getUpdates", **options)
            for update in batch:
                settings = store.settings()
                if settings["tg_token"] != token:
                    break
                offset = update["update_id"] + 1
                try:
                    admin.handle(telegram, update, settings)
                except (APIError, ValueError) as error:
                    try:
                        telegram.send(settings["admin_id"], str(error))
                    except APIError:
                        LOG.warning("Не удалось сообщить администратору об ошибке.")
        except APIError as error:
            LOG.warning("%s", error)
            if error.status in {401, 404}:
                raise SystemExit(
                    "Токен Telegram недействителен. Выполните python bot.py --setup."
                ) from None
            monitor.stop.wait(max(5, error.delay))


def main():
    parser = argparse.ArgumentParser(
        description="Пересылка объявлений Lolzteam в Telegram."
    )
    parser.add_argument(
        "--setup", action="store_true", help="Задать ID администратора и токен Telegram"
    )
    parser.add_argument(
        "--group-id",
        type=int,
        help="Привязать приватную супергруппу по её числовому ID",
    )
    parser.add_argument("--version", action="version", version=VERSION)
    args = parser.parse_args()
    if args.group_id is not None and args.group_id >= 0:
        parser.error("Укажите отрицательный числовой ID супергруппы.")
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    store = Store()
    with single_instance(DATA):
        try:
            settings = store.settings()
            if args.setup:
                setup(store, args.group_id)
            elif not settings.get("tg_token"):
                setup(
                    store,
                    args.group_id,
                    load_env_file(Path(__file__).with_name(".env")),
                )
            elif args.group_id is not None:
                telegram = Telegram(settings["tg_token"])
                check_group(
                    telegram,
                    args.group_id,
                    bot_identity(telegram)["id"],
                    settings["admin_id"],
                )
                store.save(chat=args.group_id)
        except (APIError, ValueError, EOFError, OSError) as error:
            raise SystemExit(str(error) or "Настройка прервана.") from None
        monitor = Monitor(store)
        worker = Thread(target=monitor.run, name="lolz-monitor", daemon=True)
        worker.start()
        try:
            updates(store, monitor)
        except KeyboardInterrupt:
            print("\nБот остановлен.")
        finally:
            monitor.stop.set()
            monitor.wake.set()
            worker.join(timeout=20)


if __name__ == "__main__":
    main()
