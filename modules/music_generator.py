"""
Ambient music generator for podcast intro and outro.

Synthesizes a minimalist electronic ambient track in A minor using pure Python
and numpy — no external audio assets or APIs required.

Character: Intelligent, slightly mysterious, forward-looking. Appropriate for
a tech/science podcast.

Layers:
  - Pad: Detuned A minor chord (A2, C3, E3) with slow LFO tremolo
  - Arpeggio: Plucked notes in A minor pentatonic with dotted-16th delay echo
  - Bass: Low A1 (55Hz) with slow pulse envelope
  - Space: Delay echo on the full mix

Generated files are cached — regenerated only if missing.

Usage:
    from modules.music_generator import ensure_music_files
    intro_path, outro_path = ensure_music_files(cfg)
"""

from __future__ import annotations

import logging
import wave
from pathlib import Path

import numpy as np

from config import PodcastConfig

logger = logging.getLogger(__name__)

SR = 44100  # sample rate


# ── Low-level synthesis primitives ───────────────────────────────────────────

def _osc(freq_amp_pairs: list[tuple[float, float]], duration: float) -> np.ndarray:
    """Sum of sine oscillators."""
    n = int(SR * duration)
    t = np.arange(n, dtype=np.float64) / SR
    out = np.zeros(n)
    for freq, amp in freq_amp_pairs:
        out += amp * np.sin(2 * np.pi * freq * t)
    return out


def _lfo(rate: float, depth: float, center: float, duration: float) -> np.ndarray:
    """Low-frequency oscillator returning a modulator array."""
    n = int(SR * duration)
    t = np.arange(n, dtype=np.float64) / SR
    return center + depth * np.sin(2 * np.pi * rate * t)


def _pluck(freq: float, duration_samples: int) -> np.ndarray:
    """Single plucked note: sine with exponential decay."""
    t = np.arange(duration_samples, dtype=np.float64) / SR
    decay = np.exp(-7.0 * t / max(duration_samples / SR, 1e-9))
    return np.sin(2 * np.pi * freq * t) * decay


def _delay(sig: np.ndarray, ms: float, feedback: float) -> np.ndarray:
    """Multi-tap delay echo for spaciousness."""
    d = int(ms * SR / 1000)
    out = sig.copy()
    for k in range(1, 5):
        offset = k * d
        if offset >= len(sig):
            break
        echo = np.zeros_like(sig)
        echo[offset:] = sig[: len(sig) - offset] * (feedback**k)
        out += echo
    return out


