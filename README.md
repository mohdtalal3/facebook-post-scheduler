# fb-post-scheduler

A self-hosted Flask dashboard for scheduling and auto-posting to Facebook Pages. Manage multiple pages, set daily/weekly schedules, post text, styled backgrounds, or images — all from a clean web UI. No Google Sheets polling required.

---

## Features

- **Multi-page management** — add Facebook Pages via the Graph API, tokens stored server-side only
- **Post types** — Text, Text + Background (preset styles), Text + Image (Google Drive or direct URL)
- **Cron-style scheduling** — powered by APScheduler, fires posts at the exact configured time with timezone support
- **Edit / pause / delete** posts without restarting
- **First comment** — automatically posts a first comment after each post
- **Slack notifications** — success and failure alerts via Incoming Webhook
- **Password authentication** — single-password login, 30-day session
- **JSON storage** — `schedule.json` for post data, `tokens.json` for page tokens (never exposed in the browser)

---

## Tech Stack

| Layer | Library |
|---|---|
| Web framework | Flask |
| Scheduler | APScheduler (BackgroundScheduler + CronTrigger) |
| Facebook API | Graph API v25.0 |
| Image download | Google Drive API v3 (service account) |
| Notifications | Slack Incoming Webhooks |
| Auth | Flask session (password from `.env`) |
| Config | python-dotenv |

---

## Project Structure

```
fb-post-scheduler/
├── app.py                  # Flask app + APScheduler + all routes
├── main.py                 # Standalone Google Sheets-based poster (legacy)
├── requirements.txt
├── .env                    # Secrets (never commit)
├── feedblitz_credentials.json  # Google service account (never commit)
├── data/
│   ├── schedule.json       # Pages + post schedules
│   └── tokens.json         # Page access tokens
└── templates/
    ├── base.html
    ├── login.html
    ├── index.html
    ├── add_page.html
    ├── page_detail.html
    └── edit_post.html
```

---

## Setup

### 1. Clone & install

```bash
git clone https://github.com/mohdtalal3/facebook-post-scheduler
cd fb-post-scheduler
pip install -r requirements.txt
```

### 2. Configure `.env`

Create a `.env` file in the project root:

```env
APP_PASSWORD=your-login-password
SECRET_KEY=a-long-random-secret-string

FACEBOOK_USER_ACCESS_TOKEN=EAAxxxxxxxx...
SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...
CREDENTIALS_FILE=feedblitz_credentials.json
```

| Variable | Required | Description |
|---|---|---|
| `APP_PASSWORD` | ✅ | Password for the web dashboard |
| `SECRET_KEY` | ✅ | Flask session secret (use a long random string) |
| `FACEBOOK_USER_ACCESS_TOKEN` | ✅ | Used once to fetch your pages on the Add Page screen |
| `SLACK_WEBHOOK_URL` | Optional | Slack channel to receive post notifications |
| `CREDENTIALS_FILE` | For Drive images | Path to Google service account JSON |

### 3. Google Drive images (optional)

To post images from Google Drive links:

1. Create a [Google Cloud service account](https://console.cloud.google.com/iam-admin/serviceaccounts)
2. Enable the **Google Drive API**
3. Download the JSON key file and place it in the project root
4. Set `CREDENTIALS_FILE=your-key-file.json` in `.env`
5. Share each Drive file/folder with the service account email (`...@...iam.gserviceaccount.com`)

### 4. Run

```bash
python3 app.py
```

Open [http://localhost:5005](http://localhost:5005), log in with your `APP_PASSWORD`.

---

## Usage

### Adding a page

1. Click **Add Page** in the navbar
2. Optionally enter a Facebook User Access Token (or leave blank to use the one from `.env`)
3. Click **Fetch Pages** — select the pages to add
4. Click **Save Selected Pages**

Tokens are stored server-side in `data/tokens.json` and never shown in the browser.

### Scheduling a post

1. Click a page card on the dashboard
2. Fill in the **Schedule New Post** form:
   - **Post type** — Text / Text + Background / Text + Image
   - **Time + Timezone** — when to fire the post
   - **Days** — which days of the week to repeat
   - **Text** — post content
   - **First Comment** — optional comment posted immediately after
3. Click **Schedule Post**

The post is registered in APScheduler immediately and will fire at the exact configured time.

### Editing a post

Click the ✏️ **pencil** button on any post card to open the edit form. Changes take effect immediately — the cron job is updated without restarting.

---

## Notes

- Run with a single process (not multi-worker gunicorn) — APScheduler runs inside the app process
- The scheduler only fires posts whose scheduled time is **after** the app started — no accidental duplicate posts on restart
- `data/` and `.env` are in `.gitignore` — tokens and passwords are never committed

---

## License

MIT
