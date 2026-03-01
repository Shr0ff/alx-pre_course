#!/usr/bin/env python3
"""Guardian RSS to Instagram content pipeline prototype.

This script polls The Guardian RSS feeds, scores new stories for significance,
creates a "BREAKING"-style caption, and optionally posts to Instagram via the
Graph API.

Environment variables:
- OPENAI_API_KEY: required when USE_AI_SCORING=true
- INSTAGRAM_ACCESS_TOKEN: required when ENABLE_POSTING=true
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import textwrap
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, List, Optional, Tuple

try:
    import feedparser
except Exception:
    feedparser = None

try:
    import requests
except Exception:
    requests = None

try:
    from openai import OpenAI
except Exception:  # openai is optional unless AI scoring enabled
    OpenAI = None

DEFAULT_FEEDS = [
    "https://www.theguardian.com/world/rss",
    "https://www.theguardian.com/uk/rss",
    "https://www.theguardian.com/international/rss",
]

KEYWORDS = {
    "strike": 2,
    "attack": 2,
    "killed": 3,
    "explosion": 2,
    "missile": 2,
    "president": 1,
    "invasion": 3,
    "emergency": 2,
    "sanctions": 2,
    "war": 3,
    "ceasefire": 2,
}


@dataclass
class BotConfig:
    db_path: str = "guardian_bot.db"
    poll_interval_seconds: int = 45
    keyword_threshold: int = 4
    ai_score_threshold: int = 8
    use_ai_scoring: bool = False
    enable_posting: bool = False
    dry_run: bool = True
    feeds: Tuple[str, ...] = tuple(DEFAULT_FEEDS)


class StoryStore:
    def __init__(self, db_path: str) -> None:
        self.conn = sqlite3.connect(db_path)
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS seen_stories (
                article_id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                url TEXT NOT NULL,
                published_at TEXT,
                processed_at TEXT NOT NULL
            )
            """
        )
        self.conn.commit()

    def seen(self, article_id: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM seen_stories WHERE article_id = ?", (article_id,)
        ).fetchone()
        return row is not None

    def mark_seen(self, article_id: str, title: str, url: str, published_at: str) -> None:
        self.conn.execute(
            """
            INSERT OR IGNORE INTO seen_stories(article_id, title, url, published_at, processed_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (article_id, title, url, published_at, datetime.now(timezone.utc).isoformat()),
        )
        self.conn.commit()


def extract_article_id(entry: dict) -> str:
    return entry.get("id") or entry.get("link") or entry.get("title", "")


def keyword_score(text: str) -> int:
    lowered = text.lower()
    return sum(weight for word, weight in KEYWORDS.items() if word in lowered)


def ai_score(openai_client: OpenAI, title: str, summary: str) -> int:
    prompt = textwrap.dedent(
        f"""
        You are a global news significance rater.
        Rate this story from 1 to 10 based on likely global impact.
        Return only a single integer.

        Headline: {title}
        Summary: {summary[:1200]}
        """
    ).strip()

    response = openai_client.responses.create(
        model="gpt-4o-mini",
        input=prompt,
        temperature=0,
    )
    raw = response.output_text.strip()

    try:
        score = int("".join(ch for ch in raw if ch.isdigit())[:2])
    except ValueError as exc:
        raise RuntimeError(f"Could not parse AI score from response: {raw!r}") from exc

    return max(1, min(score, 10))


def classify_tone(text: str) -> str:
    lowered = text.lower()
    if any(k in lowered for k in ("war", "missile", "invasion", "strike", "attack")):
        return "BREAKING NEWS"
    if any(k in lowered for k in ("scandal", "resign", "corruption", "leak")):
        return "VIRAL"
    if any(k in lowered for k in ("image", "video", "footage", "pictured")):
        return "FIRST IMAGES EMERGE"
    return "BREAKING"


def build_caption(title: str, summary: str, url: str) -> str:
    tone = classify_tone(f"{title} {summary}")
    context = (summary or "Developing story.").replace("\n", " ").strip()
    context_short = context[:220] + ("..." if len(context) > 220 else "")

    hashtags = "#Breaking #WorldNews #Guardian #Geopolitics #NewsUpdate"
    return textwrap.dedent(
        f"""
        🚨 {tone}
        {title}

        {context_short}

        Source: The Guardian
        {url}

        {hashtags}
        """
    ).strip()


def get_entries(feeds: Iterable[str]) -> List[dict]:
    if not feedparser:
        raise RuntimeError("feedparser package not installed. Install with `pip install feedparser`.")

    entries: List[dict] = []
    for feed_url in feeds:
        parsed = feedparser.parse(feed_url)
        if parsed.bozo:
            logging.warning("Feed parse warning for %s: %s", feed_url, parsed.bozo_exception)
        entries.extend(parsed.entries)
    return entries


def post_to_instagram(*, access_token: str, ig_user_id: str, image_url: str, caption: str) -> str:
    if not requests:
        raise RuntimeError("requests package not installed. Install with `pip install requests`.")
    create_url = f"https://graph.facebook.com/v21.0/{ig_user_id}/media"
    publish_url = f"https://graph.facebook.com/v21.0/{ig_user_id}/media_publish"

    create_resp = requests.post(
        create_url,
        data={"image_url": image_url, "caption": caption, "access_token": access_token},
        timeout=20,
    )
    create_resp.raise_for_status()
    creation_id = create_resp.json()["id"]

    publish_resp = requests.post(
        publish_url,
        data={"creation_id": creation_id, "access_token": access_token},
        timeout=20,
    )
    publish_resp.raise_for_status()
    return publish_resp.json().get("id", "")


def should_process(
    *,
    title: str,
    summary: str,
    config: BotConfig,
    openai_client: Optional[OpenAI],
) -> Tuple[bool, int, Optional[int]]:
    combined = f"{title}\n{summary}"
    kw_score = keyword_score(combined)
    if kw_score < config.keyword_threshold:
        return False, kw_score, None

    if config.use_ai_scoring:
        if not openai_client:
            raise RuntimeError("AI scoring enabled but OpenAI client unavailable.")
        rank = ai_score(openai_client, title=title, summary=summary)
        return rank >= config.ai_score_threshold, kw_score, rank

    return True, kw_score, None


def run_loop(config: BotConfig) -> None:
    store = StoryStore(config.db_path)

    openai_client = None
    if config.use_ai_scoring:
        if not OpenAI:
            raise RuntimeError("openai package not installed. Install with `pip install openai`.")
        openai_client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

    logging.info("Starting Guardian bot. Polling every %ss", config.poll_interval_seconds)

    while True:
        try:
            entries = get_entries(config.feeds)
            entries.sort(key=lambda e: e.get("published_parsed") or time.gmtime(0))

            for entry in entries:
                article_id = extract_article_id(entry)
                if not article_id or store.seen(article_id):
                    continue

                title = entry.get("title", "Untitled")
                url = entry.get("link", "")
                summary = entry.get("summary", "")
                published = entry.get("published", "")

                is_major, kw, rank = should_process(
                    title=title, summary=summary, config=config, openai_client=openai_client
                )

                if not is_major:
                    logging.info("Skipped: %s (kw=%s ai=%s)", title, kw, rank)
                    store.mark_seen(article_id, title, url, published)
                    continue

                caption = build_caption(title=title, summary=summary, url=url)
                logging.info("MAJOR: %s (kw=%s ai=%s)", title, kw, rank)
                logging.info("Caption preview:\n%s", caption)

                if config.enable_posting and not config.dry_run:
                    token = os.getenv("INSTAGRAM_ACCESS_TOKEN")
                    ig_user_id = os.getenv("INSTAGRAM_IG_USER_ID")
                    image_url = os.getenv("INSTAGRAM_IMAGE_URL")

                    if not token or not ig_user_id or not image_url:
                        raise RuntimeError(
                            "Posting enabled but INSTAGRAM_ACCESS_TOKEN, INSTAGRAM_IG_USER_ID, "
                            "or INSTAGRAM_IMAGE_URL missing."
                        )

                    media_id = post_to_instagram(
                        access_token=token,
                        ig_user_id=ig_user_id,
                        image_url=image_url,
                        caption=caption,
                    )
                    logging.info("Published to Instagram, media id=%s", media_id)

                store.mark_seen(article_id, title, url, published)

        except Exception:
            logging.exception("Loop iteration failed")

        time.sleep(config.poll_interval_seconds)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Guardian breaking news bot prototype")
    parser.add_argument("--db-path", default="guardian_bot.db")
    parser.add_argument("--poll-interval", type=int, default=45)
    parser.add_argument("--keyword-threshold", type=int, default=4)
    parser.add_argument("--use-ai-scoring", action="store_true")
    parser.add_argument("--ai-score-threshold", type=int, default=8)
    parser.add_argument("--enable-posting", action="store_true")
    parser.add_argument("--live", action="store_true", help="Actually publish if posting is enabled")
    parser.add_argument(
        "--feeds-json",
        help="JSON list of feed URLs. If omitted, Guardian defaults are used.",
    )
    return parser.parse_args()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    args = parse_args()

    feeds = tuple(DEFAULT_FEEDS)
    if args.feeds_json:
        feeds = tuple(json.loads(args.feeds_json))

    config = BotConfig(
        db_path=args.db_path,
        poll_interval_seconds=args.poll_interval,
        keyword_threshold=args.keyword_threshold,
        ai_score_threshold=args.ai_score_threshold,
        use_ai_scoring=args.use_ai_scoring,
        enable_posting=args.enable_posting,
        dry_run=not args.live,
        feeds=feeds,
    )

    run_loop(config)


if __name__ == "__main__":
    main()
