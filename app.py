"""
Facebook Auto-Poster — Flask Dashboard
──────────────────────────────────────
• Manage Facebook pages via the Graph API
• Schedule posts (Text / Text+Background / Text+Image) with day/time rules
• schedule.json  ← posts + page metadata  (no tokens)
• tokens.json    ← page_id → access_token  (never sent to browser)
• Background scheduler ticks every second, reads schedule.json
• Slack notifications on every post success / failure
"""

import os
import re
import json
import uuid
import time
import io
import threading
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

import requests
from google.oauth2 import service_account
from googleapiclient.discovery import build as google_build
from googleapiclient.http import MediaIoBaseDownload
from functools import wraps
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, session
from dotenv import load_dotenv

load_dotenv()

# ── App setup ─────────────────────────────────────────────────────────────────
app = Flask(__name__)
app.secret_key             = os.getenv("SECRET_KEY", "change-this-in-dot-env")
app.permanent_session_lifetime = timedelta(days=30)

GRAPH_API_BASE = "https://graph.facebook.com/v25.0"
BASE_DIR       = os.path.dirname(os.path.abspath(__file__))
DATA_DIR       = os.path.join(BASE_DIR, "data")
SCHEDULE_FILE  = os.path.join(DATA_DIR, "schedule.json")
TOKENS_FILE    = os.path.join(DATA_DIR, "tokens.json")

_file_lock = threading.Lock()

# ── Lookups ───────────────────────────────────────────────────────────────────
TZ_MAP = {
    "EST": "America/New_York",  "EDT": "America/New_York",
    "CST": "America/Chicago",   "CDT": "America/Chicago",
    "MST": "America/Denver",    "MDT": "America/Denver",
    "PST": "America/Los_Angeles", "PDT": "America/Los_Angeles",
    "UTC": "UTC", "GMT": "UTC",
    "PKT": "Asia/Karachi", "PKST": "Asia/Karachi",
    "PAK": "Asia/Karachi", "PAKISTAN": "Asia/Karachi",
}

BACKGROUND_PRESETS = {
    "106018623298955":  "Solid purple",
    "365653833956649":  "Pink tropical plants",
    "618093735238824":  "Brown illustration",
    "191761991491375":  "3D hearts",
    "2193627793985415": "3D heart eyes emojis",
    "200521337465306":  "3D flame emojis",
    "1821844087883360": "Walking Yellow illustration",
    "177465482945164":  "Light purple 3D cube pattern",
    "160419724814650":  "Orange with Pink illustration",
    "248623902401250":  "3D smiling emoji background",
    "240401816771706":  "3D rose emojis",
    "1868855943417360": "3D crying laughter emoji",
    "255989551804163":  "Eye Pink illustration",
    "1654916007940525": "Light grey illustration",
    "1679248482160767": "Light blue illustration",
    "518948401838663":  "Pink heart pattern on pink background",
    "423339708139719":  "Illustration",
    "204187940028597":  "Solid red",
    "518596398537417":  "Red illustration",
    "901751159967576":  "Gradient dark orange red",
    "1271157196337260": "Solid red (alt)",
    "174496469882866":  "Lemon Yellow illustration",
    "862667370603267":  "Egg Light yellow illustration",
    "127541261450947":  "Ball Green illustration",
}

DAYS_SHORT = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

# ── Data helpers ──────────────────────────────────────────────────────────────
def ensure_data_dir():
    os.makedirs(DATA_DIR, exist_ok=True)
    if not os.path.exists(SCHEDULE_FILE):
        with open(SCHEDULE_FILE, "w") as f:
            json.dump({"pages": {}, "posts": []}, f, indent=2)
    if not os.path.exists(TOKENS_FILE):
        with open(TOKENS_FILE, "w") as f:
            json.dump({}, f, indent=2)


def load_schedule() -> dict:
    with _file_lock:
        with open(SCHEDULE_FILE) as f:
            return json.load(f)


