import hmac
import json
import os
import re
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urljoin, urlparse
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel
from selenium import webdriver
from selenium.webdriver.firefox.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from starlette.concurrency import run_in_threadpool

API_KEY = os.environ["API_KEY"]

# Hard safety gate for real bids. The workflow can request a submission,
# but nothing is clicked unless the host service explicitly allows it.
ALLOW_SUBMIT = os.environ.get("ALLOW_SUBMIT", "0").strip().lower() in {
    "1", "true", "yes", "on",
}

# The host runs a single Firefox ESR profile, so only one browser session
# may exist at a time. Workflow inspects and real bids queue behind this.
BROWSER_LOCK = threading.Lock()

# Cloudflare / bot-check interstitials. When one of these is on screen the
# real page has not loaded yet, so every lookup below would see an empty DOM.
CHALLENGE_TITLES = (
    "just a moment",
    "attention required",
    "checking your browser",
    "please wait",
)
CHALLENGE_TEXT = (
    "performing security verification",
    "checking your browser",
    "verify you are human",
    "enable javascript and cookies to continue",
)

def _clear_stale_profile_lock():
    """Remove a leftover Firefox lock when no process owns it.

    Firefox writes ``lock`` as a symlink to ``<host>:+<pid>``. A crash, or a
    manual probe run as another user, leaves it behind and every later
    session fails with "Failed to set preferences".
    """
    lock = PROFILE / "lock"
    if not lock.is_symlink():
        return

    pid = None
    try:
        target = os.readlink(lock)
        if "+" in target:
            pid = int(target.rsplit("+", 1)[1])
    except (OSError, ValueError):
        pid = None

    if pid is not None and os.path.exists(f"/proc/{pid}"):
        return

    for name in ("lock", ".parentlock"):
        try:
            (PROFILE / name).unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass


def _run_exclusive(func, *args, timeout=120):
    """Run ``func`` under the shared Firefox profile lock.

    Waits up to ``timeout`` seconds for a free slot instead of failing with
    a WebDriver error; returns a structured ``busy`` result on timeout.
    """
    if not BROWSER_LOCK.acquire(timeout=timeout):
        return {
            "status": "busy",
            "submitted": False,
            "reason": (
                "Another Firefox session is still running; this request "
                "did not start a browser."
            ),
        }
    try:
        _clear_stale_profile_lock()
        return func(*args)
    finally:
        BROWSER_LOCK.release()

# Where the persistent Firefox profile, screenshots and portfolio images live.
# Point DATA_DIR at any directory the service user can write to.
DATA_DIR = Path(os.environ.get("LOKI_DATA_DIR", "/srv/docker/loki-playwright"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

PROFILE = Path(os.environ.get("LOKI_PROFILE_DIR", DATA_DIR / "test-profile"))
SCREENSHOTS = Path(os.environ.get("LOKI_SCREENSHOT_DIR", DATA_DIR / "screenshots"))
SCREENSHOTS.mkdir(parents=True, exist_ok=True)

# Portfolio images attached to every offer. Drop exactly four images in this
# folder and they are attached automatically; no workflow change needed.
ATTACHMENTS_DIR = Path(os.environ.get("LOKI_ATTACHMENTS_DIR", DATA_DIR / "attachments"))
ATTACHMENTS_DIR.mkdir(parents=True, exist_ok=True)

# Firefox binary. On Debian/Ubuntu with the ESR package this is the default.
FIREFOX_BINARY = os.environ.get("LOKI_FIREFOX_BINARY", "/usr/bin/firefox-esr")
ATTACHMENT_COUNT = 4
ALLOWED_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}
MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024
ATTACHMENTS_REQUIRED = os.environ.get("ATTACHMENTS_REQUIRED", "1").strip().lower() in {
    "1", "true", "yes", "on",
}

# Mostaql's bid form runs preventUrlsValidator() and min_length[200] on the
# proposal. The validator rejects any scheme, any e-mail, and any bare
# "word.tld" token (it treats every two-letter TLD as a domain). Catching that
# here turns a doomed submit click into a clear alert.
MOSTAQL_MIN_PROPOSAL = 200
MOSTAQL_MAX_PROPOSAL = 10000
PROPOSAL_URL_PATTERN = re.compile(
    r"(?:https?://|ftp://|www\.)"
    r"|[\w.+-]+@[\w-]+(?:\.[\w-]+)+"
    r"|\b[\w-]+(?:\.[\w-]+)*\.(?:"
    r"com|net|org|edu|gov|mil|int|info|biz|pro|name|mobi|asia|jobs|travel|"
    r"cat|coop|aero|museum|tel|post|local|dev|app|site|online|link|xyz|"
    r"io|co|me|ai|ly|tv|cc|ws|to|sh|gg|so|fm|am|"
    r"sa|eg|ae|kw|qa|bh|om|jo|lb|ps|iq|ye|sd|ma|tn|dz|"
    r"uk|us|de|fr|tr|in|pk|ng|ke|za|ru|cn|jp|kr|br|ca|au|es|it|nl|se|no|fi|"
    r"dk|pl|pt|gr|ch|at|cz|ro|hu|bg|ua|ir"
    r")\b",
    re.IGNORECASE,
)


def _mostaql_proposal_problems(proposal):
    """Return the reasons Mostaql would reject this proposal, if any."""
    problems = []
    if len(proposal) < MOSTAQL_MIN_PROPOSAL:
        problems.append(
            f"Mostaql needs at least {MOSTAQL_MIN_PROPOSAL} characters "
            f"and this proposal has {len(proposal)}"
        )
    if len(proposal) > MOSTAQL_MAX_PROPOSAL:
        problems.append(
            f"Mostaql allows at most {MOSTAQL_MAX_PROPOSAL} characters "
            f"and this proposal has {len(proposal)}"
        )
    match = PROPOSAL_URL_PATTERN.search(proposal)
    if match:
        problems.append(
            "Mostaql forbids links, e-mails, and domain names in a proposal "
            f"(found {match.group(0)!r})"
        )
    if "<" in proposal or ">" in proposal:
        problems.append("Mostaql rejects proposals containing '<' or '>'")
    return problems


def _resolve_attachments(raw):
    """Return the four images to attach to an offer.

    ``raw`` may be a JSON array, a newline/comma separated list, or empty.
    Empty means "use every image in ATTACHMENTS_DIR", so replacing the photos
    there changes what every later offer attaches.
    """
    if raw and str(raw).strip():
        text = str(raw).strip()
        parsed = None
        try:
            parsed = json.loads(text)
        except (ValueError, TypeError):
            parsed = None
        if isinstance(parsed, list):
            items = [str(item) for item in parsed]
        else:
            items = [part.strip() for part in re.split(r"[,\n]", text) if part.strip()]
    else:
        items = sorted(
            str(path)
            for path in ATTACHMENTS_DIR.iterdir()
            if path.is_file() and path.suffix.lower() in ALLOWED_IMAGE_SUFFIXES
        )

    root = ATTACHMENTS_DIR.resolve()
    resolved = []
    for item in items:
        path = Path(item).expanduser()
        try:
            path = path.resolve()
        except OSError as exc:
            raise HTTPException(
                status_code=400,
                detail=f"Attachment path cannot be resolved: {item} ({exc})",
            )
        if root not in path.parents:
            raise HTTPException(
                status_code=400,
                detail=f"Attachment must live inside {root}: {item}",
            )
        if not path.is_file():
            raise HTTPException(status_code=400, detail=f"Attachment not found: {item}")
        if path.suffix.lower() not in ALLOWED_IMAGE_SUFFIXES:
            raise HTTPException(
                status_code=400, detail=f"Unsupported attachment type: {path.name}"
            )
        if path.stat().st_size > MAX_ATTACHMENT_BYTES:
            raise HTTPException(
                status_code=400, detail=f"Attachment is larger than 10 MB: {path.name}"
            )
        resolved.append(path)

    if len(resolved) != ATTACHMENT_COUNT:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Exactly {ATTACHMENT_COUNT} attachments are required, "
                f"found {len(resolved)} ({root})"
            ),
        )
    return resolved


