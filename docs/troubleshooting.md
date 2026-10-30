# Troubleshooting

Everything here comes from failures that actually happened while building this, not from theory.

## The service will not start

**`Failed to set preferences: ...` or Firefox exits immediately**

A stale profile lock. Firefox writes `data/test-profile/lock` as a symlink to `<host>:+<pid>`; a crashed run or a probe started as another user leaves it behind.

```bash
ls -l /opt/freelance-autobid/data/test-profile/lock
sudo systemctl stop freelance-autobid
sudo rm -f /opt/freelance-autobid/data/test-profile/lock
sudo chown -R autobid:autobid /opt/freelance-autobid/data/test-profile
sudo systemctl start freelance-autobid
```

The service also clears the lock itself when no process owns it, but a *live* run and `login.py` must never share the profile.

**Profile owned by the wrong user**

Never run `host_app.py`, `login.py` or a manual probe as `root`. One `sudo` run makes every later run fail with a permissions error.

## The bid form never appears

`already_bid` is returned when `form#project__bid` (Mostaql) or `#offer-form` (Nafezly) is absent. Usually that is correct:

- Mostaql free accounts get **one bid per day**. After it is used the page says `لا يمكنك تقديم المزيد من العروض الآن` and no form renders until the reset.
- You already bid on that project.
- The project closed between the alert and the run.

Check the screenshot the service saved — the page text tells you which of the three it was.

## Attachments

**`attachment_failed` on Mostaql**

Mostaql uses Dropzone. The form also contains a plain `input[type=file]` that Dropzone **ignores** — sending files to it produces no error and no upload. The service drives Dropzone's own hidden input (created at init and appended to `<body>`, not inside the form) and then waits for `multiple_up_completed`, which Mostaql sets only for a dropzone inside the form's `.form-group`.

If this breaks after a redesign, the check to redo is: does `[data-ui="dropzone"]` still exist, and does its hidden input accept four files?

**`attachments_unsupported`**

The form on that page has no upload field at all. The service refuses to bid rather than post a proposal without images.

**Wrong image count**

The service insists on exactly four files, all inside the configured attachments directory, image suffixes only, ≤10 MB each. It fails loudly instead of silently attaching three.

## "The form disappeared — did it submit?"

Mostaql removes the bid form on success, so the service treats `form_disappeared` as success, but only after checking for a visible validation alert. If neither an alert nor a confirmation is readable, the result is `submit_failed` or `submit_unknown` and is reported as a failure. A bid you think landed but did not is worse than an alert.

To confirm independently, open the project page in a browser — your offer, its amount, and the attached images are listed under `العروض المقدمة`.

## Mostaql rejects the proposal text

The bid form runs a client-side validator *in addition* to native HTML5 validation:

- `min_length[200]` on `bid__details`
- `prevent_urls` — rejects any scheme, any e-mail, and any bare `word.tld`, including two-letter TLDs
- `multiple_up_completed` — uploaded files must be finished

`form.checkValidity()` returns `true` for text that fails all three. The service mirrors the length and link rules in Python before opening a browser, so you get `validation_failed` with the offending fragment instead of a dead click.

## Bot checks

A page titled *Just a moment…*, *Attention Required!* or *Checking your browser* means Cloudflare never let the real page load. The workflow detects those titles and the alert says the AI never saw the job. The service waits up to 40 seconds for the interstitial to clear; if it does not, the run fails cleanly.

If this starts happening every time, the session cookies have probably expired. Re-run `login.py`.

## Costs

Each platform clamps to its own minimum, so a $10 bid can be submitted as $25:

- The LLM proposes a number from the advertised budget.
- The backend reads the form's real minimum (`costValidationMinValue` on Mostaql, the `min` attribute on Nafezly's `#cost`) and raises the value to it.
- The alert shows `(raised from X to the project minimum)` when that happens.

If the floor looks wrong, the project page is the source of truth — read the minimum from the form, not from the budget text.

