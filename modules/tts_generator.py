"""
Stage 4 — TTS Generation (Google Gemini TTS)

Converts a podcast script to audio. Supports two modes:

DIALOGUE MODE (cfg.enable_dialogue=True, default):
    Detects "HOST:" / "COHOST:" labels in the script and synthesizes each
    speaker's lines with their own Gemini voice (cfg.gemini_tts_voice for
    host 1, cfg.gemini_tts_voice_2 for host 2). Consecutive same-speaker
    lines are grouped into chunks to minimise API calls.

SINGLE-VOICE MODE:
    Falls back to single-voice chunking when dialogue labels are not detected
    or cfg.enable_dialogue=False.

MUSIC (cfg.enable_music=True, default):
    After stitching speech, mixes in generated intro and outro music via
    ffmpeg concatenation. Music files are synthesised once and cached.

AUDIO PIPELINE:
    script -> parse dialogue -> per-speaker WAV chunks -> ffmpeg concat -> speech.mp3
    speech.mp3 + intro.wav + outro.wav -> ffmpeg concat -> final episode.mp3

IDEMPOTENCY:
    Per-chunk WAV files are reused if they exist. The final episode MP3 is
    skipped entirely unless --force is passed.

DRY RUN:
    Skips Gemini API calls; uses silent WAV placeholders. Music is also
    skipped in dry-run mode to avoid codec issues with silent audio.

CLI usage:
    python modules/tts_generator.py --date 2026-06-02
    python modules/tts_generator.py --date 2026-06-02 --force
    python modules/tts_generator.py --date 2026-06-02 --dry-run
"""

from __future__ import annotations

import logging
import re
import shutil
import struct
import subprocess
from datetime import date
from pathlib import Path
from typing import List, Tuple

import click

from config import PodcastConfig, get_config
from modules.utils.retry import retry_with_backoff

logger = logging.getLogger(__name__)


# ── ffmpeg discovery ──────────────────────────────────────────────────────────

def find_ffmpeg(cfg: PodcastConfig | None = None) -> str:
    """
    Resolve the ffmpeg executable path.

    Order: cfg.ffmpeg_path -> PATH -> winget package glob.
    Raises FileNotFoundError with setup instructions if not found.
    """
    if cfg and cfg.ffmpeg_path:
        return cfg.ffmpeg_path
    found = shutil.which("ffmpeg")
    if found:
        return found
    winget_base = Path.home() / "AppData/Local/Microsoft/WinGet/Packages"
    if winget_base.exists():
        candidates = sorted(winget_base.glob("Gyan.FFmpeg_*/*/bin/ffmpeg.exe"))
        if candidates:
            path = str(candidates[-1])
            logger.info("ffmpeg found via winget fallback: %s", path)
            return path
    raise FileNotFoundError(
        "ffmpeg not found. Options:\n"
        "  1. Restart your terminal after 'winget install ffmpeg'\n"
        "  2. Add FFMPEG_PATH=C:\\path\\to\\ffmpeg.exe to your .env"
    )


# ── Script chunking (single-voice) ────────────────────────────────────────────

def chunk_script(text: str, max_chars: int = 4800) -> List[str]:
    """
    Split plain text into chunks for Gemini TTS, never mid-sentence.

    Splits at paragraph boundaries first, then sentence boundaries if a
    paragraph still exceeds max_chars.
    """
    paragraphs = [p.strip() for p in re.split(r"\n\n+", text) if p.strip()]
    chunks: List[str] = []
    current = ""

    for para in paragraphs:
        if len(para) > max_chars:
            if current:
                chunks.append(current)
                current = ""
            chunks.extend(_sentence_chunks(para, max_chars))
        elif not current:
            current = para
        elif len(current) + 2 + len(para) <= max_chars:
            current += "\n\n" + para
        else:
            chunks.append(current)
            current = para

    if current:
        chunks.append(current)
    return chunks


def _sentence_chunks(text: str, max_chars: int) -> List[str]:
    sentences = re.split(r"(?<=[.!?])\s+", text)
    chunks: List[str] = []
    current = ""
    for sent in sentences:
        if not current:
            if len(sent) > max_chars:
                logger.warning("Single sentence exceeds max_chars=%d (len=%d).", max_chars, len(sent))
            current = sent
        elif len(current) + 1 + len(sent) <= max_chars:
            current += " " + sent
        else:
            chunks.append(current)
            current = sent
    if current:
        chunks.append(current)
    return chunks