def _attach_files(driver, form, paths):
    """Upload the portfolio images through the platform's own file widget.

    Both platforms hide the file input behind an "attach files" toggle and
    upload asynchronously, then record the server-side result in a hidden
    field. The field name differs per platform:

    * Nafezly uses a fileuploader widget and publishes a JSON list in
      ``input[name="fileuploader-list-file"]``.
    * Mostaql uses a dropzone widget that posts to ``/ajax/upload`` and
      publishes the uploaded files in ``input[name="files"]`` (``#uploadedFiles``),
      with one ``.dz-preview.dz-success`` element per finished upload. Dropzone
      ignores the form's fallback file input and listens to its own hidden
      input, which it appends to ``<body>`` at init.

    Nothing is submitted until one of those widgets confirms a server-side
    entry per image.
    """
    info = {
        "requested": len(paths),
        "uploaded": 0,
        "supported": False,
        "names": [],
    }
    if not paths:
        info["supported"] = None
        return info

    info["supported"] = True
    field = None
    widget = None

    # Mostaql-style dropzone: the widget ignores the form's fallback
    # ``input[type=file]`` and listens to its own hidden input, which Dropzone
    # creates and appends to <body> when it initialises. Point Selenium at that
    # exact input, otherwise the files are silently dropped on the floor.
    dropzone_probe = None
    try:
        dropzone_probe = driver.execute_script(
            """
            const form = arguments[0];
            const findDz = () => form.querySelector('[data-ui="dropzone"]')
                || form.querySelector('.dropzone[data-input-name="files"]')
                || form.querySelector('.dz-message')?.closest('.dropzone')
                || form.querySelector('.dropzone');
            let dz = findDz();
            if (dz && !dz.dropzone && typeof initDropZone === 'function'
                && window.Mostaql && Mostaql.dropzoneConfig) {
                initDropZone(Mostaql.dropzoneConfig);
            }
            dz = findDz();
            if (!dz) return null;
            const inst = dz.dropzone;
            const input = inst && (inst.hiddenFileInput
                || inst.element.querySelector('input.dz-hidden-input'));
            if (!input) return {error: 'dropzone has no hidden input'};
            input.id = 'loki-upload-input';
            input.style.visibility = 'visible';
            input.style.opacity = '1';
            input.style.position = 'fixed';
            input.style.top = '0px';
            input.style.left = '0px';
            input.style.height = '1px';
            input.style.width = '1px';
            return {
                ok: true,
                maxFiles: inst.options ? inst.options.maxFiles : null,
                previews: dz.querySelectorAll('.dz-preview').length
            };
            """,
            form,
        )
    except Exception as exc:
        dropzone_probe = {"error": str(exc)}

    if dropzone_probe and dropzone_probe.get("ok"):
        field = driver.find_element("css selector", "input#loki-upload-input")
        widget = "dropzone-hidden-input"
    else:
        try:
            inputs = form.find_elements("css selector", "input[type=file]")
        except Exception:
            inputs = []
        if not inputs:
            info["supported"] = False
            info["reason"] = (
                "No file input or dropzone inside the offer form."
                + (f" ({dropzone_probe.get('error')})" if dropzone_probe else "")
            )
            return info
        field = inputs[0]
        widget = "file-input"
        # Reveal the input so Firefox accepts the file selection.
        try:
            driver.execute_script(
                """
                const input = arguments[0];
                let node = input;
                for (let i = 0; i < 8 && node; i++) {
                    if (node.style && node.style.display === 'none') {
                        node.style.display = 'block';
                    }
                    node = node.parentElement;
                }
                input.style.opacity = '1';
                input.style.position = 'static';
                input.style.height = '1px';
                input.style.width = '1px';
                """,
                field,
            )
        except Exception:
            pass

    info["widget"] = widget
    try:
        field.send_keys("\n".join(str(path) for path in paths))
    except Exception as exc:
        info["reason"] = f"Could not hand the images to the {widget} widget: {exc}"
        return info

    state = {}
    deadline = time.time() + 120
    while time.time() < deadline:
        try:
            state = driver.execute_script(
                """
                const root = arguments[0] || document;
                const local = (sel) => root.querySelector(sel);
                const dz = local('[data-ui="dropzone"]')
                    || local('.dropzone[data-input-name="files"]')
                    || local('.dz-message')?.closest('.dropzone')
                    || local('.dropzone');
                const scope = dz || root;

                // Nafezly: fileuploader publishes a JSON list here.
                const nafezlyField = local('input[name="fileuploader-list-file"]');
                let nafezlyList = [];
                if (nafezlyField && nafezlyField.value) {
                    try { nafezlyList = JSON.parse(nafezlyField.value) || []; } catch (e) {}
                }

                // Mostaql: dropzone records finished uploads in a hidden field,
                // as a "/"-joined list of server-side names.
                const dropzoneField = local('input[name="files"], #uploadedFiles')
                    || document.querySelector('input[name="files"], #uploadedFiles');
                const rawDropzone = dropzoneField && dropzoneField.value
                    ? String(dropzoneField.value).trim()
                    : "";
                let dropzoneList = [];
                if (rawDropzone) {
                    try {
                        const parsed = JSON.parse(rawDropzone);
                        if (Array.isArray(parsed)) dropzoneList = parsed;
                        else if (parsed && typeof parsed === "object") dropzoneList = [parsed];
                    } catch (e) {
                        dropzoneList = rawDropzone
                            .split(/[,\\/]+/)
                            .map((v) => v.trim())
                            .filter(Boolean)
                            .map((v) => ({ file: v }));
                    }
                }

                return {
                    nafezly: nafezlyList,
                    dropzone: dropzoneList,
                    dropzoneRaw: rawDropzone,
                    dropzonePreviews: scope.querySelectorAll('.dz-preview').length,
                    dropzoneSuccess: scope.querySelectorAll('.dz-preview.dz-success').length,
                    dropzoneErrors: scope.querySelectorAll('.dz-preview.dz-error').length,
                    items: root.querySelectorAll('.fileuploader-item').length,
                    text: ((local('.fileuploader-items-list') || dz || {})
                        .innerText || '').slice(0, 800)
                };
                """,
                form,
            )
        except Exception:
            time.sleep(2)
            continue

        nafezly_done = [
            entry for entry in state.get("nafezly") or []
            if isinstance(entry, dict) and (entry.get("file") or entry.get("name"))
        ]
        if len(nafezly_done) >= len(paths):
            info["uploaded"] = len(nafezly_done)
            info["names"] = [
                entry.get("name") or Path(str(entry.get("file"))).name
                for entry in nafezly_done
            ]
            info["list"] = nafezly_done
            info["widget"] = "fileuploader"
            return info

        dropzone_done = [
            entry for entry in state.get("dropzone") or []
            if isinstance(entry, dict) and (entry.get("file") or entry.get("name"))
        ]
        if len(dropzone_done) >= len(paths):
            info["uploaded"] = len(dropzone_done)
            info["names"] = [
                entry.get("name")
                or Path(str(entry.get("file") or entry.get("id"))).name
                for entry in dropzone_done
            ]
            info["list"] = dropzone_done
            info["widget"] = "dropzone-field"
            return info

        # Last resort: trust the widget's own success previews, but never when
        # it is reporting an error for any of the images.
        preview_done = int(state.get("dropzoneSuccess") or 0)
        preview_errors = int(state.get("dropzoneErrors") or 0)
        if preview_done >= len(paths) and preview_errors == 0:
            info["uploaded"] = preview_done
            info["names"] = [Path(str(path)).name for path in paths]
            info["list"] = [{"file": Path(str(path)).name} for path in paths]
            info["widget"] = "dropzone-preview"
            return info

        time.sleep(2)

    entries = [
        entry
        for entry in (state.get("nafezly") or state.get("dropzone") or [])
        if isinstance(entry, dict) and (entry.get("file") or entry.get("name"))
    ]
    info["uploaded"] = len(entries)
    info["list"] = entries
    info["reason"] = state.get("text") or "the uploader never confirmed the images"
    return info

