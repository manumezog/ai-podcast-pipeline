"""
Pushes pipeline run status to GitHub after each scheduled run.
Called automatically by run_podcast.bat — not intended for manual use.

Usage: python push_status.py <exit_code>
"""

import sys
import json
import base64
import httpx
from datetime import date, datetime, timezone
from pathlib import Path


def push_status(exit_code: int) -> None:
    sys.path.insert(0, str(Path(__file__).parent))
    from config import get_config
    cfg = get_config()

    success = exit_code == 0

    # Read last 3000 chars of log for context
    log_path = Path(cfg.logs_dir) / "scheduler.log"
    log_tail = ""
    if log_path.exists():
        text = log_path.read_text(encoding="utf-8", errors="ignore")
        log_tail = text[-3000:] if len(text) > 3000 else text

    status = {
        "last_run": datetime.now(timezone.utc).isoformat(),
        "success": success,
        "exit_code": exit_code,
        "episode_date": str(date.today()),
        "log_tail": log_tail,
    }

    headers = {
        "Authorization": f"Bearer {cfg.github_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    api_url = "https://api.github.com/repos/manumezog/ai-podcast-pipeline/contents/status/latest.json"

    # Get existing SHA if file already exists
    sha = None
    r = httpx.get(api_url, headers=headers)
    if r.status_code == 200:
        sha = r.json()["sha"]

    content = base64.b64encode(json.dumps(status, indent=2).encode()).decode()
    body: dict = {
        "message": f"Pipeline status {date.today()} — {'OK' if success else 'FAILED'}",
        "content": content,
    }
    if sha:
        body["sha"] = sha

    r2 = httpx.put(api_url, headers=headers, json=body)
    if r2.status_code in (200, 201):
        print(f"Status pushed to GitHub: {'SUCCESS' if success else 'FAILED'}")
    else:
        print(f"Failed to push status: {r2.status_code} {r2.text[:200]}")


if __name__ == "__main__":
    code = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    push_status(code)
