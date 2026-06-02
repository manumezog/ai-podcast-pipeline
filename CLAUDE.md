# AI Podcast Pipeline — Project Brief

## What We Are Building
An automated pipeline that discovers content on a specific knowledge domain,
writes a podcast script using an LLM, converts it to speech via TTS, and
produces a finished .mp3 episode ready for local review and eventual
Spotify publishing.

## Project Goals
- Fully automated: runs on a cron schedule with zero human intervention
- Modular: each pipeline stage is its own isolated module
- Testable: every stage can be run independently and inspected
- Cost-efficient: optimized for low API spend without sacrificing quality
- Local-first: all output saved locally first, publishing is a manual final step

---

## Tech Stack
- Language: Python 3.11+
- LLM: Anthropic Claude API (claude-sonnet-4-6) for scripting and curation
- TTS: ElevenLabs API (Turbo v2.5 model) for voice generation
- Audio: ffmpeg for stitching, normalization, and final .mp3 production
- Scheduling: cron (or APScheduler for in-process scheduling)
- Config: .env file for all API keys and settings, python-dotenv to load them
- Dependencies: managed via requirements.txt

---

## Pipeline Architecture

The pipeline has 5 sequential stages. Build them as independent Python modules:

### Stage 1 — content_discovery.py
- Pull from 3 sources: NewsAPI, Reddit (via PRAW), and ArXiv API
- Filter by a configurable TOPIC keyword list defined in config.py
- Score and rank items by recency and relevance
- Output: a list of dicts with title, url, summary, source, date
- Save raw results to /data/raw/YYYY-MM-DD.json

### Stage 2 — content_curator.py
- Take Stage 1 output
- Use Claude API to score each item for quality and uniqueness (1-10)
- Filter out anything below threshold (default: 6)
- Select top N items (default: 5) for the episode
- Output: curated list saved to /data/curated/YYYY-MM-DD.json

### Stage 3 — script_writer.py
- Take Stage 2 output
- Call Claude API with the scriptwriting prompt (see Prompt section below)
- Output: a structured podcast script saved to /scripts/YYYY-MM-DD.md
- Script must follow the template: intro, N content segments, outro
- Estimated read time should target 10 minutes (~1500 words)

### Stage 4 — tts_generator.py
- Take the script from Stage 3
- Split script into chunks if needed (ElevenLabs has a 5000 char limit per call)
- Call ElevenLabs API with voice_id from config
- Save individual audio chunks to /audio/chunks/
- Stitch chunks with ffmpeg into a single clean .mp3
- Apply normalization pass with ffmpeg (-af loudnorm)
- Save final file to /audio/episodes/YYYY-MM-DD.mp3

### Stage 5 — pipeline_runner.py
- Orchestrates Stages 1-4 in sequence
- Logs each stage start, end, and any errors to /logs/pipeline.log
- On failure: log the error, send a simple notification (print to console for now)
- On success: print episode path and duration
- Designed to be called by cron

---

## Directory Structure to Create
/project-root
CLAUDE.md
requirements.txt
.env.example
config.py
pipeline_runner.py
/modules
content_discovery.py
content_curator.py
script_writer.py
tts_generator.py
/data
/raw
/curated
/scripts
/audio
/chunks
/episodes
/logs
/prompts
scriptwriter_prompt.md
curator_prompt.md

---

## Config File (config.py)
All tunable parameters go here, not hardcoded:
- TOPIC: str — the knowledge domain (e.g. "robotics and automation")
- SHOW_NAME: str — name of the podcast
- HOST_NAME: str — name of the AI host persona
- HOST_VOICE_ID: str — ElevenLabs voice ID
- EPISODE_TARGET_WORDS: int — default 1500
- CURATION_THRESHOLD: int — minimum quality score, default 6
- TOP_N_STORIES: int — stories per episode, default 5
- NEWS_SOURCES: list — NewsAPI domains to prioritize
- REDDIT_SUBREDDITS: list — subreddits to monitor

---

## Scriptwriting Prompt (save to /prompts/scriptwriter_prompt.md)
You are the writer for {SHOW_NAME}, a podcast hosted by {HOST_NAME}.
{HOST_NAME} has a strong, clear point of view. They are knowledgeable but never
condescending. They speak with intellectual curiosity, occasional dry wit, and
genuine enthusiasm for the subject. They do not summarize — they analyze,
connect ideas, and surface implications the listener would not have noticed
themselves.
Today's episode covers {TOP_N_STORIES} stories from the world of {TOPIC}.
FORMAT:

Intro (100 words): Hook the listener. Reference something surprising or
counterintuitive from today's stories. Do not list what is coming up.
Story segments (one per story, ~250 words each): Start each with a sharp
one-sentence hook. Explain what happened, why it matters, and what it
implies for the future. Connect to previous stories where relevant.
Outro (100 words): Synthesize a unifying theme across today's stories.
End with one open question worth sitting with.

TONE: Intelligent, warm, opinionated, never corporate.
TARGET LENGTH: {EPISODE_TARGET_WORDS} words total.
OUTPUT: Plain text only, no markdown, no stage directions, no section headers.
TODAY'S STORIES:
{CURATED_STORIES}

---

## Content Curation Prompt (save to /prompts/curator_prompt.md)

You are a senior editorial assistant for a podcast about {TOPIC}.
Review the following list of news items and score each one from 1-10 on:

Relevance: how closely it fits {TOPIC}
Novelty: is this a genuinely new development or evergreen background info
Listener value: would a curious non-expert find this interesting and useful

Return a JSON array. Each item must have: title, url, score, one_line_reason.
Sort by score descending. Return only the JSON, no preamble.
ITEMS:
{RAW_ITEMS}

---

## Implementation Notes

- Use httpx or requests for all API calls, with retry logic (3 attempts,
  exponential backoff) on any HTTP 429 or 5xx
- All API keys loaded from .env via python-dotenv, never hardcoded
- ffmpeg must be installed on the system (document this in README)
- For TTS testing without spending ElevenLabs credits, add a --dry-run flag
  to tts_generator.py that skips the API call and copies a placeholder .mp3
- Each module should be runnable standalone:
  python modules/content_discovery.py --date 2026-06-02
- Add a simple --test flag to pipeline_runner.py that runs the full pipeline
  on a cached Stage 1 result so you can test scripting and TTS without
  hitting discovery APIs every time

---

## What to Build First (Suggested Order)

1. Project scaffold: folders, config.py, .env.example, requirements.txt
2. content_discovery.py — get data flowing in
3. script_writer.py — get readable output fast so you can evaluate quality
4. tts_generator.py — produce your first audio file
5. content_curator.py — add quality filtering once the pipeline works
6. pipeline_runner.py — wire everything together
7. README.md — document setup, dependencies, how to run

---

## First Task for Claude Code

Scaffold the entire project structure. Create all directories and empty
module files with docstrings. Create requirements.txt with all dependencies.
Create .env.example with placeholder keys. Create config.py with all
parameters set to sensible defaults for a robotics-focused podcast.
Do not implement module logic yet — just the scaffolding and config.