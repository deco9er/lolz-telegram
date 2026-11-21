import json
import math
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from hashlib import sha256
from http.client import HTTPException
from threading import Lock
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from appearance import FALLBACKS

TG_TOKEN = re.compile(r"[0-9]{5,20}:[A-Za-z0-9_-]{20,100}")
LOLZ_TOKEN = re.compile(r"[!-~]{12,4096}")
LOLZ_BASE = "https://api.lolz.team"
TELEGRAM_BASE = "https://api.telegram.org"
DEFAULT_RATE_WINDOW_SECONDS = 60
MINIMUM_REQUEST_INTERVAL_SECONDS = 0.25
MAX_SUPPORTED_RESET_TIMESTAMP = 253402214400


class APIError(Exception):
    def __init__(self, message, status=0, delay=0, detail="", quota=None):
        super().__init__(message)
        self.status = status
        self.delay = delay
        self.detail = detail
        self.quota = quota


@dataclass(frozen=True)
class Quota:
    limit: int | None
    remaining: int | None
    reset: int | None
    bucket: str
    observed_at: int
    expires_at: float | None


def nonnegative(value):
    if isinstance(value, int) and not isinstance(value, bool):
        return value if value >= 0 else None
    if isinstance(value, str) and re.fullmatch(r"[0-9]{1,12}", value.strip()):
        return int(value)
    return None


def parse_rate_quota(headers, data):
    system_info = as_dict(data.get("system_info"))
    rate_limit = as_dict(system_info.get("rate_limit"))
    reported_values = []
    for header, key in (
        ("Limit", "limit"),
        ("Remaining", "remaining"),
        ("Reset", "reset"),
    ):
        header_value = nonnegative(headers.get(f"X-RateLimit-{header}"))
        reported_values.append(
            header_value
            if header_value is not None
            else nonnegative(rate_limit.get(key))
        )
    limit, remaining, reset = reported_values
    if reset is not None and not 0 < reset < MAX_SUPPORTED_RESET_TIMESTAMP:
        reset = None
    if limit is None and remaining is None and reset is None:
        return None
    observed_at = int(time.time())
    server_time = nonnegative(system_info.get("time")) or observed_at
    expires_at = time.monotonic() + max(0, reset - server_time) if reset else None
    bucket = " ".join(str(rate_limit.get("bucket") or "GET").split()) or "GET"
    return Quota(limit, remaining, reset, bucket[:60], observed_at, expires_at)


@dataclass
class Usage:
    endpoints: Counter = field(default_factory=Counter)
    successes: int = 0
    errors: int = 0
    limited: int = 0
    deferred: int = 0
    quota: Quota | None = None
    blocked_until: float = 0

    def retry_in(self):
        return max(0, math.ceil(self.blocked_until - time.monotonic()))

    def record(self, endpoint, quota, error=None):
        self.endpoints[endpoint] += 1
        if error is None:
            self.successes += 1
        else:
            self.errors += 1
            self.limited += int(error.status == 429)
        if quota is not None:
            previous_expiry = self.quota.expires_at if self.quota is not None else None
            self.quota = quota
            if quota.remaining == 0:
                expiry = quota.expires_at
                if expiry is None and error is None:
                    now = time.monotonic()
                    expiry = (
                        previous_expiry
                        if previous_expiry and previous_expiry > now
                        else now + DEFAULT_RATE_WINDOW_SECONDS
                    )
                if expiry is not None:
                    self.blocked_until = max(self.blocked_until, expiry + 1)
        if error is not None and error.status == 429:
            self.blocked_until = max(self.blocked_until, time.monotonic() + error.delay)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


http = build_opener(NoRedirect())


def request(service, url, *, headers=None, body=None, timeout=20):
    http_request = Request(
        url,
        headers={"Accept": "application/json", **(headers or {})},
        data=None if body is None else json.dumps(body).encode("utf-8"),
    )
    try:
        try:
            response = http.open(http_request, timeout=timeout)
        except HTTPError as error:
            response = error
        with response:
            status, headers, raw = response.code, response.headers, response.read()
    except (URLError, OSError, HTTPException):
        raise APIError(
            f"{service}: соединение прервано или истекло время ожидания."
        ) from None
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeError):
        data = {}
    data = as_dict(data)
    quota = parse_rate_quota(headers, data) if service == "Lolzteam" else None
    if status != 200 or not data:
        retry = nonnegative(as_dict(data.get("parameters")).get("retry_after"))
        if retry is None:
            retry = nonnegative(headers.get("Retry-After"))
        if status == 429:
            waits = [retry] if retry is not None else []
            if quota is not None and quota.expires_at is not None:
                waits.append(math.ceil(quota.expires_at - time.monotonic() + 1))
            retry = max(1, max(waits, default=DEFAULT_RATE_WINDOW_SECONDS))
        detail = (
            str(data.get("description") or "").lower() if service == "Telegram" else ""
        )
        reason = {
            401: "проверьте токен",
            403: "проверьте права доступа",
            404: "ресурс не найден",
            409: "конфликт обновлений",
            429: "достигнут лимит запросов",
        }.get(status, "запрос не выполнен")
        raise APIError(
            f"{service}: {reason} (HTTP {status}).", status, retry or 0, detail, quota
        )
    return data, quota


def parse_int(value):
    try:
        return int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0


def as_dict(value):
    return value if isinstance(value, dict) else {}


def records(value):
    if isinstance(value, dict):
        value = list(value.values())
    if not isinstance(value, list):
        raise APIError("Lolzteam: ожидался список в ответе API.")
    return [item for item in value if isinstance(item, dict)]


