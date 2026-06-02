# AI Podcast Pipeline

A fully automated pipeline that discovers content on a knowledge domain, writes a podcast script using Google Gemini, converts it to speech via Gemini TTS, produces a finished MP3 episode with music, and publishes it to Spotify, Apple Podcasts, Amazon Music, and YouTube Music via an RSS feed.

Zero human intervention after the daily cron fires.

---

## What It Produces

- A ~10-minute dialogue-format episode between two AI hosts
- Ambient intro and outro music (synthesized, no external assets needed)
- MP3 uploaded to Cloudflare R2 (free tier)
- RSS feed updated on GitHub Pages
- All subscribed podcast platforms auto-ingest the new episode

---

## Pipeline Stages

```
Stage 1 — content_discovery.py
  NewsAPI + ArXiv → scored, deduped item list → data/raw/YYYY-MM-DD.json

Stage 2 — content_curator.py
  Gemini scores and filters → top 5 stories → data/curated/YYYY-MM-DD.json

Stage 3 — script_writer.py
  Gemini writes a two-host dialogue script → scripts/YYYY-MM-DD.md

Stage 4 — tts_generator.py
  Gemini TTS synthesizes per-speaker WAV chunks → stitched MP3 with music

Stage 5 — pipeline_runner.py
  Orchestrates stages 1–4, writes episode metadata

Stage 6 — publisher.py
  Uploads MP3 to Cloudflare R2, updates RSS feed on GitHub, all platforms sync
```

Each stage is independently runnable and idempotent — re-runs resume from the failed stage rather than starting over.

---

## Tech Stack

| Component | Technology |
|---|---|
| Language | Python 3.11+ |
| LLM (curation + scripting) | Google Gemini 2.5 Flash |
| TTS | Google Gemini TTS (`gemini-2.5-flash-preview-tts`) |
| Audio processing | ffmpeg |
| Music generation | Pure Python + numpy (no external assets) |
| Content sources | NewsAPI, ArXiv |
| Audio hosting | Cloudflare R2 (free tier) |
| RSS hosting | GitHub Pages (free) |

---

## Prerequisites

- Python 3.11+
- ffmpeg installed and on PATH
  ```
  winget install ffmpeg        # Windows
  brew install ffmpeg          # macOS
  sudo apt install ffmpeg      # Ubuntu
  ```
- API keys (see Configuration)

---

## Installation

```bash
git clone https://github.com/yourusername/ai-podcast-pipeline.git
cd ai-podcast-pipeline
pip install -r requirements.txt
cp .env.example .env
# Fill in your API keys in .env
```

---

## Configuration

All settings live in `.env`. Copy `.env.example` and fill in your values.

### Required API Keys