def _fade(sig: np.ndarray, fade_in: float, fade_out: float) -> np.ndarray:
    n = len(sig)
    fi = min(int(fade_in * SR), n // 2)
    fo = min(int(fade_out * SR), n // 2)
    sig = sig.copy()
    if fi:
        sig[:fi] *= np.linspace(0.0, 1.0, fi)
    if fo:
        sig[-fo:] *= np.linspace(1.0, 0.0, fo)
    return sig


def _normalise(sig: np.ndarray, target: float = 0.72) -> np.ndarray:
    peak = np.max(np.abs(sig))
    return sig / peak * target if peak > 1e-9 else sig


def _save_wav(sig: np.ndarray, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pcm = (np.clip(sig, -1.0, 1.0) * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SR)
        wf.writeframes(pcm.tobytes())


# ── Track builder ─────────────────────────────────────────────────────────────

def _build_track(
    duration: float,
    arp_notes: list[float],
    arp_note_len_s: float,
    arp_start_s: float,
    arp_end_offset_s: float,
    fade_in: float,
    fade_out: float,
) -> np.ndarray:
    """
    Shared synthesis engine for intro and outro.

    A minor chord pad + arpeggiated melody + bass pulse, mixed and spaced
    with delay echo, then shaped with the given fade envelope.
    """
    n = int(SR * duration)

    # ── Pad: A minor chord (A2=110, C3=130.81, E3=164.81 Hz) ──────────────
    # Slight detuning between oscillators creates a natural chorus/ensemble feel
    pad = (
        _osc([(110.00, 0.30), (110.18, 0.10), (220.00, 0.14)], duration)   # A2
        + _osc([(130.81, 0.25), (131.05, 0.09), (261.62, 0.11)], duration)  # C3
        + _osc([(164.81, 0.20), (165.10, 0.07), (329.62, 0.09)], duration)  # E3
    )
    # Slow tremolo (1.1 Hz) for gentle movement
    pad *= _lfo(rate=1.1, depth=0.14, center=0.86, duration=duration)

    # ── Arpeggio ──────────────────────────────────────────────────────────
    note_samples = int(arp_note_len_s * SR)
    arp_start = int(arp_start_s * SR)
    arp_end = int((duration - arp_end_offset_s) * SR)
    arp = np.zeros(n)
    pos, note_i = arp_start, 0
    while pos < arp_end:
        freq = arp_notes[note_i % len(arp_notes)]
        chunk_len = min(note_samples, arp_end - pos)
        arp[pos : pos + chunk_len] = 0.20 * _pluck(freq, chunk_len)
        pos += note_samples
        note_i += 1
    # Dotted-16th delay (187 ms at ~90 BPM) gives a rhythmic echo feel
    arp = _delay(arp, ms=187.0, feedback=0.38)

    # ── Bass pulse: A1 (55 Hz) with slow 0.45 Hz LFO ─────────────────────
    bass = _osc([(55.0, 1.0), (110.0, 0.28)], duration)
    bass *= _lfo(rate=0.45, depth=0.28, center=0.72, duration=duration) * 0.26

    # ── Mix ───────────────────────────────────────────────────────────────
    mix = pad * 0.52 + arp * 0.65 + bass * 0.55
    mix = _delay(mix, ms=260.0, feedback=0.22)
    mix = _fade(mix, fade_in=fade_in, fade_out=fade_out)
    return _normalise(mix)


# ── Public interface ──────────────────────────────────────────────────────────

def generate_intro(duration: float = 20.0) -> np.ndarray:
    """
    Intro music: fades in over 2s, active through the middle, fades out over
    3s. Designed to be placed before the episode speech — by the end it is
    near-silent so a simple concat with the speech sounds seamless.
    """
    # Ascending arpeggio feel (A minor pentatonic going up)
    arp_notes = [220.0, 261.63, 329.63, 392.0, 440.0, 523.25, 440.0, 392.0]
    return _build_track(
        duration=duration,
        arp_notes=arp_notes,
        arp_note_len_s=0.22,
        arp_start_s=2.0,
        arp_end_offset_s=3.5,
        fade_in=2.0,
        fade_out=3.5,
    )


def generate_outro(duration: float = 18.0) -> np.ndarray:
    """
    Outro music: begins near-silence, builds gently, then fades out over 3s.
    Designed to be placed after the speech — the near-silent start makes the
    concat from speech sound like a natural fade-to-music resolution.
    """
    # Descending arpeggio feel (mirrors intro, creates a bookend sense)
    arp_notes = [440.0, 392.0, 329.63, 261.63, 220.0, 261.63, 329.63, 392.0]
    return _build_track(
        duration=duration,
        arp_notes=arp_notes,
        arp_note_len_s=0.28,
        arp_start_s=3.0,
        arp_end_offset_s=3.0,
        fade_in=3.5,
        fade_out=3.0,
    )


def ensure_music_files(cfg: PodcastConfig) -> tuple[Path, Path]:
    """
    Return (intro_path, outro_path), generating and caching them if needed.

    Files are saved to audio/music/intro.wav and audio/music/outro.wav.
    Regenerated only if missing — safe to call on every pipeline run.
    """
    music_dir = Path(cfg.audio_dir) / "music"
    intro_path = music_dir / "intro.wav"
    outro_path = music_dir / "outro.wav"

    if not intro_path.exists():
        logger.info("Generating intro music (%.0fs)...", cfg.intro_music_duration)
        _save_wav(generate_intro(cfg.intro_music_duration), intro_path)
        logger.info("Intro music saved: %s", intro_path)

    if not outro_path.exists():
        logger.info("Generating outro music (%.0fs)...", cfg.outro_music_duration)
        _save_wav(generate_outro(cfg.outro_music_duration), outro_path)
        logger.info("Outro music saved: %s", outro_path)

    return intro_path, outro_path