# ── Dialogue parsing ──────────────────────────────────────────────────────────

def parse_dialogue(
    text: str, host1: str, host2: str
) -> List[Tuple[str, str]]:
    """
    Parse a dialogue-format script into (speaker, text) pairs.

    Expects lines like:
        Alex: First sentence of their turn.
        Sam: Their response here.

    Lines not matching either host name are silently skipped (handles
    occasional Gemini formatting quirks).

    Returns:
        List of (speaker_name, text) tuples in script order.
    """
    segments: List[Tuple[str, str]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        for name in (host1, host2):
            prefix = f"{name}:"
            if line.lower().startswith(prefix.lower()):
                speech = line[len(prefix):].strip()
                if speech:
                    segments.append((name, speech))
                break
    return segments


def group_speaker_segments(
    segments: List[Tuple[str, str]], max_chars: int
) -> List[Tuple[str, str]]:
    """
    Merge consecutive same-speaker segments into chunks up to max_chars.

    Reduces API calls while preserving speaker alternation. A speaker switch
    always starts a new chunk even if the current chunk is short.
    """
    groups: List[Tuple[str, str]] = []
    current_speaker = ""
    current_text = ""

    for speaker, text in segments:
        if speaker == current_speaker:
            candidate = current_text + " " + text
            if len(candidate) <= max_chars:
                current_text = candidate
                continue
            else:
                groups.append((current_speaker, current_text))
                current_text = text
        else:
            if current_text:
                groups.append((current_speaker, current_text))
            current_speaker = speaker
            current_text = text

    if current_text:
        groups.append((current_speaker, current_text))

    return groups


# ── PCM / WAV helpers ─────────────────────────────────────────────────────────

def pcm_to_wav(
    pcm_bytes: bytes,
    sample_rate: int = 24000,
    channels: int = 1,
    bit_depth: int = 16,
) -> bytes:
    """Prepend a WAVE header to raw Gemini TTS PCM bytes."""
    byte_rate = sample_rate * channels * bit_depth // 8
    block_align = channels * bit_depth // 8
    data_size = len(pcm_bytes)
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF", 36 + data_size, b"WAVE",
        b"fmt ", 16, 1, channels, sample_rate,
        byte_rate, block_align, bit_depth,
        b"data", data_size,
    )
    return header + pcm_bytes


def _make_silent_wav(duration_seconds: float = 1.0) -> bytes:
    n_samples = int(duration_seconds * 24000)
    return pcm_to_wav(b"\x00" * (n_samples * 2))


# ── Per-chunk synthesis ───────────────────────────────────────────────────────

