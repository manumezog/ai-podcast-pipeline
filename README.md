# AI Podcast Pipeline

A fully automated pipeline that discovers content on a knowledge domain, writes a podcast script using Google Gemini, converts it to speech using a **cloned host voice** (F5-TTS on Modal GPU) and a Gemini co-host voice, produces a finished MP3 with music, and publishes it to Spotify, Apple Podcasts, Amazon Music, and YouTube Music via an RSS feed.

Zero human intervention after the daily cron fires.

---

## What It Produces

- A ~10-minute dialogue-format episode between two hosts
- **Alex** (host) speaks in your own cloned voice via F5-TTS on Modal
- **Sam** (co-host) speaks in a Gemini TTS voice
- Ambient intro and outro music (synthesized, no external assets needed)
- MP3 uploaded to Cloudflare R2 (free tier)
- RSS feed updated on GitHub Pages
- All subscribed podcast platforms auto-ingest the new episode

---

## Pipeline Stages

```
Stage 1 — content_discovery.py
  NewsAPI + ArXiv -> scored, deduped item list -> data/raw/YYYY-MM-DD.json

Stage 2 — content_curator.py
  Gemini scores and filters -> top 5 stories -> data/curated/YYYY-MM-DD.json

Stage 3 — script_writer.py
  Gemini writes a two-host dialogue script -> scripts/YYYY-MM-DD.md

Stage 4 — tts_generator.py
  Alex's lines -> F5-TTS (cloned voice) via Modal GPU
  Sam's lines  -> Gemini TTS (Aoede voice)
  Chunks stitched into MP3 with intro/outro music

Stage 5 — pipeline_runner.py
  Orchestrates stages 1-4, writes episode metadata

Stage 6 — publisher.py
  Uploads MP3 to Cloudflare R2, updates RSS feed on GitHub
```

Each stage is independently runnable and idempotent — re-runs resume from the failed stage rather than starting over.

---

## Tech Stack

