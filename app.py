import os
import subprocess
import sys

import gradio as gr
import uvicorn

from main import app as fastapi_app


def ensure_playwright_chromium() -> None:
    """Install Playwright's Chromium binary if it is not already present."""
    subprocess.run(
        [sys.executable, "-m", "playwright", "install", "chromium"],
        check=False,
    )


with gr.Blocks() as demo:
    gr.Markdown("PDFY API")

# Mount minimal Gradio app at /gradio so Hugging Face detects Gradio while / serves PDFY PWA
app = gr.mount_gradio_app(fastapi_app, demo, path="/gradio")


if __name__ == "__main__":
    ensure_playwright_chromium()
    port = int(os.environ.get("PORT", "7860"))
    uvicorn.run(app, host="0.0.0.0", port=port)
