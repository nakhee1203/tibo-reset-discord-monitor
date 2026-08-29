#!/usr/bin/env python3
from __future__ import annotations

import html
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

USERNAME = os.getenv("TARGET_USERNAME", "thsottiaux").strip().lstrip("@")
WEBHOOK = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
MENTION = os.getenv("DISCORD_MENTION", "").strip()
STATE_FILE = Path(os.getenv("STATE_FILE", "state.json"))
FEED_URL = os.getenv(
    "FREE_FEED_URL",
    f"https://fxtwitter.com/{USERNAME}/feed.xml"
)
FAIL_THRESHOLD = int(os.getenv("SOURCE_FAILURE_THRESHOLD", "3"))
CLASSIFIER_VERSION = "3.1"

UA = "Mozilla/5.0 TiboResetMonitor/3.1"

STATUS_RE = re.compile(
    r"(?:x\.com|twitter\.com|fxtwitter\.com|fixupx\.com)/([^/?#]+)/status/(\d+)",
    re.I,
)

TAG_RE = re.compile(r"<[^>]+>")

RESET = [
    r"\breset(?:s|ting|ted)?\b",
    r"\brefresh(?:es|ing|ed)?\b",
    r"\breplenish(?:es|ing|ed)?\b",
    r"\brestore(?:s|d|ing)?\b",
]

USAGE = [
    r"\busage limits?\b",
    r"\brate limits?\b",
    r"\bweekly limits?\b",
    r"\bbanked(?: reset| limits?)?\b",
    r"\bquota(?:s)?\b",
]

CONTEXT = [
    r"\bcodex\b",
    r"\bchatgpt work\b",
    r"\bchatgpt\b",
    r"\bgpt(?:[- ]?\d[\w.-]*)?\b",
    r"\bplus\b",
    r"\bpro\b",
    r"\bpaid users?\b",
    r"\bsubscriptions?\b",
]

FUTURE = [
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
    r"\bmight\b",
    r"\bcould\b",
]

MILESTONE = [
    r"\bmilestone\b",
    r"\bactive users?\b",
    r"\bhit (?:a |the )?(?:new )?milestone\b",
]

CELEBRATE = [
    r"\bcelebrat(?:e|es|ed|ing|ion)\b",
    r"\bgift\b",
    r"\bsurprise\b",
    r"\brejoice\b",
    r"\btreat\b",
]

STRONG = [
    r"\breset incoming\b",
    r"\breset will (?:land|arrive|happen)\b",
    r"\bwill reset (?:the )?(?:usage )?limits?\b",
    r"\b(?:usage )?limits? will (?:be )?reset\b",
    r"\bhold on to your codex\b",
    r"\bhold onto your codex\b",
]

COMPLETE = [
    r"\bhas been reset\b",
    r"\bhave been reset\b",
    r"\bwas reset\b",
    r"\breset (?:is )?(?:live|done|complete|completed)\b",
    r"\breset has landed\b",
    r"\breset landed\b",
    r"\breset has been propagated\b",
    r"\bfully reset\b",
    r"\bjust reset\b",
]

NEGATE = [
    r"\bno reset\b",
    r"\bnot (?:a )?reset\b",
    r"\bnot resetting\b",
    r"\bwon't reset\b",
    r"\bwill not reset\b",
    r"\bno plans? to reset\b",
    r"\bdon't expect (?:a )?reset\b",
    r"\bdo not expect (?:a )?reset\b",
]


def has(text: str, patterns: list[str]) -> bool:
    return any(re.search(p, text, re.I | re.S) for p in patterns)


