"""
Stage 3 — Script Writing

Calls Gemini API to produce a podcast episode script from the curated story list.

MODEL:
    Uses cfg.gemini_model (default: gemini-2.5-flash) via the google-genai SDK.
    The same GOOGLE_API_KEY used for TTS is reused here — no extra credentials.

PROMPT STRUCTURE:
    system_instruction = static persona + format rules (stable across episodes)
    user message       = today's curated stories (changes every episode)

IDEMPOTENCY:
    If scripts/YYYY-MM-DD.md already exists, returns the cached path unless
    --force is passed.

OUTPUT:
    Plain text podcast script saved to scripts/YYYY-MM-DD.md.
    Target ~1500 words / ~10 minutes read time.

CLI usage:
    python modules/script_writer.py --date 2026-06-02
    python modules/script_writer.py --date 2026-06-02 --force
"""

from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path
from typing import Any, List

import click

from config import PodcastConfig, get_config
from modules.utils.retry import retry_with_backoff

logger = logging.getLogger(__name__)


def load_scriptwriter_prompt(cfg: PodcastConfig) -> str:
    """Read the raw template text from prompts/scriptwriter_prompt.md."""
    path = Path(cfg.prompts_dir) / "scriptwriter_prompt.md"
    return path.read_text(encoding="utf-8")


def build_system_prompt(template: str, cfg: PodcastConfig) -> str:
    """
    Strip comment header lines and substitute static config values.

    Extracts everything before "TODAY'S STORIES:" as the system instruction.
    """
    lines = [l for l in template.split("\n") if not l.startswith("#")]
    content = "\n".join(lines).strip()
    marker = "TODAY'S STORIES:"
    system_part = content[:content.index(marker)].strip() if marker in content else content
    return system_part.format(
        SHOW_NAME=cfg.show_name,
        HOST_NAME=cfg.host_name,
        HOST_NAME_2=cfg.host_name_2,
        TOP_N_STORIES=cfg.top_n_stories,
        TOPIC=cfg.topic,
        EPISODE_TARGET_WORDS=cfg.episode_target_words,
    )


def build_user_message(curated_items: List[dict[str, Any]], cfg: PodcastConfig) -> str:
    """Format the curated story list into the user message sent to Gemini."""
    stories_text = "\n\n".join(
        f"Story {i + 1}: {item['title']}\n"
        f"Source: {item.get('source', 'unknown')} | Date: {item.get('date', '')}\n"
        f"URL: {item['url']}\n"
        f"Summary: {item.get('summary', '').strip()}"
        for i, item in enumerate(curated_items)
    )
    return f"TODAY'S STORIES:\n\n{stories_text}"


def call_gemini_scriptwriter(
    system_prompt: str,
    user_message: str,
    cfg: PodcastConfig,
) -> str:
    """
    Invoke Gemini API to generate the episode script.

    Uses system_instruction for the static persona/format rules and sends
    the curated stories as the user turn. Uses retry_with_backoff.

    Returns:
        Raw script text as a string.
    """
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=cfg.google_api_key)

    def _call() -> str:
        response = client.models.generate_content(
            model=cfg.gemini_model,
            contents=user_message,
            config=types.GenerateContentConfig(
                system_instruction=system_prompt,
                max_output_tokens=8192,
                temperature=1.0,
            ),
        )
        return response.text.strip()

    return retry_with_backoff(
        _call,
        max_attempts=cfg.retry_max_attempts,
        base_delay=cfg.retry_base_delay,
    )


def run_script_writer(
    curated_items: List[dict[str, Any]],
    run_date: date,
    cfg: PodcastConfig,
    force: bool = False,
) -> Path:
    """
    Main entry point for Stage 3.

    Args:
        curated_items: Output from Stage 2 (content_curator).
        run_date: Determines output filename (scripts/YYYY-MM-DD.md).
        cfg: Pipeline configuration.
        force: Re-run even if scripts/YYYY-MM-DD.md already exists.

    Returns:
        Path to the written script file.
    """
    output_path = Path(cfg.scripts_dir) / f"{run_date}.md"

    if output_path.exists() and not force:
        logger.info("Stage 3: output exists at %s, skipping.", output_path)
        return output_path

    template = load_scriptwriter_prompt(cfg)
    system_prompt = build_system_prompt(template, cfg)
    user_message = build_user_message(curated_items, cfg)

    logger.info("Stage 3: calling Gemini (%s) to write script...", cfg.gemini_model)
    script_text = call_gemini_scriptwriter(system_prompt, user_message, cfg)

    word_count = len(script_text.split())
    logger.info("Stage 3: script written — %d words.", word_count)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(script_text, encoding="utf-8")
    logger.info("Stage 3: saved to %s.", output_path)

    return output_path


@click.command()
@click.option("--date", "run_date_str", default=None, help="Episode date YYYY-MM-DD.")
@click.option("--force", is_flag=True, default=False)
def cli(run_date_str: str | None, force: bool) -> None:
    """Run Stage 3 script writing as a standalone command."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    run_date = date.fromisoformat(run_date_str) if run_date_str else date.today()
    cfg = get_config()
    curated_path = Path(cfg.data_dir) / "curated" / f"{run_date}.json"
    if not curated_path.exists():
        print(f"No Stage 2 output at {curated_path}. Run content_curator.py first.")
        raise SystemExit(1)
    curated_items = json.loads(curated_path.read_text(encoding="utf-8"))
    script_path = run_script_writer(curated_items, run_date, cfg, force=force)
    print(f"Script written to {script_path}.")


if __name__ == "__main__":
    cli()
