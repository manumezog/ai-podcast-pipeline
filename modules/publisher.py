"""
Stage 6 — Publisher

Uploads the episode MP3 to Cloudflare R2 (free storage), generates show notes
via Gemini, updates the RSS feed XML on GitHub, and pushes it so all subscribed
podcast platforms (Spotify, Apple Podcasts, Amazon Music, etc.) auto-ingest
the new episode.

SETUP (one-time):
    See PUBLISHING_SETUP.md for step-by-step account creation instructions.
    Required env vars: CLOUDFLARE_R2_*, GITHUB_TOKEN, GITHUB_REPO, PODCAST_RSS_URL

FLOW:
    1. Upload YYYY-MM-DD.mp3 → R2 bucket → get public URL
    2. Generate 2-3 sentence show notes via Gemini from episode metadata
    3. Fetch current feed.xml from GitHub
    4. Prepend new <item> to feed, trim to max_feed_items
    5. Push updated feed.xml back to GitHub
    6. Write data/published/YYYY-MM-DD.json as completion sentinel

IDEMPOTENCY:
    If data/published/YYYY-MM-DD.json exists, skips all steps unless --force.

CLI usage:
    python modules/publisher.py --date 2026-06-02
    python modules/publisher.py --date 2026-06-02 --dry-run
    python modules/publisher.py --date 2026-06-02 --force
"""

from __future__ import annotations

import base64
import json
import logging
import re
from datetime import date, datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

import click
import httpx

from config import PodcastConfig, get_config
from modules.utils.retry import retry_with_backoff

logger = logging.getLogger(__name__)

_ITUNES_NS = "http://www.itunes.com/dtds/podcast-1.0.dtd"
_CONTENT_NS = "http://purl.org/rss/1.0/modules/content/"


# ── R2 upload ────────────────────────────────────────────────────────────────

def upload_to_r2(audio_path: Path, run_date: date, cfg: PodcastConfig) -> str:
    """
    Upload episode MP3 to Cloudflare R2 and return its public URL.

    Uses boto3's S3-compatible client with Cloudflare's R2 endpoint.

    Returns:
        Public URL string, e.g. https://pub-xxx.r2.dev/2026-06-02.mp3
    """
    import boto3

    endpoint = f"https://{cfg.cloudflare_r2_account_id}.r2.cloudflarestorage.com"
    s3 = boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=cfg.cloudflare_r2_access_key_id,
        aws_secret_access_key=cfg.cloudflare_r2_secret_access_key,
        region_name="auto",
    )

    object_key = f"{run_date}.mp3"

    def _upload():
        s3.upload_file(
            str(audio_path),
            cfg.cloudflare_r2_bucket_name,
            object_key,
            ExtraArgs={"ContentType": "audio/mpeg"},
        )

    retry_with_backoff(_upload, max_attempts=cfg.retry_max_attempts, base_delay=cfg.retry_base_delay)

    public_url = f"{cfg.cloudflare_r2_public_url.rstrip('/')}/{object_key}"
    logger.info("Uploaded %s → %s", audio_path.name, public_url)
    return public_url


# ── Show notes generation ─────────────────────────────────────────────────────

def generate_show_notes(episode_metadata: dict[str, Any], cfg: PodcastConfig) -> str:
    """
    Generate a 2-3 sentence episode description using Gemini.

    Falls back to a plain story list if the API call fails.
    """
    stories = episode_metadata.get("stories", [])
    story_titles = "\n".join(f"- {s['title']}" for s in stories)

    prompt = (
        f"Write a 2-3 sentence episode description for a podcast episode of '{cfg.show_name}'. "
        f"The episode covers these stories:\n{story_titles}\n\n"
        "Be engaging and specific. No fluff. No 'In this episode...' opener. "
        "Write as if describing what the listener will learn and why they should care. "
        "Plain text only, no markdown."
    )

    try:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=cfg.google_api_key)

        def _call() -> str:
            response = client.models.generate_content(
                model=cfg.gemini_model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    max_output_tokens=256,
                    temperature=0.7,
                ),
            )
            return response.text.strip()

        return retry_with_backoff(_call, max_attempts=cfg.retry_max_attempts, base_delay=cfg.retry_base_delay)
    except Exception as exc:
        logger.warning("Show notes generation failed (%s), using story list.", exc)
        return f"Today's stories: {', '.join(s['title'] for s in stories[:3])}."


