import re
from datetime import datetime, timedelta, timezone
from html import escape, unescape
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

from api import parse_int
from appearance import compose, utf16_length

FORUM_URL = "https://lolz.live/"
MOSCOW = timezone(timedelta(hours=3))
MESSAGE_LIMIT = 3900
BLOCKS = {
    "div",
    "p",
    "ul",
    "ol",
    "li",
    "blockquote",
    "pre",
    "section",
    "article",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "table",
    "tr",
    "hr",
    "details",
    "summary",
}
VOID = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "source",
    "wbr",
}
IGNORED = {"script", "style", "button", "svg", "template", "noscript"}


def link(value, base=FORUM_URL):
    if not value:
        return ""
    try:
        url = urljoin(base, str(value).strip())
        parsed = urlsplit(url)
        if (
            parsed.scheme in {"http", "https"}
            and parsed.hostname
            and not parsed.username
        ):
            if not any(ch.isspace() or ord(ch) < 32 for ch in url) and len(url) <= 2000:
                return url
    except ValueError:
        pass
    return ""


def format_moscow_time(value):
    return datetime.fromtimestamp(value, MOSCOW).strftime("%d.%m.%Y %H:%M:%S") + " МСК"


def normalize_whitespace(text):
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    text = text.replace("\u200b", "").replace("\ufeff", "")
    text = re.sub(r"[^\S\n]+", " ", text)
    text = "\n".join(line.strip() for line in text.split("\n"))
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text.encode("utf-8", errors="replace").decode("utf-8")


class ForumHTML(HTMLParser):
    def __init__(self, base, keep_newlines):
        super().__init__(convert_charrefs=True)
        self.base = base
        self.keep_newlines = keep_newlines
        self.elements = []
        self.chunks = []
        self.lists = []

    def add(self, text):
        if not text or any(tag in IGNORED for tag, _, _ in self.elements):
            return
        self.chunks.append(text)

    def newline(self):
        if self.chunks and not self.chunks[-1].endswith("\n"):
            self.add("\n")

    def handle_starttag(self, tag, attributes):
        attrs = dict(attributes)
        if tag in BLOCKS:
            self.newline()
        if tag in {"ul", "ol"}:
            self.lists.append(
                parse_int(attrs.get("start")) or 1 if tag == "ol" else None
            )
        if tag == "li":
            index = self.lists[-1] if self.lists else None
            self.add(f"{index}. " if index is not None else "• ")
            if index is not None:
                self.lists[-1] += 1
        if tag == "br":
            self.add("\n")
        if tag in {"img", "video", "audio", "iframe", "source"}:
            target = link(attrs.get("data-src") or attrs.get("src"), self.base)
            label = attrs.get("alt") or {"img": "Изображение", "audio": "Аудио"}.get(
                tag, "Медиа"
            )
            if target or attrs.get("alt"):
                self.add(f" {label} ({target}) " if target else f" {label} ")
        if tag not in VOID:
            target = link(attrs.get("href"), self.base) if tag == "a" else ""
            self.elements.append((tag, target, len(self.chunks)))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for index in range(len(self.elements) - 1, -1, -1):
            if self.elements[index][0] == tag:
                _, target, start = self.elements[index]
                if target:
                    label = "".join(self.chunks[start:]).strip()
                    if not label:
                        self.add(target)
                    elif label != target:
                        self.add(f" ({target})")
                for closed, _, _ in self.elements[index:]:
                    if closed in {"ul", "ol"} and self.lists:
                        self.lists.pop()
                del self.elements[index:]
                break
        if tag in BLOCKS:
            self.newline()
        elif tag in {"td", "th"}:
            self.add("  ")

    def handle_data(self, data):
        if not self.keep_newlines and not any(
            tag == "pre" for tag, _, _ in self.elements
        ):
            data = re.sub(r"[ \t\r\n\f]+", " ", data)
            if data == " " and (not self.chunks or self.chunks[-1].endswith("\n")):
                return
        self.add(data)


def plain_html(value, base=FORUM_URL, *, keep_newlines=False):
    parser = ForumHTML(base, keep_newlines)
    parser.feed(value)
    parser.close()
    while parser.elements:
        parser.handle_endtag(parser.elements[-1][0])
    return normalize_whitespace("".join(parser.chunks))


def bbcode(value):
    value = escape(value, quote=False)
    value = re.sub(
        r"\[/?(?:b|i|u|s|strike|color|size|font|align|center|left|right|indent|"
        r"highlight|plain|sub|sup)(?:=[^\]\r\n]*)?\]",
        "",
        value,
        flags=re.I,
    )
    for key, tag in {
        "code": "pre",
        "icode": "code",
        "quote": "blockquote",
        "spoiler": "div",
        "list": "ul",
    }.items():
        value = re.sub(rf"\[{key}(?:=[^\]\r\n]*)?\]", f"<{tag}>", value, flags=re.I)
        value = re.sub(rf"\[/{key}\]", f"</{tag}>", value, flags=re.I)
    value = value.replace("[*]", "<br>• ")
    value = re.sub(
        r"\[url(?:=([^\]\r\n]+))?\](.*?)\[/url\]",
        lambda m: f'<a href="{escape(unescape(m[1] or m[2]), quote=True)}">{m[2]}</a>',
        value,
        flags=re.I | re.S,
    )
    value = re.sub(
        r"\[img(?:=[^\]\r\n]*)?\](.*?)\[/img\]",
        lambda m: f'<img src="{escape(unescape(m[1]), quote=True)}">',
        value,
        flags=re.I | re.S,
    )
    return value