## n8n side

**`$env` expressions are empty** — set `N8N_BLOCK_ENV_ACCESS_IN_NODE=false` and restart n8n. Confirm nothing else is shadowing the name: `docker exec <n8n> env | grep LOKI`.

**`401 Unauthorized` from the backend** — two separate causes, easy to confuse:

1. The key differs. `LOKI_API_KEY` in n8n must equal `API_KEY` in `host-api.env` exactly. Compare without printing the whole secret:
   ```bash
   grep '^API_KEY=' /opt/freelance-autobid/host-api.env | cut -d= -f2 | sha256sum
   docker exec <n8n> printenv LOKI_API_KEY | sha256sum
   ```
2. The key is right but the request never arrives. `LOKI_BACKEND_URL` is resolved **inside the container**, where `localhost` is the container, not the host.

**Connection refused / timeout**

| n8n runs | use |
|---|---|
| on the host | `http://127.0.0.1:8000` |
| Docker Desktop | `http://host.docker.internal:8000` |
| Docker on Linux (bridge) | `http://172.17.0.1:8000` (`docker network inspect bridge`) |

Test from inside the container, because that is the process that must connect:

```bash
docker exec -it <n8n> wget -qO- --header="Authorization: Bearer $LOKI_API_KEY" \
  "$LOKI_BACKEND_URL/health"
```

**The workflow runs but nothing is bid** — by design, one of the gates is off:

- `Submit Enabled?` false → you get `Telegram: Not Sent` with the price it would have used.
- `ALLOW_SUBMIT=0` → the form is filled and validated and the Bid Result alert says `dry_run_prepared`, with a screenshot to inspect.

Both must be on for a real bid.

**Import shows unknown nodes or *Credential not found*** — the LangChain nodes are missing (old n8n, or a stripped image), or the credential reference survived an upgrade. Install/enable the LangChain nodes and re-import; then pick your credentials from each dropdown.

**Same job twice** — there is no dedupe layer. A second run on the same project hits `already_bid` at the page level. If your feed repeats jobs aggressively, dedupe upstream.

## Telegram says "chat not found"

- The bot cannot start a conversation. Send it a message (or `/start`) first, then re-read the id.
- Groups: the id is negative (`-100…`) and the bot must be a member, with privacy mode off or the message must mention it.
- Channels: the bot must be an administrator.
- Re-read the id from the source of truth:

```bash
curl -s "https://api.telegram.org/bot<TOKEN>/getUpdates" \
  | python3 -c 'import json,sys; [print(u["message"]["chat"]["id"], u["message"]["chat"].get("title") or u["message"]["chat"].get("first_name")) for u in json.load(sys.stdin)["result"] if "message" in u]'
```

## The service is inactive or restarts in a loop

```bash
systemctl status freelance-autobid --no-pager
journalctl -u freelance-autobid -n 60 --no-pager
```

| log line | cause |
|---|---|
| `address already in use` | another uvicorn holds port 8000 |
| `KeyError: 'API_KEY'` | the env file is missing, unreadable, or not loaded |
| `FileNotFoundError … firefox` | wrong `LOKI_FIREFOX_BINARY` |
| `Permission denied … test-profile` | profile owned by another user |
| `Failed to set preferences` | stale `lock`, see above |

## A run hangs and blocks everything

The service allows one browser at a time. A browser that never quits holds the lock, and every later call waits.

```bash
ps aux | grep -E 'firefox|geckodriver' | grep -v grep
sudo systemctl restart freelance-autobid
```

If it repeats, check `/dev/shm` size — Firefox needs it, and small containers starve it:

```bash
df -h /dev/shm
```

## Disk fills up

`data/test-profile` is ~340 MB and grows slowly; `data/screenshots` grows one file per attempt. Prune screenshots, keep the profile:

```bash
find /opt/freelance-autobid/data/screenshots -type f -mtime +30 -delete
```