# ── RSS feed management ───────────────────────────────────────────────────────

def _rss_date(d: date) -> str:
    """Format a date as RFC 2822 for RSS <pubDate>."""
    dt = datetime(d.year, d.month, d.day, 6, 0, 0, tzinfo=timezone.utc)
    return format_datetime(dt)


def _duration_str(seconds: float) -> str:
    """Convert seconds to HH:MM:SS for iTunes duration tag."""
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"


def _build_new_item(
    episode_metadata: dict[str, Any],
    audio_url: str,
    audio_size_bytes: int,
    show_notes: str,
    run_date: date,
    cfg: PodcastConfig,
) -> str:
    """Return an RSS <item> XML string for the episode."""
    title = episode_metadata.get("title", f"{cfg.show_name} — {run_date}")
    pub_date = _rss_date(run_date)
    duration = _duration_str(episode_metadata.get("duration_seconds", 0))
    guid = audio_url  # stable unique identifier

    # Escape special characters for XML
    def _esc(s: str) -> str:
        return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")

    artwork = cfg.podcast_artwork_url or ""

    return f"""    <item>
      <title>{_esc(title)}</title>
      <enclosure url="{_esc(audio_url)}" length="{audio_size_bytes}" type="audio/mpeg"/>
      <guid isPermaLink="false">{_esc(guid)}</guid>
      <pubDate>{pub_date}</pubDate>
      <description>{_esc(show_notes)}</description>
      <itunes:summary>{_esc(show_notes)}</itunes:summary>
      <itunes:duration>{duration}</itunes:duration>
      <itunes:episodeType>full</itunes:episodeType>
      <itunes:explicit>false</itunes:explicit>
      <itunes:image href="{_esc(artwork)}"/>
    </item>"""


def _build_new_feed(cfg: PodcastConfig, first_item_xml: str) -> str:
    """Create a brand-new RSS feed XML from scratch."""
    artwork = cfg.podcast_artwork_url or ""
    website = cfg.podcast_website_url or cfg.podcast_rss_url or ""
    email = cfg.podcast_owner_email or ""

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"
     xmlns:itunes="{_ITUNES_NS}"
     xmlns:content="{_CONTENT_NS}">
  <channel>
    <title>{cfg.show_name}</title>
    <link>{website}</link>
    <description>{cfg.podcast_description}</description>
    <language>{cfg.podcast_language}</language>
    <itunes:author>{cfg.host_name}</itunes:author>
    <itunes:summary>{cfg.podcast_description}</itunes:summary>
    <itunes:owner>
      <itunes:name>{cfg.podcast_owner_name or cfg.host_name}</itunes:name>
      <itunes:email>{email}</itunes:email>
    </itunes:owner>
    <itunes:category text="{cfg.podcast_category}"/>
    <itunes:explicit>false</itunes:explicit>
    <itunes:image href="{artwork}"/>
    <image>
      <url>{artwork}</url>
      <title>{cfg.show_name}</title>
      <link>{website}</link>
    </image>
{first_item_xml}
  </channel>
