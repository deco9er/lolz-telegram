import json
import logging
import re
import time
from collections import Counter
from dataclasses import dataclass
from threading import Event, Lock

from api import APIError, Lolz, Telegram, as_dict, parse_int
from appearance import compose
from render import render, upgrade_parts

NAMES = {
    "work": "Работа и услуги",
    "vacancies": "Вакансии",
    "contractors": "Поиск исполнителей",
}
ROLES = {
    "работа и услуги": "work",
    "вакансии": "vacancies",
    "вакансия": "vacancies",
    "поиск исполнителей": "contractors",
}
GROUP_SEND_INTERVAL = 3.2
CREATION_SCAN_OVERLAP_SECONDS = 5 * 60
ARCHIVE_PAGES_PER_CYCLE = 4
LOG = logging.getLogger(__name__)


def forum_id(node):
    return parse_int(node.get("node_id") or node.get("forum_id"))


def forum_title(node):
    return str(node.get("title") or node.get("forum_title") or "")


def title_key(title):
    return " ".join(re.findall(r"\w+", title.casefold().replace("ё", "е")))


def is_forum(node):
    return str(node.get("node_type_id") or "forum").casefold() == "forum"


def thread_forum(thread):
    forum = as_dict(thread.get("forum"))
    node = forum_id(thread) or forum_id(forum)
    title = thread.get("node_title") or (
        forum_title(forum) if forum_id(forum) == node else ""
    )
    return node, str(title)


@dataclass(frozen=True)
class Section:
    id: int
    title: str
    topic_name: str
    legacy_name: str = ""


def discover(nodes):
    work = [n for n in nodes if ROLES.get(title_key(forum_title(n))) == "work"]
    roots = {forum_id(n) for n in work}
    parents = {forum_id(n): parse_int(n.get("parent_node_id")) for n in nodes}

    def under_work(node):
        parent = forum_id(node)
        seen = set()
        while parent and parent not in seen:
            if parent in roots:
                return True
            seen.add(parent)
            parent = parents.get(parent, 0)
        return False

    result = {}
    direct = [n for n in work if is_forum(n) and forum_id(n)]
    if len(direct) == 1:
        result["work"] = forum_id(direct[0])
    for key in ("vacancies", "contractors"):
        matches = [
            n
            for n in nodes
            if is_forum(n)
            and forum_id(n)
            and ROLES.get(title_key(forum_title(n))) == key
        ]
        scoped = [n for n in matches if under_work(n)]
        matches = scoped or matches
        if len(matches) == 1:
            result[key] = forum_id(matches[0])
    return result


def section_conflicts(nodes, configured):
    available = {forum_id(node): node for node in nodes if is_forum(node)}
    conflicts = {}
    for key, node_id in configured.items():
        node = available.get(node_id)
        if node is None:
            conflicts[key] = f"форум {node_id} недоступен"
            continue
        title = forum_title(node)
        role = ROLES.get(title_key(title))
        if role and role != key:
            conflicts[key] = f"ID {node_id} относится к разделу «{title}»"
    return conflicts


def check_group(telegram, chat_id, bot_id, admin_id=None):
    chat = telegram.call("getChat", chat_id=chat_id)
    if (
        chat.get("type") != "supergroup"
        or not chat.get("is_forum")
        or chat.get("username")
        or chat.get("active_usernames")
    ):
        raise ValueError("Требуется приватная супергруппа с включёнными темами.")
    for user_id in (bot_id, admin_id):
        if user_id is None:
            continue
        member = telegram.call("getChatMember", chat_id=chat_id, user_id=user_id)
        if member.get("status") == "creator":
            continue
        if member.get("status") != "administrator" or (
            user_id == bot_id and not member.get("can_manage_topics")
        ):
            raise ValueError(
                "Вы должны быть админом группы. Боту нужны права администратора и «Управление темами»."
            )
    return chat


def events(rows, forum, since):
    for row in rows:
        if thread_forum(row)[0] != forum:
            continue
        if row.get("is_hidden") or row.get("discussion_state") in {
            "deleted",
            "moderated",
        }:
            continue
        thread = parse_int(row.get("thread_id"))
        created = parse_int(row.get("post_date"))
        bumped = parse_int(row.get("last_bump_date"))
        if thread <= 0 or created <= 0:
            continue
        if created >= since:
            yield thread, "new", created, created, bumped
        if bumped > created and bumped >= since:
            yield thread, "bump", bumped, created, bumped


