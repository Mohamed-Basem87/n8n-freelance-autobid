# Freelance Auto-Bid — n8n + Firefox

An n8n workflow plus a small Selenium service that write a tailored proposal, pick a price and duration, attach four portfolio images, and submit the offer on **Mostaql** and **Nafezly** — then report every attempt, success or failure, to Telegram.

It is the whole pipeline, not a template shell: the workflow, the browser backend, the deployment files and the operational notes.

```
 job feed  (RSS, scraper, bot, manual curl — anything)
     │  POST /webhook/loki-job
     ▼
 ┌─────────────────────────────────────────────┐
 │ n8n workflow                                │
 │  parse → inspect page → LLM → eligibility   │
 │  → submit gate → POST /prepare-offer        │
 └─────────────────────────────────────────────┘
     │  platform, url, period, cost, proposal
     ▼
 ┌─────────────────────────────────────────────┐
 │ host service (FastAPI + Selenium)           │
 │  headless Firefox, already-logged-in profile│
 │  fills the real form · uploads 4 images     │
 │  clicks submit · reads the result back      │
 └─────────────────────────────────────────────┘
     │
     ├── screenshot on disk
     └── status → Telegram alert
```

## What it actually does

1. **Receives a job** over a webhook. Anything that can POST JSON can drive it.
2. **Inspects the live page** in the browser and hands the text to an LLM.
3. **Writes the proposal** in the project's language, with the client's name when it is visible, and no pricing inside the text.
4. **Chooses cost and duration** from the project's own budget: the lowest figure inside the advertised range, raised only to the platform's real minimum. Missing budget or duration falls back to a documented default instead of blocking the bid.
5. **Fills the real form** — cost, period, proposal — and uploads exactly **four** portfolio images through the site's own upload widget (Dropzone on Mostaql, FileUploader on Nafezly).
6. **Submits**, then verifies: the form disappearing is a success, a visible validation alert is a failure, an unreadable outcome is reported as a failure rather than a guess.
7. **Alerts you** with job title, URL, platform, price, period, proposal, and the screenshot path.

## Two independent gates

Real bids are irreversible, so they need two switches, in different systems:

| gate | where | default in this repo |
|---|---|---|
| `Submit Enabled?` | n8n workflow | **off** (`={{ false }}`) |
| `ALLOW_SUBMIT` | backend env file | **0** |

Both must be on. With either off, the service still fills and validates the form and returns `dry_run_prepared` — a free rehearsal that costs no bid. That is the recommended way to test.

## Requirements

- **n8n** with the LangChain nodes, reachable from your browser, public API v1 available.
- **A chat model in n8n.** The workflow ships with Groq; any chat-model node with the same interface works.
- **A Telegram bot** and the chat id to send alerts to.
- **A Linux host** (Debian/Ubuntu tested) with Firefox ESR + geckodriver, Python 3.11+.
- **Accounts** on the platforms you enable, with bids available. Logged in once inside the bot profile.
- **Exactly four portfolio images**, because every proposal attaches four.

## Install

### 1. Backend

```bash
git clone https://github.com/Mohamed-Basem87/n8n-freelance-autobid.git
cd n8n-freelance-autobid
sudo ./deploy/install.sh
```

The script installs the packages, creates an `autobid` service user, builds a venv, prepares `/opt/freelance-autobid/data/{test-profile,screenshots,attachments}`, generates `host-api.env` with a random `API_KEY` and starts the service. It prints the key — copy it into n8n.

Check it:

```bash
curl http://localhost:8000/health     # {"status":"ok"}
```

### 2. Log in once

The service is headless and will not type passwords. Run, on a machine with a display (or X11/VNC):

```bash
sudo systemctl stop freelance-autobid
sudo -u autobid DISPLAY=:0 /opt/freelance-autobid/venv/bin/python /opt/freelance-autobid/login.py
sudo systemctl start freelance-autobid
```

Log in to both sites, clear any Cloudflare check, close the window. The cookies live in the profile and are reused by every later run.

### 3. Portfolio images

```bash
cp portfolio-{1,2,3,4}.png /opt/freelance-autobid/data/attachments/
```

Exactly four files, `.jpg/.jpeg/.png/.webp`, each under 10 MB. Any other count is refused *before* the browser opens, so you never post a proposal with missing images.

### 4. n8n

Set these on the n8n container/host:

```env
LOKI_API_KEY=<the API_KEY printed by install.sh>
LOKI_BACKEND_URL=http://host.docker.internal:8000   # or the host's IP
TELEGRAM_CHAT_ID=<your chat id>
N8N_BLOCK_ENV_ACCESS_IN_NODE=false                  # expressions read $env
```

Then **Import from File** → `workflow/freelance-autobid.json`, and attach credentials to six nodes:

| node | credential |
|---|---|
| `Groq Chat Model` | Groq API (or swap for another chat model) |
| `Telegram: Bid Result`, `Telegram: Error`, `Telegram: Skipped`, `Telegram: Not Sent` | your Telegram bot |

Credentials are *not* in the export — only references are, and they were stripped, so n8n will ask you for them.

### 5. Dry run, then arm

```bash
curl -X POST http://localhost:8000/prepare-offer \
  -H "Authorization: Bearer $LOKI_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"platform":"mostaql","url":"https://mostaql.com/project/123456",
       "period":3,"cost":25,"offer_description":"<at least 200 characters>","submit":false}'
```

Expect `dry_run_prepared`, `attachments.uploaded: 4`, `validation.browserValid: true`, and a screenshot. Look at it. Then arm:

1. `ALLOW_SUBMIT=1` in `host-api.env`, restart the service.
2. Set `Submit Enabled?` to `={{ true }}` in the workflow.

## Webhook contract

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

- `platform` is `mostaql` or `nafezly` (`kafiil` is recognised but not enabled).
- `url` must be `https` on the platform's own host. Anything else is rejected before a browser opens.
- `budget` is optional; the LLM reads it from the text as well.
- `description` is what the model sees. Pass the real project text, not a stub.

## Backend API

All endpoints except `/health` need `Authorization: Bearer <API_KEY>`.

| endpoint | purpose |
|---|---|
| `GET /health` | liveness |
| `POST /inspect` | open a platform URL and return title, text, screenshot |
| `POST /prepare-offer` | fill the form, upload images, optionally submit |
| `POST /open-login` | open the platform's home page in the bot profile |
| `POST /resolve-tracking` | follow a shortener to the real platform URL |

`/prepare-offer` accepts `platform`, `url`, `period` (1–90), `cost`, `offer_description`, `submit`, and optional `attachments`.

Limits enforced before any browser work:

| platform | cost | proposal |
|---|---|---|
| Mostaql | $5–$10,000 | 200–10,000 chars, no links/emails/domain names |
| Nafezly | $1–$10,000 | 30–5,000 chars |

Mostaql's rule is not cosmetic: its form rejects any `http://`, `www.`, e-mail, or bare `word.tld` token, and anything under 200 characters. The service mirrors that check so you get a clear alert instead of a dead submit click.

## Status values

| status | meaning |
|---|---|
| `submitted` | the form was accepted and disappeared |
| `dry_run_prepared` | filled and validated, nothing clicked |
| `already_bid` | no form because an offer already exists, or the daily limit is spent |
| `validation_failed` | the form or the service rejected the values |
| `attachment_failed` | fewer than four images landed |
| `attachments_unsupported` | the form has no upload field; nothing is submitted |
| `submit_disabled` | submission asked for, `ALLOW_SUBMIT` off |
| `challenge_failed` | a bot check never cleared |
| `review_required` | the model judged the job unsuitable or unclear |
| `submit_failed` / `submit_unknown` | click happened, result unreadable; treated as failure |

## Known limitations

- **Mostaql allows one bid per day** on the free plan. A second attempt returns `already_bid`; that is the platform, not a bug.
- **Selectors are tied to the current markup.** A redesign needs `host_app.py` updating. Screenshots and `page_title` in the alert tell you quickly whether it is a layout change.
- **The LLM is not deterministic.** The same job can come back `review_required` once and `eligible` the next time. The prompt is tuned to avoid that, but it is a model, not a rule engine.
- **Nafezly reports an existing offer as `review_required`** ("form not found") rather than `already_bid`. Cosmetic; nothing is submitted.
- **`/inspect` truncates page text at 12,000 characters.** Long projects lose their tail.
- **No dedupe queue.** Duplicates are stopped by reading the page (`already_bid`), not by a database.
- **One browser at a time.** Requests serialize on a lock; a stuck run blocks the queue until it times out.
- **The prompt contains a signature rule.** Set your own name there, or remove the signature.

## Troubleshooting

The failures worth knowing about are documented in [docs/troubleshooting.md](docs/troubleshooting.md): Firefox refusing to start, Cloudflare interstitials, Dropzone's hidden input, attachment counts, and "form disappeared but did it submit?".

## Legal and risk

Automating bids almost certainly breaks the terms of service of these platforms. You are responsible for your own account, and a ban is a real outcome. Nothing here bypasses a captcha, a login, or a paywall — but it is still automation, and the sites may treat it as such.

The software is provided as-is, with no warranty, and site changes will break it eventually.

## Credits

Built by **Mohamed Basem** — data analyst, Excel/Power Query/Power BI/Python/SQL.

MIT licensed, see [LICENSE](LICENSE).