def save_schedule(data: dict):
    with _file_lock:
        with open(SCHEDULE_FILE, "w") as f:
            json.dump(data, f, indent=2)


def load_tokens() -> dict:
    with _file_lock:
        with open(TOKENS_FILE) as f:
            return json.load(f)


def save_tokens(data: dict):
    with _file_lock:
        with open(TOKENS_FILE, "w") as f:
            json.dump(data, f, indent=2)


# ── Facebook helpers ──────────────────────────────────────────────────────────
def get_facebook_pages(user_token: str) -> list:
    resp = requests.get(
        f"{GRAPH_API_BASE}/me/accounts",
        params={"fields": "id,name,access_token", "access_token": user_token},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json().get("data", [])


def fb_post_text(page_id: str, token: str, message: str) -> dict:
    resp = requests.post(
        f"{GRAPH_API_BASE}/{page_id}/feed",
        data={"message": message, "access_token": token},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def fb_post_text_background(page_id: str, token: str, message: str, preset_id: str) -> dict:
    resp = requests.post(
        f"{GRAPH_API_BASE}/{page_id}/feed",
        data={"message": message, "text_format_preset_id": preset_id, "access_token": token},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


_drive_svc = None


def _get_drive_svc():
    global _drive_svc
    if _drive_svc is None:
        creds_file = os.getenv("CREDENTIALS_FILE", "feedblitz_credentials.json")
        creds = service_account.Credentials.from_service_account_file(
            creds_file, scopes=["https://www.googleapis.com/auth/drive.readonly"]
        )
        _drive_svc = google_build("drive", "v3", credentials=creds)
    return _drive_svc


def _extract_drive_file_id(url: str):
    for pattern in (r'/file/d/([a-zA-Z0-9_-]+)', r'id=([a-zA-Z0-9_-]+)', r'/d/([a-zA-Z0-9_-]+)'):
        m = re.search(pattern, url)
        if m:
            return m.group(1)
    return None


def fb_post_photo(page_id: str, token: str, caption: str, image_url: str) -> dict:
    file_id = _extract_drive_file_id(image_url)
    if file_id:
        svc       = _get_drive_svc()
        meta      = svc.files().get(fileId=file_id, fields="name,mimeType").execute()
        buffer    = io.BytesIO()
        drive_req = svc.files().get_media(fileId=file_id)
        downloader = MediaIoBaseDownload(buffer, drive_req)
        done = False
        while not done:
            _, done = downloader.next_chunk()
        buffer.seek(0)
        resp = requests.post(
            f"{GRAPH_API_BASE}/{page_id}/photos",
            files={"source": (meta["name"], buffer, meta["mimeType"])},
            data={"caption": caption, "access_token": token},
            timeout=60,
        )
    else:
        resp_img = requests.get(image_url, stream=True, timeout=60)
        resp_img.raise_for_status()
        ct     = resp_img.headers.get("Content-Type", "image/jpeg").split(";")[0].strip() or "image/jpeg"
        buffer = io.BytesIO()
        for chunk in resp_img.iter_content(8192):
            buffer.write(chunk)
        buffer.seek(0)
        resp = requests.post(
            f"{GRAPH_API_BASE}/{page_id}/photos",
            files={"source": ("image.jpg", buffer, ct)},
            data={"caption": caption, "access_token": token},
            timeout=60,
        )
    resp.raise_for_status()
    return resp.json()


def fb_add_comment(post_id: str, token: str, message: str) -> dict:
    resp = requests.post(
        f"{GRAPH_API_BASE}/{post_id}/comments",
        data={"message": message, "access_token": token},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


# ── Slack notifier ────────────────────────────────────────────────────────────
def send_slack(message: str, success: bool = True):
    webhook_url = os.getenv("SLACK_WEBHOOK_URL")
    if not webhook_url:
        return
    color = "#36a64f" if success else "#cc0000"
    icon  = ":white_check_mark:" if success else ":x:"
    try:
        requests.post(
            webhook_url,
            json={"attachments": [{
                "color":  color,
                "text":   f"{icon} {message}",
                "footer": "Facebook Auto-Poster",
                "ts":     int(time.time()),
            }]},
            timeout=10,
        )
    except Exception as exc:
        print(f"[Slack] {exc}")


# ── Scheduler helpers ─────────────────────────────────────────────────────────
def normalize_tz(raw: str) -> str:
    if not raw:
        return "UTC"
    return TZ_MAP.get(raw.strip().upper(), raw.strip())


def get_scheduled_utc(time_str: str, tz_str: str) -> datetime:
    iana = normalize_tz(tz_str)
    tz   = ZoneInfo(iana)
    parsed = None
    for fmt in ("%I:%M %p", "%H:%M"):
        try:
            parsed = datetime.strptime(time_str.strip(), fmt)
            break
        except ValueError:
            pass
    if parsed is None:
        raise ValueError(f"Unrecognised time format: {time_str!r}")
    now_local   = datetime.now(tz)
    local_sched = now_local.replace(
        hour=parsed.hour, minute=parsed.minute, second=0, microsecond=0
    )
    return local_sched.astimezone(ZoneInfo("UTC"))


def execute_post_thread(post: dict, page_name: str, token: str):
    post_type     = post.get("post_type", "Text")
    text          = post.get("text", "")
    page_id       = post["page_id"]
    image_url     = post.get("image_url", "").strip()
    background_id = post.get("background_id", "").strip()
    first_comment = post.get("first_comment", "").strip()
    ts            = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    print(f"[{ts}] Posting to '{page_name}' — type={post_type}")

    try:
        if post_type == "Text_Background" and background_id:
            result = fb_post_text_background(page_id, token, text, background_id)
        elif post_type == "Text_Image" and image_url:
            result = fb_post_photo(page_id, token, text, image_url)
        else:
            result = fb_post_text(page_id, token, text)

        post_id_fb     = result.get("post_id") or result.get("id")
        comment_status = ""

        if first_comment and post_id_fb:
            try:
                cr = fb_add_comment(post_id_fb, token, first_comment)
                comment_status = f"\nFirst comment ✓ (ID: `{cr.get('id')}`)"
                print(f"  Comment posted — ID: {cr.get('id')}")
            except Exception as exc:
                comment_status = f"\nComment *FAILED*: `{exc}`"
                send_slack(
                    f"*COMMENT FAILED* on *{page_name}*\n"
                    f"Post ID: `{post_id_fb}`\nError: `{exc}`",
                    success=False,
                )

        send_slack(
            f"*Post published!* on *{page_name}*\n"
            f"Type: `{post_type}` | Post ID: `{post_id_fb}`\n"
            f"Time: `{ts}`\n"
            f"Text: {text[:200]}"
            f"{comment_status}",
            success=True,
        )

        sched = load_schedule()
        for p in sched["posts"]:
            if p["id"] == post["id"]:
                p["last_posted"] = ts
                break
        save_schedule(sched)
        print(f"  Done — post ID: {post_id_fb}")

    except requests.HTTPError as exc:
        err = exc.response.text
        print(f"  HTTP ERROR: {err}")
        send_slack(
            f"*POST FAILED* on *{page_name}*\n"
            f"Type: `{post_type}`\nError: `{err[:400]}`",
            success=False,
        )
    except Exception as exc:
        print(f"  ERROR: {exc}")
        send_slack(
            f"*POST FAILED* on *{page_name}*\n"
            f"Type: `{post_type}`\nError: `{exc}`",
            success=False,
        )


# ── Scheduler loop ────────────────────────────────────────────────────────────
_ap_scheduler = BackgroundScheduler(timezone="UTC")
_DAY_MAP      = {0: "mon", 1: "tue", 2: "wed", 3: "thu", 4: "fri", 5: "sat", 6: "sun"}


def _make_cron_trigger(post: dict) -> CronTrigger:
    time_str = post.get("time", "00:00").strip()
    tz_name  = normalize_tz(post.get("timezone", "UTC"))
    days     = post.get("days", list(range(7)))
    parsed   = None
    for fmt in ("%H:%M", "%I:%M %p"):
        try:
            parsed = datetime.strptime(time_str, fmt)
            break
        except ValueError:
            pass
    if parsed is None:
        raise ValueError(f"Cannot parse time: {time_str!r}")
    day_of_week = ",".join(_DAY_MAP[d] for d in sorted(days))
    return CronTrigger(
        hour=parsed.hour, minute=parsed.minute, second=0,
        day_of_week=day_of_week, timezone=tz_name,
    )


def _execute_scheduled_post(post_id: str):
    """APScheduler job — reads fresh post data at fire time."""
    sched     = load_schedule()
    post      = next((p for p in sched["posts"] if p["id"] == post_id), None)
    if not post or not post.get("active", True):
        return
    tokens    = load_tokens()
    page_id   = post["page_id"]
    page_name = sched["pages"].get(page_id, {}).get("name", page_id)
    token     = tokens.get(page_id, "")
    if not token:
        print(f"[Scheduler] No token for page {page_id} — skipping.")
        return
    threading.Thread(
        target=execute_post_thread, args=(post, page_name, token), daemon=True,
    ).start()


def _schedule_post_job(post: dict):
    """Add or replace a post's cron job. Removes it if inactive."""
    job_id = f"post_{post['id']}"
    if not post.get("active", True):
        _unschedule_post_job(post["id"])
        return
    try:
        trigger = _make_cron_trigger(post)
        _ap_scheduler.add_job(
            _execute_scheduled_post,
            trigger,
            args=[post["id"]],
            id=job_id,
            name=f"Post → {post['page_id']}",
            replace_existing=True,
            coalesce=True,
            misfire_grace_time=59,
        )
    except Exception as exc:
        print(f"[Scheduler] Could not schedule post {post['id']}: {exc}")


def _unschedule_post_job(post_id: str):
    try:
        _ap_scheduler.remove_job(f"post_{post_id}")
    except Exception:
        pass


def reload_all_post_jobs():
    """Load every active post from schedule.json into APScheduler."""
    sched = load_schedule()
    for post in sched.get("posts", []):
        _schedule_post_job(post)
    count = sum(1 for j in _ap_scheduler.get_jobs())
    print(f"[Startup] {count} post job(s) loaded into APScheduler.")


# ── Jinja2 globals ────────────────────────────────────────────────────────────
app.jinja_env.globals.update(enumerate=enumerate, zip=zip)


# ── Auth ─────────────────────────────────────────────────────────────────────
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login", next=request.path))
        return f(*args, **kwargs)
    return decorated


@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("logged_in"):
        return redirect(url_for("index"))
    error = None
    if request.method == "POST":
        password     = request.form.get("password", "")
        app_password = os.getenv("APP_PASSWORD", "")
        if not app_password:
            error = "APP_PASSWORD is not configured in .env."
        elif password == app_password:
            session.permanent    = True
            session["logged_in"] = True
            next_url = request.form.get("next") or url_for("index")
            return redirect(next_url)
        else:
            error = "Incorrect password. Please try again."
    return render_template("login.html", error=error, next=request.args.get("next", ""))


@app.route("/logout")
def logout():
    session.clear()
    flash("You have been logged out.", "info")
    return redirect(url_for("login"))


# ── Routes ────────────────────────────────────────────────────────────────────
@app.route("/")
@login_required
def index():
    sched  = load_schedule()
    pages  = sched.get("pages", {})
    posts  = sched.get("posts", [])
    counts = {}
    active_counts = {}
    for p in posts:
        pid = p["page_id"]
        counts[pid]        = counts.get(pid, 0) + 1
        if p.get("active", True):
            active_counts[pid] = active_counts.get(pid, 0) + 1

    today        = date.today().isoformat()
    posted_today = sum(1 for p in posts if (p.get("last_posted") or "")[:10] == today)

    return render_template(
        "index.html",
        pages=pages,
        counts=counts,
        active_counts=active_counts,
        posted_today=posted_today,
        total_posts=len(posts),
    )


@app.route("/pages/add", methods=["GET", "POST"])
@login_required
def add_page():
    fb_pages = []
    error    = None

    if request.method == "POST":
        action = request.form.get("action")

        if action == "fetch":
            provided = request.form.get("user_token", "").strip()
            token    = provided or os.getenv("FACEBOOK_USER_ACCESS_TOKEN", "")
            if not token:
                error = "No token provided and no FACEBOOK_USER_ACCESS_TOKEN set in .env."
            else:
                try:
                    raw_pages = get_facebook_pages(token)
                    if not raw_pages:
                        error = "No pages found for this token."
                    else:
                        # Store page tokens server-side — never sent to browser
                        session["page_tokens"] = {
                            p["id"]: p["access_token"] for p in raw_pages
                        }
                        # Strip access_token before passing to template
                        fb_pages = [{"id": p["id"], "name": p["name"]} for p in raw_pages]
                except requests.HTTPError as exc:
                    error = exc.response.json().get("error", {}).get("message", exc.response.text)
                except Exception as exc:
                    error = str(exc)

        elif action == "save":
            selected_ids = request.form.getlist("page_ids")
            page_tokens  = session.get("page_tokens", {})
            if not selected_ids:
                flash("No pages selected.", "warning")
                return redirect(url_for("add_page"))
            if not page_tokens:
                flash("Session expired — please fetch pages again.", "warning")
                return redirect(url_for("add_page"))

            sched  = load_schedule()
            tokens = load_tokens()
            added  = 0
            for pid in selected_ids:
                name  = request.form.get(f"name_{pid}", pid)
                token = page_tokens.get(pid, "")
                sched["pages"][pid] = {
                    "name":     name,
                    "added_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                }
                tokens[pid] = token
                added += 1

            save_schedule(sched)
            save_tokens(tokens)
            session.pop("page_tokens", None)
            flash(f"Added {added} page(s) successfully.", "success")
            return redirect(url_for("index"))

    has_env_token = bool(os.getenv("FACEBOOK_USER_ACCESS_TOKEN", "").strip())
    return render_template(
        "add_page.html",
        fb_pages=fb_pages,
        error=error,
        has_env_token=has_env_token,
    )


@app.route("/pages/<page_id>")
@login_required
def page_detail(page_id):
    sched = load_schedule()
    page  = sched["pages"].get(page_id)
    if not page:
        flash("Page not found.", "danger")
        return redirect(url_for("index"))

    posts = [p for p in sched["posts"] if p["page_id"] == page_id]
    return render_template(
        "page_detail.html",
        page=page,
        page_id=page_id,
        posts=posts,
        tz_options=sorted(TZ_MAP.keys()),
        backgrounds=BACKGROUND_PRESETS,
        days_short=DAYS_SHORT,
    )


@app.route("/pages/<page_id>/post", methods=["POST"])
@login_required
def add_post(page_id):
    sched = load_schedule()
    if page_id not in sched["pages"]:
        flash("Page not found.", "danger")
        return redirect(url_for("index"))

    days_raw = request.form.getlist("days")
    days     = [int(d) for d in days_raw if d.isdigit()]

    post = {
        "id":            str(uuid.uuid4()),
        "page_id":       page_id,
        "post_type":     request.form.get("post_type", "Text"),
        "text":          request.form.get("text", "").strip(),
        "time":          request.form.get("time", "").strip(),
        "timezone":      request.form.get("timezone", "UTC"),
        "days":          days,
        "background_id": request.form.get("background_id", "").strip(),
        "image_url":     request.form.get("image_url", "").strip(),
        "first_comment": request.form.get("first_comment", "").strip(),
        "active":        True,
        "last_posted":   None,
        "created_at":    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }

    sched["posts"].append(post)
    save_schedule(sched)
    _schedule_post_job(post)
    flash("Post scheduled!", "success")
    return redirect(url_for("page_detail", page_id=page_id))


@app.route("/posts/<post_id>/edit", methods=["GET", "POST"])
@login_required
def edit_post(post_id):
    sched = load_schedule()
    post  = next((p for p in sched["posts"] if p["id"] == post_id), None)
    if not post:
        flash("Post not found.", "danger")
        return redirect(url_for("index"))

    page_id = post["page_id"]
    page    = sched["pages"].get(page_id, {})

    if request.method == "POST":
        days_raw = request.form.getlist("days")
        post["post_type"]     = request.form.get("post_type", "Text")
        post["text"]          = request.form.get("text", "").strip()
        post["time"]          = request.form.get("time", "").strip()
        post["timezone"]      = request.form.get("timezone", "UTC")
        post["days"]          = [int(d) for d in days_raw if d.isdigit()]
        post["background_id"] = request.form.get("background_id", "").strip()
        post["image_url"]     = request.form.get("image_url", "").strip()
        post["first_comment"] = request.form.get("first_comment", "").strip()
        save_schedule(sched)
        _schedule_post_job(post)
        flash("Post updated!", "success")
        return redirect(url_for("page_detail", page_id=page_id))

    return render_template(
        "edit_post.html",
        post=post,
        page=page,
        page_id=page_id,
        tz_options=sorted(TZ_MAP.keys()),
        backgrounds=BACKGROUND_PRESETS,
        days_short=DAYS_SHORT,
    )


@app.route("/posts/<post_id>/toggle", methods=["POST"])
@login_required
def toggle_post(post_id):
    sched = load_schedule()
    for p in sched["posts"]:
        if p["id"] == post_id:
            p["active"] = not p.get("active", True)
            save_schedule(sched)
            _schedule_post_job(p)
            return jsonify({"ok": True, "active": p["active"]})
    return jsonify({"ok": False}), 404


@app.route("/posts/<post_id>/delete", methods=["POST"])
@login_required
def delete_post(post_id):
    sched   = load_schedule()
    page_id = next((p["page_id"] for p in sched["posts"] if p["id"] == post_id), None)
    sched["posts"] = [p for p in sched["posts"] if p["id"] != post_id]
    save_schedule(sched)
    _unschedule_post_job(post_id)
    flash("Post deleted.", "info")
    return redirect(
        url_for("page_detail", page_id=page_id) if page_id else url_for("index")
    )


@app.route("/pages/<page_id>/delete", methods=["POST"])
@login_required
def delete_page(page_id):
    sched = load_schedule()
    for p in sched["posts"]:
        if p["page_id"] == page_id:
            _unschedule_post_job(p["id"])
    sched["pages"].pop(page_id, None)
    sched["posts"] = [p for p in sched["posts"] if p["page_id"] != page_id]
    save_schedule(sched)
    tokens = load_tokens()
    tokens.pop(page_id, None)
    save_tokens(tokens)
    flash("Page removed.", "info")
    return redirect(url_for("index"))


@app.route("/api/status")
@login_required
def api_status():
    sched  = load_schedule()
    today  = date.today().isoformat()
    return jsonify({
        "pages":         len(sched.get("pages", {})),
        "posts":         len(sched.get("posts", [])),
        "posted_today":  sum(1 for j in _ap_scheduler.get_jobs()),
        "scheduler":     "running",
        "timestamp":     datetime.now().isoformat(),
    })


# ── Startup ───────────────────────────────────────────────────────────────────
ensure_data_dir()
reload_all_post_jobs()
_ap_scheduler.start()
print("[Startup] APScheduler started.")

if __name__ == "__main__":
    app.run(host="0.0.0.0",debug=False, port=5005, use_reloader=False)