PLATFORMS = {
    "mostaql": {
        "hosts": {"mostaql.com", "www.mostaql.com"},
        "home": "https://mostaql.com/",
    },
    "nafezly": {
        "hosts": {"nafezly.com", "www.nafezly.com"},
        "home": "https://nafezly.com/",
    },
    "kafiil": {
        "hosts": {"kafiil.com", "www.kafiil.com"},
        "home": "https://kafiil.com/",
    },
}

def authenticate(authorization):
    expected = f"Bearer {API_KEY}"
    if not authorization or not hmac.compare_digest(authorization, expected):
        raise HTTPException(status_code=401, detail="Unauthorized")

def validate_url(platform, url):
    config = PLATFORMS.get(platform.lower().strip())
    parsed = urlparse(url)
    if (
        not config
        or parsed.scheme != "https"
        or parsed.hostname not in config["hosts"]
        or parsed.username
        or parsed.password
    ):
        raise HTTPException(status_code=400, detail="Platform or URL not allowed")


SUBMIT_SUCCESS_MARKERS = (
    "ØªÙ… Ø¥Ø±Ø³Ø§Ù„ Ø¹Ø±Ø¶Ùƒ",
    "ØªÙ… Ø§Ø±Ø³Ø§Ù„ Ø¹Ø±Ø¶Ùƒ",
    "ØªÙ… ØªÙ‚Ø¯ÙŠÙ… Ø¹Ø±Ø¶Ùƒ",
    "ØªÙ… Ø¥Ø¶Ø§ÙØ© Ø¹Ø±Ø¶Ùƒ",
    "Ø¹Ø±Ø¶Ùƒ Ø¨Ù†Ø¬Ø§Ø­",
)

SUBMIT_ERROR_MARKERS = (
    "Ø§Ù„Ø­Ø¯ Ø§Ù„Ø£Ø¯Ù†Ù‰",
    "Ø£Ù‚Ù„ Ù…Ù† Ø§Ù„Ø­Ø¯",
    "Ù‚ÙŠÙ…Ø© Ø§Ù„Ø¹Ø±Ø¶",
    "Ù„Ø§ ÙŠÙ…ÙƒÙ†",
    "Ø®Ø·Ø£",
    "Ø¹Ø°Ø±Ø§Ù‹",
    "Ø¹Ø°Ø±Ø§",
)


def _safe_page_info(driver):
    """Title/URL reads can throw while the page is navigating."""
    try:
        title = driver.title
    except Exception:
        title = ""
    try:
        url = driver.current_url
    except Exception:
        url = ""
    return title, url


def _dismiss_alert(driver):
    """Accept a native dialog so it cannot block the click or the page."""
    try:
        alert = driver.switch_to.alert
        text = (alert.text or "").strip()
        alert.accept()
        return text
    except Exception:
        return None


def _read_page_state(driver):
    return driver.execute_script("""
        const form = document.querySelector('form#project__bid')
            || document.querySelector('#offer-form');
        return {
            readyState: document.readyState,
            formPresent: !!form,
            url: location.href,
            text: (document.body ? document.body.innerText : '').slice(0, 8000)
        };
    """)


def _challenge_state(driver):
    """True when the page is a Cloudflare-style bot check, not the real page.

    Cloudflare's interstitial replaces the document with a short "Just a
    moment..." page, so everything downstream (offer form, budget text,
    submit button) is missing and the caller must wait instead of parsing it.
    """
    try:
        title = (driver.title or "").strip().lower()
        body = driver.find_element("tag name", "body").text.strip().lower()[:3000]
    except Exception:
        return False

    if any(marker in title for marker in CHALLENGE_TITLES):
        return True
    return any(marker in body for marker in CHALLENGE_TEXT)