| Key | Where to get it |
|---|---|
| `GOOGLE_API_KEY` | [Google AI Studio](https://aistudio.google.com) → API Keys |
| `NEWSAPI_KEY` | [newsapi.org](https://newsapi.org) → free account |
| `CLOUDFLARE_R2_*` | Cloudflare Dashboard → R2 → Manage R2 API Tokens |
| `GITHUB_TOKEN` | GitHub → Settings → Developer settings → Fine-grained tokens |

Reddit credentials are optional — ArXiv + NewsAPI are sufficient.

### Key Settings in `.env`

```env
# Podcast identity
SHOW_NAME=The Robotics Brief
HOST_NAME=Alex          # AI host 1 name (voice: Puck)
TOPIC=robotics and automation

# Episode tuning
EPISODE_TARGET_WORDS=1500
TOP_N_STORIES=5
CURATION_THRESHOLD=6    # Gemini score 1-10; items below this are dropped

# TTS voices (Gemini built-ins: Aoede, Charon, Fenrir, Kore, Puck, Orbit, Zephyr)
GEMINI_TTS_VOICE=Puck       # Host 1
GEMINI_TTS_VOICE_2=Aoede    # Host 2 (co-host Sam)

# Audio
SPEECH_TEMPO=1.12           # 1.0 = normal speed, 1.12 = 12% faster
INTRO_MUSIC_DURATION=6.0    # seconds
OUTRO_MUSIC_DURATION=12.0   # seconds
```

See `.env.example` for the full list including publishing keys.

---

## Running the Pipeline

### Full run (produces and publishes a new episode)
```bash
python pipeline_runner.py --publish
```

### Without publishing (local episode only)
```bash
python pipeline_runner.py
```

### Cost-free test run (uses cached discovery, stub curator, no TTS)
```bash
python pipeline_runner.py --test --stub-curator --dry-run
```

### Force re-run of a specific date
```bash
python pipeline_runner.py --date 2026-06-02 --force
```

### Run individual stages
```bash
python modules/content_discovery.py --date 2026-06-02
python modules/content_curator.py --date 2026-06-02 --stub
python modules/script_writer.py --date 2026-06-02
python modules/tts_generator.py --date 2026-06-02 --dry-run
python -m modules.publisher --date 2026-06-02 --dry-run
```

---

## CLI Flags

| Flag | Effect |
|---|---|
| `--date YYYY-MM-DD` | Target date (default: today) |
| `--force` | Re-run all stages even if outputs exist |
| `--test` | Skip discovery, use cached Stage 1 output |
| `--stub-curator` | Skip Gemini curation, pass all items through with score=7 |
| `--dry-run` | Skip Gemini TTS and R2 upload; use silent audio placeholders |
| `--publish` | Run Stage 6: upload to R2 and push RSS feed to GitHub |

---

## Project Structure

```
ai-podcast-pipeline/
├── pipeline_runner.py          # Stage 5 — orchestrator
├── config.py                   # All settings via PodcastConfig(BaseSettings)
├── requirements.txt
├── .env.example                # Template for environment variables
├── PUBLISHING_SETUP.md         # Step-by-step Cloudflare R2 + GitHub setup
│
├── modules/
│   ├── content_discovery.py    # Stage 1 — NewsAPI + ArXiv
│   ├── content_curator.py      # Stage 2 — Gemini scoring + filtering
│   ├── script_writer.py        # Stage 3 — Gemini dialogue script
│   ├── tts_generator.py        # Stage 4 — Gemini TTS + ffmpeg
│   ├── music_generator.py      # Ambient intro/outro music via numpy
│   ├── publisher.py            # Stage 6 — R2 upload + RSS + GitHub
│   └── utils/
│       ├── retry.py            # Exponential backoff for API calls
│       └── seen_urls.py        # Cross-episode URL deduplication store
│
├── prompts/
│   ├── scriptwriter_prompt.md  # Two-host dialogue prompt template
│   └── curator_prompt.md       # Curation scoring prompt template
│
└── data/                       # Generated outputs (gitignored)
    ├── raw/                    # Stage 1 output
    ├── curated/                # Stage 2 output
    ├── episodes/               # Episode metadata JSON
    └── published/              # Publication sentinels
```

---

## How Idempotency Works

Every stage checks whether its output file already exists before running:

```
data/raw/YYYY-MM-DD.json        → Stage 1 guard
data/curated/YYYY-MM-DD.json    → Stage 2 guard
scripts/YYYY-MM-DD.md           → Stage 3 guard
audio/episodes/YYYY-MM-DD.mp3  → Stage 4 guard
data/episodes/YYYY-MM-DD.json  → Stage 5 guard (completion sentinel)
data/published/YYYY-MM-DD.json → Stage 6 guard
```

If Stage 4 fails, re-running picks up from Stage 4 — Stages 1–3 are not re-executed. Use `--force` to override.

---

## Cross-Episode Deduplication

`SeenURLStore` (`modules/utils/seen_urls.py`) tracks which story URLs have been used in past episodes. Stories already covered are excluded from discovery automatically. URLs are only marked as seen after a **fully successful real episode** — failed runs and dry-runs don't consume URLs.

The store purges entries older than 30 days automatically.

---

## Dialogue Format

Scripts are generated in a two-host format:

```
Alex: Welcome to The Robotics Brief, I'm Alex.
Sam: And I'm Sam. Something interesting happened this week...
Alex: Right, and what's wild is — it's not the tech that's surprising.
Sam: No, it's the timing. Because if you look at what came out of MIT last month...
```

Each speaker's lines are synthesized with a distinct Gemini TTS voice and stitched in sequence. The final episode is: `[6s music intro] → [dialogue] → [12s music outro]`.

---

## Music Generation

Intro and outro music are synthesized locally using numpy — no external music assets or APIs required. The track is a minimalist ambient electronic piece in A minor (pad + arpeggio + bass pulse + delay echo). Generated once and cached in `audio/music/`.

---

## Publishing

See `PUBLISHING_SETUP.md` for full step-by-step instructions to configure Cloudflare R2 and GitHub Pages.

Once configured, `--publish` uploads the episode MP3 to R2, generates show notes via Gemini, and pushes an updated `feed.xml` to GitHub. All subscribed platforms (Spotify, Apple Podcasts, Amazon Music, YouTube Music) auto-ingest the new episode within a few hours.

---

## Automating with Cron

```bash
# Run every day at 6am
0 6 * * * cd /path/to/ai-podcast-pipeline && python pipeline_runner.py --publish >> logs/cron.log 2>&1
```

---

## Cost

At daily frequency with default settings:

| Service | Usage | Cost |
|---|---|---|
| Gemini 2.5 Flash (text) | ~2000 tokens/episode | ~$0.001 |
| Gemini TTS | ~1500 words/episode | ~$0.02–0.05 |
| NewsAPI | 1 request/day | Free tier |
| ArXiv | 1 request/day | Free |
| Cloudflare R2 | ~10MB/episode | Free tier (10GB) |
| GitHub Pages | RSS feed | Free |

**Estimated total: ~$1–2/month.**

---

## License

MIT