| Component | Technology |
|---|---|
| Language | Python 3.11+ |
| LLM (curation + scripting) | Google Gemini 2.5 Flash |
| Host voice (Alex) | F5-TTS on Modal serverless GPU |
| Co-host voice (Sam) | Google Gemini TTS (`gemini-2.5-flash-preview-tts`) |
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
- Modal CLI authenticated (`pip install modal && modal token new`)
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
| `GOOGLE_API_KEY` | [Google AI Studio](https://aistudio.google.com) -> API Keys |
| `NEWSAPI_KEY` | [newsapi.org](https://newsapi.org) -> free account |
| `CLOUDFLARE_R2_*` | Cloudflare Dashboard -> R2 -> Manage R2 API Tokens |
| `GITHUB_TOKEN` | GitHub -> Settings -> Developer settings -> Fine-grained tokens |

Reddit credentials are optional — ArXiv + NewsAPI are sufficient.

### Key Settings in `.env`

```env
# Podcast identity
SHOW_NAME=The Robotics Brief
HOST_NAME=Alex          # main host name (uses cloned voice)
HOST_NAME_2=Sam         # co-host name (uses Gemini TTS voice)
TOPIC=robotics and automation

# Episode tuning
EPISODE_TARGET_WORDS=1500
TOP_N_STORIES=5
CURATION_THRESHOLD=6    # Gemini score 1-10; items below this are dropped
TTS_MAX_CHARS_PER_CHUNK=500   # keep low for reliable F5-TTS chunks

# Co-host Gemini TTS voice (Aoede, Charon, Fenrir, Kore, Puck, Orbit, Zephyr)
GEMINI_TTS_VOICE_2=Aoede

# Audio
SPEECH_TEMPO=1.12           # 1.0 = normal speed, 1.12 = 12% faster
INTRO_MUSIC_DURATION=6.0
OUTRO_MUSIC_DURATION=12.0
```

See `.env.example` for the full list including publishing keys.

---

## Voice Cloning Setup (Host — Alex)

Alex's voice is cloned from a short audio sample using F5-TTS, served via a persistent Modal serverless GPU endpoint.

### One-time setup

**1. Record your voice sample**

Record ~10 seconds of yourself speaking clearly. Save it as an MP3 or WAV file anywhere on your machine. Note the exact words you spoke.

**2. Set the reference audio in `.env`**

```env
F5_REF_AUDIO_PATH=C:\path\to\your_voice_sample.mp3
F5_REF_TEXT=The exact sentence you spoke in that recording.
```

**3. Deploy the Modal TTS server**

```bash
pip install modal
modal token new          # opens browser, sign up free at modal.com
modal deploy modal_tts_server.py
```

Modal prints a permanent URL:
```
Created Web Function URL for TTSServer.run => https://yourname--f5-tts-server-ttsserver-run.modal.run
```

**4. Add that URL to `.env`**

```env
F5_SERVER_URL=https://yourname--f5-tts-server-ttsserver-run.modal.run
```

That's it. The URL never changes. Modal spins up a T4 GPU automatically when the pipeline calls it — no Colab, no manual steps, no session management.

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
| `--dry-run` | Skip TTS API calls; use silent audio placeholders |
| `--publish` | Run Stage 6: upload to R2 and push RSS feed to GitHub |

---

## Project Structure

```
ai-podcast-pipeline/
├── pipeline_runner.py          # Stage 5 — orchestrator
├── config.py                   # All settings via PodcastConfig(BaseSettings)
├── modal_tts_server.py         # F5-TTS serverless GPU endpoint (deploy to Modal)
├── requirements.txt
├── .env.example                # Template for environment variables
├── PUBLISHING_SETUP.md         # Step-by-step Cloudflare R2 + GitHub setup
│
├── modules/
│   ├── content_discovery.py    # Stage 1 — NewsAPI + ArXiv
│   ├── content_curator.py      # Stage 2 — Gemini scoring + filtering
│   ├── script_writer.py        # Stage 3 — Gemini dialogue script
│   ├── tts_generator.py        # Stage 4 — F5-TTS (Alex) + Gemini TTS (Sam)
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
data/raw/YYYY-MM-DD.json        -> Stage 1 guard
data/curated/YYYY-MM-DD.json    -> Stage 2 guard
scripts/YYYY-MM-DD.md           -> Stage 3 guard
audio/episodes/YYYY-MM-DD.mp3  -> Stage 4 guard
data/episodes/YYYY-MM-DD.json  -> Stage 5 guard (completion sentinel)
data/published/YYYY-MM-DD.json -> Stage 6 guard
```

If Stage 4 fails, re-running picks up from Stage 4 — Stages 1–3 are not re-executed. Use `--force` to override.

---

## Cross-Episode Deduplication

`SeenURLStore` (`modules/utils/seen_urls.py`) tracks which story URLs have been used in past episodes. Stories already covered are excluded from discovery automatically. URLs are only marked as seen after a fully successful real episode — failed runs and dry-runs don't consume URLs.

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

Alex's lines are synthesized with your cloned F5-TTS voice via Modal. Sam's lines use Gemini TTS (Aoede). The final episode is: `[6s music intro] -> [dialogue] -> [12s music outro]`.

---

## Music Generation

Intro and outro music are synthesized locally using numpy — no external music assets or APIs required. The track is a minimalist ambient electronic piece in A minor (pad + arpeggio + bass pulse + delay echo). Generated once and cached in `audio/music/`.

---

## Publishing

See `PUBLISHING_SETUP.md` for full step-by-step instructions to configure Cloudflare R2 and GitHub Pages.

Once configured, `--publish` uploads the episode MP3 to R2, generates show notes via Gemini, and pushes an updated `feed.xml` to GitHub. All subscribed platforms (Spotify, Apple Podcasts, Amazon Music, YouTube Music) auto-ingest the new episode within a few hours.

---

## Automating with Windows Task Scheduler

The included `run_podcast.bat` runs the full pipeline and logs output to `logs/scheduler.log`. Schedule it via Task Scheduler to run every Saturday (or daily) at a fixed time.

```
Action: run_podcast.bat
Start in: C:\Users\yourname\Desktop\IA Projects\podcaster
```

---

## Cost

At daily frequency with default settings:

| Service | Usage | Monthly cost |
|---|---|---|
| Gemini 2.5 Flash (text) | ~2000 tokens/episode | ~$0.03 |
| Gemini TTS (Sam only) | ~750 words/episode | ~$0.30–0.60 |
| Modal F5-TTS (Alex, T4 GPU) | ~350s GPU/episode | ~$0.20 |
| NewsAPI | 1 request/day | Free tier |
| ArXiv | 1 request/day | Free |
| Cloudflare R2 | ~10MB/episode | Free tier (10GB) |
| GitHub Pages | RSS feed | Free |

**Estimated total: ~$0.60–1/month at daily frequency.**

Modal provides **$30/month in free compute credits** — covering approximately 5 months of daily episodes before any charges apply.

---

## License

MIT