def _wait_for_challenge(driver, timeout=40):
    """Let a bot-check page resolve itself, reloading once if it stalls.

    The shared Firefox profile keeps the ``cf_clearance`` cookie, so a normal
    visit clears the check in a few seconds. Returns ``(passed, seconds)``.
    """
    if not _challenge_state(driver):
        return True, 0.0

    started = time.time()
    reloaded = False
    while time.time() - started < timeout:
        time.sleep(2)
        if not _challenge_state(driver):
            return True, round(time.time() - started, 1)
        if not reloaded and time.time() - started >= timeout / 2:
            reloaded = True
            try:
                driver.refresh()
            except Exception:
                pass
    return False, round(time.time() - started, 1)


def _submit_and_confirm(driver, click_selector, timeout=30):
    """Click a platform submit button and wait for proof that it landed.

    Returns (submitted, confirmation, alerts, state). ``submitted`` is True
    only when the page itself shows the offer went through: the bid/offer
    form disappeared or an explicit success message appeared.

    Ambiguous outcomes are always reported as failures. A wrong "not
    submitted" causes a human to look; a wrong "submitted" could cause a
    duplicate bid, which is the worse error.
    """
    alerts = []
    state = {}

    button = WebDriverWait(driver, 10).until(
        lambda d: d.find_element("css selector", click_selector)
    )
    try:
        driver.execute_script(
            "arguments[0].scrollIntoView({block: 'center'});", button
        )
    except Exception:
        pass

    try:
        button.click()
    except Exception as exc:
        alert_text = _dismiss_alert(driver)
        if alert_text:
            alerts.append(alert_text)
        else:
            return (
                False,
                f"click_failed: {type(exc).__name__}",
                alerts,
                state,
            )

    deadline = time.time() + timeout
    while time.time() < deadline:
        alert_text = _dismiss_alert(driver)
        if alert_text:
            alerts.append(alert_text)

        try:
            state = _read_page_state(driver)
        except Exception:
            # Page is mid-navigation; retry shortly.
            time.sleep(0.5)
            continue

        text = state.get("text") or ""
        lowered = text.lower()

        if any(marker in text for marker in SUBMIT_SUCCESS_MARKERS):
            return True, "success_message", alerts, state
        if "offer has been submitted" in lowered or "offer has been sent" in lowered:
            return True, "success_message", alerts, state

        if not state.get("formPresent") and state.get("readyState") == "complete":
            return True, "form_disappeared", alerts, state

        time.sleep(0.5)

    return False, "timeout_waiting_for_confirmation", alerts, state


def _as_int(value):
    """Best-effort numeric read for DOM attributes that may be formatted."""
    if value is None or value == "":
        return None
    try:
        text = str(value).strip().replace(",", "").replace("\u066c", "")
        return int(round(float(text)))
    except (TypeError, ValueError):
        return None


def _mostaql_min_cost(driver, form):
    """Lowest bid the project page will accept (page JS wins over attributes)."""
    candidates = []
    try:
        raw = driver.execute_script(
            "try { return window.costValidationMinValue; } catch (e) { return null; }"
        )
    except Exception:
        raw = None

    parsed = _as_int(raw)
    if parsed is not None:
        candidates.append(parsed)

    cost_el = form.find_element("id", "bid__cost")
    for attr in ("min", "data-min"):
        parsed = _as_int(cost_el.get_attribute(attr))
        if parsed is not None:
            candidates.append(parsed)

    return max(candidates) if candidates else None


def _nafezly_limits(form):
    cost_el = form.find_element("id", "cost")
    return {
        "min": _as_int(cost_el.get_attribute("min")),
        "max": _as_int(cost_el.get_attribute("max")),
    }


