import base64
import binascii
import csv
import hashlib
import hmac
import html
import io
import os
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import parse_qs

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    ApiClient,
    ButtonsTemplate,
    Configuration,
    MessagingApi,
    PostbackAction,
    PushMessageRequest,
    ReplyMessageRequest,
    TemplateMessage,
    TextMessage,
)
from linebot.v3.webhooks import MessageEvent, PostbackEvent, TextMessageContent


load_dotenv()

CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
DATABASE_PATH = os.getenv("DATABASE_PATH", "lineb.db")
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
ADMIN_SESSION_SECRET = os.getenv("ADMIN_SESSION_SECRET", "")
ADMIN_SETUP_TOKEN = os.getenv("ADMIN_SETUP_TOKEN", "")

app = FastAPI(title="音控小幫手", docs_url=None, redoc_url=None)
handler = WebhookHandler(CHANNEL_SECRET) if CHANNEL_SECRET else None
configuration = Configuration(access_token=CHANNEL_ACCESS_TOKEN) if CHANNEL_ACCESS_TOKEN else None


@contextmanager
def database_connection():
    connection = sqlite3.connect(DATABASE_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def init_database():
    Path(DATABASE_PATH).parent.mkdir(parents=True, exist_ok=True)
    with database_connection() as connection:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_type TEXT NOT NULL,
                source_id TEXT NOT NULL,
                title TEXT NOT NULL,
                created_by TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS availability (
                event_id INTEGER NOT NULL,
                user_id TEXT NOT NULL,
                display_name TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('available', 'unavailable')),
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (event_id, user_id),
                FOREIGN KEY (event_id) REFERENCES events(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS members (
                line_user_id TEXT PRIMARY KEY,
                line_display_name TEXT NOT NULL,
                real_name TEXT NOT NULL,
                class_name TEXT NOT NULL,
                seat_number INTEGER NOT NULL CHECK (seat_number BETWEEN 1 AND 99),
                verified_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS verification_sessions (
                line_user_id TEXT PRIMARY KEY,
                step TEXT NOT NULL CHECK (step IN ('name', 'class_seat')),
                real_name TEXT,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS admins (
                line_user_id TEXT PRIMARY KEY,
                display_name TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS event_recipients (
                event_id INTEGER NOT NULL,
                user_id TEXT NOT NULL,
                delivery_status TEXT NOT NULL DEFAULT 'pending',
                delivered_at TEXT,
                PRIMARY KEY (event_id, user_id),
                FOREIGN KEY (event_id) REFERENCES events(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS work_roles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                display_order INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS assignments (
                event_id INTEGER NOT NULL,
                user_id TEXT NOT NULL,
                role_id INTEGER NOT NULL,
                notes TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (event_id, user_id, role_id),
                FOREIGN KEY (event_id) REFERENCES events(id) ON DELETE CASCADE,
                FOREIGN KEY (role_id) REFERENCES work_roles(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_events_source
                ON events(source_type, source_id, id DESC);
            CREATE INDEX IF NOT EXISTS idx_members_class_seat
                ON members(class_name, seat_number);
            CREATE INDEX IF NOT EXISTS idx_recipients_event
                ON event_recipients(event_id, delivery_status);
            CREATE INDEX IF NOT EXISTS idx_assignments_event
                ON assignments(event_id, role_id);
            """
        )
        columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(events)")
        }
        if "is_broadcast" not in columns:
            connection.execute(
                "ALTER TABLE events ADD COLUMN is_broadcast INTEGER NOT NULL DEFAULT 0"
            )
        for order, role_name in enumerate(("音控", "簡報", "攝影", "機動"), 1):
            connection.execute(
                "INSERT OR IGNORE INTO work_roles (name, display_order) VALUES (?, ?)",
                (role_name, order),
            )


@app.on_event("startup")
def startup():
    init_database()


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    if request.url.path.startswith("/admin"):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Robots-Tag"] = "noindex, nofollow"
    return response


@app.get("/")
def home():
    return {"status": "ok", "bot": "音控小幫手", "line_configured": bool(handler and configuration)}


@app.post("/callback", response_class=PlainTextResponse)
async def callback(request: Request):
    if handler is None or configuration is None:
        raise HTTPException(status_code=503, detail="LINE credentials are not configured")
    signature = request.headers.get("X-Line-Signature")
    if not signature:
        raise HTTPException(status_code=400, detail="Missing X-Line-Signature")
    body = (await request.body()).decode("utf-8")
    try:
        handler.handle(body, signature)
    except InvalidSignatureError as exc:
        raise HTTPException(status_code=400, detail="Invalid signature") from exc
    return "OK"


def source_context(event):
    source = event.source
    if source.type == "group":
        return "group", source.group_id
    if source.type == "room":
        return "room", source.room_id
    return "user", source.user_id


def reply(event, messages):
    if not isinstance(messages, list):
        messages = [messages]
    with ApiClient(configuration) as api_client:
        MessagingApi(api_client).reply_message(
            ReplyMessageRequest(reply_token=event.reply_token, messages=messages)
        )


def get_display_name(api, event, user_id):
    try:
        if event.source.type == "group":
            profile = api.get_group_member_profile(event.source.group_id, user_id)
        elif event.source.type == "room":
            profile = api.get_room_member_profile(event.source.room_id, user_id)
        else:
            profile = api.get_profile(user_id)
        return profile.display_name
    except Exception:
        return f"成員 {user_id[-6:]}"


def start_verification(user_id):
    with database_connection() as connection:
        connection.execute(
            """
            INSERT INTO verification_sessions (line_user_id, step, real_name)
            VALUES (?, 'name', NULL)
            ON CONFLICT(line_user_id) DO UPDATE SET
                step = 'name', real_name = NULL, updated_at = CURRENT_TIMESTAMP
            """,
            (user_id,),
        )


def get_verification_session(user_id):
    with database_connection() as connection:
        return connection.execute(
            "SELECT step, real_name FROM verification_sessions WHERE line_user_id = ?",
            (user_id,),
        ).fetchone()


def save_verification_name(user_id, real_name):
    with database_connection() as connection:
        connection.execute(
            """
            UPDATE verification_sessions
            SET step = 'class_seat', real_name = ?, updated_at = CURRENT_TIMESTAMP
            WHERE line_user_id = ?
            """,
            (real_name, user_id),
        )


def cancel_verification(user_id):
    with database_connection() as connection:
        connection.execute("DELETE FROM verification_sessions WHERE line_user_id = ?", (user_id,))


def parse_class_seat(value):
    value = value.strip()
    patterns = [
        r"^(.{1,20}?)\s*班\s*(\d{1,2})\s*號?$",
        r"^(.{1,20}?)\s*[-／/、\s]\s*(\d{1,2})\s*號?$",
    ]
    for pattern in patterns:
        match = re.fullmatch(pattern, value)
        if match:
            class_name = match.group(1).strip()
            seat_number = int(match.group(2))
            if class_name and 1 <= seat_number <= 99:
                return class_name, seat_number
    return None


def save_member(user_id, line_display_name, real_name, class_name, seat_number):
    with database_connection() as connection:
        connection.execute(
            """
            INSERT INTO members
                (line_user_id, line_display_name, real_name, class_name, seat_number)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(line_user_id) DO UPDATE SET
                line_display_name = excluded.line_display_name,
                real_name = excluded.real_name,
                class_name = excluded.class_name,
                seat_number = excluded.seat_number,
                updated_at = CURRENT_TIMESTAMP
            """,
            (user_id, line_display_name, real_name, class_name, seat_number),
        )
        connection.execute("DELETE FROM verification_sessions WHERE line_user_id = ?", (user_id,))


def get_member(user_id):
    with database_connection() as connection:
        return connection.execute("SELECT * FROM members WHERE line_user_id = ?", (user_id,)).fetchone()


def list_members():
    with database_connection() as connection:
        return connection.execute(
            """
            SELECT line_user_id, real_name, class_name, seat_number,
                   line_display_name, verified_at, updated_at
            FROM members
            ORDER BY class_name COLLATE NOCASE, seat_number, real_name COLLATE NOCASE
            """
        ).fetchall()


def add_admin(user_id, display_name):
    with database_connection() as connection:
        connection.execute(
            """
            INSERT INTO admins (line_user_id, display_name) VALUES (?, ?)
            ON CONFLICT(line_user_id) DO UPDATE SET display_name = excluded.display_name
            """,
            (user_id, display_name),
        )


def is_line_admin(user_id):
    if not user_id:
        return False
    with database_connection() as connection:
        return connection.execute(
            "SELECT 1 FROM admins WHERE line_user_id = ?", (user_id,)
        ).fetchone() is not None


def claim_initial_admin(user_id, display_name):
    with database_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        used = connection.execute(
            "SELECT value FROM settings WHERE key = 'admin_setup_used'"
        ).fetchone()
        if used is not None:
            return False
        connection.execute(
            "INSERT INTO admins (line_user_id, display_name) VALUES (?, ?)",
            (user_id, display_name),
        )
        connection.execute(
            "INSERT INTO settings (key, value) VALUES ('admin_setup_used', '1')"
        )
        return True


def notify_admins(real_name, class_name, seat_number, line_display_name):
    if configuration is None:
        return
    with database_connection() as connection:
        admin_ids = [row["line_user_id"] for row in connection.execute("SELECT line_user_id FROM admins")]
    message = TextMessage(
        text=(
            "🔔 新的身分資料\n\n"
            f"姓名：{real_name}\n"
            f"班級：{class_name}班\n"
            f"座號：{seat_number}號\n"
            f"LINE 名稱：{line_display_name}\n\n"
            "管理頁：https://bot.steventeng.uk/admin"
        )
    )
    with ApiClient(configuration) as api_client:
        api = MessagingApi(api_client)
        for admin_id in admin_ids:
            try:
                api.push_message(PushMessageRequest(to=admin_id, messages=[message]))
            except Exception:
                app.logger.exception("Failed to notify LINE admin") if hasattr(app, "logger") else None


def create_event(source_type, source_id, title, created_by, is_broadcast=False):
    with database_connection() as connection:
        cursor = connection.execute(
            """
            INSERT INTO events
                (source_type, source_id, title, created_by, is_broadcast)
            VALUES (?, ?, ?, ?, ?)
            """,
            (source_type, source_id, title, created_by, int(is_broadcast)),
        )
        return cursor.lastrowid


def get_latest_event(source_type, source_id):
    with database_connection() as connection:
        return connection.execute(
            "SELECT id, title, created_at FROM events WHERE source_type = ? AND source_id = ? ORDER BY id DESC LIMIT 1",
            (source_type, source_id),
        ).fetchone()


def get_event(event_id):
    with database_connection() as connection:
        return connection.execute(
            """
            SELECT id, source_type, source_id, title, is_broadcast, created_at
            FROM events WHERE id = ?
            """,
            (event_id,),
        ).fetchone()


def list_events():
    with database_connection() as connection:
        return connection.execute(
            """
            SELECT e.id, e.title, e.created_at, e.is_broadcast,
                   COUNT(DISTINCT r.user_id) AS recipient_count,
                   SUM(CASE WHEN a.status = 'available' THEN 1 ELSE 0 END) AS available_count,
                   SUM(CASE WHEN a.status = 'unavailable' THEN 1 ELSE 0 END) AS unavailable_count
            FROM events e
            LEFT JOIN event_recipients r ON r.event_id = e.id
            LEFT JOIN availability a ON a.event_id = e.id AND a.user_id = r.user_id
            GROUP BY e.id
            ORDER BY e.id DESC
            """
        ).fetchall()


def update_event(event_id, title):
    with database_connection() as connection:
        connection.execute("UPDATE events SET title = ? WHERE id = ?", (title, event_id))


def delete_event(event_id):
    with database_connection() as connection:
        connection.execute("DELETE FROM events WHERE id = ?", (event_id,))


def list_member_recipients():
    with database_connection() as connection:
        return connection.execute(
            """
            SELECT line_user_id, real_name, class_name, seat_number
            FROM members
            ORDER BY class_name COLLATE NOCASE, seat_number, real_name COLLATE NOCASE
            """
        ).fetchall()


def save_recipient(event_id, user_id, delivery_status):
    with database_connection() as connection:
        connection.execute(
            """
            INSERT INTO event_recipients
                (event_id, user_id, delivery_status, delivered_at)
            VALUES (?, ?, ?, CASE WHEN ? = 'sent' THEN CURRENT_TIMESTAMP ELSE NULL END)
            ON CONFLICT(event_id, user_id) DO UPDATE SET
                delivery_status = excluded.delivery_status,
                delivered_at = excluded.delivered_at
            """,
            (event_id, user_id, delivery_status, delivery_status),
        )


def is_event_recipient(event_id, user_id):
    with database_connection() as connection:
        return connection.execute(
            "SELECT 1 FROM event_recipients WHERE event_id = ? AND user_id = ?",
            (event_id, user_id),
        ).fetchone() is not None


def broadcast_event(event_id, title):
    recipients = list_member_recipients()
    sent = 0
    failed = 0
    if configuration is None:
        for member in recipients:
            save_recipient(event_id, member["line_user_id"], "failed")
        return sent, len(recipients)
    with ApiClient(configuration) as api_client:
        api = MessagingApi(api_client)
        for member in recipients:
            user_id = member["line_user_id"]
            save_recipient(event_id, user_id, "pending")
            try:
                api.push_message(
                    PushMessageRequest(
                        to=user_id,
                        messages=[build_event_message(event_id, title)],
                    )
                )
                save_recipient(event_id, user_id, "sent")
                sent += 1
            except Exception:
                save_recipient(event_id, user_id, "failed")
                failed += 1
    return sent, failed


def list_work_roles():
    with database_connection() as connection:
        return connection.execute(
            "SELECT id, name, display_order FROM work_roles ORDER BY display_order, id"
        ).fetchall()


def create_work_role(name):
    with database_connection() as connection:
        order = connection.execute(
            "SELECT COALESCE(MAX(display_order), 0) + 1 FROM work_roles"
        ).fetchone()[0]
        connection.execute(
            "INSERT INTO work_roles (name, display_order) VALUES (?, ?)",
            (name, order),
        )


def update_work_role(role_id, name):
    with database_connection() as connection:
        connection.execute("UPDATE work_roles SET name = ? WHERE id = ?", (name, role_id))


def delete_work_role(role_id):
    with database_connection() as connection:
        connection.execute("DELETE FROM work_roles WHERE id = ?", (role_id,))


def save_assignment(event_id, user_id, role_id, notes=""):
    with database_connection() as connection:
        connection.execute(
            """
            INSERT INTO assignments (event_id, user_id, role_id, notes)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(event_id, user_id, role_id) DO UPDATE SET notes = excluded.notes
            """,
            (event_id, user_id, role_id, notes),
        )


def delete_assignment(event_id, user_id, role_id):
    with database_connection() as connection:
        connection.execute(
            "DELETE FROM assignments WHERE event_id = ? AND user_id = ? AND role_id = ?",
            (event_id, user_id, role_id),
        )


def get_event_roster(event_id):
    with database_connection() as connection:
        return connection.execute(
            """
            SELECT m.line_user_id, m.real_name, m.class_name, m.seat_number,
                   r.delivery_status, a.status,
                   GROUP_CONCAT(w.name, '、') AS role_names
            FROM event_recipients r
            JOIN members m ON m.line_user_id = r.user_id
            LEFT JOIN availability a
                ON a.event_id = r.event_id AND a.user_id = r.user_id
            LEFT JOIN assignments s
                ON s.event_id = r.event_id AND s.user_id = r.user_id
            LEFT JOIN work_roles w ON w.id = s.role_id
            WHERE r.event_id = ?
            GROUP BY m.line_user_id
            ORDER BY m.class_name COLLATE NOCASE, m.seat_number, m.real_name COLLATE NOCASE
            """,
            (event_id,),
        ).fetchall()


def get_event_assignments(event_id):
    with database_connection() as connection:
        return connection.execute(
            """
            SELECT s.user_id, s.role_id, s.notes, m.real_name, m.class_name,
                   m.seat_number, w.name AS role_name
            FROM assignments s
            JOIN members m ON m.line_user_id = s.user_id
            JOIN work_roles w ON w.id = s.role_id
            WHERE s.event_id = ?
            ORDER BY w.display_order, w.id, m.class_name COLLATE NOCASE,
                     m.seat_number, m.real_name COLLATE NOCASE
            """,
            (event_id,),
        ).fetchall()


def update_member_admin(user_id, real_name, class_name, seat_number):
    with database_connection() as connection:
        connection.execute(
            """
            UPDATE members SET real_name = ?, class_name = ?, seat_number = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE line_user_id = ?
            """,
            (real_name, class_name, seat_number, user_id),
        )


def delete_member_admin(user_id):
    with database_connection() as connection:
        connection.execute("DELETE FROM availability WHERE user_id = ?", (user_id,))
        connection.execute("DELETE FROM assignments WHERE user_id = ?", (user_id,))
        connection.execute("DELETE FROM event_recipients WHERE user_id = ?", (user_id,))
        connection.execute("DELETE FROM verification_sessions WHERE line_user_id = ?", (user_id,))
        connection.execute("DELETE FROM members WHERE line_user_id = ?", (user_id,))


def set_availability_admin(event_id, user_id, status):
    if status == "pending":
        with database_connection() as connection:
            connection.execute(
                "DELETE FROM availability WHERE event_id = ? AND user_id = ?",
                (event_id, user_id),
            )
        return
    member = get_member(user_id)
    if member is None:
        raise ValueError("member not found")
    save_availability(event_id, user_id, member["real_name"], status)


def save_availability(event_id, user_id, display_name, status):
    with database_connection() as connection:
        connection.execute(
            """
            INSERT INTO availability (event_id, user_id, display_name, status)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(event_id, user_id) DO UPDATE SET
                display_name = excluded.display_name,
                status = excluded.status,
                updated_at = CURRENT_TIMESTAMP
            """,
            (event_id, user_id, display_name, status),
        )


def get_availability(event_id):
    with database_connection() as connection:
        return connection.execute(
            "SELECT display_name, status FROM availability WHERE event_id = ? ORDER BY status, display_name COLLATE NOCASE",
            (event_id,),
        ).fetchall()


def build_event_message(event_id, title):
    return TemplateMessage(
        alt_text=f"{title[:350]}：請回覆是否有空",
        template=ButtonsTemplate(
            title=title[:40],
            text="請選擇是否有空：",
            actions=[
                PostbackAction(label="✅ 有空", data=f"action=availability&event_id={event_id}&status=available", display_text="✅ 有空"),
                PostbackAction(label="❌ 沒空", data=f"action=availability&event_id={event_id}&status=unavailable", display_text="❌ 沒空"),
            ],
        ),
    )


def format_availability(event_row, rows):
    available = [row["display_name"] for row in rows if row["status"] == "available"]
    unavailable = [row["display_name"] for row in rows if row["status"] == "unavailable"]
    return (
        f"🎛️ {event_row['title']}\n\n"
        f"✅ 有空（{len(available)}）\n" + ("\n".join(available) or "（尚無）") + "\n\n"
        f"❌ 沒空（{len(unavailable)}）\n" + ("\n".join(unavailable) or "（尚無）")
    )


def session_cookie(username, expires=None):
    expires = expires or int(time.time()) + 8 * 60 * 60
    payload = f"{username}:{expires}"
    signature = hmac.new(ADMIN_SESSION_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(f"{payload}:{signature}".encode()).decode()


def valid_admin_session(request):
    if not ADMIN_SESSION_SECRET:
        return False
    try:
        raw = base64.urlsafe_b64decode(request.cookies.get("lineb_admin", "").encode()).decode()
        username, expires, signature = raw.rsplit(":", 2)
        payload = f"{username}:{expires}"
        expected = hmac.new(ADMIN_SESSION_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
        return username == ADMIN_USERNAME and int(expires) > time.time() and hmac.compare_digest(signature, expected)
    except (ValueError, TypeError, UnicodeError, binascii.Error):
        return False


def csrf_token(request):
    cookie = request.cookies.get("lineb_admin", "")
    return hmac.new(
        ADMIN_SESSION_SECRET.encode(),
        f"csrf:{cookie}".encode(),
        hashlib.sha256,
    ).hexdigest()


def require_admin(request):
    if not valid_admin_session(request):
        raise HTTPException(status_code=401, detail="請先登入管理員")


def require_csrf(request, values):
    require_admin(request)
    supplied = values.get("csrf", [""])[0]
    if not secrets.compare_digest(supplied, csrf_token(request)):
        raise HTTPException(status_code=403, detail="安全驗證失敗，請重新整理頁面")


async def form_values(request):
    return parse_qs((await request.body()).decode("utf-8"), keep_blank_values=True)


def admin_navigation(request):
    token = html.escape(csrf_token(request), quote=True)
    return f"""<header class="admin-nav"><a class="brand" href="/admin">🎛️ 音控管理</a>
    <nav><a href="/admin/members">成員</a><a href="/admin/events">活動</a>
    <a href="/admin/roles">工作類別</a><a href="/admin/export.csv">匯出成員</a></nav>
    <form method="post" action="/admin/logout"><input type="hidden" name="csrf" value="{token}"><button class="secondary" type="submit">登出</button></form></header>"""


def page_shell(title, body):
    return f"""<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(title)}</title>
<style>
:root{{--bg:#f4f7fb;--card:#fff;--ink:#172033;--muted:#6b7280;--brand:#0b7a53;--brand2:#075d40;--danger:#b42318;--line:#e5e7eb}}
*{{box-sizing:border-box}} body{{margin:0;background:var(--bg);color:var(--ink);font-family:system-ui,-apple-system,"Noto Sans TC",sans-serif}}
.wrap{{max-width:1180px;margin:0 auto;padding:24px 18px 48px}} .card{{background:var(--card);border:1px solid var(--line);border-radius:18px;padding:22px;box-shadow:0 8px 30px #102a4312;margin-bottom:18px}}
h1{{margin:0 0 8px;font-size:28px}} h2{{margin:0 0 16px;font-size:21px}} p{{color:var(--muted)}} label{{font-weight:650;font-size:14px}}
input,select,textarea{{width:100%;padding:11px 13px;border:1px solid #cbd5e1;border-radius:10px;font-size:16px;margin:6px 0 14px;background:white}} textarea{{min-height:80px;resize:vertical}}
button,.button{{display:inline-block;background:var(--brand);color:white;border:0;border-radius:10px;padding:10px 15px;text-decoration:none;font-weight:700;cursor:pointer}} button:hover,.button:hover{{background:var(--brand2)}}
.secondary{{background:#eef2f6;color:var(--ink)}} .danger{{background:var(--danger)}} .small{{padding:7px 10px;font-size:13px}}
.top{{display:flex;justify-content:space-between;gap:14px;align-items:center;margin-bottom:18px;flex-wrap:wrap}} .muted{{color:var(--muted)}} .error{{color:var(--danger)}}
.admin-nav{{display:flex;align-items:center;gap:20px;margin-bottom:24px;flex-wrap:wrap}} .admin-nav .brand{{font-size:20px;font-weight:800;color:var(--ink);text-decoration:none}} .admin-nav nav{{display:flex;gap:14px;flex:1;flex-wrap:wrap}} .admin-nav nav a{{color:var(--brand2);font-weight:700;text-decoration:none}} .admin-nav form{{margin:0}}
.table-wrap{{overflow:auto}} table{{width:100%;border-collapse:collapse}} th,td{{padding:11px;text-align:left;border-bottom:1px solid var(--line);white-space:nowrap;vertical-align:middle}} th{{font-size:13px;color:var(--muted)}} td form{{margin:0}}
.badge{{background:#e8f5ef;color:#08734c;padding:5px 9px;border-radius:999px;font-size:13px;font-weight:700}} .badge.warn{{background:#fff3d6;color:#8a5800}} .badge.no{{background:#feeceb;color:#a51d16}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:16px}} .metric{{font-size:34px;font-weight:850;margin-top:6px}} .actions{{display:flex;gap:8px;align-items:center;flex-wrap:wrap}} .inline{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;align-items:end}} .inline input,.inline select{{margin-bottom:0}}
@media(max-width:600px){{.wrap{{padding:14px 9px}}.card{{padding:15px}}.admin-nav nav{{order:3;width:100%}}}}
@media print{{body{{background:white}}.admin-nav,.no-print{{display:none!important}}.wrap{{max-width:none;padding:0}}.card{{box-shadow:none;border:0;padding:0}}}}
</style></head><body><main class="wrap">{body}</main></body></html>"""


@app.get("/admin/login", response_class=HTMLResponse)
def admin_login_page(request: Request):
    if valid_admin_session(request):
        return RedirectResponse("/admin", status_code=303)
    body = """<div class="card" style="max-width:430px;margin:8vh auto"><h1>管理員登入</h1>
    <p>查看成員的身分驗證資料。</p><form method="post" action="/admin/login">
    <label>帳號</label><input name="username" autocomplete="username" required>
    <label>密碼</label><input name="password" type="password" autocomplete="current-password" required>
    <button type="submit">登入</button></form></div>"""
    return HTMLResponse(page_shell("管理員登入", body))


@app.post("/admin/login")
async def admin_login(request: Request):
    if not ADMIN_PASSWORD or not ADMIN_SESSION_SECRET:
        raise HTTPException(status_code=503, detail="管理員登入尚未設定")
    values = parse_qs((await request.body()).decode("utf-8"))
    username = values.get("username", [""])[0]
    password = values.get("password", [""])[0]
    if not (secrets.compare_digest(username, ADMIN_USERNAME) and secrets.compare_digest(password, ADMIN_PASSWORD)):
        body = """<div class="card" style="max-width:430px;margin:8vh auto"><h1>登入失敗</h1>
        <p class="error">帳號或密碼不正確。</p><a class="button" href="/admin/login">重新登入</a></div>"""
        return HTMLResponse(page_shell("登入失敗", body), status_code=401)
    response = RedirectResponse("/admin", status_code=303)
    response.set_cookie("lineb_admin", session_cookie(username), max_age=8 * 60 * 60, httponly=True, secure=True, samesite="strict")
    return response


@app.post("/admin/logout")
async def admin_logout(request: Request):
    values = await form_values(request)
    require_csrf(request, values)
    response = RedirectResponse("/admin/login", status_code=303)
    response.delete_cookie("lineb_admin")
    return response


@app.get("/admin", response_class=HTMLResponse)
def admin_dashboard(request: Request):
    if not valid_admin_session(request):
        return RedirectResponse("/admin/login", status_code=303)
    members = list_members()
    events = list_events()
    roles = list_work_roles()
    latest = events[0] if events else None
    latest_card = (
        f'<a class="button" href="/admin/events/{latest["id"]}">查看：{html.escape(latest["title"])}</a>'
        if latest else '<span class="muted">尚未建立活動</span>'
    )
    body = f"""{admin_navigation(request)}<div class="top"><div><h1>最高權限控制台</h1>
    <div class="muted">管理成員、活動、工作類別、回覆與分工</div></div></div>
    <section class="grid">
      <div class="card"><div class="muted">已驗證成員</div><div class="metric">{len(members)}</div><a href="/admin/members">管理成員 →</a></div>
      <div class="card"><div class="muted">活動</div><div class="metric">{len(events)}</div><a href="/admin/events">管理活動 →</a></div>
      <div class="card"><div class="muted">工作類別</div><div class="metric">{len(roles)}</div><a href="/admin/roles">調整工作 →</a></div>
    </section>
    <section class="card"><h2>最新活動</h2>{latest_card}</section>"""
    return HTMLResponse(page_shell("最高權限控制台", body))


@app.get("/admin/members", response_class=HTMLResponse)
def admin_members(request: Request):
    if not valid_admin_session(request):
        return RedirectResponse("/admin/login", status_code=303)
    rows = list_members()
    table_rows = "".join(
        "<tr>"
        f"<td><strong>{html.escape(row['real_name'])}</strong></td>"
        f"<td>{html.escape(row['class_name'])}班</td><td>{row['seat_number']}號</td>"
        f"<td>{html.escape(row['line_display_name'])}</td>"
        f"<td><a class=\"button small\" href=\"/admin/members/{html.escape(row['line_user_id'], quote=True)}\">修改</a></td></tr>"
        for row in rows
    ) or '<tr><td colspan="5" class="muted">目前尚無驗證資料</td></tr>'
    body = f"""{admin_navigation(request)}<div class="top"><div><h1>成員管理</h1><div class="muted">可修改或刪除任何成員資料</div></div>
    <a class="button" href="/admin/export.csv">匯出 CSV</a></div><div class="card"><span class="badge">共 {len(rows)} 人</span>
    <div class="table-wrap"><table><thead><tr><th>姓名</th><th>班級</th><th>座號</th><th>LINE 名稱</th><th>操作</th></tr></thead><tbody>{table_rows}</tbody></table></div></div>"""
    return HTMLResponse(page_shell("成員管理", body))


@app.get("/admin/members/{user_id}", response_class=HTMLResponse)
def admin_member_edit_page(user_id: str, request: Request):
    if not valid_admin_session(request):
        return RedirectResponse("/admin/login", status_code=303)
    member = get_member(user_id)
    if member is None:
        raise HTTPException(status_code=404, detail="找不到成員")
    token = html.escape(csrf_token(request), quote=True)
    uid = html.escape(user_id, quote=True)
    body = f"""{admin_navigation(request)}<div class="card"><h1>修改成員</h1>
    <form method="post" action="/admin/members/{uid}"><input type="hidden" name="csrf" value="{token}">
    <label>姓名</label><input name="real_name" value="{html.escape(member['real_name'], quote=True)}" required maxlength="40">
    <label>班級</label><input name="class_name" value="{html.escape(member['class_name'], quote=True)}" required maxlength="20">
    <label>座號</label><input name="seat_number" type="number" min="1" max="99" value="{member['seat_number']}" required>
    <div class="actions"><button type="submit">儲存修改</button><a class="button secondary" href="/admin/members">返回</a></div></form></div>
    <div class="card"><h2>危險操作</h2><form method="post" action="/admin/members/{uid}/delete" onsubmit="return confirm('確定刪除此成員及其活動資料？')">
    <input type="hidden" name="csrf" value="{token}"><button class="danger" type="submit">刪除成員</button></form></div>"""
    return HTMLResponse(page_shell("修改成員", body))


@app.post("/admin/members/{user_id}")
async def admin_member_update(user_id: str, request: Request):
    values = await form_values(request)
    require_csrf(request, values)
    real_name = values.get("real_name", [""])[0].strip()
    class_name = values.get("class_name", [""])[0].strip()
    try:
        seat_number = int(values.get("seat_number", [""])[0])
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="座號格式錯誤") from exc
    if not real_name or not class_name or not 1 <= seat_number <= 99:
        raise HTTPException(status_code=400, detail="成員資料不完整")
    update_member_admin(user_id, real_name, class_name, seat_number)
    return RedirectResponse("/admin/members", status_code=303)


@app.post("/admin/members/{user_id}/delete")
async def admin_member_delete(user_id: str, request: Request):
    values = await form_values(request)
    require_csrf(request, values)
    delete_member_admin(user_id)
    return RedirectResponse("/admin/members", status_code=303)


@app.get("/admin/roles", response_class=HTMLResponse)
def admin_roles(request: Request):
    if not valid_admin_session(request):
        return RedirectResponse("/admin/login", status_code=303)
    roles = list_work_roles()
    token = html.escape(csrf_token(request), quote=True)
    role_rows = "".join(
        f"""<tr><td><form class="actions" method="post" action="/admin/roles/{role['id']}"><input type="hidden" name="csrf" value="{token}">
        <input style="margin:0;max-width:260px" name="name" value="{html.escape(role['name'], quote=True)}" required maxlength="30"><button class="small" type="submit">改名</button></form></td>
        <td><form method="post" action="/admin/roles/{role['id']}/delete" onsubmit="return confirm('刪除此工作會一併移除相關分工，確定嗎？')"><input type="hidden" name="csrf" value="{token}"><button class="danger small" type="submit">刪除</button></form></td></tr>"""
        for role in roles
    )
    body = f"""{admin_navigation(request)}<div class="top"><div><h1>工作類別</h1><div class="muted">預設為音控、簡報、攝影、機動，可自由新增或調整</div></div></div>
    <div class="card"><h2>新增工作</h2><form class="inline" method="post" action="/admin/roles"><input type="hidden" name="csrf" value="{token}"><input name="name" placeholder="例如：燈光" required maxlength="30"><button type="submit">新增</button></form></div>
    <div class="card"><div class="table-wrap"><table><thead><tr><th>工作名稱</th><th>操作</th></tr></thead><tbody>{role_rows}</tbody></table></div></div>"""
    return HTMLResponse(page_shell("工作類別", body))


@app.post("/admin/roles")
async def admin_role_create(request: Request):
    values = await form_values(request)
    require_csrf(request, values)
    name = values.get("name", [""])[0].strip()
    if not 1 <= len(name) <= 30:
        raise HTTPException(status_code=400, detail="工作名稱格式錯誤")
    try:
        create_work_role(name)
    except sqlite3.IntegrityError as exc:
        raise HTTPException(status_code=400, detail="工作名稱已存在") from exc
    return RedirectResponse("/admin/roles", status_code=303)


@app.post("/admin/roles/{role_id}")
async def admin_role_update(role_id: int, request: Request):
    values = await form_values(request)
    require_csrf(request, values)
    name = values.get("name", [""])[0].strip()
    if not 1 <= len(name) <= 30:
        raise HTTPException(status_code=400, detail="工作名稱格式錯誤")
    try:
        update_work_role(role_id, name)
    except sqlite3.IntegrityError as exc:
        raise HTTPException(status_code=400, detail="工作名稱已存在") from exc
    return RedirectResponse("/admin/roles", status_code=303)


@app.post("/admin/roles/{role_id}/delete")
async def admin_role_delete(role_id: int, request: Request):
    values = await form_values(request)
    require_csrf(request, values)
    delete_work_role(role_id)
    return RedirectResponse("/admin/roles", status_code=303)


@app.get("/admin/events", response_class=HTMLResponse)
def admin_events(request: Request):
    if not valid_admin_session(request):
        return RedirectResponse("/admin/login", status_code=303)
    events = list_events()
    token = html.escape(csrf_token(request), quote=True)
    event_rows = "".join(
        f"""<tr><td><a href="/admin/events/{row['id']}"><strong>{html.escape(row['title'])}</strong></a></td>
        <td>{row['recipient_count']}</td><td>{row['available_count'] or 0}</td><td>{row['unavailable_count'] or 0}</td>
        <td>{html.escape(row['created_at'])} UTC</td></tr>""" for row in events
    ) or '<tr><td colspan="5" class="muted">尚未建立活動</td></tr>'
    body = f"""{admin_navigation(request)}<div class="top"><div><h1>活動管理</h1><div class="muted">建立後會自動私訊所有已驗證成員</div></div></div>
    <div class="card"><h2>建立新活動</h2><form class="inline" method="post" action="/admin/events"><input type="hidden" name="csrf" value="{token}"><input name="title" placeholder="活動名稱" required maxlength="100"><button type="submit">建立並發送</button></form></div>
    <div class="card"><div class="table-wrap"><table><thead><tr><th>活動</th><th>收件人</th><th>有空</th><th>沒空</th><th>建立時間</th></tr></thead><tbody>{event_rows}</tbody></table></div></div>"""
    return HTMLResponse(page_shell("活動管理", body))


@app.post("/admin/events")
async def admin_event_create(request: Request):
    values = await form_values(request)
    require_csrf(request, values)
    title = values.get("title", [""])[0].strip()
    if not 1 <= len(title) <= 100:
        raise HTTPException(status_code=400, detail="活動名稱格式錯誤")
    event_id = create_event("admin", "global", title, "web-admin", is_broadcast=True)
    broadcast_event(event_id, title)
    return RedirectResponse(f"/admin/events/{event_id}", status_code=303)


@app.get("/admin/events/{event_id}", response_class=HTMLResponse)
def admin_event_detail(event_id: int, request: Request):
    if not valid_admin_session(request):
        return RedirectResponse("/admin/login", status_code=303)
    event = get_event(event_id)
    if event is None:
        raise HTTPException(status_code=404, detail="找不到活動")
    roster = get_event_roster(event_id)
    roles = list_work_roles()
    assignments = get_event_assignments(event_id)
    token = html.escape(csrf_token(request), quote=True)
    roster_rows = "".join(
        f"""<tr><td><strong>{html.escape(row['real_name'])}</strong></td><td>{html.escape(row['class_name'])}班 {row['seat_number']}號</td>
        <td><form class="actions" method="post" action="/admin/events/{event_id}/availability"><input type="hidden" name="csrf" value="{token}"><input type="hidden" name="user_id" value="{html.escape(row['line_user_id'], quote=True)}">
        <select name="status" style="margin:0;min-width:120px"><option value="pending" {'selected' if row['status'] is None else ''}>⏳ 未回覆</option><option value="available" {'selected' if row['status'] == 'available' else ''}>✅ 有空</option><option value="unavailable" {'selected' if row['status'] == 'unavailable' else ''}>❌ 沒空</option></select><button class="small" type="submit">更新</button></form></td>
        <td>{html.escape(row['role_names'] or '未分工')}</td><td>{html.escape(row['delivery_status'])}</td></tr>"""
        for row in roster
    ) or '<tr><td colspan="5" class="muted">尚無收件人</td></tr>'
    member_options = "".join(
        f'<option value="{html.escape(row["line_user_id"], quote=True)}">{html.escape(row["class_name"])}班 {row["seat_number"]}號－{html.escape(row["real_name"])}</option>'
        for row in roster
    )
    role_options = "".join(
        f'<option value="{role["id"]}">{html.escape(role["name"])}</option>' for role in roles
    )
    assignment_rows = "".join(
        f"""<tr><td>{html.escape(row['role_name'])}</td><td>{html.escape(row['real_name'])}</td><td>{html.escape(row['class_name'])}班 {row['seat_number']}號</td><td>{html.escape(row['notes'])}</td>
        <td><form method="post" action="/admin/events/{event_id}/assignments/delete"><input type="hidden" name="csrf" value="{token}"><input type="hidden" name="user_id" value="{html.escape(row['user_id'], quote=True)}"><input type="hidden" name="role_id" value="{row['role_id']}"><button class="danger small" type="submit">移除</button></form></td></tr>"""
        for row in assignments
    ) or '<tr><td colspan="5" class="muted">尚未分工</td></tr>'
    available_count = sum(1 for row in roster if row["status"] == "available")
    unavailable_count = sum(1 for row in roster if row["status"] == "unavailable")
    pending_count = len(roster) - available_count - unavailable_count
    body = f"""{admin_navigation(request)}<div class="top"><div><h1>{html.escape(event['title'])}</h1><div class="muted">建立於 {html.escape(event['created_at'])} UTC</div></div>
    <div class="actions"><a class="button" href="/admin/events/{event_id}/work-sheet">列印分工表</a><a class="button secondary" href="/admin/events">返回</a></div></div>
    <section class="grid"><div class="card"><div class="muted">有空</div><div class="metric">{available_count}</div></div><div class="card"><div class="muted">沒空</div><div class="metric">{unavailable_count}</div></div><div class="card"><div class="muted">未回覆</div><div class="metric">{pending_count}</div></div></section>
    <div class="card"><h2>活動設定</h2><form class="inline" method="post" action="/admin/events/{event_id}"><input type="hidden" name="csrf" value="{token}"><input name="title" value="{html.escape(event['title'], quote=True)}" required maxlength="100"><button type="submit">修改名稱</button></form>
    <div class="actions" style="margin-top:14px"><form method="post" action="/admin/events/{event_id}/broadcast"><input type="hidden" name="csrf" value="{token}"><button type="submit">重新發送給所有成員</button></form>
    <form method="post" action="/admin/events/{event_id}/delete" onsubmit="return confirm('確定刪除此活動、回覆及分工？')"><input type="hidden" name="csrf" value="{token}"><button class="danger" type="submit">刪除活動</button></form></div></div>
    <div class="card"><h2>新增分工</h2><form class="inline" method="post" action="/admin/events/{event_id}/assignments"><input type="hidden" name="csrf" value="{token}"><select name="user_id" required>{member_options}</select><select name="role_id" required>{role_options}</select><input name="notes" placeholder="備註（選填）" maxlength="100"><button type="submit">加入分工</button></form></div>
    <div class="card"><h2>分工結果</h2><div class="table-wrap"><table><thead><tr><th>工作</th><th>姓名</th><th>班級座號</th><th>備註</th><th>操作</th></tr></thead><tbody>{assignment_rows}</tbody></table></div></div>
    <div class="card"><h2>所有回覆</h2><div class="table-wrap"><table><thead><tr><th>姓名</th><th>班級座號</th><th>回覆</th><th>工作</th><th>傳送</th></tr></thead><tbody>{roster_rows}</tbody></table></div></div>"""
    return HTMLResponse(page_shell(event["title"], body))


@app.post("/admin/events/{event_id}")
async def admin_event_update(event_id: int, request: Request):
    values = await form_values(request)
    require_csrf(request, values)
    title = values.get("title", [""])[0].strip()
    if not 1 <= len(title) <= 100:
        raise HTTPException(status_code=400, detail="活動名稱格式錯誤")
    update_event(event_id, title)
    return RedirectResponse(f"/admin/events/{event_id}", status_code=303)


@app.post("/admin/events/{event_id}/broadcast")
async def admin_event_broadcast(event_id: int, request: Request):
    values = await form_values(request)
    require_csrf(request, values)
    event = get_event(event_id)
    if event is None:
        raise HTTPException(status_code=404, detail="找不到活動")
    broadcast_event(event_id, event["title"])
    return RedirectResponse(f"/admin/events/{event_id}", status_code=303)


@app.post("/admin/events/{event_id}/delete")
async def admin_event_delete(event_id: int, request: Request):
    values = await form_values(request)
    require_csrf(request, values)
    delete_event(event_id)
    return RedirectResponse("/admin/events", status_code=303)


@app.post("/admin/events/{event_id}/assignments")
async def admin_assignment_create(event_id: int, request: Request):
    values = await form_values(request)
    require_csrf(request, values)
    user_id = values.get("user_id", [""])[0]
    notes = values.get("notes", [""])[0].strip()[:100]
    try:
        role_id = int(values.get("role_id", [""])[0])
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="工作資料錯誤") from exc
    if not is_event_recipient(event_id, user_id):
        raise HTTPException(status_code=400, detail="成員不在此活動名單")
    save_assignment(event_id, user_id, role_id, notes)
    return RedirectResponse(f"/admin/events/{event_id}", status_code=303)


@app.post("/admin/events/{event_id}/availability")
async def admin_availability_update(event_id: int, request: Request):
    values = await form_values(request)
    require_csrf(request, values)
    user_id = values.get("user_id", [""])[0]
    status = values.get("status", [""])[0]
    if status not in {"pending", "available", "unavailable"}:
        raise HTTPException(status_code=400, detail="回覆狀態錯誤")
    if not is_event_recipient(event_id, user_id):
        raise HTTPException(status_code=400, detail="成員不在此活動名單")
    set_availability_admin(event_id, user_id, status)
    return RedirectResponse(f"/admin/events/{event_id}", status_code=303)


@app.post("/admin/events/{event_id}/assignments/delete")
async def admin_assignment_delete(event_id: int, request: Request):
    values = await form_values(request)
    require_csrf(request, values)
    user_id = values.get("user_id", [""])[0]
    try:
        role_id = int(values.get("role_id", [""])[0])
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="工作資料錯誤") from exc
    delete_assignment(event_id, user_id, role_id)
    return RedirectResponse(f"/admin/events/{event_id}", status_code=303)


@app.get("/admin/events/{event_id}/work-sheet", response_class=HTMLResponse)
def admin_work_sheet(event_id: int, request: Request):
    if not valid_admin_session(request):
        return RedirectResponse("/admin/login", status_code=303)
    event = get_event(event_id)
    if event is None:
        raise HTTPException(status_code=404, detail="找不到活動")
    roles = list_work_roles()
    assignments = get_event_assignments(event_id)
    sections = []
    for role in roles:
        people = [row for row in assignments if row["role_id"] == role["id"]]
        names = "、".join(
            f"{html.escape(row['real_name'])}（{html.escape(row['class_name'])}班 {row['seat_number']}號）"
            + (f"－{html.escape(row['notes'])}" if row["notes"] else "")
            for row in people
        ) or "尚未安排"
        sections.append(f'<div class="card"><h2>{html.escape(role["name"])}</h2><p style="font-size:18px;color:var(--ink)">{names}</p></div>')
    body = f"""{admin_navigation(request)}<div class="top"><div><h1>{html.escape(event['title'])}－分工表</h1><div class="muted">產生時間：{time.strftime('%Y-%m-%d %H:%M')}</div></div>
    <div class="actions no-print"><button onclick="window.print()">列印／存成 PDF</button><a class="button secondary" href="/admin/events/{event_id}">返回活動</a></div></div>{''.join(sections)}"""
    return HTMLResponse(page_shell(f"{event['title']}分工表", body))


@app.get("/admin/export.csv")
def admin_export(request: Request):
    if not valid_admin_session(request):
        return RedirectResponse("/admin/login", status_code=303)
    output = io.StringIO()
    output.write("\ufeff")
    writer = csv.writer(output)
    writer.writerow(["姓名", "班級", "座號", "LINE 名稱", "驗證時間", "更新時間"])
    for row in list_members():
        writer.writerow([row["real_name"], row["class_name"], row["seat_number"], row["line_display_name"], row["verified_at"], row["updated_at"]])
    return Response(output.getvalue(), media_type="text/csv; charset=utf-8", headers={"Content-Disposition": "attachment; filename=members.csv"})


if handler is not None:

    @handler.add(MessageEvent, message=TextMessageContent)
    def handle_message(event):
        text = event.message.text.strip()
        command = text.lower()
        user_id = getattr(event.source, "user_id", None)

        if command == "ping":
            reply(event, TextMessage(text="pong 🎛️"))
            return

        if command.startswith("/設定管理員"):
            if event.source.type != "user" or not user_id:
                reply(event, TextMessage(text="請私訊 Bot 設定管理員。"))
                return
            supplied = text[len("/設定管理員"):].strip()
            if not ADMIN_SETUP_TOKEN or not secrets.compare_digest(supplied, ADMIN_SETUP_TOKEN):
                reply(event, TextMessage(text="管理員設定代碼不正確。"))
                return
            with ApiClient(configuration) as api_client:
                display_name = get_display_name(MessagingApi(api_client), event, user_id)
            if not claim_initial_admin(user_id, display_name):
                reply(event, TextMessage(text="管理員已經完成綁定，此設定代碼已失效。"))
                return
            reply(event, TextMessage(text="✅ 已綁定為管理員。此設定代碼現已失效，之後會收到新的身分驗證通知。"))
            return

        if command in {"身分驗證", "身份驗證", "開始驗證", "/verify", "/重新驗證"}:
            if event.source.type != "user" or not user_id:
                reply(event, TextMessage(text="🔒 為保護個資，請私訊 Bot 輸入「身分驗證」。"))
                return
            start_verification(user_id)
            reply(event, TextMessage(text="開始身分驗證。\n\n請輸入你的真實姓名：\n（輸入 /取消 可中止）"))
            return

        if command == "/取消" and user_id:
            cancel_verification(user_id)
            reply(event, TextMessage(text="已取消身分驗證。"))
            return

        if command in {"我的資料", "/me"} and user_id:
            member = get_member(user_id)
            if member is None:
                reply(event, TextMessage(text="你尚未完成驗證。請輸入「身分驗證」。"))
            else:
                reply(event, TextMessage(text=f"👤 我的資料\n\n姓名：{member['real_name']}\n班級：{member['class_name']}班\n座號：{member['seat_number']}號\n\n如需修改，輸入「身分驗證」重新填寫。"))
            return

        if user_id and event.source.type == "user":
            session = get_verification_session(user_id)
            if session is not None:
                if session["step"] == "name":
                    if not 2 <= len(text) <= 40 or "\n" in text:
                        reply(event, TextMessage(text="姓名格式不正確，請輸入 2～40 個字。"))
                        return
                    save_verification_name(user_id, text)
                    reply(event, TextMessage(text="請輸入班級與座號。\n例如：116班 01號"))
                    return
                parsed = parse_class_seat(text)
                if parsed is None:
                    reply(event, TextMessage(text="格式無法辨識，請依照範例輸入：\n116班 01號"))
                    return
                class_name, seat_number = parsed
                with ApiClient(configuration) as api_client:
                    line_display_name = get_display_name(MessagingApi(api_client), event, user_id)
                save_member(user_id, line_display_name, session["real_name"], class_name, seat_number)
                notify_admins(session["real_name"], class_name, seat_number, line_display_name)
                reply(event, TextMessage(text=f"✅ 身分資料已送出\n\n姓名：{session['real_name']}\n班級：{class_name}班\n座號：{seat_number}號\n\n管理員已收到資料。"))
                return

        if command.startswith("/event"):
            if not is_line_admin(user_id):
                reply(event, TextMessage(text="只有管理員可以建立並群發活動。"))
                return
            title = text[6:].strip()
            if not title:
                reply(event, TextMessage(text="用法：/event 活動名稱"))
                return
            source_type, source_id = source_context(event)
            event_id = create_event(
                source_type, source_id, title, user_id, is_broadcast=True
            )
            sent, failed = broadcast_event(event_id, title)
            reply(
                event,
                TextMessage(
                    text=(
                        f"✅ 已建立活動：{title}\n\n"
                        f"成功私訊：{sent} 人\n"
                        f"傳送失敗：{failed} 人\n\n"
                        "可到管理網站查看回覆與製作分工表。"
                    )
                ),
            )
            return

        if command == "/list":
            source_type, source_id = source_context(event)
            event_row = get_latest_event(source_type, source_id)
            if event_row is None:
                reply(event, TextMessage(text="目前還沒有活動。先輸入：/event 活動名稱"))
                return
            reply(event, TextMessage(text=format_availability(event_row, get_availability(event_row["id"]))))
            return

        if command == "/help":
            reply(event, TextMessage(text=(
                "🎛️ 音控小幫手\n\n"
                "身分驗證－填寫姓名、班級與座號\n"
                "我的資料－查看已填資料\n"
                "/event 活動名稱－建立活動\n"
                "/list－查看最新活動名單\n"
                "ping－測試 Bot"
            )))


    @handler.add(PostbackEvent)
    def handle_postback(event):
        values = parse_qs(event.postback.data)
        if values.get("action", [None])[0] != "availability":
            return
        try:
            event_id = int(values.get("event_id", [""])[0])
        except ValueError:
            reply(event, TextMessage(text="無效的活動資料。"))
            return
        status = values.get("status", [None])[0]
        if status not in {"available", "unavailable"}:
            reply(event, TextMessage(text="無效的回覆選項。"))
            return
        event_row = get_event(event_id)
        if event_row is None:
            reply(event, TextMessage(text="找不到這個活動。"))
            return
        user_id = getattr(event.source, "user_id", None)
        if not user_id:
            reply(event, TextMessage(text="無法識別你的 LINE 帳號。"))
            return
        source_type, source_id = source_context(event)
        same_chat = (
            event_row["source_type"] == source_type
            and event_row["source_id"] == source_id
        )
        if not same_chat and not is_event_recipient(event_id, user_id):
            reply(event, TextMessage(text="你不在這個活動的填寫名單中。"))
            return
        member = get_member(user_id)
        if member is not None:
            display_name = member["real_name"]
        else:
            with ApiClient(configuration) as api_client:
                display_name = get_display_name(MessagingApi(api_client), event, user_id)
        save_availability(event_id, user_id, display_name, status)
        status_text = "✅ 有空" if status == "available" else "❌ 沒空"
        reply(event, TextMessage(text=f"已記錄：{display_name} → {status_text}"))
