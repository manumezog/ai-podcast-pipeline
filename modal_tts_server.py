"""
F5-TTS serverless GPU endpoint on Modal.

Deploy once:
    pip install modal
    modal token new          # opens browser to authenticate
    modal deploy modal_tts_server.py

Modal prints a stable HTTPS URL like:
    https://<workspace>--f5-tts-server-ttsserver-run.modal.run

Paste that URL into your .env as F5_SERVER_URL.

The container stays warm for 10 minutes after the last call, so consecutive
episode chunks are fast (~3-5s each on T4). Cold start (first call of the day)
takes ~30s while the GPU spins up and the model loads.

Cost: ~$0.24/episode on T4. Modal gives generous free monthly credits.
"""

import os

import modal

image = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install("f5-tts", "soundfile", "numpy", "fastapi")
    .run_commands(
        # Bake model weights into the image so cold starts skip the download
        'python -c "from f5_tts.api import F5TTS; F5TTS()"'
    )
)

app = modal.App("f5-tts-server")

MAX_REF_AUDIO_BYTES = 5_000_000   # 5 MB
MAX_GEN_TEXT_CHARS  = 5_000


@app.cls(
    gpu="T4",
    image=image,
    timeout=120,
    scaledown_window=600,  # keep warm 10 min between calls
    secrets=[modal.Secret.from_name("tts-secrets")],
)
class TTSServer:
    @modal.enter()
    def load(self):
        from f5_tts.api import F5TTS
        self.tts = F5TTS()

    @modal.fastapi_endpoint(method="POST")
    def run(self, item: dict, request: "Request"):
        import base64
        import io
        import tempfile

        import numpy as np
        import soundfile as sf
        from fastapi import HTTPException, Request
        from fastapi.responses import Response

        # ── Authentication ────────────────────────────────────────────────
        expected = os.environ.get("TTS_API_KEY", "")
        if not expected or request.headers.get("x-api-key") != expected:
            raise HTTPException(status_code=401, detail="Unauthorized")

        # ── Input validation ──────────────────────────────────────────────
        ref_b64  = item.get("ref_audio", "")
        ref_text = item.get("ref_text", "")
        gen_text = item.get("gen_text", "")

        ref_bytes = base64.b64decode(ref_b64)
        if len(ref_bytes) > MAX_REF_AUDIO_BYTES:
            raise HTTPException(status_code=413, detail="ref_audio too large")
        if len(gen_text) > MAX_GEN_TEXT_CHARS:
            raise HTTPException(status_code=413, detail="gen_text too long")

        # ── Inference ─────────────────────────────────────────────────────
        tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
        try:
            tmp.write(ref_bytes)
            tmp.flush()
            tmp.close()
            wav, sr, _ = self.tts.infer(
                ref_file=tmp.name,
                ref_text=ref_text,
                gen_text=gen_text,
            )
        finally:
            os.unlink(tmp.name)

        buf = io.BytesIO()
        sf.write(buf, np.array(wav), sr, format="WAV")
        buf.seek(0)
        return Response(content=buf.read(), media_type="audio/wav")
