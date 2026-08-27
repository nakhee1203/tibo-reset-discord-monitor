#!/usr/bin/env python3
"""Free Tibo X reset monitor -> Discord.

No X API key is required. The monitor reads public, third-party sources:
  1) FxTwitter RSS for @thsottiaux
  2) Codex Radar public JSON as a fallback/secondary source

Required environment variable:
  DISCORD_WEBHOOK_URL

Optional:
  TARGET_USERNAME=thsottiaux
  DISCORD_MENTION=<@123456789012345678>  # or <@&ROLE_ID> or @everyone
  ALERT_AMBIGUOUS=true
  ALERT_COMPLETED=false
  STATE_FILE=state.json
  FREE_FEED_URLS=https://fxtwitter.com/{handle}/feed.xml
  RADAR_URLS=https://codexradar.com/current.json,https://codex-reset-radar.pages.dev/current.json
"""

from __future__ import annotations

import html
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

DEFAULT_USERNAME = "thsottiaux"
DEFAULT_STATE_FILE = "state.json"
DEFAULT_FEED_URLS = "https://fxtwitter.com/{handle}/feed.xml"
DEFAULT_RADAR_URLS = (
    "https://codexradar.com/current.json,"
    "https://codex-reset-radar.pages.dev/current.json"
)
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36 "
    "tibo-reset-discord-monitor/2.0"
)

STATUS_ID_RE = re.compile(r"(?:x\.com|twitter\.com|fxtwitter\.com|fixupx\.com)/([^/?#]+)/status/(\d+)", re.I)
TAG_RE = re.compile(r"<[^>]+>")


@dataclass(frozen=True)
class Classification:
    kind: str
    reasons: tuple[str, ...]


RESET_RE = re.compile(
    r"\b(reset(?:s|ting|ted)?|refresh(?:es|ing|ed)?|replenish(?:es|ing|ed)?|restore(?:s|d|ing)?)\b",
    re.IGNORECASE,
)

CONTEXT_PATTERNS = [
    r"\busage\b",
    r"\blimits?\b",
    r"\bcodex\b",
    r"\bchatgpt\b",
    r"\bgpt(?:[- ]?\d[\w.-]*)?\b",
    r"\bplus\b",
    r"\bpro\b",
    r"\bsubscriptions?\b",
    r"\bsubscribers?\b",
    r"\bpaid users?\b",
    r"\bbanked\b",
    r"\bweekly\b",
    r"\brate limits?\b",
]

FUTURE_PATTERNS = [
    r"\bwill\b",
    r"\bgoing to\b",
    r"\bgonna\b",
    r"\babout to\b",
    r"\bplan(?:ning)? to\b",
    r"\bscheduled\b",
    r"\bincoming\b",
    r"\bsoon\b",
    r"\blater(?: today)?\b",
    r"\btonight\b",
    r"\bthis evening\b",
    r"\bthis afternoon\b",
    r"\btomorrow\b",
    r"\bnext hour\b",
    r"\bin the next\b",
    r"\bwithin the next\b",
    r"\bby (?:tonight|tomorrow|this evening)\b",
    r"\bETA\b",
]

COMPLETION_PATTERNS = [
    r"\bhas been reset\b",
    r"\bhave been reset\b",
    r"\bwas reset\b",
    r"\breset (?:is )?(?:live|done|complete|completed)\b",
    r"\breset has landed\b",
    r"\breset landed\b",
    r"\breset has been propagated\b",
    r"\breset (?:is|was) propagated\b",
    r"\bfully reset\b",
    r"\bjust reset\b",
]

STRONG_FUTURE_PATTERNS = [
    r"\breset incoming\b",
    r"\breset will (?:land|arrive|happen)\b",
    r"\bwill reset (?:the )?(?:usage )?limits?\b",
    r"\b(?:usage )?limits? will (?:be )?reset\b",
    r"\bresetting (?:the )?(?:usage )?limits?\b.{0,100}\b(?:tomorrow|tonight|soon|later|morning|evening|next hour)\b",
    r"\bfull reset\b.{0,100}\b(?:tonight|tomorrow|soon|later|next hour|this evening)\b",
]


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def csv_env(name: str, default: str) -> list[str]:
    value = os.getenv(name, default)
    return [part.strip() for part in value.split(",") if part.strip()]


