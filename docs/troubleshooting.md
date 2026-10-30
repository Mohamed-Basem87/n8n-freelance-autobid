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

**`$env` expressions are empty** — set `N8N_BLOCK_ENV_ACCESS_IN_NODE=false` and restart n8n.

**`401 Unauthorized` from the backend** — the n8n header and `API_KEY` in `host-api.env` differ, or the n8n container cannot reach `LOKI_BACKEND_URL`. Test from inside the container:

```bash
docker exec -it <n8n> wget -qO- http://host.docker.internal:8000/health
```

**The workflow runs but nothing is bid** — by design. Check both gates: `Submit Enabled?` in the workflow and `ALLOW_SUBMIT` in the env file. The `Telegram: Not Sent` alert tells you the gate is off and what price would have been used.

**Same job twice** — there is no dedupe layer. A second run on the same project hits `already_bid` at the page level. If your feed repeats jobs aggressively, dedupe upstream.
