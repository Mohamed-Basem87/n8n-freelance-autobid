import hmac
import json
import os
import re
import threading
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urljoin, urlparse
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel
from selenium import webdriver
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.firefox.options import Options
from selenium.webdriver.support.ui import Select, WebDriverWait
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
DATA_DIR = Path(os.environ.get("LOKI_DATA_DIR", "/opt/freelance-autobid/data"))
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

# Screenshots are delivered to Telegram and then removed from the host. The
# bot token lives only in the protected environment file beside this service.
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

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
    "تم إرسال عرضك",
    "تم ارسال عرضك",
    "تم تقديم عرضك",
    "تم إضافة عرضك",
    "عرضك بنجاح",
    "تم إنشاء العرض",
    "تم تعديل العرض",
)

SUBMIT_ERROR_MARKERS = (
    "الحد الأدنى",
    "أقل من الحد",
    "قيمة العرض",
    "لا يمكن",
    "خطأ",
    "عذراً",
    "عذرا",
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
            || document.querySelector('#offer-form')
            || document.querySelector('form[action*="projectBid"]');
        const visible = el => {
            if (!el) return false;
            const rect = el.getBoundingClientRect();
            const style = getComputedStyle(el);
            return style.display !== 'none'
                && style.visibility !== 'hidden'
                && rect.width > 0
                && rect.height > 0;
        };
        return {
            readyState: document.readyState,
            formPresent: !!form,
            formVisible: visible(form),
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

        if not state.get("formVisible") and state.get("readyState") == "complete":
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


def _kafiil_limits(form):
    price_el = form.find_element("css selector", 'input[name="price"]')
    return {
        "min": _as_int(price_el.get_attribute("min")),
        "max": _as_int(price_el.get_attribute("max")),
    }


def _capture_proposal_screenshot(driver, form, platform):
    """Photograph the filled offer form before anything is submitted.

    Kafiil, Mostaql, and Nafezly all replace or hide the form after a bid
    lands, so the pre-submit view is the only image that shows the actual
    proposal, price, and duration together.
    """
    path = SCREENSHOTS / f"{platform}-proposal-{int(time.time())}.png"
    try:
        driver.execute_script(
            "arguments[0].scrollIntoView({block: 'center'});", form
        )
        time.sleep(0.5)
        driver.save_screenshot(str(path))
        return str(path)
    except Exception:
        return None


def prepare_kafiil_offer(url, period, cost, proposal, submit=False, attachments=None):
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
            screenshot = SCREENSHOTS / "kafiil-challenge.png"
            try:
                driver.save_screenshot(str(screenshot))
            except Exception:
                pass
            return {
                "status": "challenge_failed",
                "submitted": False,
                "platform": "kafiil",
                "reason": (
                    "Bot check did not finish within "
                    f"{challenge_wait}s; the real project page never loaded."
                ),
                "page_title": driver.title,
                "final_url": driver.current_url,
                "challenge_wait_s": challenge_wait,
                "screenshot": str(screenshot),
            }

        if urlparse(driver.current_url).hostname not in PLATFORMS["kafiil"]["hosts"]:
            raise HTTPException(status_code=400, detail="Unexpected final project host")

        try:
            form = WebDriverWait(driver, 12).until(
                lambda d: d.find_element(
                    "css selector", 'form[action*="projectBid"]'
                )
            )
        except Exception:
            page_text = driver.find_element("tag name", "body").text[:5000]
            screenshot = SCREENSHOTS / "kafiil-form-missing.png"
            try:
                driver.save_screenshot(str(screenshot))
            except Exception:
                pass
            already_bid = (
                "عدد العروض" in page_text
                or "تم إنشاء العرض" in page_text
                or "يمكنك التعديل على عرض المشروع" in page_text
            )
            return {
                "status": "already_bid" if already_bid else "review_required",
                "submitted": False,
                "platform": "kafiil",
                "reason": (
                    "Kafiil's bid form was not found. The project may be "
                    "closed, or an offer has already been submitted."
                ),
                "page_title": driver.title,
                "final_url": driver.current_url,
                "page_text": page_text,
                "screenshot": str(screenshot),
            }

        if not (form.get_attribute("action") or "").startswith(
            "https://kafiil.com/projectBid"
        ):
            raise HTTPException(status_code=400, detail="Unexpected Kafiil offer form action")

        project_id = None
        try:
            project_id = form.find_element(
                "css selector", 'input[name="project_id"]'
            ).get_attribute("value")
        except Exception:
            pass

        period_el = Select(form.find_element("css selector", 'select[name="duration"]'))
        price_el = form.find_element("css selector", 'input[name="price"]')
        description_el = form.find_element("css selector", 'textarea[name="description"]')

        available_periods = [
            int(option.get_attribute("value"))
            for option in period_el.options
            if (option.get_attribute("value") or "").isdigit()
        ]
        if available_periods and int(period) not in available_periods:
            screenshot = SCREENSHOTS / "kafiil-duration-out-of-range.png"
            try:
                driver.save_screenshot(str(screenshot))
            except Exception:
                pass
            return {
                "status": "review_required",
                "submitted": False,
                "platform": "kafiil",
                "reason": (
                    f"Kafiil does not offer {period} days; valid choices are "
                    f"{available_periods[0]}–{available_periods[-1]}."
                ),
                "page_title": driver.title,
                "final_url": driver.current_url,
                "screenshot": str(screenshot),
            }

        limits = _kafiil_limits(form)
        minimum_cost = limits.get("min")
        maximum_cost = limits.get("max")
        requested_cost = int(cost)
        effective_cost = requested_cost
        floor_adjusted = False
        if minimum_cost and requested_cost < minimum_cost:
            effective_cost = minimum_cost
            floor_adjusted = True

        if maximum_cost and effective_cost > maximum_cost:
            screenshot = SCREENSHOTS / "kafiil-price-out-of-range.png"
            try:
                driver.save_screenshot(str(screenshot))
            except Exception:
                pass
            return {
                "status": "price_out_of_range",
                "submitted": False,
                "platform": "kafiil",
                "reason": (
                    f"Kafiil accepts offers between {minimum_cost} and "
                    f"{maximum_cost}; the agent selected {requested_cost}."
                ),
                "requested_cost": requested_cost,
                "minimum_cost": minimum_cost,
                "maximum_cost": maximum_cost,
                "page_title": driver.title,
                "final_url": driver.current_url,
                "screenshot": str(screenshot),
            }

        period_el.select_by_value(str(period))

        price_el.clear()
        price_el.send_keys(str(effective_cost))

        description_el.clear()
        description_el.send_keys(proposal)

        driver.execute_script("""
            const form = document.querySelector('form[action*="projectBid"]');
            for (const el of form.querySelectorAll('input, select, textarea')) {
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
            }
        """)

        validation = driver.execute_script("""
            const form = document.querySelector('form[action*="projectBid"]');
            return {
                browserValid: form.checkValidity(),
                fields: ['duration', 'price', 'description'].map(name => {
                    const el = form.querySelector(`[name="${name}"]`);
                    return {
                        name,
                        valid: el.checkValidity(),
                        message: el.validationMessage,
                        valueLength: (el.value || '').length
                    };
                }),
                submitButtonClicked: false
            };
        """)

        proposal_screenshot = _capture_proposal_screenshot(driver, form, "kafiil")

        if not submit:
            return {
                "status": "dry_run_prepared",
                "submitted": False,
                "platform": "kafiil",
                "project_id": project_id,
                "attachments": {
                    "supported": False,
                    "uploaded": 0,
                    "reason": "Kafiil's bid form has no attachment field.",
                },
                "page_title": driver.title,
                "final_url": driver.current_url,
                "period_days": period,
                "requested_cost": requested_cost,
                "cost_usd": effective_cost,
                "minimum_cost": minimum_cost,
                "maximum_cost": maximum_cost,
                "floor_adjusted": floor_adjusted,
                "validation": validation,
                "proposal_screenshot": proposal_screenshot,
                "message": "Kafiil offer fields filled for inspection only. The submit button was not clicked.",
            }

        if not ALLOW_SUBMIT:
            screenshot = SCREENSHOTS / "kafiil-submit-disabled.png"
            try:
                driver.save_screenshot(str(screenshot))
            except Exception:
                pass
            return {
                "status": "submit_disabled",
                "submitted": False,
                "platform": "kafiil",
                "project_id": project_id,
                "reason": (
                    "Submission was requested but ALLOW_SUBMIT is not enabled "
                    "on the host service."
                ),
                "page_title": driver.title,
                "final_url": driver.current_url,
                "proposal_screenshot": proposal_screenshot,
                "screenshot": str(screenshot),
            }

        if not validation.get("browserValid"):
            screenshot = SCREENSHOTS / "kafiil-submit-invalid.png"
            try:
                driver.save_screenshot(str(screenshot))
            except Exception:
                pass
            return {
                "status": "validation_failed",
                "submitted": False,
                "platform": "kafiil",
                "project_id": project_id,
                "reason": "The browser rejected the form values; nothing was clicked.",
                "validation": validation,
                "page_title": driver.title,
                "final_url": driver.current_url,
                "proposal_screenshot": proposal_screenshot,
                "screenshot": str(screenshot),
            }

        clicked = True
        submitted, confirmation, alerts, state = _submit_and_confirm(
            driver, 'form[action*="projectBid"] input[type="submit"]'
        )
        validation["submitButtonClicked"] = True

        page_title, final_url = _safe_page_info(driver)
        screenshot = SCREENSHOTS / (
            "kafiil-submitted.png" if submitted else "kafiil-submit-failed.png"
        )
        try:
            driver.save_screenshot(str(screenshot))
        except Exception:
            pass

        result = {
            "status": "submitted" if submitted else "submit_failed",
            "submitted": submitted,
            "platform": "kafiil",
            "project_id": project_id,
            "attachments": {
                "supported": False,
                "uploaded": 0,
                "reason": "Kafiil's bid form has no attachment field.",
            },
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
            "validation": validation,
            "proposal_screenshot": proposal_screenshot,
            "screenshot": str(screenshot),
            "message": (
                "Kafiil accepted the offer."
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
        screenshot = SCREENSHOTS / "kafiil-offer-error.png"
        try:
            driver.save_screenshot(str(screenshot))
        except Exception:
            pass

        page_title, final_url = _safe_page_info(driver)
        return {
            "status": "submit_unknown" if clicked else "review_required",
            "submitted": False,
            "platform": "kafiil",
            "reason": str(exc),
            "clicked_before_error": clicked,
            "page_title": page_title,
            "final_url": final_url,
            "screenshot": str(screenshot),
        }
    finally:
        driver.quit()

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

        proposal_screenshot = _capture_proposal_screenshot(driver, form, "nafezly")

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
                "proposal_screenshot": proposal_screenshot,
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
                "proposal_screenshot": proposal_screenshot,
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
                "proposal_screenshot": proposal_screenshot,
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
            "proposal_screenshot": proposal_screenshot,
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
                if "لا يمكنك تقديم المزيد من العروض" in snippet:
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

        if "مفتوح" not in page_text:
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

        proposal_screenshot = _capture_proposal_screenshot(driver, form, "mostaql")

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
                "proposal_screenshot": proposal_screenshot,
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
                "proposal_screenshot": proposal_screenshot,
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
                "proposal_screenshot": proposal_screenshot,
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
            "proposal_screenshot": proposal_screenshot,
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
        try:
            driver.get(url)
        except TimeoutException:
            # Heavy pages (Cloudflare interstitials, trackers, big images) can
            # blow the navigation budget while the DOM is already usable. Stop
            # loading and retry once before deciding the page is really dead.
            try:
                driver.execute_script("window.stop()")
            except Exception:
                pass
            time.sleep(2)
            try:
                driver.get(url)
            except TimeoutException:
                try:
                    driver.execute_script("window.stop()")
                except Exception:
                    pass
        try:
            WebDriverWait(driver, 15).until(
                lambda d: d.execute_script("return document.readyState") in ("interactive", "complete")
            )
        except TimeoutException:
            pass
        challenge_passed, challenge_wait = _wait_for_challenge(driver)
        screenshot = SCREENSHOTS / f"inspect-{platform}.png"
        driver.save_screenshot(str(screenshot))
        body = driver.find_element("tag name", "body").text
        if not body.strip() and not driver.title.strip():
            raise HTTPException(
                status_code=502,
                detail="Page did not load after one navigation retry",
            )

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
    if platform not in {"nafezly", "mostaql", "kafiil"}:
        raise HTTPException(
            status_code=400,
            detail="Prepare-offer supports Nafezly, Mostaql, and Kafiil only",
        )

    validate_url(platform, request.url)

    if not 1 <= request.period <= 90:
        raise HTTPException(status_code=400, detail="Period must be 1–90 days")

    # Broad sanity limits only. The workflow must select a cost
    # consistent with the individual project's advertised budget.
    if platform == "nafezly" and not 1 <= request.cost <= 10000:
        raise HTTPException(status_code=400, detail="Nafezly cost must be $1–$10,000")
    if platform == "mostaql" and not 5 <= request.cost <= 10000:
        raise HTTPException(status_code=400, detail="Mostaql cost must be $5–$10,000")
    if platform == "kafiil" and not 5 <= request.cost <= 10000:
        raise HTTPException(status_code=400, detail="Kafiil cost must be $5–$10,000")

    proposal = request.offer_description.strip()
    if not 30 <= len(proposal) <= 5000:
        raise HTTPException(
            status_code=400,
            detail="Proposal must contain 30–5,000 characters",
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
    # Kafiil's bid form has no attachment widget, so the portfolio images
    # are only required on Mostaql and Nafezly.
    if (
        platform != "kafiil"
        and request.attachments_required
        and ATTACHMENTS_REQUIRED
    ):
        attachment_paths = _resolve_attachments(request.attachments)

    if platform == "nafezly":
        prepare_function = prepare_nafezly_offer
    elif platform == "mostaql":
        prepare_function = prepare_mostaql_offer
    else:
        prepare_function = prepare_kafiil_offer

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
        # The shortener lives behind Tailscale and occasionally drops a
        # connection or fails DNS for a second; retry before failing the run.
        last_exc = None
        for attempt in range(3):
            try:
                response = opener.open(req, timeout=15)
                status = response.status
                location = response.headers.get("Location")
                response.close()
                last_exc = None
                break
            except HTTPError as exc:
                status = exc.code
                location = exc.headers.get("Location")
                exc.close()
                last_exc = None
                break
            except (URLError, TimeoutError, OSError) as exc:
                last_exc = exc
                if attempt < 2:
                    time.sleep(2 * (attempt + 1))
        if last_exc is not None:
            raise HTTPException(
                status_code=502,
                detail=f"Tracking link could not be resolved after 3 attempts: {type(last_exc).__name__}",
            ) from last_exc

        if status not in (301, 302, 303, 307, 308) or not location:
            raise HTTPException(
                status_code=502,
                detail="Tracking link did not redirect to the expected platform",
            )

        current = urljoin(current, location)

    raise HTTPException(status_code=502, detail="Too many tracking redirects")


def _screenshot_path(raw, field="screenshot", must_exist=True):
    """Resolve a path only when it stays inside the screenshots directory."""
    candidate = Path(raw).expanduser().resolve()
    root = SCREENSHOTS.resolve()
    if root != candidate and root not in candidate.parents:
        raise HTTPException(
            status_code=400,
            detail=f"{field} must be inside {SCREENSHOTS}",
        )
    if candidate.suffix.lower() not in ALLOWED_IMAGE_SUFFIXES:
        raise HTTPException(
            status_code=400,
            detail=f"{field} must be a PNG, JPG, JPEG, or WebP image",
        )
    if must_exist and not candidate.is_file():
        raise HTTPException(status_code=404, detail=f"{field} not found")
    return candidate


def _remove_screenshot(path):
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return False


def _telegram_send_photo(path, caption):
    """Upload one screenshot with stdlib multipart, no third-party client."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        raise HTTPException(
            status_code=503,
            detail="Telegram credentials are not configured on the host service",
        )

    boundary = f"----LokiScreenshot{uuid.uuid4().hex}"
    body = bytearray()

    def add_field(name, value):
        body.extend(
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                f"{value}\r\n"
            ).encode("utf-8")
        )

    add_field("chat_id", TELEGRAM_CHAT_ID)
    if caption:
        add_field("caption", caption[:1024])

    body.extend(
        (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="photo"; '
            f'filename="{path.name}"\r\n'
            f"Content-Type: image/png\r\n\r\n"
        ).encode("utf-8")
    )
    body.extend(path.read_bytes())
    body.extend(b"\r\n")
    body.extend(f"--{boundary}--\r\n".encode("ascii"))

    request = Request(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto",
        data=bytes(body),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    try:
        with build_opener().open(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise HTTPException(
            status_code=502,
            detail=f"Telegram rejected the photo: {detail}",
        ) from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Telegram photo upload failed: {type(exc).__name__}",
        ) from exc

    if not payload.get("ok"):
        raise HTTPException(
            status_code=502,
            detail=f"Telegram rejected the photo: {payload}",
        )
    return (payload.get("result") or {}).get("message_id")


class NotifyScreenshotRequest(BaseModel):
    screenshot: str | None = None
    caption: str = ""
    cleanup: str | None = None
    send: bool = True


@app.post("/notify-screenshot")
async def notify_screenshot(
    request: NotifyScreenshotRequest,
    authorization: str | None = Header(default=None),
):
    """Send a bid screenshot to Telegram, then always delete the image files.

    The n8n workflow calls this after a bid so the operator sees the filled
    proposal in Telegram. ``send=false`` performs cleanup only, which is used
    for skipped jobs. Cleanup is in ``finally`` on purpose: the host must not
    keep screenshots around when the notification fails.
    """
    authenticate(authorization)

    photo = None
    if request.screenshot:
        photo = _screenshot_path(request.screenshot, must_exist=request.send)
    elif request.send:
        raise HTTPException(status_code=400, detail="screenshot is required when send is true")

    cleanup = []
    if photo is not None:
        cleanup.append(photo)
    for raw in (request.cleanup or "").split(","):
        raw = raw.strip()
        if not raw:
            continue
        candidate = _screenshot_path(raw, field="cleanup", must_exist=False)
        if candidate not in cleanup:
            cleanup.append(candidate)

    message_id = None
    error = None
    if request.send:
        try:
            message_id = await run_in_threadpool(
                _telegram_send_photo, photo, request.caption
            )
        except HTTPException as exc:
            error = {"status_code": exc.status_code, "detail": exc.detail}
        finally:
            deleted = [str(path) for path in cleanup if _remove_screenshot(path)]
    else:
        deleted = [str(path) for path in cleanup if _remove_screenshot(path)]

    if error:
        return {
            "status": "telegram_failed",
            "sent": False,
            "error": error,
            "deleted": deleted,
        }

    return {
        "status": "sent" if request.send else "cleaned",
        "sent": request.send,
        "message_id": message_id,
        "deleted": deleted,
    }


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

