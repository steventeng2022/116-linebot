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

            CREATE INDEX IF NOT EXISTS idx_events_source
                ON events(source_type, source_id, id DESC);
            CREATE INDEX IF NOT EXISTS idx_members_class_seat
                ON members(class_name, seat_number);
            """
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
            SELECT real_name, class_name, seat_number, line_display_name, verified_at, updated_at
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


def create_event(source_type, source_id, title, created_by):
    with database_connection() as connection:
        cursor = connection.execute(
            "INSERT INTO events (source_type, source_id, title, created_by) VALUES (?, ?, ?, ?)",
            (source_type, source_id, title, created_by),
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
            "SELECT id, source_type, source_id, title FROM events WHERE id = ?", (event_id,)
        ).fetchone()


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


def page_shell(title, body):
    return f"""<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(title)}</title>
<style>
:root{{--bg:#f4f7fb;--card:#fff;--ink:#172033;--muted:#6b7280;--brand:#0b7a53;--line:#e5e7eb}}
*{{box-sizing:border-box}} body{{margin:0;background:var(--bg);color:var(--ink);font-family:system-ui,-apple-system,"Noto Sans TC",sans-serif}}
.wrap{{max-width:1100px;margin:0 auto;padding:32px 18px}} .card{{background:var(--card);border:1px solid var(--line);border-radius:18px;padding:24px;box-shadow:0 8px 30px #102a4312}}
h1{{margin:0 0 8px;font-size:28px}} p{{color:var(--muted)}} input{{width:100%;padding:12px 14px;border:1px solid #cbd5e1;border-radius:10px;font-size:16px;margin:6px 0 14px}}
button,.button{{display:inline-block;background:var(--brand);color:white;border:0;border-radius:10px;padding:11px 16px;text-decoration:none;font-weight:700;cursor:pointer}}
.top{{display:flex;justify-content:space-between;gap:14px;align-items:center;margin-bottom:18px}} .muted{{color:var(--muted)}} .error{{color:#b42318}}
.table-wrap{{overflow:auto}} table{{width:100%;border-collapse:collapse}} th,td{{padding:12px;text-align:left;border-bottom:1px solid var(--line);white-space:nowrap}} th{{font-size:13px;color:var(--muted)}}
.badge{{background:#e8f5ef;color:#08734c;padding:5px 9px;border-radius:999px;font-size:13px;font-weight:700}} @media(max-width:600px){{.wrap{{padding:18px 10px}}.card{{padding:16px}}}}
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
def admin_logout():
    response = RedirectResponse("/admin/login", status_code=303)
    response.delete_cookie("lineb_admin")
    return response


@app.get("/admin", response_class=HTMLResponse)
def admin_dashboard(request: Request):
    if not valid_admin_session(request):
        return RedirectResponse("/admin/login", status_code=303)
    rows = list_members()
    table_rows = "".join(
        "<tr>"
        f"<td><strong>{html.escape(row['real_name'])}</strong></td>"
        f"<td>{html.escape(row['class_name'])}班</td><td>{row['seat_number']}號</td>"
        f"<td>{html.escape(row['line_display_name'])}</td>"
        f"<td>{html.escape(row['updated_at'])} UTC</td></tr>"
        for row in rows
    ) or '<tr><td colspan="5" class="muted">目前尚無驗證資料</td></tr>'
    body = f"""<div class="top"><div><h1>音控成員管理</h1><div class="muted">身分驗證資料</div></div>
    <form method="post" action="/admin/logout"><button type="submit">登出</button></form></div>
    <div class="card"><div class="top"><span class="badge">共 {len(rows)} 人</span><a class="button" href="/admin/export.csv">匯出 CSV</a></div>
    <div class="table-wrap"><table><thead><tr><th>姓名</th><th>班級</th><th>座號</th><th>LINE 名稱</th><th>更新時間</th></tr></thead>
    <tbody>{table_rows}</tbody></table></div></div>"""
    return HTMLResponse(page_shell("音控成員管理", body))


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
            title = text[6:].strip()
            if not title:
                reply(event, TextMessage(text="用法：/event 活動名稱"))
                return
            source_type, source_id = source_context(event)
            event_id = create_event(source_type, source_id, title, user_id)
            reply(event, build_event_message(event_id, title))
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
        source_type, source_id = source_context(event)
        if event_row["source_type"] != source_type or event_row["source_id"] != source_id:
            reply(event, TextMessage(text="這個活動不屬於目前的聊天室。"))
            return
        user_id = getattr(event.source, "user_id", None)
        if not user_id:
            reply(event, TextMessage(text="無法識別你的 LINE 帳號。"))
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
