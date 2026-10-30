#!/usr/bin/env python3
"""Open Firefox with the bot's profile so you can log in once.

The service is headless and cannot type credentials for you. Run this on a
machine with a display (or over X11/VNC), log in to Mostaql and Nafezly, solve
any Cloudflare check, then close the browser window. The session cookies are
written to the profile directory and reused by every later run.

    DISPLAY=:0 ./venv/bin/python login.py

Run it as the same user that runs the service, otherwise the profile files end
up owned by the wrong account and Firefox refuses to start.
"""

import os
import sys
import time
from pathlib import Path

from selenium import webdriver
from selenium.webdriver.firefox.options import Options

DATA_DIR = Path(os.environ.get("LOKI_DATA_DIR", "/opt/freelance-autobid/data"))
PROFILE = Path(os.environ.get("LOKI_PROFILE_DIR", DATA_DIR / "test-profile"))
FIREFOX_BINARY = os.environ.get("LOKI_FIREFOX_BINARY", "/usr/bin/firefox-esr")

SITES = [
    ("Mostaql", "https://mostaql.com/login"),
    ("Nafezly", "https://nafezly.com/login"),
]


def main() -> int:
    if not Path(FIREFOX_BINARY).exists():
        print(f"Firefox not found at {FIREFOX_BINARY}", file=sys.stderr)
        return 1

    # Firefox refuses to start against a profile another process owns.
    lock = PROFILE / "lock"
    if lock.is_symlink():
        print(f"refusing to start: {lock} exists, is the service still running?")
        print("stop it first:  sudo systemctl stop freelance-autobid")
        return 1

    PROFILE.mkdir(parents=True, exist_ok=True)

    options = Options()
    options.binary_location = FIREFOX_BINARY
    options.add_argument("-profile")
    options.add_argument(str(PROFILE))

    driver = webdriver.Firefox(options=options)
    try:
        for name, url in SITES:
            print(f"  {name}: {url}")
            driver.get(url)
        print()
        print("Log in to both sites in the Firefox window.")
        print("When you are done, close the window (or press Ctrl+C here).")
        while True:
            time.sleep(2)
            if not driver.window_handles:
                break
    except KeyboardInterrupt:
        pass
    finally:
        try:
            driver.quit()
        except Exception:
            pass
    print("profile saved:", PROFILE)
    return 0


if __name__ == "__main__":
    sys.exit(main())