</rss>"""


def _insert_item_into_feed(existing_xml: str, new_item_xml: str, max_items: int) -> str:
    """Insert new_item_xml as the first item in existing_xml, trimming old items."""
    # Insert after the last <image> or <itunes:image> block, or after <channel> opening tags
    # Strategy: find the first <item> and insert before it, or find </channel> and insert before
    first_item_match = re.search(r"(\s*<item>)", existing_xml)
    if first_item_match:
        insert_pos = first_item_match.start()
        updated = existing_xml[:insert_pos] + "\n" + new_item_xml + "\n" + existing_xml[insert_pos:]
    else:
        # No existing items — insert before </channel>
        insert_pos = existing_xml.rfind("</channel>")
        if insert_pos == -1:
            raise ValueError("Malformed RSS feed: no </channel> tag found.")
        updated = existing_xml[:insert_pos] + new_item_xml + "\n  " + existing_xml[insert_pos:]

    # Trim to max_items
    items = re.findall(r"<item>.*?</item>", updated, re.DOTALL)
    if len(items) > max_items:
        for old_item in items[max_items:]:
            updated = updated.replace(old_item, "", 1)

    return updated


# ── GitHub interaction ────────────────────────────────────────────────────────

def _github_api(method: str, path: str, token: str, body: dict | None = None) -> dict:
    """Make a GitHub API call, return the JSON response."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    url = f"https://api.github.com{path}"
    with httpx.Client(timeout=30) as client:
        if method == "GET":
            r = client.get(url, headers=headers)
        elif method == "PUT":
            r = client.put(url, headers=headers, json=body)
        else:
            raise ValueError(f"Unsupported method: {method}")
    r.raise_for_status()
    return r.json()


def fetch_rss_from_github(cfg: PodcastConfig) -> tuple[str | None, str | None]:
    """
    Fetch the current RSS feed from GitHub.

    Returns:
        (feed_xml_content, file_sha) — sha is needed to update the file.
        Returns (None, None) if the file doesn't exist yet.
    """
    owner_repo = cfg.github_repo
    path = cfg.github_rss_path
    try:
        data = _github_api("GET", f"/repos/{owner_repo}/contents/{path}", cfg.github_token)
        content = base64.b64decode(data["content"]).decode("utf-8")
        return content, data["sha"]
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return None, None
        raise


def push_rss_to_github(feed_xml: str, sha: str | None, cfg: PodcastConfig, run_date: date) -> None:
    """
    Create or update feed.xml in the GitHub repo via the Contents API.

    Args:
        feed_xml: The full updated RSS XML string.
        sha: The existing file's SHA (None if creating for the first time).
        cfg: Pipeline configuration.
        run_date: Used in the commit message.
    """
    owner_repo = cfg.github_repo
    path = cfg.github_rss_path
    encoded = base64.b64encode(feed_xml.encode("utf-8")).decode("ascii")

    body: dict[str, Any] = {
        "message": f"Add episode {run_date}",
        "content": encoded,
    }
    if sha:
        body["sha"] = sha

    def _push():
        _github_api("PUT", f"/repos/{owner_repo}/contents/{path}", cfg.github_token, body)

    retry_with_backoff(_push, max_attempts=cfg.retry_max_attempts, base_delay=cfg.retry_base_delay)
    logger.info("RSS feed pushed to GitHub: %s/%s", owner_repo, path)


# ── Main entry point ──────────────────────────────────────────────────────────

