"""Compatibility launcher for the unified workbench's live page."""

import argparse
import subprocess
import sys
import webbrowser

from render_web.config import Config


def main():
    parser = argparse.ArgumentParser(description="CR workbench live match page")
    parser.add_argument("--open", action="store_true")
    args = parser.parse_args()
    subprocess.run([sys.executable, "-m", "render_web", "start", "--no-browser"], check=True)
    url = f"http://127.0.0.1:{Config.load().port}/live/"
    print(url, flush=True)
    if args.open:
        webbrowser.open(url)


if __name__ == "__main__":
    main()