def any_pattern(text: str, patterns: list[str]) -> bool:
    return any(re.search(pattern, text, re.IGNORECASE | re.DOTALL) for pattern in patterns)


def classify_post(text: str) -> Classification:
    clean = " ".join(text.replace("’", "'").split())
    has_reset = bool(RESET_RE.search(clean))
    if not has_reset:
        return Classification("IRRELEVANT", ())

    has_context = any_pattern(clean, CONTEXT_PATTERNS)
    has_future = any_pattern(clean, FUTURE_PATTERNS)
    has_completion = any_pattern(clean, COMPLETION_PATTERNS)
    has_strong_future = any_pattern(clean, STRONG_FUTURE_PATTERNS)

    reasons: list[str] = ["reset 관련 표현"]
    if has_context:
        reasons.append("GPT/Codex/사용량 문맥")
    if has_future:
        reasons.append("미래 시점 표현")
    if has_completion:
        reasons.append("완료 표현")
    if has_strong_future:
        reasons.append("강한 리셋 예고 패턴")

    if has_strong_future or (has_context and has_future and not has_completion):
        return Classification("PREANNOUNCEMENT", tuple(reasons))
    if has_completion and not has_future:
        return Classification("COMPLETED", tuple(reasons))
    if has_future or has_context:
        return Classification("AMBIGUOUS", tuple(reasons))
    return Classification("IRRELEVANT", ())