def prepare_nafezly_offer(url, period, cost, proposal, submit=False, attachments=None):
    options = Options()
    options.binary_location = FIREFOX_BINARY
    options.add_argument("-profile")
    options.add_argument(str(PROFILE))
    options.add_argument("-headless")

    driver = webdriver.Firefox(options=options)
    clicked = False
    try:
        driver.set_page_load_timeout(45)
        driver.get(url)
        WebDriverWait(driver, 15).until(
            lambda d: d.execute_script(
                "return document.readyState"
            ) in ("interactive", "complete")
        )

        challenge_passed, challenge_wait = _wait_for_challenge(driver)
        if not challenge_passed:
            screenshot = SCREENSHOTS / "nafezly-challenge.png"
            try:
                driver.save_screenshot(str(screenshot))
            except Exception:
                pass
            return {
                "status": "challenge_failed",
                "submitted": False,
                "platform": "nafezly",
                "reason": (
                    "Cloudflare bot check did not finish within "
                    f"{challenge_wait}s; the real project page never loaded."
                ),
                "page_title": driver.title,
                "final_url": driver.current_url,
                "challenge_wait_s": challenge_wait,
                "screenshot": str(screenshot),
            }

        if urlparse(driver.current_url).hostname not in PLATFORMS["nafezly"]["hosts"]:
            raise HTTPException(status_code=400, detail="Unexpected final project host")

        try:
            form = WebDriverWait(driver, 12).until(
                lambda d: d.find_element("css selector", "#offer-form")
            )
        except Exception:
            page_text = driver.find_element("tag name", "body").text[:5000]
            screenshot = SCREENSHOTS / "prepare-offer-form-missing.png"
            driver.save_screenshot(str(screenshot))
            return {
                "status": "review_required",
                "submitted": False,
                "reason": "Offer form was not found; project may be closed or the page may require attention.",
                "page_title": driver.title,
                "final_url": driver.current_url,
                "page_text": page_text,
                "screenshot": str(screenshot),
            }

        if form.get_attribute("action") != "https://nafezly.com/offer/create":
            raise HTTPException(status_code=400, detail="Unexpected offer form action")

        period_el = driver.find_element("id", "period")
        cost_el = driver.find_element("id", "cost")
        description_el = driver.find_element("id", "offer_description")

        limits = _nafezly_limits(form)
        minimum_cost = limits.get("min")
        maximum_cost = limits.get("max")
        requested_cost = int(cost)
        effective_cost = requested_cost
        floor_adjusted = False
        if minimum_cost and requested_cost < minimum_cost:
            effective_cost = minimum_cost
            floor_adjusted = True

        if maximum_cost and effective_cost > maximum_cost:
            screenshot = SCREENSHOTS / "nafezly-price-out-of-range.png"
            try:
                driver.save_screenshot(str(screenshot))
            except Exception:
                pass
            return {
                "status": "price_out_of_range",
                "submitted": False,
                "platform": "nafezly",
                "reason": (
                    f"Nafezly accepts offers between {minimum_cost} and "
                    f"{maximum_cost}; the agent selected {requested_cost}."
                ),
                "requested_cost": requested_cost,
                "minimum_cost": minimum_cost,
                "maximum_cost": maximum_cost,
                "page_title": driver.title,
                "final_url": driver.current_url,
                "screenshot": str(screenshot),
            }

        period_el.clear()
        period_el.send_keys(str(period))

        cost_el.clear()
        cost_el.send_keys(str(effective_cost))

        description_el.clear()
        description_el.send_keys(proposal)

        # Trigger the site's existing earnings calculation.
        driver.execute_script(
            "arguments[0].dispatchEvent(new KeyboardEvent('keyup', "
            "{bubbles:true, key:'1'}));",
            cost_el,
        )

        attachment_info = _attach_files(driver, form, attachments or [])
        if attachments and (
            attachment_info.get("supported") is not True
            or attachment_info.get("uploaded", 0) < len(attachments)
        ):
            screenshot = SCREENSHOTS / "nafezly-attachment-failed.png"
            try:
                driver.save_screenshot(str(screenshot))
            except Exception:
                pass
            return {
                "status": "attachment_failed",
                "submitted": False,
                "platform": "nafezly",
                "reason": (
                    "The portfolio images could not be attached to the offer "
                    "form, so nothing was submitted."
                ),
                "attachments": attachment_info,
                "page_title": driver.title,
                "final_url": driver.current_url,
                "period_days": period,
                "requested_cost": requested_cost,
                "cost_usd": effective_cost,
                "minimum_cost": minimum_cost,
                "floor_adjusted": floor_adjusted,
                "screenshot": str(screenshot),
            }

        validation = driver.execute_script("""
            const form = document.querySelector('#offer-form');
            const fields = ['period', 'cost', 'offer_description'];
            return {
                browserValid: form.checkValidity(),
                fields: fields.map(id => {
                    const el = document.getElementById(id);
                    return {
                        id,
                        valid: el.checkValidity(),
                        message: el.validationMessage
                    };
                }),
                earnings: document.querySelector('#gain_value')?.value || null,
                proposalLength: document.querySelector('#offer_description')?.value.length || 0
            };
        """)
        validation["submitButtonClicked"] = False

        if not submit:
            return {
                "status": "dry_run_prepared",
                "submitted": False,
                "platform": "nafezly",
                "attachments": attachment_info,
                "page_title": driver.title,
                "final_url": driver.current_url,
                "period_days": period,
                "requested_cost": requested_cost,
                "cost_usd": effective_cost,
                "minimum_cost": minimum_cost,
                "maximum_cost": maximum_cost,
                "floor_adjusted": floor_adjusted,
                "expected_net_usd": round(effective_cost * 0.85, 2),
                "validation": validation,
                "message": "Form filled for inspection only. No offer was submitted.",
            }

        if not ALLOW_SUBMIT:
            screenshot = SCREENSHOTS / "nafezly-submit-disabled.png"
            try:
                driver.save_screenshot(str(screenshot))
            except Exception:
                pass
            return {
                "status": "submit_disabled",
                "submitted": False,
                "platform": "nafezly",
                "attachments": attachment_info,
                "reason": (
                    "Submission was requested but ALLOW_SUBMIT is not enabled "
                    "on the host service."
                ),
                "page_title": driver.title,
                "final_url": driver.current_url,
                "screenshot": str(screenshot),
            }

        if not validation.get("browserValid"):
            screenshot = SCREENSHOTS / "nafezly-submit-invalid.png"
            try:
                driver.save_screenshot(str(screenshot))
            except Exception:
                pass
            return {
                "status": "validation_failed",
                "submitted": False,
                "platform": "nafezly",
                "attachments": attachment_info,
                "reason": "The browser rejected the form values; nothing was clicked.",
                "validation": validation,
                "page_title": driver.title,
                "final_url": driver.current_url,
                "screenshot": str(screenshot),
            }

        clicked = True
        submitted, confirmation, alerts, state = _submit_and_confirm(
            driver, "#make-offer"
        )
        validation["submitButtonClicked"] = True

        page_title, final_url = _safe_page_info(driver)
        screenshot = SCREENSHOTS / (
            "nafezly-submitted.png" if submitted else "nafezly-submit-failed.png"
        )
        try:
            driver.save_screenshot(str(screenshot))
        except Exception:
            pass

        result = {
            "status": "submitted" if submitted else "submit_failed",
            "submitted": submitted,
            "platform": "nafezly",
            "attachments": attachment_info,
            "confirmation": confirmation,
            "alerts": alerts,
            "page_title": page_title,
            "final_url": final_url,
            "period_days": period,
            "requested_cost": requested_cost,
            "cost_usd": effective_cost,
            "minimum_cost": minimum_cost,
            "maximum_cost": maximum_cost,
            "floor_adjusted": floor_adjusted,
            "expected_net_usd": round(effective_cost * 0.85, 2),
            "validation": validation,
            "screenshot": str(screenshot),
            "message": (
                "Nafezly accepted the offer."
                if submitted
                else "The offer was not confirmed; review the evidence before retrying."
            ),
        }
        if not submitted:
            result["page_text"] = (state.get("text") or "")[:3000]
        return result
    except HTTPException:
        raise
    except Exception as exc:
        screenshot = SCREENSHOTS / "nafezly-offer-error.png"
        try:
            driver.save_screenshot(str(screenshot))
        except Exception:
            pass

        page_title, final_url = _safe_page_info(driver)
        return {
            "status": "submit_unknown" if clicked else "review_required",
            "submitted": False,
            "platform": "nafezly",
            "reason": str(exc),
            "clicked_before_error": clicked,
            "page_title": page_title,
            "final_url": final_url,
            "screenshot": str(screenshot),
        }
    finally:
        driver.quit()

