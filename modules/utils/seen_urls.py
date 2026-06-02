"""
Cross-episode URL deduplication store.

Tracks which story URLs have already appeared in a past episode so
content_discovery.py can skip them on subsequent runs. Persists to a JSON
file that is auto-purged of entries older than max_age_days on every save.

Usage:
    from modules.utils.seen_urls import SeenURLStore

    store = SeenURLStore("data/seen_urls.json", max_age_days=30)
    store.load()

    fresh_items = [item for item in raw_items if not store.is_seen(item["url"])]

    for item in selected_items:
        store.mark_seen(item["url"])

    store.save()  # purges stale entries, then writes atomically
"""

from __future__ import annotations

import json
import logging
import tempfile
from datetime import date, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)


class SeenURLStore:
    """
    JSON-backed store for tracking already-published story URLs.

    File schema:
        {
            "https://example.com/story-one": "2026-05-10",
            "https://example.com/story-two": "2026-05-12"
        }

    Keys are URLs. Values are ISO date strings (YYYY-MM-DD) of when each URL
    was first marked as seen.
    """

    def __init__(self, filepath: str = "data/seen_urls.json", max_age_days: int = 30) -> None:
        """
        Args:
            filepath: Path to the JSON persistence file.
            max_age_days: Entries older than this number of days are purged on save().
        """
        self.filepath = Path(filepath)
        self.max_age_days = max_age_days
        self._store: dict[str, str] = {}

    def load(self) -> None:
        """Load seen URLs from disk. Safe to call when the file does not exist yet."""
        if not self.filepath.exists():
            self._store = {}
            return
        try:
            raw = json.loads(self.filepath.read_text(encoding="utf-8"))
            self._store = {k: v for k, v in raw.items() if isinstance(k, str) and isinstance(v, str)}
            logger.debug("Loaded %d seen URLs from %s.", len(self._store), self.filepath)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Could not load seen URLs from %s (%s). Starting fresh.", self.filepath, exc)
            self._store = {}

    def save(self) -> None:
        """Purge stale entries, then write the store to disk atomically."""
        self._purge_stale()
        self.filepath.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.filepath.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._store, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(self.filepath)
        logger.debug("Saved %d seen URLs to %s.", len(self._store), self.filepath)

    def is_seen(self, url: str) -> bool:
        """Return True if url was marked seen within the past max_age_days days."""
        date_str = self._store.get(url)
        if date_str is None:
            return False
        try:
            seen_date = date.fromisoformat(date_str)
            return (date.today() - seen_date).days < self.max_age_days
        except ValueError:
            return False

    def mark_seen(self, url: str, seen_date: date | None = None) -> None:
        """
        Record url as seen on seen_date (defaults to today).

        Args:
            url: The story URL to track.
            seen_date: The date to record. Defaults to today.
        """
        self._store[url] = (seen_date or date.today()).isoformat()

    def _purge_stale(self) -> None:
        """Remove entries older than max_age_days. Called automatically by save()."""
        cutoff = date.today() - timedelta(days=self.max_age_days)
        before = len(self._store)
        self._store = {
            url: ds
            for url, ds in self._store.items()
            if _parse_date_safe(ds) is not None and _parse_date_safe(ds) >= cutoff
        }
        purged = before - len(self._store)
        if purged:
            logger.debug("Purged %d stale URL entries (older than %d days).", purged, self.max_age_days)

    def __len__(self) -> int:
        return len(self._store)

    def __repr__(self) -> str:
        return f"SeenURLStore(filepath={self.filepath!r}, entries={len(self._store)})"


def _parse_date_safe(date_str: str) -> date | None:
    try:
        return date.fromisoformat(date_str)
    except ValueError:
        return None