def synthesize_chunk_f5_tts(
    text: str,
    chunk_index: int,
    run_date: date,
    cfg: PodcastConfig,
    dry_run: bool = False,
) -> Path:
    """
    Synthesize one text chunk with F5-TTS using the host's cloned voice.

    Two modes selected by cfg.f5_server_url:
      - Remote (Colab/GPU): POSTs {ref_audio_b64, ref_text, gen_text} to the
        Flask server running on your Colab notebook. Fast (~2-5s/chunk on T4).
      - Local CPU: uses the locally installed f5-tts package. Slow but free.

    Falls back to a silent WAV placeholder in dry-run mode.
    """
    chunk_dir = Path(cfg.audio_dir) / "chunks" / str(run_date)
    chunk_dir.mkdir(parents=True, exist_ok=True)

    ext = "wav" if dry_run else "mp3"
    chunk_path = chunk_dir / f"chunk_{chunk_index:03d}_f5.{ext}"

    if chunk_path.exists():
        logger.debug("F5-TTS chunk %03d exists, reusing.", chunk_index)
        return chunk_path

    if dry_run:
        chunk_path.write_bytes(_make_silent_wav())
        return chunk_path

    from pydub import AudioSegment

    if cfg.f5_server_url:
        # ── Remote Colab server ───────────────────────────────────────────
        import base64, httpx

        ref_b64 = base64.b64encode(Path(cfg.f5_ref_audio_path).read_bytes()).decode()

        def _call_remote() -> Path:
            resp = httpx.post(
                cfg.f5_server_url,
                headers={"x-api-key": cfg.f5_api_key},
                json={"ref_audio": ref_b64, "ref_text": cfg.f5_ref_text, "gen_text": text},
                timeout=120,
            )
            resp.raise_for_status()
            segment = AudioSegment.from_file(__import__("io").BytesIO(resp.content))
            segment.export(str(chunk_path), format="mp3")
            return chunk_path

        out = retry_with_backoff(
            _call_remote,
            max_attempts=cfg.retry_max_attempts,
            base_delay=cfg.retry_base_delay,
        )
    else:
        # ── Local CPU inference ───────────────────────────────────────────
        try:
            from f5_tts.api import F5TTS
        except ImportError:
            raise RuntimeError(
                "f5-tts is not installed. Run: pip install f5-tts\n"
                "Or set F5_SERVER_URL in .env to use your Colab GPU server instead."
            )
        import soundfile as sf
        import numpy as np

        tts = F5TTS()

        def _call_local() -> Path:
            wav, sr, _ = tts.infer(
                ref_file=cfg.f5_ref_audio_path,
                ref_text=cfg.f5_ref_text,
                gen_text=text,
            )
            tmp_wav = chunk_path.with_suffix(".tmp.wav")
            sf.write(str(tmp_wav), np.array(wav), sr)
            segment = AudioSegment.from_wav(str(tmp_wav))
            segment.export(str(chunk_path), format="mp3")
            tmp_wav.unlink(missing_ok=True)
            return chunk_path

        out = retry_with_backoff(
            _call_local,
            max_attempts=cfg.retry_max_attempts,
            base_delay=cfg.retry_base_delay,
        )

    logger.info("F5-TTS chunk %03d: %d chars -> %s.", chunk_index, len(text), chunk_path.name)
    return out


def synthesize_chunk(
    text: str,
    chunk_index: int,
    run_date: date,
    cfg: PodcastConfig,
    voice: str | None = None,
    dry_run: bool = False,
) -> Path:
    """
    Synthesize one text chunk with Gemini TTS and save as WAV.

    Args:
        text: Text to synthesize.
        chunk_index: Used in the output filename.
        run_date: Organises output under audio/chunks/YYYY-MM-DD/.
        cfg: Pipeline configuration.
        voice: Override voice name. Defaults to cfg.gemini_tts_voice.
        dry_run: Skip API call; write a silent placeholder WAV.

    Returns:
        Path to the saved .wav file.
    """
    voice = voice or cfg.gemini_tts_voice
    chunk_dir = Path(cfg.audio_dir) / "chunks" / str(run_date)
    chunk_dir.mkdir(parents=True, exist_ok=True)
    # Embed voice in filename so re-runs with different voices don't reuse wrong files
    safe_voice = voice.lower()
    chunk_path = chunk_dir / f"chunk_{chunk_index:03d}_{safe_voice}.wav"

    if chunk_path.exists():
        logger.debug("Chunk %03d (%s) exists, reusing.", chunk_index, voice)
        return chunk_path

    if dry_run:
        chunk_path.write_bytes(_make_silent_wav())
        return chunk_path

    from google import genai
    from google.genai import types

    client = genai.Client(api_key=cfg.google_api_key)

    def _call() -> bytes:
        response = client.models.generate_content(
            model=cfg.gemini_tts_model,
            contents=text,
            config=types.GenerateContentConfig(
                response_modalities=["AUDIO"],
                speech_config=types.SpeechConfig(
                    voice_config=types.VoiceConfig(
                        prebuilt_voice_config=types.PrebuiltVoiceConfig(
                            voice_name=voice
                        )
                    )
                ),
            ),
        )
        pcm_data = response.candidates[0].content.parts[0].inline_data.data
        if isinstance(pcm_data, str):
            import base64
            pcm_data = base64.b64decode(pcm_data)
        return pcm_data

    pcm_bytes = retry_with_backoff(
        _call,
        max_attempts=cfg.retry_max_attempts,
        base_delay=cfg.retry_base_delay,
    )
    wav_bytes = pcm_to_wav(pcm_bytes)
    chunk_path.write_bytes(wav_bytes)
    logger.info("Chunk %03d [%s]: %d chars -> %d bytes WAV.", chunk_index, voice, len(text), len(wav_bytes))
    return chunk_path


# ── Audio stitching ───────────────────────────────────────────────────────────