def prepare_mostaql_offer(url, period, cost, proposal, submit=False, attachments=None):
    options = Options()
    options.binary_location = FIREFOX_BINARY
    options.add_argument("-profile")
    options.add_argument(str(PROFILE))
    options.add_argument("-headless")

    driver = webdriver.Firefox(options=options)

    clicked = False
    try:
        driver.set_page_load_timeout(45)
        driver.get(url)

        WebDriverWait(driver, 15).until(
            lambda d: d.execute_script(
                "return document.readyState"
            ) in ("interactive", "complete")
        )

        challenge_passed, challenge_wait = _wait_for_challenge(driver)
        if not challenge_passed:
            screenshot = SCREENSHOTS / "mostaql-challenge.png"
            try:
                driver.save_screenshot(str(screenshot))
            except Exception:
                pass
            return {
                "status": "challenge_failed",
                "submitted": False,
                "platform": "mostaql",
                "reason": (
                    "Cloudflare bot check did not finish within "
                    f"{challenge_wait}s; the real project page never loaded."
                ),
                "page_title": driver.title,
                "final_url": driver.current_url,
                "challenge_wait_s": challenge_wait,
                "screenshot": str(screenshot),
            }

        if urlparse(driver.current_url).hostname not in PLATFORMS["mostaql"]["hosts"]:
            raise HTTPException(
                status_code=400,
                detail="Unexpected final project host",
            )

        try:
            form = WebDriverWait(driver, 12).until(
                lambda d: d.find_element(
                    "css selector", "form#project__bid"
                )
            )
        except Exception:
            # The bid form disappears once you have already bid on a project.
            bid_count = 0
            try:
                bid_count = int(
                    driver.execute_script(
                        "return document.querySelectorAll('.bid').length;"
                    ) or 0
                )
            except Exception:
                pass

            screenshot = SCREENSHOTS / (
                "mostaql-already-bid.png" if bid_count else "mostaql-form-missing.png"
            )
            try:
                driver.save_screenshot(str(screenshot))
            except Exception:
                pass

            if bid_count:
                reason = (
                    "The bid form is absent and the page already lists bids, "
                    "so an offer has most likely been submitted already."
                )
                try:
                    snippet = driver.find_element("tag name", "body").text[:4000]
                except Exception:
                    snippet = ""
                if "Ù„Ø§ ÙŠÙ…ÙƒÙ†Ùƒ ØªÙ‚Ø¯ÙŠÙ… Ø§Ù„Ù…Ø²ÙŠØ¯ Ù…Ù† Ø§Ù„Ø¹Ø±ÙˆØ¶" in snippet:
                    reason = (
                        "The bid form is absent and the page says no more bids "
                        "can be submitted right now (platform bid limit), so "
                        "either an offer already exists on this project or the "
                        "account must buy extra bids."
                    )
                return {
                    "status": "already_bid",
                    "submitted": False,
                    "platform": "mostaql",
                    "reason": reason,
                    "bid_count": bid_count,
                    "page_title": driver.title,
                    "final_url": driver.current_url,
                    "screenshot": str(screenshot),
                }

            page_text = driver.find_element("tag name", "body").text[:5000]
            return {
                "status": "review_required",
                "submitted": False,
                "platform": "mostaql",
                "reason": "Mostaql bid form was not found on the project page.",
                "page_title": driver.title,
                "final_url": driver.current_url,
                "page_text": page_text,
                "screenshot": str(screenshot),
            }

        if form.get_attribute("action") != "https://mostaql.com/bid":
            raise HTTPException(
                status_code=400,
                detail="Unexpected Mostaql offer form action",
            )

        page_text = driver.find_element("tag name", "body").text

        if "Ù…ÙØªÙˆØ­" not in page_text:
            screenshot = SCREENSHOTS / "mostaql-project-review-required.png"
            driver.save_screenshot(str(screenshot))
            return {
                "status": "review_required",
                "submitted": False,
                "reason": "Could not confirm that the Mostaql project is open.",
                "page_title": driver.title,
                "final_url": driver.current_url,
                "screenshot": str(screenshot),
            }

        period_el = form.find_element("id", "bid__period")
        cost_el = form.find_element("id", "bid__cost")
        details_el = form.find_element("id", "bid__details")

        minimum_cost = _mostaql_min_cost(driver, form)
        requested_cost = int(cost)
        effective_cost = requested_cost
        floor_adjusted = False
        if minimum_cost and requested_cost < minimum_cost:
            # Policy: always bid the lowest price the project will accept.
            effective_cost = minimum_cost
            floor_adjusted = True

        period_el.clear()
        period_el.send_keys(str(period))

        cost_el.clear()
        cost_el.send_keys(str(effective_cost))

        details_el.clear()
        details_el.send_keys(proposal)

        # Allow the site's own JavaScript to calculate the net amount.
        driver.execute_script("""
            for (const id of ['bid__period', 'bid__cost', 'bid__details']) {
                const el = document.getElementById(id);
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
            }
        """)

        attachment_info = _attach_files(driver, form, attachments or [])
        if attachments and attachment_info.get("supported") is not True:
            screenshot = SCREENSHOTS / "mostaql-attachments-unsupported.png"
            try:
                driver.save_screenshot(str(screenshot))
            except Exception:
                pass
            return {
                "status": "attachments_unsupported",
                "submitted": False,
                "platform": "mostaql",
                "reason": (
                    "This bid form has no attachment field, so the four "
                    "portfolio images cannot be attached. Nothing was submitted."
                ),
                "attachments": attachment_info,
                "page_title": driver.title,
                "final_url": driver.current_url,
                "period_days": period,
                "requested_cost": requested_cost,
                "cost_usd": effective_cost,
                "minimum_cost": minimum_cost,
                "floor_adjusted": floor_adjusted,
                "screenshot": str(screenshot),
            }
        if attachments and attachment_info.get("uploaded", 0) < len(attachments):
            screenshot = SCREENSHOTS / "mostaql-attachment-failed.png"
            try:
                driver.save_screenshot(str(screenshot))
            except Exception:
                pass
            return {
                "status": "attachment_failed",
                "submitted": False,
                "platform": "mostaql",
                "reason": (
                    "The portfolio images could not be attached to the bid "
                    "form, so nothing was submitted."
                ),
                "attachments": attachment_info,
                "page_title": driver.title,
                "final_url": driver.current_url,
                "period_days": period,
                "requested_cost": requested_cost,
                "cost_usd": effective_cost,
                "minimum_cost": minimum_cost,
                "floor_adjusted": floor_adjusted,
                "screenshot": str(screenshot),
            }

        validation = driver.execute_script("""
            const form = document.querySelector('form#project__bid');
            const ids = ['bid__period', 'bid__cost', 'bid__details'];

            return {
                browserValid: form.checkValidity(),
                fields: ids.map(id => {
                    const el = document.getElementById(id);
                    return {
                        id,
                        valueLength: el.value.length,
                        valid: el.checkValidity(),
                        message: el.validationMessage
                    };
                }),
                calculatedNetAmount:
                    document.querySelector('#bid__realCost')?.value || null,
                submitButtonClicked: false
            };
        """)

        if not submit:
            screenshot = SCREENSHOTS / "mostaql-offer-dry-run.png"
            driver.save_screenshot(str(screenshot))

            return {
                "status": "dry_run_prepared",
                "submitted": False,
                "platform": "mostaql",
                "attachments": attachment_info,
                "page_title": driver.title,
                "final_url": driver.current_url,
                "period_days": period,
                "requested_cost": requested_cost,
                "cost_usd": effective_cost,
                "minimum_cost": minimum_cost,
                "floor_adjusted": floor_adjusted,
                "validation": validation,
                "screenshot": str(screenshot),
                "message": (
                    "Mostaql offer fields filled for inspection only. "
                    "The submit button was not clicked."
                ),
            }

        if not ALLOW_SUBMIT:
            screenshot = SCREENSHOTS / "mostaql-submit-disabled.png"
            try:
                driver.save_screenshot(str(screenshot))
            except Exception:
                pass
            return {
                "status": "submit_disabled",
                "submitted": False,
                "platform": "mostaql",
                "attachments": attachment_info,
                "reason": (
                    "Submission was requested but ALLOW_SUBMIT is not enabled "
                    "on the host service."
                ),
                "page_title": driver.title,
                "final_url": driver.current_url,
                "screenshot": str(screenshot),
            }

        if not validation.get("browserValid"):
            screenshot = SCREENSHOTS / "mostaql-submit-invalid.png"
            try:
                driver.save_screenshot(str(screenshot))
            except Exception:
                pass
            return {
                "status": "validation_failed",
                "submitted": False,
                "platform": "mostaql",
                "attachments": attachment_info,
                "reason": "The browser rejected the form values; nothing was clicked.",
                "validation": validation,
                "page_title": driver.title,
                "final_url": driver.current_url,
                "screenshot": str(screenshot),
            }

        clicked = True
        submitted, confirmation, alerts, state = _submit_and_confirm(
            driver, "#bid__submit"
        )
        validation["submitButtonClicked"] = True

        page_title, final_url = _safe_page_info(driver)
        screenshot = SCREENSHOTS / (
            "mostaql-submitted.png" if submitted else "mostaql-submit-failed.png"
        )
        try:
            driver.save_screenshot(str(screenshot))
        except Exception:
            pass

        result = {
            "status": "submitted" if submitted else "submit_failed",
            "submitted": submitted,
            "platform": "mostaql",
            "attachments": attachment_info,
            "confirmation": confirmation,
            "alerts": alerts,
            "page_title": page_title,
            "final_url": final_url,
            "period_days": period,
            "requested_cost": requested_cost,
            "cost_usd": effective_cost,
            "minimum_cost": minimum_cost,
            "floor_adjusted": floor_adjusted,
            "validation": validation,
            "screenshot": str(screenshot),
            "message": (
                "Mostaql accepted the bid."
                if submitted
                else "The bid was not confirmed; review the evidence before retrying."
            ),
        }
        if not submitted:
            result["page_text"] = (state.get("text") or "")[:3000]
        return result

    except HTTPException:
        raise
    except Exception as exc:
        screenshot = SCREENSHOTS / "mostaql-offer-error.png"
        try:
            driver.save_screenshot(str(screenshot))
        except Exception:
            pass

        page_title, final_url = _safe_page_info(driver)

        # If the submit button was already clicked, the outcome is unknown.
        # Never report this as a plain failure: retrying could double-bid.
        return {
            "status": "submit_unknown" if clicked else "review_required",
            "submitted": False,
            "platform": "mostaql",
            "reason": str(exc),
            "clicked_before_error": clicked,
            "page_title": page_title,
            "final_url": final_url,
            "screenshot": str(screenshot),
        }

    finally:
        driver.quit()

