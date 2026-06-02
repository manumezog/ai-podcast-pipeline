"""
Stage 2 — Content Curation

Uses Gemini API to score each raw story for quality, relevance, and novelty.
Filters out items below cfg.curation_threshold. Returns the top cfg.top_n_stories.

MODEL:
    Uses cfg.gemini_model (default: gemini-2.5-flash) via the google-genai SDK.
    Same GOOGLE_API_KEY as TTS and script writing — no extra credentials.

SOURCE-SPECIFIC SCORING:
    The curator prompt instructs Gemini to apply different criteria by source:
      - newsapi: weight recency and mainstream relevance
      - reddit:  weight community signal and discussion quality
      - arxiv:   weight technical novelty; +1 bonus for new capabilities

JSON OUTPUT:
    response_mime_type="application/json" is set so Gemini returns clean JSON
    without markdown fences, eliminating fragile post-processing.

STUB MODE:
    _curator_stub() returns all items with score=7 — no API calls.
    Active when use_stub=True or when GOOGLE_API_KEY is not set.

Idempotency:
    If data/curated/YYYY-MM-DD.json exists, returns it unless --force is passed.

Output schema (list of dicts, saved to data/curated/YYYY-MM-DD.json):
    [
        {
            "title": str,
            "url": str,
            "summary": str,
            "source": "newsapi" | "reddit" | "arxiv",
            "date": "YYYY-MM-DD",
            "score": int,
            "one_line_reason": str
        },
        ...
    ]

CLI usage:
    python modules/content_curator.py --date 2026-06-02
    python modules/content_curator.py --date 2026-06-02 --stub
    python modules/content_curator.py --date 2026-06-02 --force
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date
from pathlib import Path
from typing import Any, List

import click

from config import PodcastConfig, get_config
from modules.utils.retry import retry_with_backoff

logger = logging.getLogger(__name__)

CuratedItem = dict[str, Any]


def _curator_stub(items: List[dict[str, Any]]) -> List[CuratedItem]:
    """
    Pass-through stub — returns all items with score=7, no API calls.

    Allows full pipeline testing before the curation quality is validated.
    """
    return [
        {**item, "score": 7, "one_line_reason": "stub: not yet curated"}
        for item in items
    ]


def _load_curator_prompt(cfg: PodcastConfig) -> str:
    path = Path(cfg.prompts_dir) / "curator_prompt.md"
    return path.read_text(encoding="utf-8")


def _build_curator_system_prompt(template: str, cfg: PodcastConfig) -> str:
    """Extract the static system prompt portion (everything before ITEMS:)."""
    lines = [l for l in template.split("\n") if not l.startswith("#")]
    content = "\n".join(lines).strip()
    marker = "ITEMS:"
    system_part = content[:content.index(marker)].strip() if marker in content else content
    return system_part.replace("{TOPIC}", cfg.topic)


def _curator_gemini(items: List[dict[str, Any]], cfg: PodcastConfig) -> List[CuratedItem]:
    """
    Score and filter items via Gemini API using the curator prompt.

    Sets response_mime_type="application/json" so Gemini returns clean JSON
    without fences. Source type metadata is included per item so Gemini can
    apply source-specific scoring weights.

    Returns items scoring >= cfg.curation_threshold, sorted by score
    descending, limited to cfg.top_n_stories.
    """
    from google import genai
    from google.genai import types

    template = _load_curator_prompt(cfg)
    system_prompt = _build_curator_system_prompt(template, cfg)

    # Pre-filter to top N*3 items by raw_score before sending to Gemini.
    # Sending all discovered items risks truncating the response; raw_score
    # already provides a good recency+source-quality pre-ranking.
    pre_limit = cfg.top_n_stories * 3
    candidates = sorted(items, key=lambda x: x.get("raw_score", 0), reverse=True)[:pre_limit]
    logger.info("Curator: sending %d/%d pre-ranked items to Gemini.", len(candidates), len(items))

    items_payload = [
        {
            "title": item["title"],
            "url": item["url"],
            "source": item.get("source", "unknown"),
            "date": item.get("date", ""),
            "summary": item.get("summary", "")[:300],
        }
        for item in candidates
    ]
    user_message = f"ITEMS:\n\n{json.dumps(items_payload, indent=2, ensure_ascii=False)}"

    client = genai.Client(api_key=cfg.google_api_key)

    def _call() -> str:
        response = client.models.generate_content(
            model=cfg.gemini_model,
            contents=user_message,
            config=types.GenerateContentConfig(
                system_instruction=system_prompt,
                response_mime_type="application/json",
                max_output_tokens=8192,
                temperature=0.2,
            ),
        )
        return response.text.strip()

    raw_text = retry_with_backoff(
        _call,
        max_attempts=cfg.retry_max_attempts,
        base_delay=cfg.retry_base_delay,
    )

    # Strip markdown fences as a safety net (shouldn't be needed with mime type set)
    raw_text = re.sub(r"^```(?:json)?\s*", "", raw_text)
    raw_text = re.sub(r"\s*```$", "", raw_text).strip()

    try:
        scored_list = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        logger.error("Gemini returned invalid JSON for curation: %s\nRaw: %s", exc, raw_text[:500])
        raise

    score_map: dict[str, dict] = {
        entry["url"]: entry for entry in scored_list if "url" in entry
    }

    results: List[CuratedItem] = []
    for item in candidates:
        scored = score_map.get(item["url"])
        score = int(scored.get("score", 0)) if scored else 0
        reason = str(scored.get("one_line_reason", "")) if scored else "not returned by curator"
        if score >= cfg.curation_threshold:
            results.append({**item, "score": score, "one_line_reason": reason})

    results.sort(key=lambda x: x["score"], reverse=True)
    return results[: cfg.top_n_stories]


def run_curation(
    raw_items: List[dict[str, Any]],
    run_date: date,
    cfg: PodcastConfig,
    force: bool = False,
    use_stub: bool = False,
) -> List[CuratedItem]:
    """
    Main entry point for Stage 2.

    Args:
        raw_items: Output from Stage 1 (content_discovery).
        run_date: Determines output filename.
        cfg: Pipeline configuration.
        force: Re-run even if data/curated/YYYY-MM-DD.json already exists.
        use_stub: Bypass Gemini and use _curator_stub(). Falls back to stub
                  automatically if GOOGLE_API_KEY is not set.

    Returns:
        Curated list written to data/curated/YYYY-MM-DD.json.
    """
    output_path = Path(cfg.data_dir) / "curated" / f"{run_date}.json"

    if output_path.exists() and not force:
        logger.info("Stage 2: output exists at %s, skipping.", output_path)
        return json.loads(output_path.read_text(encoding="utf-8"))

    if use_stub or not cfg.google_api_key:
        if not cfg.google_api_key and not use_stub:
            logger.warning("GOOGLE_API_KEY not set — falling back to curator stub.")
        curated = _curator_stub(raw_items)
        curated = sorted(curated, key=lambda x: x.get("score", 0), reverse=True)
        curated = curated[: cfg.top_n_stories]
    else:
        curated = _curator_gemini(raw_items, cfg)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(curated, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.info("Stage 2: wrote %d curated items to %s.", len(curated), output_path)
    return curated


@click.command()
@click.option("--date", "run_date_str", default=None, help="Episode date YYYY-MM-DD.")
@click.option("--force", is_flag=True, default=False)
@click.option("--stub", is_flag=True, default=False, help="Use pass-through stub instead of Gemini.")
def cli(run_date_str: str | None, force: bool, stub: bool) -> None:
    """Run Stage 2 content curation as a standalone command."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    run_date = date.fromisoformat(run_date_str) if run_date_str else date.today()
    cfg = get_config()
    raw_path = Path(cfg.data_dir) / "raw" / f"{run_date}.json"
    if not raw_path.exists():
        print(f"No Stage 1 output at {raw_path}. Run content_discovery.py first.")
        raise SystemExit(1)
    raw_items = json.loads(raw_path.read_text(encoding="utf-8"))
    items = run_curation(raw_items, run_date, cfg, force=force, use_stub=stub)
    print(f"Curated {len(items)} items for {run_date}.")


if __name__ == "__main__":
    cli()
