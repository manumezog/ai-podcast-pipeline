"""
Stage 5 — Pipeline Runner

Orchestrates Stages 1–4 in sequence. Designed for cron invocation or manual
runs. Logs to logs/pipeline.log and stderr. Writes episode metadata on success.

EPISODE METADATA (data/episodes/YYYY-MM-DD.json):
    {
        "title": "<SHOW_NAME> — YYYY-MM-DD",
        "date": "YYYY-MM-DD",
        "duration_seconds": float,
        "word_count": int,
        "story_count": int,
        "stories": [{"title": str, "url": str}, ...],
        "script_path": str,
        "audio_path": str
    }

IDEMPOTENCY:
    data/episodes/YYYY-MM-DD.json is the completion sentinel. If it exists and
    --force is not passed, the runner exits immediately. Each stage also has its
    own idempotency guard so partial failures resume from the failed stage.

MODES:
    --date TEXT       Episode date YYYY-MM-DD (default: today)
    --force           Re-run all stages even if outputs exist
    --test            Use cached Stage 1 output; skip discovery API calls
    --stub-curator    Use pass-through curation stub (score=7 for all stories)
    --dry-run         Skip Gemini TTS API calls (Stage 4)

CLI usage:
    python pipeline_runner.py
    python pipeline_runner.py --date 2026-06-02 --force
    python pipeline_runner.py --test --stub-curator --dry-run
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import subprocess
from datetime import date
from pathlib import Path
from typing import Any, List

import click

from config import PodcastConfig, get_config
from modules.content_discovery import run_discovery, mark_urls_seen
from modules.content_curator import run_curation
from modules.notifier import notify_failure, notify_success
from modules.script_writer import run_script_writer
from modules.tts_generator import run_tts_generator, find_ffmpeg
from modules.publisher import run_publisher

logger = logging.getLogger(__name__)


def setup_logging(cfg: PodcastConfig) -> None:
    """
    Configure the root logger to write to logs/pipeline.log and stderr.

    RotatingFileHandler: 5 MB per file, 3 backups — safe for long-running cron.
    """
    log_path = Path(cfg.logs_dir) / "pipeline.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root = logging.getLogger()
    if root.handlers:
        return  # already configured (e.g. called twice in tests)

    root.setLevel(logging.INFO)

    fh = logging.handlers.RotatingFileHandler(
        log_path,
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    fh.setFormatter(fmt)
    root.addHandler(fh)

    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    root.addHandler(sh)


def get_audio_duration(audio_path: Path) -> float:
    """
    Return audio duration in seconds using ffprobe.

    Returns 0.0 on any error (missing file, ffprobe not on PATH, etc.).
    """
    try:
        ffmpeg = find_ffmpeg()
        ffprobe = str(Path(ffmpeg).parent / "ffprobe")
        result = subprocess.run(
            [
                ffprobe,
                "-v", "quiet",
                "-print_format", "json",
                "-show_format",
                str(audio_path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            data = json.loads(result.stdout)
            return float(data["format"]["duration"])
    except Exception as exc:
        logger.warning("ffprobe failed for %s: %s", audio_path, exc)
    return 0.0


def write_episode_metadata(
    run_date: date,
    curated_items: List[dict[str, Any]],
    script_path: Path,
    audio_path: Path,
    cfg: PodcastConfig,
) -> Path:
    """
    Write episode summary JSON to data/episodes/YYYY-MM-DD.json.

    Reads the script to compute word_count. Uses ffprobe for duration_seconds.
    """
    output_path = Path(cfg.data_dir) / "episodes" / f"{run_date}.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)

    script_text = script_path.read_text(encoding="utf-8") if script_path.exists() else ""
    word_count = len(script_text.split())
    duration = get_audio_duration(audio_path)

    metadata: dict[str, Any] = {
        "title": f"{cfg.show_name} — {run_date}",
        "date": str(run_date),
        "duration_seconds": duration,
        "word_count": word_count,
        "story_count": len(curated_items),
        "stories": [
            {"title": item["title"], "url": item["url"]} for item in curated_items
        ],
        "script_path": str(script_path),
        "audio_path": str(audio_path),
    }

    output_path.write_text(
        json.dumps(metadata, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    logger.info("Episode metadata written to %s.", output_path)
    return output_path


def run_pipeline(
    run_date: date,
    cfg: PodcastConfig,
    force: bool = False,
    test_mode: bool = False,
    stub_curator: bool = False,
    dry_run: bool = False,
    publish: bool = False,
) -> None:
    """
    Execute Stages 1–4 sequentially and write episode metadata on success.

    Stage failures are logged and re-raised. Completed-stage outputs are
    preserved by idempotency guards so a re-run resumes from the failed stage.
    """
    metadata_path = Path(cfg.data_dir) / "episodes" / f"{run_date}.json"
    if metadata_path.exists() and not force:
        logger.info("Episode already complete: %s", metadata_path)
        print(f"Episode already complete: {metadata_path}")
        return

    logger.info("=== Pipeline starting for %s ===", run_date)

    # Stage 1 — Discovery
    if test_mode:
        raw_path = Path(cfg.data_dir) / "raw" / f"{run_date}.json"
        if not raw_path.exists():
            raise FileNotFoundError(
                f"--test mode requires existing {raw_path}. "
                "Run without --test first to populate Stage 1 output."
            )
        logger.info("Stage 1: --test mode, loading cached %s.", raw_path)
        raw_items = json.loads(raw_path.read_text(encoding="utf-8"))
    else:
        logger.info("Stage 1: content discovery...")
        raw_items = run_discovery(run_date, cfg, force=force)
    logger.info("Stage 1 complete: %d raw items.", len(raw_items))

    if not raw_items:
        raise RuntimeError(
            "Stage 1 returned 0 items. All discovered URLs may already be in the "
            "seen-URLs store from a previous run. The store resets automatically "
            "after 30 days, or delete data/seen_urls.json to reset it manually."
        )

    # Stage 2 — Curation
    logger.info("Stage 2: content curation (stub=%s)...", stub_curator)
    curated_items = run_curation(
        raw_items, run_date, cfg, force=force, use_stub=stub_curator
    )
    logger.info("Stage 2 complete: %d curated items.", len(curated_items))

    if not curated_items:
        raise RuntimeError(
            "Stage 2 returned no items above the curation threshold. "
            "Lower CURATION_THRESHOLD or check content sources."
        )

    # Stage 3 — Script
    logger.info("Stage 3: script writing...")
    script_path = run_script_writer(curated_items, run_date, cfg, force=force)
    logger.info("Stage 3 complete: %s", script_path)

    # Stage 4 — TTS
    logger.info("Stage 4: TTS generation (dry_run=%s)...", dry_run)
    audio_path = run_tts_generator(
        script_path, run_date, cfg, force=force, dry_run=dry_run
    )
    logger.info("Stage 4 complete: %s", audio_path)

    # Write episode metadata
    meta_path = write_episode_metadata(run_date, curated_items, script_path, audio_path, cfg)

    # Mark URLs seen only after a real completed episode (not dry-run).
    # This ensures retries and dry-run tests don't consume URLs permanently.
    if not dry_run and not test_mode:
        mark_urls_seen(raw_items, cfg)

    # Stage 6 — Publish (opt-in)
    if publish and not dry_run:
        logger.info("Stage 6: publishing...")
        run_publisher(run_date, cfg, force=force)

    logger.info("=== Pipeline complete for %s ===", run_date)
    print(f"\nEpisode ready:")
    print(f"  Audio:    {audio_path}")
    print(f"  Script:   {script_path}")
    print(f"  Metadata: {meta_path}")


@click.command()
@click.option("--date", "run_date_str", default=None, help="Episode date YYYY-MM-DD.")
@click.option("--force", is_flag=True, default=False, help="Re-run all stages.")
@click.option(
    "--test",
    "test_mode",
    is_flag=True,
    default=False,
    help="Use cached Stage 1 output; skip discovery APIs.",
)
@click.option(
    "--stub-curator",
    is_flag=True,
    default=False,
    help="Use pass-through curation stub instead of Claude.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Skip Gemini TTS API calls (Stage 4).",
)
@click.option(
    "--publish",
    is_flag=True,
    default=False,
    help="Run Stage 6: upload to R2 and update RSS feed on GitHub.",
)
def cli(
    run_date_str: str | None,
    force: bool,
    test_mode: bool,
    stub_curator: bool,
    dry_run: bool,
    publish: bool,
) -> None:
    """Run the full AI Podcast Pipeline."""
    run_date = date.fromisoformat(run_date_str) if run_date_str else date.today()
    cfg = get_config()
    setup_logging(cfg)
    try:
        run_pipeline(
            run_date=run_date,
            cfg=cfg,
            force=force,
            test_mode=test_mode,
            stub_curator=stub_curator,
            dry_run=dry_run,
            publish=publish,
        )
        # Notify on success (skip for dry-run / test to avoid noise)
        if not dry_run and not test_mode:
            episode_meta_path = Path(cfg.data_dir) / "episodes" / f"{run_date}.json"
            duration = 0.0
            audio_path = ""
            if episode_meta_path.exists():
                meta = json.loads(episode_meta_path.read_text(encoding="utf-8"))
                duration = meta.get("duration_seconds", 0.0)
                audio_path = meta.get("audio_path", "")
            notify_success(str(run_date), audio_path, duration, cfg)
    except Exception as exc:
        logger.error("Pipeline failed: %s", exc, exc_info=True)
        notify_failure(str(run_date), str(exc), cfg)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    cli()