def request_bytes(url: str, *, attempts: int = 2) -> bytes:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/rss+xml, application/atom+xml, application/json, text/xml, */*",
        "Cache-Control": "no-cache",
    }
    for attempt in range(1, attempts + 1):
        req = Request(url, headers=headers, method="GET")
        try:
            with urlopen(req, timeout=25) as response:
                return response.read()
        except HTTPError as exc:
            retryable = exc.code in {429, 500, 502, 503, 504}
            if retryable and attempt < attempts:
                time.sleep(2**attempt)
                continue
            raise RuntimeError(f"HTTP {exc.code} for {url}") from exc
        except (URLError, TimeoutError) as exc:
            if attempt < attempts:
                time.sleep(2**attempt)
                continue
            raise RuntimeError(f"Network error for {url}: {exc}") from exc
    raise RuntimeError(f"Request failed: {url}")


def request_json_post(url: str, body: dict[str, Any]) -> dict[str, Any] | None:
    payload = json.dumps(body).encode("utf-8")
    req = Request(
        url,
        data=payload,
        headers={"User-Agent": USER_AGENT, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(req, timeout=25) as response:
            raw = response.read().decode("utf-8", errors="replace")
            return json.loads(raw) if raw else None
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Discord HTTP {exc.code}: {detail[:800]}") from exc
    except (URLError, TimeoutError) as exc:
        raise RuntimeError(f"Discord network error: {exc}") from exc


def clean_text(value: str) -> str:
    text = html.unescape(value or "")
    text = TAG_RE.sub(" ", text)
    return " ".join(text.split())


def normalize_created_at(value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip()
    try:
        dt = parsedate_to_datetime(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    except (TypeError, ValueError, OverflowError):
        pass
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    except ValueError:
        return None


def extract_status_id_and_user(*values: str) -> tuple[str | None, str | None]:
    for value in values:
        match = STATUS_ID_RE.search(value or "")
        if match:
            return match.group(2), match.group(1)
    return None, None


def _find_text(node: ET.Element, local_name: str) -> str:
    for child in list(node):
        if child.tag.rsplit("}", 1)[-1] == local_name:
            return "".join(child.itertext()).strip()
    return ""


def parse_feed(raw: bytes, username: str, source_url: str) -> list[dict[str, Any]]:
    if not raw.strip():
        return []
    sample = raw[:1000].lower()
    if b"rss reader not yet whitelisted" in sample or b"just a moment" in sample:
        return []

    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise RuntimeError(f"Invalid RSS/Atom XML from {source_url}") from exc

    posts: list[dict[str, Any]] = []
    nodes = [n for n in root.iter() if n.tag.rsplit("}", 1)[-1] in {"item", "entry"}]
    for node in nodes:
        title = _find_text(node, "title")
        description = _find_text(node, "description") or _find_text(node, "content") or _find_text(node, "summary")
        guid = _find_text(node, "guid") or _find_text(node, "id")
        link = _find_text(node, "link")
        if not link:
            for child in list(node):
                if child.tag.rsplit("}", 1)[-1] == "link" and child.attrib.get("href"):
                    link = child.attrib["href"]
                    break
        published = (
            _find_text(node, "pubDate")
            or _find_text(node, "published")
            or _find_text(node, "updated")
        )

        post_id, found_user = extract_status_id_and_user(link, guid, description, title)
        if not post_id:
            continue
        if found_user and found_user.lower() != username.lower():
            continue

        title_text = clean_text(title)
        desc_text = clean_text(description)
        text = title_text or desc_text
        # Some feeds use a generic title and put the real post in description.
        if len(desc_text) > len(text) and title_text.lower().startswith(("twitter", "x post", "post by")):
            text = desc_text
        if not text:
            continue

        posts.append(
            {
                "id": post_id,
                "text": text,
                "created_at": normalize_created_at(published),
                "url": f"https://x.com/{username}/status/{post_id}",
                "source": source_url,
            }
        )
    return posts


def fetch_feed_posts(username: str, templates: list[str]) -> tuple[list[dict[str, Any]], list[str]]:
    posts: list[dict[str, Any]] = []
    errors: list[str] = []
    for template in templates:
        url = template.replace("{handle}", username)
        try:
            raw = request_bytes(url)
            parsed = parse_feed(raw, username, url)
            if parsed:
                posts.extend(parsed)
                print(f"Feed OK: {url} ({len(parsed)} posts)")
                # One healthy full timeline feed is enough. Avoid hammering mirrors.
                break
            errors.append(f"{url}: empty/unusable feed")
        except Exception as exc:  # source failure should not kill fallbacks
            errors.append(f"{url}: {exc}")
    return posts, errors


def walk_json(value: Any) -> Iterable[Any]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk_json(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk_json(child)


def strings_in(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        out: list[str] = []
        for child in value.values():
            if isinstance(child, str):
                out.append(child)
        return out
    return []


def parse_radar_json(data: Any, username: str, source_url: str) -> list[dict[str, Any]]:
    """Best-effort schema-agnostic extraction of Tibo X posts from public radar JSON."""
    posts: list[dict[str, Any]] = []
    preferred_text_keys = (
        "text", "content", "title", "summary", "quote", "post_text", "tweet_text", "message", "signal",
    )
    date_keys = ("created_at", "published_at", "date", "time", "timestamp", "posted_at")

    for node in walk_json(data):
        if not isinstance(node, dict):
            continue
        values = strings_in(node)
        post_id, found_user = extract_status_id_and_user(*values)
        if not post_id or (found_user and found_user.lower() != username.lower()):
            continue

        candidates: list[str] = []
        for key in preferred_text_keys:
            value = node.get(key)
            if isinstance(value, str):
                cleaned = clean_text(value)
                if cleaned and "/status/" not in cleaned:
                    candidates.append(cleaned)
        if not candidates:
            for value in values:
                cleaned = clean_text(value)
                if cleaned and "/status/" not in cleaned and len(cleaned) >= 8:
                    candidates.append(cleaned)
        if not candidates:
            continue

        # Prefer a sentence that actually looks reset-related; otherwise the longest useful text.
        candidates = list(dict.fromkeys(candidates))
        candidates.sort(key=lambda s: (bool(RESET_RE.search(s)), len(s)), reverse=True)
        text = candidates[0]

        created_at = None
        for key in date_keys:
            value = node.get(key)
            if isinstance(value, str):
                created_at = normalize_created_at(value)
                if created_at:
                    break

        posts.append(
            {
                "id": post_id,
                "text": text,
                "created_at": created_at,
                "url": f"https://x.com/{username}/status/{post_id}",
                "source": source_url,
            }
        )
    return posts


def fetch_radar_posts(username: str, urls: list[str]) -> tuple[list[dict[str, Any]], list[str]]:
    posts: list[dict[str, Any]] = []
    errors: list[str] = []
    for url in urls:
        try:
            raw = request_bytes(url)
            data = json.loads(raw.decode("utf-8"))
            parsed = parse_radar_json(data, username, url)
            if parsed:
                posts.extend(parsed)
                print(f"Radar OK: {url} ({len(parsed)} matching records)")
                break
            errors.append(f"{url}: JSON had no matching @{username} posts")
        except Exception as exc:
            errors.append(f"{url}: {exc}")
    return posts, errors


def merge_posts(*groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for group in groups:
        for post in group:
            post_id = str(post.get("id", ""))
            if not post_id.isdigit():
                continue
            existing = merged.get(post_id)
            if not existing:
                merged[post_id] = post
                continue
            # Prefer the record with more text and a known timestamp.
            if len(str(post.get("text", ""))) > len(str(existing.get("text", ""))):
                existing["text"] = post.get("text")
            if not existing.get("created_at") and post.get("created_at"):
                existing["created_at"] = post.get("created_at")
            if str(existing.get("source", "")).startswith("https://codex") and post.get("source"):
                existing["source"] = post.get("source")
    return sorted(merged.values(), key=lambda item: int(item["id"]))


def load_state(path: Path, username: str) -> dict[str, Any]:
    if not path.exists():
        return {"username": username, "last_seen_id": None}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        state = {}
    if state.get("username") != username:
        return {"username": username, "last_seen_id": None}
    return {"username": username, "last_seen_id": state.get("last_seen_id"), **{k: v for k, v in state.items() if k not in {"user_id"}}}


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    state.pop("user_id", None)
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def discord_allowed_mentions(mention: str) -> dict[str, Any]:
    mention = mention.strip()
    if mention in {"@everyone", "@here"}:
        return {"parse": ["everyone"]}
    match = re.fullmatch(r"<@!?(\d+)>", mention)
    if match:
        return {"parse": [], "users": [match.group(1)]}
    match = re.fullmatch(r"<@&(\d+)>", mention)
    if match:
        return {"parse": [], "roles": [match.group(1)]}
    return {"parse": []}


def kst_time(created_at: str | None) -> str:
    if not created_at:
        return "알 수 없음"
    try:
        dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone(timedelta(hours=9))).strftime("%Y-%m-%d %H:%M:%S KST")
    except ValueError:
        return created_at


def send_discord(
    webhook_url: str,
    *,
    title: str,
    description: str,
    url: str | None = None,
    fields: list[dict[str, Any]] | None = None,
    mention: str = "",
    ping: bool = True,
) -> None:
    content = mention.strip() if (ping and mention.strip()) else ""
    embed: dict[str, Any] = {
        "title": title[:256],
        "description": description[:4000],
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer": {"text": "Tibo Reset Monitor · free sources"},
    }
    if url:
        embed["url"] = url
    if fields:
        embed["fields"] = fields[:25]
    body: dict[str, Any] = {
        "content": content,
        "embeds": [embed],
        "allowed_mentions": discord_allowed_mentions(content) if content else {"parse": []},
        "username": "Tibo Reset Monitor",
    }
    separator = "&" if "?" in webhook_url else "?"
    request_json_post(f"{webhook_url}{separator}wait=true", body)


def should_alert(kind: str, alert_ambiguous: bool, alert_completed: bool) -> bool:
    return (
        kind == "PREANNOUNCEMENT"
        or (kind == "AMBIGUOUS" and alert_ambiguous)
        or (kind == "COMPLETED" and alert_completed)
    )


def alert_title(kind: str) -> str:
    return {
        "PREANNOUNCEMENT": "🚨 GPT/Codex 리셋 예고 감지",
        "AMBIGUOUS": "⚠️ 리셋 관련 게시물 감지",
        "COMPLETED": "✅ GPT/Codex 리셋 완료 감지",
    }.get(kind, "X 게시물 감지")


def main() -> int:
    webhook = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
    username = os.getenv("TARGET_USERNAME", DEFAULT_USERNAME).strip().lstrip("@")
    mention = os.getenv("DISCORD_MENTION", "").strip()
    state_path = Path(os.getenv("STATE_FILE", DEFAULT_STATE_FILE))
    alert_ambiguous = env_bool("ALERT_AMBIGUOUS", True)
    alert_completed = env_bool("ALERT_COMPLETED", False)
    feed_urls = csv_env("FREE_FEED_URLS", DEFAULT_FEED_URLS)
    radar_urls = csv_env("RADAR_URLS", DEFAULT_RADAR_URLS)

    if not webhook:
        print("ERROR: DISCORD_WEBHOOK_URL is required", file=sys.stderr)
        return 2

    state = load_state(state_path, username)
    last_seen_id = state.get("last_seen_id")

    feed_posts, feed_errors = fetch_feed_posts(username, feed_urls)
    radar_posts, radar_errors = fetch_radar_posts(username, radar_urls)
    posts = merge_posts(feed_posts, radar_posts)

    if feed_errors:
        for error in feed_errors:
            print(f"Feed warning: {error}")
    if radar_errors:
        for error in radar_errors:
            print(f"Radar warning: {error}")

    if not posts:
        print("No usable public source returned posts. Will retry on next run.")
        return 0

    # First run establishes a baseline and deliberately avoids old alerts.
    if not last_seen_id:
        newest = posts[-1]
        state["last_seen_id"] = str(newest["id"])
        state["initialized_at"] = datetime.now(timezone.utc).isoformat()
        save_state(state_path, state)
        send_discord(
            webhook,
            title="✅ 무료 Tibo 리셋 감시 시작",
            description=(
                f"@{username}의 공개 피드/레이더를 이용해 새 게시물을 감시합니다.\n"
                "X 공식 API는 사용하지 않으므로 API 요금은 없습니다. 기존 과거 글은 알림하지 않습니다."
            ),
            url=newest.get("url"),
            fields=[
                {"name": "예고 알림", "value": "켜짐", "inline": True},
                {"name": "애매한 관련 글", "value": "켜짐" if alert_ambiguous else "꺼짐", "inline": True},
                {"name": "완료 알림", "value": "켜짐" if alert_completed else "꺼짐", "inline": True},
            ],
            mention=mention,
            ping=False,
        )
        print(f"Initialized at post {newest['id']}")
        return 0

    new_posts = [p for p in posts if int(str(p["id"])) > int(str(last_seen_id))]
    if not new_posts:
        print("No new posts.")
        return 0

    newest_processed = str(last_seen_id)
    for post in new_posts:
        post_id = str(post["id"])
        text = str(post.get("text", ""))
        classification = classify_post(text)
        print(f"{post_id}: {classification.kind} | {text[:140]!r}")

        if should_alert(classification.kind, alert_ambiguous, alert_completed):
            fields = [
                {"name": "판정", "value": classification.kind, "inline": True},
                {"name": "게시 시각", "value": kst_time(post.get("created_at")), "inline": True},
                {"name": "감지 근거", "value": ", ".join(classification.reasons)[:1024] or "규칙 일치", "inline": False},
                {"name": "무료 데이터 소스", "value": str(post.get("source", "공개 소스"))[:1024], "inline": False},
            ]
            send_discord(
                webhook,
                title=alert_title(classification.kind),
                description=text or "(본문 없음)",
                url=post.get("url") or f"https://x.com/{username}/status/{post_id}",
                fields=fields,
                mention=mention,
                ping=True,
            )
        newest_processed = post_id

    state["last_seen_id"] = newest_processed
    save_state(state_path, state)
    print(f"State advanced to {newest_processed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