def stitch_audio_chunks(chunk_paths: List[Path], output_path: Path, cfg: PodcastConfig) -> Path:
    """
    Concatenate WAV chunks into a loudness-normalised MP3 via ffmpeg.

    Uses ffmpeg's concat demuxer + aresample + loudnorm + libmp3lame.
    """
    if not chunk_paths:
        raise ValueError("No chunks to stitch.")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    concat_file = output_path.with_suffix(".concat.txt")

    try:
        concat_file.write_text(
            "\n".join(f"file '{p.resolve().as_posix()}'" for p in chunk_paths),
            encoding="utf-8",
        )
        ffmpeg = find_ffmpeg(cfg)
        # atempo speeds up speech; apad adds 1s silence so the last word
        # is never clipped by the outro music crossfade.
        af_chain = f"atempo={cfg.speech_tempo},apad=pad_dur=1"
        cmd = [
            ffmpeg, "-y",
            "-f", "concat", "-safe", "0",
            "-i", str(concat_file),
            "-af", af_chain,
            "-codec:a", "libmp3lame",
            "-b:a", "128k",
            str(output_path),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            raise RuntimeError(f"ffmpeg stitch failed (exit {result.returncode}):\n{result.stderr[-2000:]}")
        logger.info("Stitched %d chunks -> %s.", len(chunk_paths), output_path)
        return output_path
    finally:
        if concat_file.exists():
            concat_file.unlink()


def _stitch_wav_python(chunk_paths: List[Path], output_path: Path) -> Path:
    """Pure-Python WAV concat — used only in dry-run to avoid MP3 codec issues."""
    import wave

    output_path.parent.mkdir(parents=True, exist_ok=True)
    params = None
    all_frames = b""
    for p in chunk_paths:
        with wave.open(str(p), "rb") as wf:
            if params is None:
                params = wf.getparams()
            all_frames += wf.readframes(wf.getnframes())
    with wave.open(str(output_path), "wb") as wf:
        wf.setparams(params)
        wf.writeframes(all_frames)
    logger.info("Dry-run: stitched %d WAV chunks -> %s.", len(chunk_paths), output_path)
    return output_path


def mix_with_music(
    speech_path: Path,
    output_path: Path,
    cfg: PodcastConfig,
) -> Path:
    """
    Prepend intro music and append outro music to the speech MP3.

    Uses simple ffmpeg concatenation. The generated music tracks are shaped
    with fades so the joins with speech sound seamless without complex mixing.

    Args:
        speech_path: Path to the speech-only MP3.
        output_path: Destination for the final episode MP3.
        cfg: Pipeline configuration.

    Returns:
        output_path on success. Returns speech_path unchanged if music
        generation fails (non-fatal fallback).
    """
    from modules.music_generator import ensure_music_files

    try:
        intro_path, outro_path = ensure_music_files(cfg)
    except Exception as exc:
        logger.warning("Music generation failed (%s) — producing speech-only episode.", exc)
        import shutil as sh
        sh.copy2(speech_path, output_path)
        return output_path

    try:
        ffmpeg = find_ffmpeg(cfg)
        # Use concat FILTER (not demuxer) so each input is decoded independently.
        # The demuxer approach corrupts MP3 when mixed with WAV inputs.
        filter_complex = (
            "[0:a]aresample=44100[a0];"
            "[1:a]aresample=44100[a1];"
            "[2:a]aresample=44100[a2];"
            "[a0][a1][a2]concat=n=3:v=0:a=1[mixed];"
            "[mixed]loudnorm[out]"
        )
        cmd = [
            ffmpeg, "-y",
            "-i", str(intro_path),
            "-i", str(speech_path),
            "-i", str(outro_path),
            "-filter_complex", filter_complex,
            "-map", "[out]",
            "-codec:a", "libmp3lame",
            "-b:a", "128k",
            str(output_path),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if result.returncode != 0:
            raise RuntimeError(f"ffmpeg music mix failed:\n{result.stderr[-1000:]}")
        logger.info("Music mix complete -> %s.", output_path)
        return output_path
    except Exception as exc:
        logger.warning("Music mixing failed (%s) — using speech-only.", exc)
        import shutil as sh
        sh.copy2(speech_path, output_path)
        return output_path


# ── Main stage entry point ────────────────────────────────────────────────────

def run_tts_generator(
    script_path: Path,
    run_date: date,
    cfg: PodcastConfig,
    force: bool = False,
    dry_run: bool = False,
) -> Path:
    """
    Main entry point for Stage 4.

    Automatically uses dialogue mode if the script contains speaker labels
    and cfg.enable_dialogue is True. Adds music if cfg.enable_music is True
    and dry_run is False.

    Returns:
        Path to the final episode .mp3 file.
    """
    output_path = Path(cfg.audio_dir) / "episodes" / f"{run_date}.mp3"

    if output_path.exists() and not force:
        logger.info("Stage 4: output exists at %s, skipping.", output_path)
        return output_path

    script_text = script_path.read_text(encoding="utf-8")

    # Detect dialogue vs single-voice
    dialogue_segments = []
    if cfg.enable_dialogue:
        dialogue_segments = parse_dialogue(script_text, cfg.host_name, cfg.host_name_2)

    if dialogue_segments:
        groups = group_speaker_segments(dialogue_segments, cfg.tts_max_chars_per_chunk)
        logger.info(
            "Stage 4: dialogue mode — %d speaker groups (%s/%s), dry_run=%s.",
            len(groups), cfg.host_name, cfg.host_name_2, dry_run,
        )
        chunk_paths: List[Path] = []
        use_f5 = bool(cfg.f5_ref_audio_path and cfg.f5_ref_text)
        for i, (speaker, text) in enumerate(groups):
            if speaker == cfg.host_name and use_f5:
                chunk_path = synthesize_chunk_f5_tts(text, i, run_date, cfg, dry_run=dry_run)
            else:
                voice = cfg.gemini_tts_voice_2 if speaker == cfg.host_name_2 else cfg.gemini_tts_voice
                chunk_path = synthesize_chunk(text, i, run_date, cfg, voice=voice, dry_run=dry_run)
            chunk_paths.append(chunk_path)
            logger.info("Stage 4: group %d/%d [%s] complete.", i + 1, len(groups), speaker)
    else:
        chunks = chunk_script(script_text, max_chars=cfg.tts_max_chars_per_chunk)
        logger.info("Stage 4: single-voice mode — %d chunks, dry_run=%s.", len(chunks), dry_run)
        chunk_paths = []
        use_f5 = bool(cfg.f5_ref_audio_path and cfg.f5_ref_text)
        for i, text in enumerate(chunks):
            if use_f5:
                chunk_path = synthesize_chunk_f5_tts(text, i, run_date, cfg, dry_run=dry_run)
            else:
                chunk_path = synthesize_chunk(text, i, run_date, cfg, dry_run=dry_run)
            chunk_paths.append(chunk_path)
            logger.info("Stage 4: chunk %d/%d complete.", i + 1, len(chunks))

    # Stitch speech
    if dry_run:
        speech_path = output_path.with_name(f"{run_date}_speech.mp3")
        _stitch_wav_python(chunk_paths, speech_path)
        # In dry-run we skip music (silent audio + loudnorm = LAME crash)
        import shutil as sh
        sh.copy2(speech_path, output_path)
        return output_path

    speech_path = output_path.with_name(f"{run_date}_speech.mp3")
    stitch_audio_chunks(chunk_paths, speech_path, cfg)

    # Mix with music
    if cfg.enable_music:
        mix_with_music(speech_path, output_path, cfg)
    else:
        import shutil as sh
        sh.copy2(speech_path, output_path)

    # Clean up intermediate speech file
    if speech_path.exists() and speech_path != output_path:
        speech_path.unlink()

    return output_path


@click.command()
@click.option("--date", "run_date_str", default=None, help="Episode date YYYY-MM-DD.")
@click.option("--force", is_flag=True, default=False)
@click.option("--dry-run", is_flag=True, default=False, help="Skip Gemini API calls.")
def cli(run_date_str: str | None, force: bool, dry_run: bool) -> None:
    """Run Stage 4 TTS generation as a standalone command."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    run_date = date.fromisoformat(run_date_str) if run_date_str else date.today()
    cfg = get_config()
    script_path = Path(cfg.scripts_dir) / f"{run_date}.md"
    if not script_path.exists():
        print(f"No Stage 3 output at {script_path}. Run script_writer.py first.")
        raise SystemExit(1)
    audio_path = run_tts_generator(script_path, run_date, cfg, force=force, dry_run=dry_run)
    print(f"Episode audio: {audio_path}")


if __name__ == "__main__":
    cli()
