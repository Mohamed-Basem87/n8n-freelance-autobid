# Freelance Auto-Bid — n8n + Firefox

An n8n workflow plus a small Selenium service that writes a tailored proposal, picks a price and duration, attaches four portfolio images, and submits the offer on **Mostaql** and **Nafezly** — then reports every attempt, success or failure, to Telegram.

It is the whole pipeline, not a template shell: the workflow, the browser backend, the deployment files and the operational notes.

**Contents**

1. [What it does](#1-what-it-does)
2. [Architecture](#2-architecture)
3. [Getting started](#3-getting-started)
4. [Configuration](#4-configuration)
5. [Workflow setup](#5-workflow-setup)
6. [Browser authentication](#6-browser-authentication)
7. [Armed and unarmed](#7-armed-and-unarmed)
8. [Backend API](#8-backend-api)
9. [Troubleshooting](#9-troubleshooting)
10. [Known limitations](#10-known-limitations)
11. [Legal and risk](#11-legal-and-risk)

---

## 1. What it does

1. **Receives a job** over a webhook. Anything that can POST JSON can drive it.
2. **Inspects the live page** in the browser and hands the text to an LLM.
3. **Writes the proposal** in the project's language, with the client's name when it is visible, and no pricing inside the text.
4. **Chooses cost and duration** from the project's own budget: the lowest figure inside the advertised range, raised only to the platform's real minimum.
5. **Fills the real form** — cost, period, proposal — and uploads exactly **four** portfolio images through the site's own upload widget (Dropzone on Mostaql, FileUploader on Nafezly).
6. **Submits**, then verifies: the form disappearing is a success, a visible validation alert is a failure, an unreadable outcome is reported as a failure rather than a guess.
7. **Alerts you** with job title, URL, platform, price, period, proposal, and the screenshot path. If a node itself fails before an alert can be sent, a separate error workflow reports the failed node and links the execution.

## 2. Architecture

Five moving parts: your feed, n8n, the host service, Firefox inside it, and
Telegram. Three protocols: HTTP webhook, HTTP + bearer token, Telegram Bot API.

```
                job feed   (RSS · scraper · bot · cron · curl)
                    │
                    │ POST /webhook/loki-job   JSON over HTTP, no auth
                    ▼
            ┌───────┬────────────────────────────────────────────────────┐
            │ n8n workflow                                               │
            │                                                            │
            │ Parse Job → Inspect Page → AI Agent → Eligible?            │
            │ Eligible? → Submit Enabled? → Prepare Offer                │
            │                                                            │
            │ each outcome ends in a Telegram alert:                     │
            │ Error · Skipped · Not Sent · Bid Result                    │
            │ node failure → error workflow → Telegram                   │
            │                                                            │
            └───────┬─────────────────────────────────────┬──────────────┘
                    │ POST /inspect                       │ Telegram Bot API
                    │ POST /prepare-offer                 │ bot token
                    │ Authorization: Bearer <API_KEY>     │
                    │                                     │
            ┌───────┬────────────────────────────────┐    ┌──────────────┐
            │ host service                           │    │ Telegram     │
            │ FastAPI + Selenium                     │    │ your chat    │
            │ one Firefox at a time                  │    └──────────────┘
            │ headless Firefox ESR                   │
            │ profile data/test-profile/             │
            └───────┬────────────────────────────────┘
                    │
                    │ drives the real pages over HTTPS
                    ▼
      ┌─────────────┬──────────────────┐
      │ mostaql.com        nafezly.com │
      └─────────────┬──────────────────┘
                    │
                    └─► screenshots saved to data/screenshots/
```

Read it as three hops:

1. **Feed → n8n.** A JSON `POST` to the webhook starts a run. No auth by default.
2. **n8n → host service.** Two authenticated calls: `/inspect` (what does this page say?) and `/prepare-offer` (fill the form and, if armed, submit it). The `AI Agent` node calls whatever chat-model node is attached underneath it (Groq by default) — the model credential lives on that subnode, not on the agent.
3. **Host service → the sites.** Firefox, already logged in via its profile, types into the real form and uploads the four images. The result comes back as JSON, and n8n turns it into a Telegram message. n8n talks to Telegram directly, not through the service.

### Who talks to whom

| from | to | how | auth |
|---|---|---|---|
| your feed | n8n | `POST /webhook/loki-job`, JSON | none by default — see [Legal and risk](#11-legal-and-risk) |
| n8n | LLM provider | n8n credential (Groq by default) | API key in the credential |
| n8n | host service | HTTP on `:8000` | `Authorization: Bearer <API_KEY>` |
| host service | Mostaql / Nafezly | headless Firefox, real sessions | cookies in the profile |
| n8n | Telegram | Bot API | bot token in the credential |

### Why the split exists

n8n is good at orchestration and bad at being a browser. The service is good at being a browser and intentionally dumb: it takes a proposal, a cost, a period and four images, and it either submits them or explains why not. All judgement — eligibility, pricing, wording — lives in the workflow; all irreversible actions live behind two gates in the service.

### Sequencing

- **Serialized browsers.** The service holds a lock; two jobs arriving together queue rather than open two Firefox instances. A run that hangs blocks the queue until it times out (~120 s per call).
- **Typical timing.** Inspect ~10–20 s, prepare-offer ~25–90 s including uploads. The workflow waits for each call.
- **Stateless workflow.** No database, no queue. Duplicate prevention is the page itself: a project you already bid on returns `already_bid`.

## 3. Getting started

### 3.1 Prerequisites

| requirement | minimum | check |
|---|---|---|
| Linux host | Debian 11+ / Ubuntu 22.04+ | `cat /etc/os-release` |
| Firefox ESR | 102+ | `firefox-esr --version` |
| Python | 3.11+ | `python3 -V` |
| n8n | with LangChain nodes + public API | `docker ps \| grep n8n` |
| Chat model key | Groq, OpenAI, Anthropic, Ollama… | from the provider |
| Telegram bot | any | from [@BotFather](https://t.me/BotFather) |
| Platform accounts | Mostaql and/or Nafezly, logged in once | — |
| Portfolio images | exactly 4, `.png/.jpg/.jpeg/.webp`, ≤10 MB each | — |
| Disk | ~1 GB for profile + screenshots | `df -h /` |

Ubuntu note: the **snap** Firefox is confined and will not use a profile under `/opt`. Use Debian's `firefox-esr` package or Mozilla's official tarball.

### 3.2 Install the backend

```bash
git clone https://github.com/Mohamed-Basem87/n8n-freelance-autobid.git
cd n8n-freelance-autobid
sudo ./deploy/install.sh
```

What it does, in order: installs packages, creates the `autobid` system user, copies the code to `/opt/freelance-autobid`, builds a venv, creates `data/{test-profile,screenshots,attachments}`, writes `host-api.env` with a **random API key**, installs and starts the systemd unit.

It prints the key at the end. Copy it — you need it twice: in `host-api.env` (already there) and in n8n.

Verify:

```bash
curl -s http://localhost:8000/health          # {"status":"ok"}
systemctl is-active freelance-autobid          # active
```

### 3.3 Add four portfolio images

```bash
sudo cp portfolio-1.png portfolio-2.png portfolio-3.png portfolio-4.png \
        /opt/freelance-autobid/data/attachments/
sudo chown autobid:autobid /opt/freelance-autobid/data/attachments/*.png
```

Exactly four, or the service refuses to bid before the browser opens.

### 3.4 Log in once

See [Browser authentication](#6-browser-authentication). Short version:

```bash
sudo systemctl stop freelance-autobid
sudo -u autobid DISPLAY=:0 /opt/freelance-autobid/venv/bin/python \
     /opt/freelance-autobid/login.py
sudo systemctl start freelance-autobid
```

### 3.5 Set the n8n environment

The workflow reads these from `$env`, so they must exist in the **n8n process** — a compose `.env`, the container's `environment:`, or `~/.n8n/.env` for a bare-metal install:

```bash
# values for a Docker Compose deployment; see 4.3 for other setups
LOKI_API_KEY=<the key printed by install.sh>
LOKI_BACKEND_URL=http://172.17.0.1:8000
TELEGRAM_CHAT_ID=<your chat id>
N8N_BLOCK_ENV_ACCESS_IN_NODE=false
```

Restart n8n afterwards — `$env` is read at process start, not per execution.

Details and alternatives in [Configuration](#4-configuration).

### 3.6 Import and wire the workflow

See [Workflow setup](#5-workflow-setup). You need one chat-model credential and one Telegram credential, plus the small error workflow in [5.6](#56-failure-alerts).

### 3.7 Dry run

```bash
export LOKI_API_KEY=<your key>
curl -s -X POST http://localhost:8000/prepare-offer \
  -H "Authorization: Bearer $LOKI_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"platform":"mostaql","url":"https://mostaql.com/project/123456",
       "period":3,"cost":25,
       "offer_description":"<a real proposal of at least 200 characters>",
       "submit":false}'
```

Expect `dry_run_prepared`, `attachments.uploaded: 4`, `validation.browserValid: true` and a screenshot path. **Open the screenshot.** If the form is filled and four thumbnails are attached, you are ready to arm.

### 3.8 Arm it

Only after a dry run looks right — see [Armed and unarmed](#7-armed-and-unarmed).

## 4. Configuration

### 4.1 Backend (`/opt/freelance-autobid/host-api.env`)

| variable | required | default | meaning |
|---|---|---|---|
| `API_KEY` | **yes** | generated | shared secret. The workflow sends it as `Authorization: Bearer <API_KEY>` |
| `ALLOW_SUBMIT` | no | `0` | `1` permits real bids. `0` fills and validates but never clicks |
| `ATTACHMENTS_REQUIRED` | no | `1` | `0` allows bidding without images (not recommended) |
| `LOKI_DATA_DIR` | no | `/opt/freelance-autobid/data` | root for the three folders below |
| `LOKI_PROFILE_DIR` | no | `<data>/test-profile` | Firefox profile = the login session |
| `LOKI_SCREENSHOT_DIR` | no | `<data>/screenshots` | one PNG per attempt |
| `LOKI_ATTACHMENTS_DIR` | no | `<data>/attachments` | the four portfolio images |
| `LOKI_FIREFOX_BINARY` | no | `/usr/bin/firefox-esr` | Firefox binary path |

Listening address and port are **not** environment variables — they are in the systemd `ExecStart`:

```
ExecStart=/opt/freelance-autobid/venv/bin/uvicorn host_app:app --host 0.0.0.0 --port 8000
```

Change that line, then `systemctl daemon-reload && systemctl restart freelance-autobid`.

### 4.2 n8n

These names are fixed by the workflow's expressions, so do not rename them:

| variable | required | example | meaning |
|---|---|---|---|
| `LOKI_API_KEY` | **yes** | `9f3c…` | must equal `API_KEY` in `host-api.env` |
| `LOKI_BACKEND_URL` | **yes** | `http://172.17.0.1:8000` | where n8n reaches the service |
| `TELEGRAM_CHAT_ID` | **yes** | `123456789` | where alerts go |
| `N8N_BLOCK_ENV_ACCESS_IN_NODE` | **yes** | `false` | n8n blocks `$env` in expressions unless this is `false` |

### 4.3 Reaching the backend from n8n

`LOKI_BACKEND_URL` must be an address **as seen from the n8n process**, not from your laptop.

| deployment | value |
|---|---|
| n8n and the service on the same machine | `http://127.0.0.1:8000` |
| n8n in Docker Desktop (Mac/Windows) | `http://host.docker.internal:8000` |
| n8n in Docker on Linux, bridge network | the bridge gateway, usually `http://172.17.0.1:8000` |
| n8n in Docker on Linux, with `--add-host` | `http://host.docker.internal:8000` |
| another machine | the host's LAN address, firewall permitting |

Find the gateway:

```bash
docker network inspect bridge --format '{{range .IPAM.Config}}{{.Gateway}}{{end}}'
```

Test from **inside** the n8n container, because that is the process that has to connect:

```bash
docker exec -it <n8n-container> \
  wget -qO- --header="Authorization: Bearer $LOKI_API_KEY" \
  "$LOKI_BACKEND_URL/health"
```

`{"status":"ok"}` means it is reachable and the key is accepted.

### 4.4 Where each credential comes from

| credential | used by | how to get it |
|---|---|---|
| `API_KEY` | backend ↔ n8n | generated by `install.sh`; or `openssl rand -hex 32`. Put it in `host-api.env` **and** n8n's `LOKI_API_KEY` |
| Groq API key | `Groq Chat Model` node | [console.groq.com/keys](https://console.groq.com/keys) → *Create API Key* |
| Telegram bot token | the four Telegram nodes | [@BotFather](https://t.me/BotFather) → `/newbot` → token like `123456:ABC-DEF…` |
| Telegram chat id | `TELEGRAM_CHAT_ID` | send the bot a message, then `curl https://api.telegram.org/bot<TOKEN>/getUpdates` and read `result[].message.chat.id` |

For a Telegram **group**, add the bot to the group and use the negative id (e.g. `-1001234567890`). For a channel, the bot must be an admin.

Use a different model provider? Replace the `Groq Chat Model` node with any chat-model node, keep it connected to the `AI Agent` node, and leave the prompt untouched.

## 5. Workflow setup

### 5.1 Import

In n8n: **Workflows → ⋯ → Import from File** → `workflow/freelance-autobid.json`. Import `workflow/failure-alerts.json` the same way (see [5.6](#56-failure-alerts)).

Or from the CLI:

```bash
n8n import:workflow --input=/path/to/workflow/freelance-autobid.json
```

The workflow imports **inactive**. The file contains 21 nodes and no credentials.

### 5.2 Attach credentials

Five nodes need one. Open each and pick a credential (n8n offers *Create new* inline):

| node | credential type | value |
|---|---|---|
| `Groq Chat Model` | Groq API | your key |
| `Telegram: Bid Result` | Telegram API | your bot token |
| `Telegram: Error` | Telegram API | same bot |
| `Telegram: Skipped` | Telegram API | same bot |
| `Telegram: Not Sent` | Telegram API | same bot |

`AI Agent` needs none: it uses the chat-model node attached under it. If a Telegram node shows *Credential not found*, it is only a leftover reference — select your credential from the dropdown and save.

The error workflow is imported separately: open `Telegram: Failure Alert` in *Freelance Auto-Bid - Failure Alerts* and pick the same Telegram credential (the error workflow ships without credential references).

### 5.3 Check the wiring

The exported graph, exactly as it connects. Do not flatten the branches:

```
Webhook
 └─ Parse Loki Notification
     └─ Has Job URL?
         ├─ no  → Stop: No Job URL                    (ends silently)
         └─ yes → Requires Resolution?
             ├─ yes → Classify URL → Resolve Tracking URL → Inspect Job Page
             └─ no  → Inspect Job Page
                        └─ Trim Job Context
                            └─ AI Agent  (Groq Chat Model attached)
                                └─ Parse Application Proposal
                                    └─ Eligible?
                                        ├─ no  → Stop: Not Eligible → Telegram: Skipped
                                        └─ yes → Submit Enabled?
                                            ├─ no  → Stop: Submit Disabled → Telegram: Not Sent
                                            └─ yes → Prepare Offer
                                                ├─ success → Telegram: Bid Result
                                                └─ error   → Telegram: Error
```

A green run with no bid is almost always `Submit Enabled?` returning false — that is the design, not a failure.

One gap worth knowing: a job that arrives **without a URL** stops at `Stop: No Job URL` and sends nothing. If your feed can emit that, wire a Telegram node after it.

### 5.4 Things worth editing before you use it

| where | what |
|---|---|
| `AI Agent` → system message, section 3 | the skills list. Put **your** real skills there |
| `AI Agent` → system message, rule 11 | the signature. Replace with your name or delete the rule |
| `Submit Enabled?` | stays `false` until you are ready — see [Armed and unarmed](#7-armed-and-unarmed) |
| `Webhook` node | the path (`loki-job`); change it if you like, then use the new URL |

### 5.5 Activate

Toggle the workflow **Active** and confirm the production webhook answers:

```bash
curl -i -X POST http://<n8n>:5678/webhook/loki-job \
  -H 'Content-Type: application/json' \
  -d '{"source":"test","platform":"nafezly","title":"t","description":"d",
       "url":"https://nafezly.com/project/00000","budget":null}'
```

While the workflow is inactive, only the test URL works: `/webhook-test/loki-job`, and it needs **Execute workflow** clicked first.

### 5.6 Failure alerts

The main workflow sends a Telegram message for every outcome it can classify — bid landed, skipped, gate off, backend said no. It cannot send one when a node itself dies (backend HTTP 500, tracking URL unreachable, model call failed): the run aborts before any alert node.

`workflow/failure-alerts.json` closes that gap. It is three nodes: **Error Trigger → Format Failure Alert → Telegram**.

1. Import it, attach your Telegram credential to `Telegram: Failure Alert`, and publish/activate it.
2. Open the main workflow → **Settings → Error Workflow** → select *Freelance Auto-Bid - Failure Alerts*.

Every failed run then sends one message with the workflow name, the failed node, the error text, and a link to the execution. The main graph is untouched.

Quick test: import the error workflow, create a throwaway workflow with a `Webhook` node and an `HTTP Request` node pointing at `http://127.0.0.1:9/`, set the same **Error Workflow** on it, activate it, and POST to its webhook. The alert should arrive within seconds.

## 6. Browser authentication

The service never types a password. It uses a real Firefox profile that you log into once, by hand, on a machine with a display (or X11/VNC).

### 6.1 Why a profile and not a token

Both platforms authenticate with cookies, and Mostaql adds Cloudflare in front. A cookie jar inside a persistent profile reproduces a normal browser: Cloudflare sees a session that already passed, so there is no captcha to solve on every run.

The trade-off is that the profile **is** the credential. Treat `data/test-profile/` like a password.

### 6.2 Log in

```bash
sudo systemctl stop freelance-autobid
sudo -u autobid DISPLAY=:0 \
  /opt/freelance-autobid/venv/bin/python /opt/freelance-autobid/login.py
```

`login.py` opens Firefox on the bot's profile, at both login pages. Log in to **Mostaql** and **Nafezly**, complete any phone/e-mail verification, clear any Cloudflare interstitial, then close the window. It exits when the last window closes.

The service must be **stopped** first: Firefox refuses to start against a profile that another process owns, and `login.py` checks for the lock file and tells you.

No display on the server? Two options:

```bash
# X11 forwarding from your machine
ssh -X user@server sudo -u autobid DISPLAY=$DISPLAY \
  /opt/freelance-autobid/venv/bin/python /opt/freelance-autobid/login.py

# or install a VNC server and log in through it
```

### 6.3 Confirm the session survived

```bash
sudo systemctl start freelance-autobid
curl -s -X POST http://localhost:8000/inspect \
  -H "Authorization: Bearer $LOKI_API_KEY" -H 'Content-Type: application/json' \
  -d '{"platform":"mostaql","url":"https://mostaql.com/dashboard","title":"","description":""}' \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["status"], "|", d["page_title"])'
```

A dashboard title, not a login page or *Just a moment…*, means the cookies are working.

### 6.4 When the session dies

Symptoms: `challenge_failed`, a login page in the screenshot, or `already_bid` on projects that are plainly open.

Fix: stop the service, re-run `login.py`, start it again. Sessions usually last weeks; password changes, "log out everywhere", or a platform security reset will end one early.

### 6.5 Profile hygiene

| do | why |
|---|---|
| keep the profile owned by the service user | one `sudo` run as root breaks every later run |
| never commit `data/test-profile/` | it holds live cookies (~340 MB) |
| back it up, encrypted, if you care | re-logging in is the only recovery |
| clear a stale `lock` only when no Firefox is running | see [Troubleshooting](#9-troubleshooting) |

### 6.6 What is not in this repo

Everything needed to run it is here. What is deliberately absent is everything that belongs to one person:

| not included | why | how you get yours |
|---|---|---|
| the Firefox profile / browser session | ~340 MB of live login cookies for **your** accounts | `login.py` creates it once |
| API keys, bot token, chat id, model key | credentials never travel in a repo | `install.sh` or your provider |
| portfolio images | they are your work | drop four files in the attachments folder |
| your job feed | the workflow takes any JSON webhook | any RSS/scraper/bot, or `curl` |
| geckodriver | machine binary | distro package, or Selenium Manager downloads it |

## 7. Armed and unarmed

Real bids are irreversible, so they need two switches in different systems:

| gate | where | default here |
|---|---|---|
| `Submit Enabled?` | n8n workflow | **off** (`={{ false }}`) |
| `ALLOW_SUBMIT` | `host-api.env` | **0** |

Both must be on. With either off, nothing is submitted:

| gate off | what happens |
|---|---|
| `ALLOW_SUBMIT=0` (backend) | the service fills and validates the real form, uploads the images, then returns `dry_run_prepared` — a free rehearsal |
| `Submit Enabled? = false` (n8n) | the workflow stops before calling `/prepare-offer` and sends `Telegram: Not Sent` with the price it would have used |

To rehearse the whole browser path without risking a bid, leave the backend at `0` and send `"submit": true` manually — you get the filled form and a screenshot.

**To arm:**

```bash
sudo sed -i 's/^ALLOW_SUBMIT=.*/ALLOW_SUBMIT=1/' /opt/freelance-autobid/host-api.env
sudo systemctl restart freelance-autobid
```

Then set `Submit Enabled?` to `={{ true }}` in n8n.

**To disarm**, reverse either one. Disarming the backend is the safer habit: it stops real bids even if the workflow is left armed.

## 8. Backend API

All endpoints except `/health` need `Authorization: Bearer <API_KEY>`.

| endpoint | purpose |
|---|---|
| `GET /health` | liveness, no auth |
| `POST /inspect` | open a platform URL, return title, text, screenshot |
| `POST /prepare-offer` | fill the form, upload images, optionally submit |
| `POST /open-login` | load a platform home page in the bot profile (a session check, not an interactive login) |
| `POST /resolve-tracking` | follow a shortener to the real platform URL |

`/prepare-offer` accepts `platform`, `url`, `period` (1–90), `cost`, `offer_description`, `submit`, and optional `attachments`.

Limits enforced before any browser work:

| platform | cost | proposal |
|---|---|---|
| Mostaql | $5–$10,000 | 200–10,000 chars, no links/emails/domain names |
| Nafezly | $1–$10,000 | 30–5,000 chars |

Mostaql's rule is not cosmetic: its form rejects any `http://`, `www.`, e-mail, or bare `word.tld` token, and anything under 200 characters. The service mirrors that check so you get a clear alert instead of a dead submit click.

Transient failures are retried where a retry is safe. `/resolve-tracking` tries three times (2 s, then 4 s backoff) before returning `502`. `/inspect` retries one navigation timeout, stops the load, and reads the DOM that arrived; it returns `502` only if the page is completely empty. **Submits are never retried** — an ambiguous submit is reported as `submit_failed` / `submit_unknown`.

### Webhook contract

`POST http://<n8n>:5678/webhook/loki-job`

```json
{
  "source": "my-feed",
  "platform": "mostaql",
  "title": "Data analysis dashboard",
  "description": "free text, ideally the full project body",
  "url": "https://mostaql.com/project/123456",
  "budget": "$25.00 - $50.00"
}
```

- `platform` is `mostaql` or `nafezly`.
- `url` must be `https` on the platform's own host; anything else is rejected before a browser opens.
- `budget` is optional, the LLM also reads it from the text.
- `description` is what the model sees — pass the real project text, not a stub.

### Status values

| status | meaning |
|---|---|
| `submitted` | the form was accepted and disappeared |
| `dry_run_prepared` | filled and validated, nothing clicked |
| `already_bid` | no form: an offer already exists, or the daily limit is spent |
| `validation_failed` | the form or the service rejected the values |
| `attachment_failed` | fewer than four images landed |
| `attachments_unsupported` | the form has no upload field; nothing submitted |
| `submit_disabled` | submission asked for, `ALLOW_SUBMIT` off |
| `challenge_failed` | a bot check never cleared |
| `review_required` | the model judged the job unsuitable or unclear |
| `submit_failed` / `submit_unknown` | click happened, result unreadable; treated as failure |

## 9. Troubleshooting

Full detail in [docs/troubleshooting.md](docs/troubleshooting.md). The short table:

| symptom | likely cause | fix |
|---|---|---|
| `401 Unauthorized` | `LOKI_API_KEY` ≠ `API_KEY`, or no header | make them identical, restart n8n |
| connection refused / timeout from n8n | wrong `LOKI_BACKEND_URL` for the container's network | use the bridge gateway or `host.docker.internal`; test with `docker exec … wget` |
| service inactive | port taken, bad env file, Firefox missing | `journalctl -u freelance-autobid -n 50` |
| `Failed to set preferences` | profile lock or wrong owner | stop, remove `data/test-profile/lock`, `chown -R autobid:autobid data` |
| `challenge_failed` / *Just a moment…* | session expired, Cloudflare re-challenging | re-run `login.py` |
| `already_bid` on an open project | Mostaql's one-bid-per-day limit | wait for the reset; it is the platform, not a bug |
| `attachment_failed` | not exactly 4 images, >10 MB, wrong folder, or the upload widget changed | check the files, then the screenshot |
| `attachments_unsupported` | that form has no upload field | nothing is submitted; bid manually if you accept no images |
| `validation_failed` (Mostaql) | proposal under 200 chars, or contains a link/domain/e-mail | rewrite the proposal; the alert names the offending fragment |
| `review_required` + "form not found" (Nafezly) | you already bid on that project | cosmetic; nothing was submitted |
| workflow runs, nothing is bid | a gate is off | check `Submit Enabled?` and `ALLOW_SUBMIT`; read `Telegram: Not Sent` |
| failed run sends no alert | no error workflow set on the main workflow | set **Settings → Error Workflow** (see [5.6](#56-failure-alerts)); the normal alert nodes cannot run after a node error |
| `$env` values are empty | n8n blocks env access | `N8N_BLOCK_ENV_ACCESS_IN_NODE=false`, restart n8n |
| Telegram *chat not found* | wrong chat id, or you never messaged the bot | message the bot, re-read `getUpdates` |
| workflow imports with unknown nodes | LangChain nodes missing | enable/install n8n LangChain nodes, re-import |
| disk full | screenshots and profile grow | prune `data/screenshots`, keep the profile |
| every run times out | a stuck browser holds the lock | `ps aux \| grep firefox`, restart the service |

## 10. Known limitations

- **Mostaql allows one bid per day** on the free plan. A second attempt returns `already_bid`.
- **Selectors are tied to the current markup.** A redesign needs `host_app.py` updating. The screenshot and `page_title` in the alert tell you quickly whether it is a layout change.
- **The LLM is not deterministic.** The same job can come back `review_required` once and `eligible` the next time.
- **Nafezly reports an existing offer as `review_required`** ("form not found") rather than `already_bid`. Cosmetic.
- **`/inspect` truncates page text at 12,000 characters.** Long projects lose their tail.
- **No dedupe queue.** Duplicates are stopped by reading the page, not by a database.
- **One browser at a time.** Requests serialize; a hung run blocks the queue until it times out.
- **The prompt contains a signature rule.** Set your own name there, or remove the signature.
- **Ubuntu's snap Firefox is confined** and will not use a profile under `/opt`. Use Debian's `firefox-esr` or Mozilla's tarball.
- **The import does not carry credentials.** That is intentional; you attach your own.

## 11. Legal and risk

Automating bids almost certainly breaks the terms of service of these platforms. You are responsible for your own account, and a ban is a real outcome. Nothing here bypasses a captcha, a login, or a paywall — but it is still automation, and the sites may treat it as such.

The webhook has **no authentication** by default: anything that can reach n8n can ask it to place a bid. Put it behind a reverse proxy with a shared secret, or keep n8n off untrusted networks.

The software is provided as-is, with no warranty, and site changes will break it eventually.

## Credits

Built by **Mohamed Basem** — data analyst, Excel/Power Query/Power BI/Python/SQL.

MIT licensed, see [LICENSE](LICENSE).