def inspect_with_firefox(url, platform, title, description):
    options = Options()
    options.binary_location = FIREFOX_BINARY
    options.add_argument("-profile")
    options.add_argument(str(PROFILE))
    options.add_argument("-headless")

    driver = webdriver.Firefox(options=options)
    try:
        driver.set_page_load_timeout(45)
        response = {"status": "opened"}
        driver.get(url)
        WebDriverWait(driver, 15).until(
            lambda d: d.execute_script("return document.readyState") in ("interactive", "complete")
        )
        challenge_passed, challenge_wait = _wait_for_challenge(driver)
        screenshot = SCREENSHOTS / f"inspect-{platform}.png"
        driver.save_screenshot(str(screenshot))
        body = driver.find_element("tag name", "body").text

        page_structure = driver.execute_script("""
            const describe = el => ({
                tag: el.tagName.toLowerCase(),
                type: el.getAttribute('type'),
                name: el.getAttribute('name'),
                id: el.id || null,
                text: (el.innerText || el.value || el.getAttribute('aria-label') || '').trim().slice(0, 300),
                placeholder: el.getAttribute('placeholder'),
                required: !!el.required,
                disabled: !!el.disabled,
                href: el.tagName === 'A' ? el.href : null,
                action: el.tagName === 'FORM' ? el.action : null,
                method: el.tagName === 'FORM' ? el.method : null
            });

            return {
                forms: Array.from(document.forms).map(form => ({
                    ...describe(form),
                    fields: Array.from(form.querySelectorAll('input, textarea, select, button'))
                        .map(describe)
                })).slice(0, 30),
                buttons: Array.from(document.querySelectorAll('button, input[type=submit], [role=button]'))
                    .map(describe).slice(0, 100),
                links: Array.from(document.querySelectorAll('a[href]'))
                    .map(describe).filter(a => a.href).slice(0, 150)
            };
        """)
        script_info = driver.execute_script("""
            const relevant = /gain_value|offer_description|offer\\/create|make-offer|\\bcost\\b|\\bperiod\\b/i;
            return {
                scripts: Array.from(document.scripts)
                    .map(s => s.src || "(inline script)")
                    .slice(0, 100),
                matchingScripts: Array.from(document.scripts)
                    .filter(s => !s.src && relevant.test(s.textContent || ""))
                    .map(s => (s.textContent || "").slice(0, 12000))
                    .slice(0, 20),
                offerFormHtml: document.querySelector("#offer-form")?.outerHTML?.slice(0, 20000) || null
            };
        """)

        return {
            "status": "inspected",
            "platform": platform,
            "title_from_loki": title,
            "description_from_loki": description,
            "page_title": driver.title,
            "final_url": driver.current_url,
            "challenge_passed": challenge_passed,
            "challenge_wait_s": challenge_wait,
            "page_text": body[:12000],
            "page_structure": page_structure,
            "script_info": script_info,
            "screenshot": str(screenshot),
        }
    finally:
        driver.quit()