def classify(text: str) -> tuple[str, int, list[str]]:
    text = " ".join((text or "").replace("’", "'").split())

    score = 0
    reasons: list[str] = []

    h_reset = has(text, RESET)
    h_usage = has(text, USAGE)
    h_context = has(text, CONTEXT)
    h_future = has(text, FUTURE)
    h_milestone = has(text, MILESTONE)
    h_celebrate = has(text, CELEBRATE)
    h_strong = has(text, STRONG)
    h_complete = has(text, COMPLETE)
    h_negate = has(text, NEGATE)

    if h_reset:
        score += 7
        reasons.append("reset/refresh 표현 +7")

    if h_usage:
        score += 5
        reasons.append("usage/rate limit 문맥 +5")

    if h_context:
        score += 2
        reasons.append("Codex/ChatGPT 문맥 +2")

    if h_milestone:
        score += 3
        reasons.append("milestone 신호 +3")

    if h_celebrate:
        score += 3
        reasons.append("celebrate/선물 암시 +3")

    if h_future:
        score += 2
        reasons.append("미래 시점 표현 +2")

    if h_strong:
        score += 6
        reasons.append("강한 리셋 암시 +6")

    if h_complete:
        score -= 8
        reasons.append("이미 리셋 완료 -8")

    if h_negate:
        score -= 10
        reasons.append("리셋 부정 -10")

    if h_complete and not h_strong:
        return "COMPLETED", score, reasons

    if h_negate:
        return "NORMAL", score, reasons

    # reset이라는 단어가 없어도
    # Codex/usage 관련 글이면 암시 점수로 판단
    if not h_reset and not (h_context or h_usage):
        return "NORMAL", score, reasons

    if score >= 8:
        return "HIGH", score, reasons

    if score >= 5:
        return "MEDIUM", score, reasons

    return "NORMAL", score, reasons


def http_get(url: str, attempts: int = 2) -> bytes:
    req = Request(
        url,
        headers={
            "User-Agent": UA,
            "Accept": (
                "application/rss+xml, "
                "application/atom+xml, "
                "text/xml, */*"
            ),
            "Cache-Control": "no-cache",
        },
    )

    last = None

    for n in range(attempts):
        try:
            with urlopen(req, timeout=25) as r:
                return r.read()

        except (HTTPError, URLError, TimeoutError) as e:
            last = e

            if n + 1 < attempts:
                time.sleep(2)

    raise RuntimeError(f"피드 요청 실패: {last}")


def clean(value: str) -> str:
    value = html.unescape(value or "")
    value = TAG_RE.sub(" ", value)
    return " ".join(value.split())


def child_text(node: ET.Element, name: str) -> str:
    for c in list(node):
        if c.tag.rsplit("}", 1)[-1] == name:
            return "".join(c.itertext()).strip()

    return ""


def normalize_time(value: str) -> str | None:
    if not value:
        return None

    try:
        dt = parsedate_to_datetime(value)

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)

        return dt.astimezone(timezone.utc).isoformat()

    except Exception:
        pass

    try:
        dt = datetime.fromisoformat(
            value.replace("Z", "+00:00")
        )

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)

        return dt.astimezone(timezone.utc).isoformat()

    except Exception:
        return None


def fetch_posts() -> list[dict]:
    raw = http_get(FEED_URL)

    root = ET.fromstring(raw)

    posts = []

    for node in root.iter():
        if node.tag.rsplit("}", 1)[-1] not in {
            "item",
            "entry",
        }:
            continue

        title = child_text(node, "title")

        desc = (
            child_text(node, "description")
            or child_text(node, "content")
            or child_text(node, "summary")
        )

        guid = (
            child_text(node, "guid")
            or child_text(node, "id")
        )

        link = child_text(node, "link")

        if not link:
            for c in list(node):
                if (
                    c.tag.rsplit("}", 1)[-1] == "link"
                    and c.attrib.get("href")
                ):
                    link = c.attrib["href"]
                    break

        joined = " ".join(
            [
                link,
                guid,
                desc,
                title,
            ]
        )

        m = STATUS_RE.search(joined)

        if not m:
            continue

        found_user = m.group(1)
        post_id = m.group(2)

        if found_user.lower() != USERNAME.lower():
            continue

        title_text = clean(title)
        desc_text = clean(desc)

        if len(desc_text) > len(title_text):
            text = desc_text
        else:
            text = title_text

        if not text:
            continue

        published = (
            child_text(node, "pubDate")
            or child_text(node, "published")
            or child_text(node, "updated")
        )

        posts.append(
            {
                "id": post_id,
                "text": text,
                "created_at": normalize_time(published),
                "url": (
                    f"https://x.com/"
                    f"{USERNAME}/status/{post_id}"
                ),
            }
        )

    unique = {
        p["id"]: p
        for p in posts
    }

    return sorted(
        unique.values(),
        key=lambda p: int(p["id"]),
    )


