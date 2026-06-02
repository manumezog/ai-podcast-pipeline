"""
Central configuration for the AI Podcast Pipeline.

All values load from environment variables / .env file automatically via
pydantic-settings.

Production usage:
    from config import get_config
    cfg = get_config()

Test usage (override specific values without touching .env):
    from config import PodcastConfig
    cfg = PodcastConfig(topic="test topic", top_n_stories=2)
"""

from __future__ import annotations

from functools import lru_cache
from typing import List

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class PodcastConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── API Keys ──────────────────────────────────────────────────────────
    google_api_key: str = Field(default="", description="Google API key (Gemini text + TTS)")
    newsapi_key: str = Field(default="", description="NewsAPI key")
    reddit_client_id: str = Field(default="", description="Reddit OAuth client ID")
    reddit_client_secret: str = Field(default="", description="Reddit OAuth client secret")
    reddit_user_agent: str = Field(
        default="podcaster/1.0", description="Reddit API user agent string"
    )

    # ── Podcast Identity ──────────────────────────────────────────────────
    show_name: str = Field(default="The Robotics Brief")
    host_name: str = Field(default="Alex")
    topic: str = Field(default="robotics and automation")

    # ── Gemini TTS ────────────────────────────────────────────────────────
    gemini_tts_model: str = Field(
        default="gemini-2.5-flash-preview-tts",
        description="Gemini model ID for TTS generation",
    )
    gemini_tts_voice: str = Field(
        default="Puck",
        description="Voice for host 1. Options: Aoede, Charon, Fenrir, Kore, Puck, Orbit, Zephyr",
    )
    gemini_tts_voice_2: str = Field(
        default="Aoede",
        description="Voice for host 2 (co-host). Different register from voice 1.",
    )

    # ── Hosts ─────────────────────────────────────────────────────────────
    host_name_2: str = Field(default="Sam", description="Co-host name used in dialogue scripts")

    # ── Episode Tuning ────────────────────────────────────────────────────
    episode_target_words: int = Field(default=1500, ge=500)
    curation_threshold: int = Field(default=6, ge=1, le=10)
    top_n_stories: int = Field(default=5, ge=1)
    tts_max_chars_per_chunk: int = Field(
        default=4800,
        le=5000,
        description="Max characters per Gemini TTS API call (safety margin below model limit)",
    )

    # ── Content Sources ───────────────────────────────────────────────────
    news_sources: List[str] = Field(
        default_factory=lambda: [
            "techcrunch.com",
            "wired.com",
            "ieee.org",
            "therobotreport.com",
            "spectrum.ieee.org",
        ],
        description="NewsAPI domains to prioritize",
    )
    reddit_subreddits: List[str] = Field(
        default_factory=lambda: ["robotics", "MachineLearning", "artificial"],
        description="Subreddits to monitor",
    )
    arxiv_categories: List[str] = Field(
        default_factory=lambda: ["cs.RO", "cs.AI", "cs.LG"],
        description="ArXiv category codes to search",
    )
    arxiv_max_results: int = Field(default=20)
    newsapi_page_size: int = Field(default=20)
    reddit_post_limit: int = Field(default=25)

    # ── Retry ─────────────────────────────────────────────────────────────
    retry_max_attempts: int = Field(default=5)
    retry_base_delay: float = Field(
        default=3.0, description="Initial retry delay in seconds (doubles each attempt: 3s, 6s, 12s, 24s)"
    )

    # ── Paths ─────────────────────────────────────────────────────────────
    data_dir: str = Field(default="data")
    scripts_dir: str = Field(default="scripts")
    audio_dir: str = Field(default="audio")
    logs_dir: str = Field(default="logs")
    prompts_dir: str = Field(default="prompts")
    seen_urls_file: str = Field(default="data/seen_urls.json")
    ffmpeg_path: str = Field(
        default="",
        description="Full path to ffmpeg executable. Leave empty to auto-detect.",
    )

    # ── Deduplication ─────────────────────────────────────────────────────
    seen_url_max_age_days: int = Field(
        default=30,
        description="Purge seen URLs older than this many days to keep the store small",
    )

    # ── Cloudflare R2 (audio hosting) ─────────────────────────────────────
    cloudflare_r2_account_id: str = Field(default="", description="Cloudflare account ID")
    cloudflare_r2_access_key_id: str = Field(default="", description="R2 API token access key ID")
    cloudflare_r2_secret_access_key: str = Field(default="", description="R2 API token secret")
    cloudflare_r2_bucket_name: str = Field(default="podcast", description="R2 bucket name")
    cloudflare_r2_public_url: str = Field(
        default="",
        description="Public base URL for bucket, e.g. https://pub-xxx.r2.dev",
    )

    # ── GitHub (RSS feed hosting via GitHub Pages) ─────────────────────────
    github_token: str = Field(default="", description="GitHub Personal Access Token (repo: contents write)")
    github_repo: str = Field(default="", description="Repo for RSS feed, e.g. username/podcast-feed")
    github_rss_path: str = Field(default="feed.xml", description="Path to RSS file in the repo")

    # ── Podcast metadata (for RSS feed) ───────────────────────────────────
    podcast_description: str = Field(
        default="A daily podcast covering the latest in robotics and automation.",
    )
    podcast_artwork_url: str = Field(
        default="",
        description="URL to episode artwork (1400x1400 JPG). Upload to R2 bucket.",
    )
    podcast_owner_name: str = Field(
        default="",
        description="Real name of the podcast owner (you). Used in RSS <itunes:owner>, not the AI host name.",
    )
    podcast_owner_email: str = Field(
        default="",
        description="Owner email for RSS feed. Required by Spotify, Apple, Amazon for submission.",
    )
    podcast_website_url: str = Field(default="")
    podcast_category: str = Field(default="Technology")
    podcast_language: str = Field(default="en-us")
    podcast_rss_url: str = Field(
        default="",
        description="Final public RSS feed URL (GitHub Pages), e.g. https://user.github.io/podcast-feed/feed.xml",
    )
    podcast_max_feed_items: int = Field(default=50, description="Max episodes to keep in RSS feed")

    # ── TTS Style ─────────────────────────────────────────────────────────
    tts_style_prompt: str = Field(
        default=(
            "You are a podcast host. Speak at a brisk, energetic, engaged pace — "
            "not slow or measured, but quick and natural, like Spotify or NPR podcast hosts. "
            "Be warm, direct, conversational. Never sound like a broadcast announcer or audiobook reader. "
            "Contractions should sound completely natural. "
            "When the text contains 'uh', 'um', or 'hmm', render them as genuine brief hesitations, not words. "
            "An em dash (—) means a short self-interrupting pause. "
            "An ellipsis (...) means a slight trailing-off before continuing. "
            "Vary your energy — be more animated when making a strong point, more thoughtful when reasoning through something. "
            "Sound like you actually care about what you're saying."
        ),
        description="Style instruction passed to Gemini TTS as system_instruction.",
    )
    speech_tempo: float = Field(
        default=1.12,
        description="Playback speed multiplier applied via ffmpeg atempo filter. 1.12 = 12% faster.",
    )

    # ── Features ──────────────────────────────────────────────────────────
    enable_dialogue: bool = Field(default=True, description="Generate two-host dialogue scripts")
    enable_music: bool = Field(default=True, description="Add generated intro/outro music")
    intro_music_duration: float = Field(default=6.0, description="Intro music length in seconds")
    outro_music_duration: float = Field(default=12.0, description="Outro music length in seconds")

    # ── Gemini Text Model (curation + scriptwriting) ──────────────────────
    gemini_model: str = Field(
        default="gemini-2.5-flash",
        description="Gemini model for text generation. Use gemini-2.5-pro for higher quality.",
    )


@lru_cache(maxsize=1)
def get_config() -> PodcastConfig:
    """Return a cached singleton PodcastConfig loaded from .env."""
    return PodcastConfig()