@asynccontextmanager
async def lifespan(app: FastAPI):
    yield

app = FastAPI(title="Loki Host Firefox Service", lifespan=lifespan)

class PlatformRequest(BaseModel):
    platform: str

class InspectRequest(BaseModel):
    platform: str
    url: str
    title: str = ""
    description: str = ""


class PrepareOfferRequest(BaseModel):
    platform: str
    url: str
    period: int
    cost: int
    offer_description: str
    submit: bool = False
    attachments: str | None = None
    attachments_required: bool = True

@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/prepare-offer")
async def prepare_offer(
    request: PrepareOfferRequest,
    authorization: str | None = Header(default=None),
):
    authenticate(authorization)

    platform = request.platform.lower().strip()
    if platform not in {"nafezly", "mostaql"}:
        raise HTTPException(
            status_code=400,
            detail="Prepare-offer supports Nafezly and Mostaql only",
        )

    validate_url(platform, request.url)

    if not 1 <= request.period <= 90:
        raise HTTPException(status_code=400, detail="Period must be 1â€“90 days")

    # Broad sanity limits only. The workflow must select a cost
    # consistent with the individual project's advertised budget.
    if platform == "nafezly" and not 1 <= request.cost <= 10000:
        raise HTTPException(status_code=400, detail="Nafezly cost must be $1â€“$10,000")
    if platform == "mostaql" and not 5 <= request.cost <= 10000:
        raise HTTPException(status_code=400, detail="Mostaql cost must be $5â€“$10,000")

    proposal = request.offer_description.strip()
    if not 30 <= len(proposal) <= 5000:
        raise HTTPException(
            status_code=400,
            detail="Proposal must contain 30â€“5,000 characters",
        )

    # Fail before the browser opens, the same way the site would fail after
    # the form is filled: a loud, specific alert instead of a dead click.
    if platform == "mostaql":
        proposal_problems = _mostaql_proposal_problems(proposal)
        if proposal_problems:
            return {
                "status": "validation_failed",
                "submitted": False,
                "platform": platform,
                "cost_usd": request.cost,
                "period_days": request.period,
                "reason": "; ".join(proposal_problems) + ".",
                "proposal_length": len(proposal),
            }

    attachment_paths = []
    if request.attachments_required and ATTACHMENTS_REQUIRED:
        attachment_paths = _resolve_attachments(request.attachments)

    prepare_function = (
        prepare_nafezly_offer
        if platform == "nafezly"
        else prepare_mostaql_offer
    )

    if request.submit and not ALLOW_SUBMIT:
        return {
            "status": "submit_disabled",
            "submitted": False,
            "platform": platform,
            "reason": (
                "Submitting is disabled on the host service. Set ALLOW_SUBMIT=1 "
                "and restart loki-host-firefox to allow real bids."
            ),
        }

    return await run_in_threadpool(
        _run_exclusive,
        prepare_function,
        request.url,
        request.period,
        request.cost,
        proposal,
        request.submit,
        attachment_paths,
    )

@app.post("/open-login")
async def open_login(request: PlatformRequest, authorization: str | None = Header(default=None)):
    authenticate(authorization)
    platform = request.platform.lower().strip()
    if platform not in PLATFORMS:
        raise HTTPException(status_code=400, detail="Unknown platform")
    result = await run_in_threadpool(
        _run_exclusive,
        inspect_with_firefox,
        PLATFORMS[platform]["home"],
        platform,
        "",
        "",
    )
    if result.get("status") != "opened":
        return result
    return {
        **result,
        "status": "page_opened",
        "message": "The page was opened and inspected in host Firefox ESR.",
    }


TRACKING_HOST = "url-shortne.tail3d0151.ts.net"
MAX_TRACKING_REDIRECTS = 8


class NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def resolve_tracking_url(platform, url):
    platform = platform.lower().strip()
    config = PLATFORMS.get(platform)
    if not config:
        raise HTTPException(status_code=400, detail="Unknown platform")

    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != TRACKING_HOST
        or parsed.username
        or parsed.password
        or parsed.port not in (None, 443)
    ):
        raise HTTPException(status_code=400, detail="Tracking URL not allowed")

    current = url
    opener = build_opener(NoRedirectHandler())
    allowed_hosts = {TRACKING_HOST} | config["hosts"]

    for _ in range(MAX_TRACKING_REDIRECTS + 1):
        parsed = urlparse(current)
        host = (parsed.hostname or "").lower()

        if (
            parsed.scheme != "https"
            or host not in allowed_hosts
            or parsed.username
            or parsed.password
            or parsed.port not in (None, 443)
        ):
            raise HTTPException(status_code=400, detail="Redirect destination not allowed")

        if host in config["hosts"]:
            return current

        req = Request(
            current,
            headers={"User-Agent": "Loki-TrackingResolver/1.0"},
            method="GET",
        )
        try:
            response = opener.open(req, timeout=15)
            status = response.status
            location = response.headers.get("Location")
            response.close()
        except HTTPError as exc:
            status = exc.code
            location = exc.headers.get("Location")
            exc.close()
        except (URLError, TimeoutError, OSError) as exc:
            raise HTTPException(
                status_code=502,
                detail=f"Tracking link could not be resolved: {type(exc).__name__}",
            ) from exc

        if status not in (301, 302, 303, 307, 308) or not location:
            raise HTTPException(
                status_code=502,
                detail="Tracking link did not redirect to the expected platform",
            )

        current = urljoin(current, location)

    raise HTTPException(status_code=502, detail="Too many tracking redirects")


@app.post("/resolve-tracking")
async def resolve_tracking(
    request: InspectRequest,
    authorization: str | None = Header(default=None),
):
    authenticate(authorization)
    platform = request.platform.lower().strip()
    final_url = await run_in_threadpool(
        resolve_tracking_url,
        platform,
        request.url,
    )
    return {
        "status": "resolved",
        "platform": platform,
        "original_url": request.url,
        "final_url": final_url,
    }


@app.post("/inspect")
async def inspect_job(request: InspectRequest, authorization: str | None = Header(default=None)):
    authenticate(authorization)
    platform = request.platform.lower().strip()
    validate_url(platform, request.url)
    return await run_in_threadpool(
        _run_exclusive,
        inspect_with_firefox,
        request.url,
        platform,
        request.title,
        request.description,
    )