class ConfigChanged(Exception):
    pass


class Monitor:
    def __init__(self, store):
        self.store = store
        self.wake = Event()
        self.stop = Event()
        self.send_lock = Lock()
        self.note = "Ожидаю настройки."
        self.active_sections = {}
        self.section_warnings = []
        self.catalog = ("", 0, [])
        self.last_send = 0.0
        self.send_after = 0.0
        self.scan_after = None

    def save(self, **settings):
        with self.send_lock:
            self.store.save(**settings)
        self.wake.set()

    def use_auto_sections(self):
        with self.send_lock:
            self.catalog = ("", 0, [])
            self.store.delete_settings("sections", "forum_ids")
        self.wake.set()

    def assert_current(self, settings):
        if self.stop.is_set() or self.store.settings() != settings:
            raise ConfigChanged

    def forums(self, settings, *, refresh=False):
        token = settings.get("lolz_token")
        if not token:
            return []
        previous_token, refreshed, nodes = self.catalog
        if refresh or token != previous_token or time.monotonic() - refreshed >= 3600:
            nodes = Lolz(token).forums()
            self.catalog = (token, time.monotonic(), nodes)
        return nodes

    def sections(self, settings, *, refresh=False):
        nodes = self.forums(settings, refresh=refresh)
        automatic = discover(nodes)
        warnings = []
        available = {
            forum_id(node): " ".join(forum_title(node).split())
            for node in nodes
            if is_forum(node) and forum_id(node) > 0
        }
        if "forum_ids" in settings:
            selected = settings["forum_ids"]
        else:
            configured = settings.get("sections", automatic)
            conflicts = section_conflicts(nodes, configured)
            selected = []
            for key, node in configured.items():
                if key in conflicts:
                    node = automatic.get(key)
                    warnings.append(
                        f"{NAMES[key]} · {conflicts[key]}. "
                        + (
                            f"Использую ID {node} из каталога."
                            if node
                            else "Подходящий раздел в каталоге не найден."
                        )
                    )
                if node:
                    selected.append(node)
        sections = {}
        title_counts = Counter(map(title_key, available.values()))
        legacy_names = {node: NAMES[key] for key, node in automatic.items()}
        missing_count = 0
        for node in dict.fromkeys(selected):
            if node not in available:
                missing_count += 1
                continue
            title = available[node] or f"Форум {node}"
            legacy = legacy_names.get(node, "")
            name = legacy or title
            if not legacy and (len(name) > 128 or title_counts[title_key(title)] > 1):
                suffix = f" · {node}"
                name = name[: 128 - len(suffix)] + suffix
            sections[node] = Section(node, title, name, legacy)
        if missing_count:
            warnings.append(
                f"Выбранных разделов нет в текущем каталоге · {missing_count}."
            )
        self.active_sections = sections
        self.section_warnings = warnings
        return sections

    def destination(self, thread, settings, sections):
        node, title = thread_forum(thread)
        if node <= 0:
            raise APIError("Lolzteam: в данных темы нет ID её текущего раздела.")
        section = sections.get(node)
        if section and title and title_key(title) != title_key(section.title):
            sections = self.sections(settings, refresh=True)
            section = sections.get(node)
            actual_role = ROLES.get(title_key(title))
            if (
                section
                and actual_role
                and actual_role != ROLES.get(title_key(section.title))
            ):
                raise APIError(
                    f"Lolzteam: раздел темы «{title}» (ID {node}) не совпадает с каталогом."
                )
        return node, title, section

    def topic(self, telegram, chat, section):
        topic = self.store.topic(chat, section.id, legacy_name=section.legacy_name)
        if topic is None:
            created = telegram.call(
                "createForumTopic", chat_id=chat, name=section.topic_name
            )
            topic = parse_int(created.get("message_thread_id"))
            if topic <= 1:
                raise APIError("Telegram: в ответе нет номера созданного топика.")
            self.store.save_topic(chat, section.id, topic)
        return topic

    def send_part(self, telegram, chat, section, part, settings):
        with self.send_lock:
            delay = max(0, self.last_send + GROUP_SEND_INTERVAL - time.monotonic())
            if self.stop.wait(delay):
                raise ConfigChanged
            self.assert_current(settings)
            topic = self.topic(telegram, chat, section)
            try:
                telegram.send(
                    chat, topic=topic, silent=settings.get("silent", False), **part
                )
            except APIError as error:
                if (
                    "message thread not found" in error.detail
                    or "topic_deleted" in error.detail
                ):
                    self.store.forget_topic(chat, section.id)
                elif (
                    "topic_closed" in error.detail or "topic was closed" in error.detail
                ):
                    telegram.call(
                        "reopenForumTopic", chat_id=chat, message_thread_id=topic
                    )
                raise
            self.last_send = time.monotonic()

    def test_send(self, settings):
        chat = settings.get("chat")
        if not chat:
            raise ValueError("Сначала привяжите группу командой /bind.")
        sections = self.sections(settings)
        if not sections:
            raise ValueError("Сначала задайте токен Lolzteam и настройте разделы.")
        telegram = Telegram(settings["tg_token"])
        check_group(
            telegram,
            chat,
            parse_int(telegram.token.split(":", 1)[0]),
            settings["admin_id"],
        )
        for section in sections.values():
            part = compose([("test", f"Тест доставки · {section.title}")])
            self.send_part(telegram, chat, section, part, settings)

    def scan(self, lolz_client, chat, forum_id, settings, checked_at):
        scan_state = self.store.route(chat, forum_id, checked_at)
        subscribed_at = scan_state["since"]

        def scan_page(page, order="", sticky=None):
            self.assert_current(settings)
            threads, has_next_page = lolz_client.page(forum_id, page, order, sticky)
            self.store.enqueue(chat, forum_id, events(threads, forum_id, subscribed_at))
            return threads, has_next_page

        scan_page(1)
        creation_cutoff = max(
            subscribed_at, scan_state["checked"] - CREATION_SCAN_OVERLAP_SECONDS
        )
        recent_page = 1
        while True:
            threads, has_next_page = scan_page(recent_page, "post_date", False)
            creation_dates = [parse_int(thread.get("post_date")) for thread in threads]
            if not has_next_page or (
                creation_dates and 0 < min(creation_dates) < creation_cutoff
            ):
                break
            recent_page += 1
        archive_page = scan_state["sweep"]
        for _ in range(ARCHIVE_PAGES_PER_CYCLE):
            _, has_next_page = scan_page(archive_page, "post_date", False)
            archive_page = archive_page + 1 if has_next_page else 1
            if not has_next_page:
                break
        pinned_page = 1
        while True:
            _, has_next_page = scan_page(pinned_page, "post_date", True)
            if not has_next_page:
                break
            pinned_page += 1
        self.store.advance(chat, forum_id, checked=checked_at, sweep=archive_page)

    def post_event(self, telegram, lolz_client, chat, sections, event, settings):
        thread, post = lolz_client.thread(event["thread_id"])
        node, title, section = self.destination(thread, settings, sections)
        if section is None:
            reason = f"Тема {event['thread_id']} находится вне настроенных разделов · {title or node}."
            self.store.fail(event["id"], reason, int(time.time()), skipped=True)
            return
        moved = node != event["forum"]
        sent = 0 if moved else event["sent"]
        if moved:
            self.store.reroute(event["id"], node)
        parts = json.loads(event["parts"]) if event["parts"] else None
        if (
            thread.get("is_hidden")
            or thread.get("can_view") is False
            or post.get("can_view") is False
            or thread.get("discussion_state") in {"deleted", "moderated"}
            or post.get("message_state") in {"deleted", "moderated"}
        ):
            raise ValueError("Lolzteam: тема или первый пост недоступны.")
        if parts is None or moved:
            parts = render(thread, post, event)
            self.store.save_parts(event["id"], parts)
        elif parts and isinstance(parts[-1], str):
            parts = upgrade_parts(parts, sent)
            self.store.save_parts(event["id"], parts)
        if sent >= len(parts):
            self.store.sent(event["id"], sent, True)
            return
        for index in range(sent, len(parts)):
            self.send_part(telegram, chat, section, parts[index], settings)
            self.store.sent(event["id"], index + 1, index + 1 == len(parts))

    def deliver(self, telegram, lolz_client, chat, sections, settings):
        if time.monotonic() < self.send_after:
            return
        for event in self.store.pending(chat, int(time.time())):
            try:
                self.assert_current(settings)
                self.post_event(telegram, lolz_client, chat, sections, event, settings)
            except (APIError, ValueError) as error:
                status = getattr(error, "status", 0)
                if status == 429 and str(error).startswith("Lolzteam"):
                    break
                delay = max(
                    getattr(error, "delay", 0),
                    min(3600, 20 * 2 ** min(event["attempts"], 8)),
                )
                skipped = status == 404 and str(error).startswith("Lolzteam")
                self.store.fail(
                    event["id"], str(error), int(time.time()) + delay, skipped=skipped
                )
                if status == 429:
                    if str(error).startswith("Telegram"):
                        self.send_after = time.monotonic() + delay
                    break
                if status in {401, 403} and str(error).startswith("Telegram"):
                    break

    def cycle(self):
        settings = self.store.settings()
        chat = settings.get("chat")
        if settings.get("paused"):
            self.note = "На паузе."
            return
        if not chat or not settings.get("lolz_token"):
            self.note = "Добавьте токен Lolzteam и привяжите группу."
            return
        lolz_client = Lolz(settings["lolz_token"])
        if lolz_client.retry_in():
            self.note = "Опрос ждёт сброса лимита Lolzteam."
            return
        sections = self.sections(settings)
        if not sections:
            self.note = "\n".join(
                [
                    "Выберите разделы в админке, чтобы запустить мониторинг.",
                    *self.section_warnings,
                ]
            )
            return
        telegram = Telegram(settings["tg_token"])
        bot_id = int(telegram.token.split(":", 1)[0])
        check_group(telegram, chat, bot_id, settings["admin_id"])
        self.deliver(telegram, lolz_client, chat, sections, settings)
        if lolz_client.retry_in():
            self.note = "Опрос ждёт сброса лимита Lolzteam."
            return
        now = int(time.time())
        errors = list(self.section_warnings)
        section_list = list(sections.values())
        if self.scan_after in sections:
            index = list(sections).index(self.scan_after) + 1
            section_list = section_list[index:] + section_list[:index]
        for index, section in enumerate(section_list, 1):
            self.scan_after = section.id
            try:
                with self.send_lock:
                    self.assert_current(settings)
                    self.topic(telegram, chat, section)
                self.scan(lolz_client, chat, section.id, settings, now)
            except (APIError, ValueError) as error:
                errors.append(f"{section.title} · {error}")
                if isinstance(error, APIError) and error.status == 429:
                    break
            if index % 8 == 0:
                self.deliver(telegram, lolz_client, chat, sections, settings)
                if lolz_client.retry_in():
                    break
        if lolz_client.retry_in():
            errors.append("Опрос ждёт сброса лимита Lolzteam.")
        else:
            self.deliver(telegram, lolz_client, chat, sections, settings)
            if lolz_client.retry_in():
                errors.append("Опрос ждёт сброса лимита Lolzteam.")
        if time.monotonic() < self.send_after:
            errors.append("Доставка ждёт снятия лимита Telegram.")
        if len(errors) > 5:
            errors = [*errors[:5], f"Дополнительных ошибок · {len(errors) - 5}."]
        self.note = (
            "\n".join(errors) if errors else "Последняя проверка прошла успешно."
        )

    def run(self):
        while not self.stop.is_set():
            self.wake.clear()
            backoff = 0
            try:
                self.cycle()
            except ConfigChanged:
                continue
            except (APIError, ValueError) as error:
                self.note = str(error)
                backoff = getattr(error, "delay", 0)
                LOG.warning("%s", error)
            except Exception as error:
                self.note = "Внутренняя ошибка мониторинга. См. журнал процесса."
                LOG.error("Ошибка мониторинга (%s)", type(error).__name__)
            settings = self.store.settings()
            token = settings.get("lolz_token")
            limit_wait = (
                Lolz(token).retry_in() if token and not settings.get("paused") else 0
            )
            self.wake.wait(max(backoff, limit_wait) or settings.get("interval", 60))