AMOUNT = r"\d(?:[ \u00a0\u202f]\d{3}|\d)*(?:[.,]\d+)?"
CURRENCY = r"(?:₽|\$|€|₸|руб(?:лей|ля|ль)?\.?|р\.|RUB|USD|USDT|EUR|KZT)"
MONEY = re.compile(
    rf"(?<![\w.])(?:{CURRENCY}\s*{AMOUNT}|{AMOUNT}"
    rf"(?:[ \t]*[–—-][ \t]*{AMOUNT})?[ \t]*(?:[кk][ \t]*)?"
    rf"{CURRENCY})(?!\w)",
    re.I,
)
LABELED_PRICE = re.compile(
    r"\b(?:цена|бюджет|оплата|ставка|зарплата|оклад|стоимость|вознаграждение|"
    r"заработная плата|оплачиваю|плачу|з/?п)\s*[:=–—-]\s*([^\n]{1,120})",
    re.I,
)
NEGOTIABLE = re.compile(r"\b(?:по договор[её]нности|договорная|обсуждается)\b", re.I)


def price(thread, title, body):
    for key in ("price", "budget", "salary"):
        value = thread.get(key)
        if (
            isinstance(value, (int, float, str))
            and not isinstance(value, bool)
            and str(value).strip()
        ):
            currency = str(thread.get("currency") or "").strip()
            return " ".join(f"{value} {currency}".split())[:120]
    labeled = LABELED_PRICE.search(f"{title}\n{body}")
    if labeled:
        value = labeled[1].strip()
        if re.search(r"\d", value) or NEGOTIABLE.search(value):
            return value
    for text in (title, body):
        values = list(dict.fromkeys(match[0] for match in MONEY.finditer(text)))
        if len(values) == 1:
            return values[0]
        if len(values) > 1:
            return "Несколько сумм в объявлении"
        negotiable = NEGOTIABLE.search(text)
        if negotiable:
            return negotiable[0]
    return "Не указана"


def split_text(text, room):
    if utf16_length(text) <= room:
        return text, ""
    head = text.encode("utf-16-le", errors="replace")[: 2 * room].decode(
        "utf-16-le", errors="ignore"
    )
    boundary = max(head.rfind(" "), head.rfind("\n"), head.rfind("\t")) + 1
    if boundary >= len(head) * 0.7:
        head = head[:boundary]
    return head, text[len(head) :]


def plain_parts(text):
    while text:
        part, text = split_text(text, MESSAGE_LIMIT)
        if part := part.strip():
            yield {"text": part}


def upgrade_parts(parts, sent):
    upgraded = [None] * sent
    for part in parts[sent:]:
        upgraded.extend(plain_parts(plain_html(part, keep_newlines=True)))
    return upgraded


def render(thread, post, event):
    thread_id = event["thread_id"]
    url = link(thread.get("view_url")) or f"{FORUM_URL}threads/{thread_id}/"
    html = post.get("message_html")
    body = ""
    if isinstance(html, str) and html.strip():
        body = plain_html(html, url)
    if not body and post.get("message"):
        body = plain_html(bbcode(str(post["message"])), url, keep_newlines=True)
    if not body:
        body = normalize_whitespace(str(post.get("message_plain_text") or ""))
    if not body:
        raise ValueError("Lolzteam: текст первого сообщения недоступен.")
    title = " ".join(
        normalize_whitespace(
            unescape(str(thread.get("title") or f"Тема {thread_id}"))
        ).split()
    )[:400]
    created = event["created"]
    bumped = event["event_time"] if event["kind"] == "bump" else event["bumped"]
    heading = (
        ("bump", "Тема поднята") if event["kind"] == "bump" else ("new", "Новая тема")
    )
    lines = [
        heading,
        title,
        ("price", f"Цена · {price(thread, title, body)}"),
        ("calendar", f"Создана {format_moscow_time(created)}"),
    ]
    if bumped > created:
        lines.append(("bump", f"Поднята {format_moscow_time(bumped)}"))
    lines.append(("link", f"Открыть тему · {url}"))
    first = compose(lines)
    head, tail = split_text(body, MESSAGE_LIMIT - utf16_length(first["text"]) - 2)
    if head.strip():
        first["text"] += "\n\n" + head.strip()
    return [first, *plain_parts(tail)]