class Telegram:
    def __init__(self, token):
        self.token = token

    def call(self, method, **body):
        result, _ = request(
            "Telegram",
            f"{TELEGRAM_BASE}/bot{self.token}/{method}",
            headers={"Content-Type": "application/json"},
            body=body,
            timeout=body.get("timeout", 0) + 15,
        )
        if result.get("ok") is not True:
            raise APIError(
                "Telegram: API вернул ошибку.",
                parse_int(result.get("error_code")),
                detail=str(result.get("description") or "").lower(),
            )
        return result.get("result")

    def send(
        self, chat, text, *, topic=None, entities=None, keyboard=None, silent=False
    ):
        body = {
            "chat_id": chat,
            "text": text,
            "link_preview_options": {"is_disabled": True},
            "disable_notification": silent,
        }
        if topic:
            body["message_thread_id"] = topic
        if entities:
            body["entities"] = entities
        if keyboard:
            body["reply_markup"] = {"inline_keyboard": keyboard}
        try:
            return self.call("sendMessage", **body)
        except APIError as error:
            has_icons = any(
                "icon_custom_emoji_id" in button
                for row in keyboard or []
                for button in row
            )
            if (
                error.status not in {400, 403}
                or not (entities or has_icons)
                or not any(
                    part in error.detail
                    for part in (
                        "emoji",
                        "premium",
                        "button_type_invalid",
                        "button type invalid",
                        "parse entities",
                    )
                )
            ):
                raise
            body.pop("entities", None)
            if has_icons:
                fallback = []
                for row in keyboard:
                    buttons = []
                    for button in row:
                        button_payload = dict(button)
                        icon = FALLBACKS.get(
                            button_payload.pop("icon_custom_emoji_id", ""), ""
                        )
                        if icon:
                            button_payload["text"] = f"{icon} {button_payload['text']}"
                        buttons.append(button_payload)
                    fallback.append(buttons)
                body["reply_markup"] = {"inline_keyboard": fallback}
            return self.call("sendMessage", **body)


class Lolz:
    _request_lock = Lock()
    _usage_lock = Lock()
    _next_request = 0.0
    _usage = {}

    def __init__(self, token):
        self.token = token
        key = sha256(token.encode()).digest()
        with self._usage_lock:
            self.usage = self._usage.setdefault(key, Usage())

    def retry_in(self):
        with self._usage_lock:
            return self.usage.retry_in()

    def statistics(self):
        with self._usage_lock:
            usage = self.usage
            return {
                "quota": usage.quota,
                "successes": usage.successes,
                "errors": usage.errors,
                "limited": usage.limited,
                "deferred": usage.deferred,
                "endpoints": dict(sorted(usage.endpoints.items())),
                "wait": usage.retry_in(),
            }

    def get(self, path, **query):
        cls = type(self)
        endpoint = re.sub(r"/[0-9]+(?=/|$)", "/{id}", path)
        with cls._request_lock:
            with cls._usage_lock:
                if wait := self.usage.retry_in():
                    self.usage.deferred += 1
                    raise APIError(
                        "Lolzteam: запросы отложены до сброса лимита.", 429, wait
                    )
            time.sleep(max(0, cls._next_request - time.monotonic()))
            cls._next_request = time.monotonic() + MINIMUM_REQUEST_INTERVAL_SECONDS
            try:
                data, quota = request(
                    "Lolzteam",
                    f"{LOLZ_BASE}{path}?{urlencode({'locale': 'ru', **query})}",
                    headers={"Authorization": f"Bearer {self.token}"},
                )
                if data.get("errors"):
                    raise APIError("Lolzteam: API вернул ошибку.", quota=quota)
            except APIError as error:
                with cls._usage_lock:
                    self.usage.record(endpoint, error.quota, error)
                raise
            with cls._usage_lock:
                self.usage.record(endpoint, quota)
            return data

    def forums(self):
        return records(self.get("/forums", order="list").get("forums"))

    def page(self, forum_id, page, order="", sticky=None):
        query = {
            "forum_id": forum_id,
            "page": page,
            "limit": 50,
            "fields_exclude": "first_post,last_post,*.first_post,*.last_post",
        }
        if order:
            query.update(order=order, direction="desc")
        if sticky is not None:
            query["sticky"] = "1" if sticky else "0"
        data = self.get("/threads", **query)
        threads = records(data.get("threads"))
        links = as_dict(data.get("links"))
        pages = parse_int(links.get("pages"))
        more = (
            page < pages
            if pages
            else bool(links["next"])
            if "next" in links
            else len(threads) >= 50
        )
        return threads, bool(threads and more)

    def thread(self, thread_id, *, with_post=True):
        fields = (
            {"fields_include": "*"}
            if with_post
            else {"fields_exclude": "first_post,last_post"}
        )
        thread = as_dict(self.get(f"/threads/{thread_id}", **fields).get("thread"))
        if parse_int(thread.get("thread_id")) != thread_id:
            raise APIError("Lolzteam: API не вернул запрошенную тему.")
        if not with_post:
            return thread, {}
        post = as_dict(thread.get("first_post"))
        if not any(
            isinstance(post.get(k), str) and post[k].strip()
            for k in ("message_html", "message", "message_plain_text")
        ):
            post_id = parse_int(thread.get("first_post_id"))
            if post_id:
                post = as_dict(self.get(f"/posts/{post_id}").get("post"))
        return thread, post
