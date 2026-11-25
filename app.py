import os
import subprocess
import sys

import uvicorn


def ensure_playwright_chromium() -> None:
    """Install Playwright's Chromium binary if it is not already present."""
    subprocess.run(
        [sys.executable, "-m", "playwright", "install", "chromium"],
        check=False,
    )


if __name__ == "__main__":
    ensure_playwright_chromium()
    port = int(os.environ.get("PORT", "7860"))
    uvicorn.run("main:app", host="0.0.0.0", port=port)
