"""
Stage 1 — Content Discovery

Pulls stories from three sources:
  - NewsAPI   (keyword search across configured domains)
  - Reddit    (hot posts from configured subreddits, filtered by topic keywords)
  - ArXiv     (recent papers in configured categories)

Deduplication:
    Uses SeenURLStore to skip URLs already covered in past episodes. URLs are
    marked as seen only AFTER the output file is successfully written, so a
    failed run does not poison the dedup store.

Idempotency:
    If data/raw/YYYY-MM-DD.json already exists, returns the cached result
    without hitting any APIs — unless --force is passed.

Output schema (list of dicts, saved to data/raw/YYYY-MM-DD.json):
    [
        {
            "title": str,
            "url": str,
            "summary": str,
            "source": "newsapi" | "reddit" | "arxiv",
            "date": "YYYY-MM-DD",
            "raw_score": float   # 0.0–1.0 recency + source heuristic
        },
        ...
    ]

CLI usage:
    python modules/content_discovery.py --date 2026-06-02
    python modules/content_discovery.py --date 2026-06-02 --force
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, List

import click

from config import PodcastConfig, get_config
from modules.utils.retry import retry_with_backoff
from modules.utils.seen_urls import SeenURLStore

logger = logging.getLogger(__name__)

RawItem = dict[str, Any]


def fetch_from_newsapi(cfg: PodcastConfig) -> List[RawItem]:
    """
    Fetch recent articles from NewsAPI matching cfg.topic keywords.

    Searches cfg.news_sources domains. If NEWSAPI_KEY is empty, logs a
    warning and returns an empty list rather than raising.
    """
    if not cfg.newsapi_key:
        logger.warning("NEWSAPI_KEY not set — skipping NewsAPI source.")
        return []

    from newsapi import NewsApiClient
    from newsapi.newsapi_exception import NewsAPIException

    client = NewsApiClient(api_key=cfg.newsapi_key)

    try:
        response = retry_with_backoff(
            lambda: client.get_everything(
                q=cfg.topic,
                domains=",".join(cfg.news_sources),
                language="en",
                sort_by="publishedAt",
                page_size=cfg.newsapi_page_size,
            ),
            max_attempts=cfg.retry_max_attempts,
            base_delay=cfg.retry_base_delay,
            retriable_exceptions=(NewsAPIException, Exception),
        )
    except Exception as exc:
        logger.error("NewsAPI fetch failed: %s", exc)
        return []

    items: List[RawItem] = []
    for article in response.get("articles", []):
        title = (article.get("title") or "").strip()
        url = (article.get("url") or "").strip()
        if not title or not url or title == "[Removed]":
            continue
        published = article.get("publishedAt") or ""
        items.append(
            {
                "title": title,
                "url": url,
                "summary": (article.get("description") or article.get("content") or "").strip(),
                "source": "newsapi",
                "date": published[:10] if published else date.today().isoformat(),
                "raw_score": 0.0,
            }
        )

    logger.info("NewsAPI: fetched %d articles.", len(items))
    return items


def fetch_from_reddit(cfg: PodcastConfig) -> List[RawItem]:
    """
    Fetch hot posts from cfg.reddit_subreddits via PRAW.

    Filters posts whose title or selftext contains any word from cfg.topic.
    If Reddit credentials are not set, logs a warning and returns [].

    Note: Create a Reddit script app at https://www.reddit.com/prefs/apps
    and set REDDIT_CLIENT_ID, REDDIT_CLIENT_SECRET, REDDIT_USER_AGENT in .env.
    """
    if not cfg.reddit_client_id or not cfg.reddit_client_secret:
        logger.warning("Reddit credentials not set — skipping Reddit source.")
        return []

    import praw

    reddit = praw.Reddit(
        client_id=cfg.reddit_client_id,
        client_secret=cfg.reddit_client_secret,
        user_agent=cfg.reddit_user_agent,
    )
    topic_keywords = [kw.lower() for kw in cfg.topic.split()]

    items: List[RawItem] = []
    for sub_name in cfg.reddit_subreddits:
        try:
            posts = retry_with_backoff(
                lambda s=sub_name: list(reddit.subreddit(s).hot(limit=cfg.reddit_post_limit)),
                max_attempts=cfg.retry_max_attempts,
                base_delay=cfg.retry_base_delay,
            )
        except Exception as exc:
            logger.warning("Reddit r/%s fetch failed: %s", sub_name, exc)
            continue

        for post in posts:
            text = (post.title + " " + (post.selftext or "")).lower()
            if not any(kw in text for kw in topic_keywords):
                continue
            post_date = date.fromtimestamp(post.created_utc).isoformat()
            summary = (post.selftext or post.title)[:600].strip()
            items.append(
                {
                    "title": post.title,
                    "url": f"https://reddit.com{post.permalink}",
                    "summary": summary,
                    "source": "reddit",
                    "date": post_date,
                    "raw_score": 0.0,
                }
            )

    logger.info("Reddit: fetched %d posts.", len(items))
    return items


def fetch_from_arxiv(cfg: PodcastConfig) -> List[RawItem]:
    """
    Fetch recent papers from ArXiv in cfg.arxiv_categories.

    Uses the arxiv Python package. Results sorted by submitted date descending.
    """
    import arxiv

    category_query = " OR ".join(f"cat:{c}" for c in cfg.arxiv_categories)
    client = arxiv.Client()
    search = arxiv.Search(
        query=category_query,
        max_results=cfg.arxiv_max_results,
        sort_by=arxiv.SortCriterion.SubmittedDate,
        sort_order=arxiv.SortOrder.Descending,
    )

    try:
        results = retry_with_backoff(
            lambda: list(client.results(search)),
            max_attempts=cfg.retry_max_attempts,
            base_delay=cfg.retry_base_delay,
        )
    except Exception as exc:
        logger.error("ArXiv fetch failed: %s", exc)
        return []

    items: List[RawItem] = []
    for paper in results:
        items.append(
            {
                "title": paper.title.strip(),
                "url": paper.entry_id,
                "summary": paper.summary.replace("\n", " ").strip()[:600],
                "source": "arxiv",
                "date": paper.published.date().isoformat(),
                "raw_score": 0.0,
            }
        )

    logger.info("ArXiv: fetched %d papers.", len(items))
    return items


def score_item(item: RawItem) -> float:
    """
    Assign a 0.0–1.0 raw_score for pre-sorting before curation.

    Heuristic:
      - Recency: 1.0 for today, decays linearly to 0.0 at 7 days old.
      - ArXiv bonus: +0.15 (papers stay relevant longer than news).
      - Result clamped to [0.0, 1.0].

    Deep quality scoring happens in Stage 2 via Claude.
    """
    try:
        item_date = date.fromisoformat(item.get("date", ""))
        days_old = max(0, (date.today() - item_date).days)
        recency = max(0.0, 1.0 - (days_old / 7.0))
    except (ValueError, TypeError):
        recency = 0.5

    bonus = 0.15 if item.get("source") == "arxiv" else 0.0
    return min(1.0, recency + bonus)


def run_discovery(
    run_date: date,
    cfg: PodcastConfig,
    force: bool = False,
) -> List[RawItem]:
    """
    Main entry point for Stage 1.

    Fetches from all three sources, deduplicates against SeenURLStore, scores,
    sorts, and writes to data/raw/YYYY-MM-DD.json.

    Idempotency: returns cached file if it exists and force is False.
    Resilience: individual source failures are logged and skipped; the stage
                succeeds as long as at least one source returns items.
    """
    output_path = Path(cfg.data_dir) / "raw" / f"{run_date}.json"

    if output_path.exists() and not force:
        logger.info("Stage 1: output exists at %s, skipping.", output_path)
        return json.loads(output_path.read_text(encoding="utf-8"))

    store = SeenURLStore(cfg.seen_urls_file, cfg.seen_url_max_age_days)
    store.load()

    all_items: List[RawItem] = []
    for fetch_fn in (fetch_from_newsapi, fetch_from_reddit, fetch_from_arxiv):
        try:
            all_items.extend(fetch_fn(cfg))
        except Exception as exc:
            logger.error("Source %s raised unexpectedly: %s", fetch_fn.__name__, exc)

    # Remove items with duplicate URLs (keep first occurrence)
    seen_in_batch: set[str] = set()
    deduped: List[RawItem] = []
    for item in all_items:
        if item["url"] not in seen_in_batch:
            seen_in_batch.add(item["url"])
            deduped.append(item)

    # Filter URLs already seen in past episodes
    fresh = [item for item in deduped if not store.is_seen(item["url"])]
    logger.info(
        "Stage 1: %d total → %d after batch-dedup → %d after cross-episode dedup.",
        len(all_items),
        len(deduped),
        len(fresh),
    )

    # Score and sort
    for item in fresh:
        item["raw_score"] = score_item(item)
    fresh.sort(key=lambda x: x["raw_score"], reverse=True)

    # Write output
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(fresh, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.info("Stage 1: wrote %d items to %s.", len(fresh), output_path)

    # NOTE: URLs are NOT marked seen here. mark_urls_seen() is called by
    # pipeline_runner only after a fully successful episode is produced,
    # so failed/dry-run runs don't permanently consume URLs.

    return fresh


def mark_urls_seen(items: List[RawItem], cfg: PodcastConfig) -> None:
    """
    Mark all item URLs as seen in the SeenURLStore.

    Called by pipeline_runner after a successful real episode (not dry-run).
    Keeping this out of run_discovery means retries and dry-runs don't
    permanently consume URLs from future episodes.
    """
    store = SeenURLStore(cfg.seen_urls_file, cfg.seen_url_max_age_days)
    store.load()
    for item in items:
        store.mark_seen(item["url"])
    store.save()
    logger.info("Marked %d URLs as seen.", len(items))


@click.command()
@click.option(
    "--date",
    "run_date_str",
    default=None,
    help="Episode date as YYYY-MM-DD. Defaults to today.",
)
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="Re-run even if output file already exists.",
)
def cli(run_date_str: str | None, force: bool) -> None:
    """Run Stage 1 content discovery as a standalone command."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    run_date = date.fromisoformat(run_date_str) if run_date_str else date.today()
    cfg = get_config()
    items = run_discovery(run_date, cfg, force=force)
    print(f"Discovered {len(items)} items for {run_date}.")


if __name__ == "__main__":
    cli()