def load_state() -> dict:
    base = {
        "username": USERNAME,
        "last_seen_id": None,
        "classifier_version": None,
        "feed_failure_count": 0,
        "feed_failure_alerted": False,
    }

    if not STATE_FILE.exists():
        return base

    try:
        data = json.loads(
            STATE_FILE.read_text(
                encoding="utf-8"
            )
        )

        if data.get("username") != USERNAME:
            return base

        return {
            **base,
            **data,
        }

    except Exception:
        return base


def save_state(state: dict) -> None:
    state["updated_at"] = (
        datetime
        .now(timezone.utc)
        .isoformat()
    )

    STATE_FILE.write_text(
        json.dumps(
            state,
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def allowed_mentions(
    mention: str
) -> dict:

    if mention in {
        "@everyone",
        "@here",
    }:
        return {
            "parse": ["everyone"]
        }

    m = re.fullmatch(
        r"<@!?(\d+)>",
        mention,
    )

    if m:
        return {
            "parse": [],
            "users": [m.group(1)],
        }

    m = re.fullmatch(
        r"<@&(\d+)>",
        mention,
    )

    if m:
        return {
            "parse": [],
            "roles": [m.group(1)],
        }

    return {
        "parse": []
    }


def discord(
    title: str,
    description: str,
    *,
    url: str = "",
    fields=None,
    ping=False,
):

    if ping and MENTION:
        content = MENTION
    else:
        content = ""

    body = {
        "username": "Tibo Monitor",

        "content": content,

        "allowed_mentions": (
            allowed_mentions(content)
            if content
            else {"parse": []}
        ),

        "embeds": [
            {
                "title": title[:256],

                "description": (
                    description[:4000]
                ),

                "url": (
                    url
                    if url
                    else None
                ),

                "fields": (
                    fields or []
                )[:25],

                "footer": {
                    "text": (
                        "Tibo Monitor v3 "
                        "· free / no X API"
                    )
                },

                "timestamp": (
                    datetime
                    .now(timezone.utc)
                    .isoformat()
                ),
            }
        ],
    }

    body["embeds"][0] = {
        k: v
        for k, v
        in body["embeds"][0].items()
        if v is not None
    }

    payload = json.dumps(
        body
    ).encode()

    separator = (
        "&"
        if "?" in WEBHOOK
        else "?"
    )

    req = Request(
        WEBHOOK
        + separator
        + "wait=true",

        data=payload,

        headers={
            "Content-Type": (
                "application/json"
            ),
            "User-Agent": UA,
        },

        method="POST",
    )

    with urlopen(
        req,
        timeout=25,
    ) as r:
        r.read()


def kst(
    value: str | None
) -> str:

    if not value:
        return "알 수 없음"

    try:
        dt = datetime.fromisoformat(
            value.replace(
                "Z",
                "+00:00",
            )
        )

        return (
            dt.astimezone(
                timezone(
                    timedelta(hours=9)
                )
            )
            .strftime(
                "%Y-%m-%d "
                "%H:%M:%S KST"
            )
        )

    except Exception:
        return value


def title_for(
    risk: str
) -> str:

    return {
        "HIGH": (
            "🚨 HIGH · GPT/Codex "
            "리셋 가능성 높음"
        ),

        "MEDIUM": (
            "🟠 MEDIUM · GPT/Codex "
            "리셋 암시 감지"
        ),

        "COMPLETED": (
            "✅ GPT/Codex "
            "리셋 완료 관련 글"
        ),

        "NORMAL": (
            "📝 Tibo 새 게시물"
        ),
    }[risk]


def send_post(
    post: dict,
    prefix: str = "",
):

    risk, score, reasons = classify(
        post["text"]
    )

    fields = [
        {
            "name": "위험도",
            "value": risk,
            "inline": True,
        },

        {
            "name": "점수",
            "value": str(score),
            "inline": True,
        },

        {
            "name": "게시 시각",
            "value": kst(
                post.get("created_at")
            ),
            "inline": True,
        },

        {
            "name": "판단 근거",

            "value": (
                "\n".join(
                    f"• {x}"
                    for x in reasons
                )[:1024]

                or
                "특별한 리셋 신호 없음"
            ),

            "inline": False,
        },
    ]

    discord(
        prefix + title_for(risk),

        post["text"],

        url=post["url"],

        fields=fields,

        ping=(
            risk in {
                "HIGH",
                "MEDIUM",
            }
        ),
    )

    return risk, score


def main() -> int:

    if not WEBHOOK:
        print(
            "ERROR: "
            "DISCORD_WEBHOOK_URL "
            "secret이 없습니다.",
            file=sys.stderr,
        )

        return 2

    state = load_state()

    # -------------------------
    # 1. FxTwitter RSS 확인
    # -------------------------

    try:
        posts = fetch_posts()

        if not posts:
            raise RuntimeError(
                "RSS에 사용 가능한 "
                "게시물이 없음"
            )

        if state.get(
            "feed_failure_alerted"
        ):
            discord(
                "✅ Tibo Monitor · RSS 복구",

                "FxTwitter RSS를 다시 "
                "정상적으로 읽고 있습니다.",
            )

        state["feed_failure_count"] = 0

        state[
            "feed_failure_alerted"
        ] = False

    except Exception as e:

        failures = (
            int(
                state.get(
                    "feed_failure_count",
                    0,
                )
            )
            + 1
        )

        state[
            "feed_failure_count"
        ] = failures

        if (
            failures >= FAIL_THRESHOLD
            and not state.get(
                "feed_failure_alerted"
            )
        ):

            discord(
                "⚠️ Tibo Monitor · RSS 장애",

                (
                    f"FxTwitter RSS를 "
                    f"{failures}회 연속 "
                    f"읽지 못했습니다.\n\n"
                    f"오류: {e}"
                ),

                ping=True,
            )

            state[
                "feed_failure_alerted"
            ] = True

        save_state(state)

        print(
            f"RSS 실패 "
            f"{failures}회: {e}"
        )

        return 0

    last_seen = state.get(
        "last_seen_id"
    )

    # -------------------------
    # 2. 최초 실행
    # -------------------------

    if not last_seen:

        state["last_seen_id"] = (
            posts[-1]["id"]
        )

        state[
            "classifier_version"
        ] = CLASSIFIER_VERSION

        save_state(state)

        discord(
            "✅ Tibo Monitor v3 시작",

            (
                "티보 새 게시물을 전부 "
                "Discord에 기록하고, "
                "HIGH/MEDIUM 리셋 신호는 "
                "강하게 알립니다."
            ),
        )

        return 0

    # -------------------------
    # 3. 기존 버전 -> v3 교체
    # 최근 48시간 재분석
    # -------------------------

    if (
        state.get(
            "classifier_version"
        )
        != CLASSIFIER_VERSION
    ):

        cutoff = (
            datetime.now(timezone.utc)
            - timedelta(hours=48)
        )

        retro = set(
            str(x)
            for x
            in state.get(
                "retro_alerted_ids",
                [],
            )
        )

        for post in posts:

            if (
                int(post["id"])
                > int(last_seen)
            ):
                continue

            if post["id"] in retro:
                continue

            if not post.get(
                "created_at"
            ):
                continue

            try:
                dt = (
                    datetime
                    .fromisoformat(
                        post[
                            "created_at"
                        ].replace(
                            "Z",
                            "+00:00",
                        )
                    )
                )

            except Exception:
                continue

            if dt < cutoff:
                continue

            risk, _, _ = classify(
                post["text"]
            )

            if risk in {
                "HIGH",
                "MEDIUM",
            }:

                send_post(
                    post,
                    "🔁 v3 재분석 · ",
                )

                retro.add(
                    post["id"]
                )

        state[
            "retro_alerted_ids"
        ] = sorted(
            retro,
            key=int,
        )[-30:]

        state[
            "classifier_version"
        ] = CLASSIFIER_VERSION

    # -------------------------
    # 4. 새 글 찾기
    # -------------------------

    new_posts = [
        p
        for p in posts
        if int(p["id"])
        > int(last_seen)
    ]

    if not new_posts:

        save_state(state)

        print(
            "새 게시물 없음"
        )

        return 0

    # -------------------------
    # 5. 모든 티보 새 글
    # Discord 기록
    # -------------------------

    for post in new_posts:

        risk, score = send_post(
            post
        )

        print(
            f"{post['id']} "
            f"-> {risk} "
            f"({score})"
        )

    state["last_seen_id"] = (
        new_posts[-1]["id"]
    )

    state[
        "classifier_version"
    ] = CLASSIFIER_VERSION

    save_state(state)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