def run_publisher(
    run_date: date,
    cfg: PodcastConfig,
    force: bool = False,
    dry_run: bool = False,
) -> None:
    """
    Main entry point for Stage 6.

    Args:
        run_date: Episode date.
        cfg: Pipeline configuration.
        force: Re-publish even if data/published/YYYY-MM-DD.json exists.
        dry_run: Perform all steps except the actual R2 upload and GitHub push.
    """
    sentinel = Path(cfg.data_dir) / "published" / f"{run_date}.json"
    if sentinel.exists() and not force:
        logger.info("Stage 6: already published (%s), skipping.", sentinel)
        return

    # Validate required config
    missing = [
        k for k, v in {
            "CLOUDFLARE_R2_ACCOUNT_ID": cfg.cloudflare_r2_account_id,
            "CLOUDFLARE_R2_ACCESS_KEY_ID": cfg.cloudflare_r2_access_key_id,
            "CLOUDFLARE_R2_SECRET_ACCESS_KEY": cfg.cloudflare_r2_secret_access_key,
            "CLOUDFLARE_R2_PUBLIC_URL": cfg.cloudflare_r2_public_url,
            "GITHUB_TOKEN": cfg.github_token,
            "GITHUB_REPO": cfg.github_repo,
        }.items()
        if not v
    ]
    if missing:
        raise RuntimeError(
            f"Stage 6 requires these .env keys to be set: {', '.join(missing)}\n"
            "See PUBLISHING_SETUP.md for setup instructions."
        )

    # Load episode metadata
    metadata_path = Path(cfg.data_dir) / "episodes" / f"{run_date}.json"
    if not metadata_path.exists():
        raise FileNotFoundError(f"Episode metadata not found: {metadata_path}. Run pipeline first.")
    episode_metadata = json.loads(metadata_path.read_text(encoding="utf-8"))

    audio_path = Path(episode_metadata["audio_path"])
    if not audio_path.exists():
        raise FileNotFoundError(f"Episode audio not found: {audio_path}")

    audio_size = audio_path.stat().st_size

    # Step 1: Upload to R2
    if dry_run:
        audio_url = f"{cfg.cloudflare_r2_public_url.rstrip('/')}/{run_date}.mp3"
        logger.info("Stage 6 [dry-run]: would upload %s → %s", audio_path.name, audio_url)
    else:
        logger.info("Stage 6: uploading to Cloudflare R2...")
        audio_url = upload_to_r2(audio_path, run_date, cfg)

    # Step 2: Generate show notes
    logger.info("Stage 6: generating show notes...")
    show_notes = generate_show_notes(episode_metadata, cfg)
    logger.info("Stage 6: show notes — %s", show_notes[:80])

    # Step 3: Build new RSS item
    new_item_xml = _build_new_item(episode_metadata, audio_url, audio_size, show_notes, run_date, cfg)

    # Step 4: Fetch current feed
    logger.info("Stage 6: fetching current RSS feed from GitHub...")
    if dry_run:
        existing_xml, sha = None, None
        logger.info("Stage 6 [dry-run]: skipping GitHub fetch.")
    else:
        existing_xml, sha = fetch_rss_from_github(cfg)

    # Step 5: Build updated feed
    if existing_xml:
        updated_feed = _insert_item_into_feed(existing_xml, new_item_xml, cfg.podcast_max_feed_items)
        logger.info("Stage 6: inserted episode into existing feed (sha=%s).", sha)
    else:
        updated_feed = _build_new_feed(cfg, new_item_xml)
        logger.info("Stage 6: created new RSS feed from scratch.")

    # Step 6: Push to GitHub
    if dry_run:
        logger.info("Stage 6 [dry-run]: would push updated feed.xml to GitHub.")
        logger.info("Stage 6 [dry-run]: feed preview:\n%s", updated_feed[:500])
    else:
        logger.info("Stage 6: pushing RSS feed to GitHub...")
        push_rss_to_github(updated_feed, sha, cfg, run_date)

    # Write sentinel
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    result = {
        "date": str(run_date),
        "audio_url": audio_url,
        "show_notes": show_notes,
        "rss_url": cfg.podcast_rss_url,
        "dry_run": dry_run,
    }
    sentinel.write_text(json.dumps(result, indent=2), encoding="utf-8")

    logger.info("Stage 6 complete. RSS feed: %s", cfg.podcast_rss_url)
    print(f"\nPublished:")
    print(f"  Audio URL: {audio_url}")
    print(f"  RSS feed:  {cfg.podcast_rss_url}")


@click.command()
@click.option("--date", "run_date_str", default=None, help="Episode date YYYY-MM-DD.")
@click.option("--force", is_flag=True, default=False)
@click.option("--dry-run", is_flag=True, default=False, help="Skip actual uploads and GitHub push.")
def cli(run_date_str: str | None, force: bool, dry_run: bool) -> None:
    """Run Stage 6 publishing as a standalone command."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    run_date = date.fromisoformat(run_date_str) if run_date_str else date.today()
    cfg = get_config()
    run_publisher(run_date, cfg, force=force, dry_run=dry_run)


if __name__ == "__main__":
    cli()
