import tempfile
import sqlite3
from contextlib import closing
from pathlib import Path

import main
from starlette.requests import Request


def run():
    with tempfile.TemporaryDirectory() as temp_dir:
        legacy_path = str(Path(temp_dir) / "legacy.db")
        with closing(sqlite3.connect(legacy_path)) as legacy:
            legacy.execute(
                """
                CREATE TABLE events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_type TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    created_by TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            legacy.commit()
        main.DATABASE_PATH = legacy_path
        main.init_database()
        with closing(sqlite3.connect(legacy_path)) as migrated:
            columns = [row[1] for row in migrated.execute("PRAGMA table_info(events)")]
        assert "is_broadcast" in columns

        main.DATABASE_PATH = str(Path(temp_dir) / "test.db")
        main.ADMIN_SESSION_SECRET = "test-session-secret"
        main.ADMIN_PASSWORD = "test-password"
        main.init_database()

        assert main.parse_class_seat("116班 01號") == ("116", 1)
        assert main.parse_class_seat("116 / 12") == ("116", 12)
        assert main.parse_class_seat("沒有座號") is None

        main.start_verification("user-1")
        assert main.get_verification_session("user-1")["step"] == "name"
        main.save_verification_name("user-1", "王小明")
        assert main.get_verification_session("user-1")["step"] == "class_seat"
        main.save_member("user-1", "Steven", "王小明", "116", 1)
        member = main.get_member("user-1")
        assert member["real_name"] == "王小明"
        assert member["seat_number"] == 1
        assert main.get_verification_session("user-1") is None

        assert main.claim_initial_admin("admin-1", "管理員") is True
        assert main.claim_initial_admin("admin-2", "其他人") is False

        roles = main.list_work_roles()
        assert [role["name"] for role in roles] == ["音控", "簡報", "攝影", "機動"]

        event_id = main.create_event(
            "group", "group-1", "校慶", "user-1", is_broadcast=True
        )
        assert main.get_event(event_id)["is_broadcast"] == 1
        main.save_recipient(event_id, "user-1", "sent")
        assert main.is_event_recipient(event_id, "user-1") is True
        main.save_availability(event_id, "user-1", member["real_name"], "available")
        main.save_assignment(event_id, "user-1", roles[0]["id"], "主控台")
        assignments = main.get_event_assignments(event_id)
        assert assignments[0]["role_name"] == "音控"
        assert assignments[0]["notes"] == "主控台"
        roster = main.get_event_roster(event_id)
        assert roster[0]["status"] == "available"
        assert roster[0]["role_names"] == "音控"

        main.set_availability_admin(event_id, "user-1", "unavailable")
        assert main.get_event_roster(event_id)[0]["status"] == "unavailable"
        main.set_availability_admin(event_id, "user-1", "available")

        event_summary = main.list_events()[0]
        assert event_summary["recipient_count"] == 1
        assert event_summary["available_count"] == 1

        broadcast_id = main.create_event(
            "admin", "global", "全員測試", "web-admin", is_broadcast=True
        )
        original_configuration = main.configuration
        main.configuration = None
        sent, failed = main.broadcast_event(broadcast_id, "全員測試")
        main.configuration = original_configuration
        assert (sent, failed) == (0, 1)
        assert main.is_event_recipient(broadcast_id, "user-1") is True

        summary = main.format_availability(main.get_latest_event("group", "group-1"), main.get_availability(event_id))
        assert "有空（1）" in summary and "王小明" in summary
        assert "availability" in main.build_event_message(event_id, "校慶").to_json()

        main.create_work_role("燈光")
        assert "燈光" in [role["name"] for role in main.list_work_roles()]
        main.update_work_role(roles[0]["id"], "音控主控")
        assert main.list_work_roles()[0]["name"] == "音控主控"

        main.update_member_admin("user-1", "王大明", "117", 2)
        assert main.get_member("user-1")["seat_number"] == 2

        token = main.session_cookie("admin", expires=2_000_000_000)
        assert token

        request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/admin",
                "headers": [(b"cookie", f"lineb_admin={token}".encode())],
                "query_string": b"",
                "server": ("testserver", 443),
                "scheme": "https",
            }
        )
        assert main.valid_admin_session(request) is True
        assert len(main.csrf_token(request)) == 64
        dashboard = main.admin_dashboard(request)
        assert dashboard.status_code == 200
        assert "最高權限控制台" in dashboard.body.decode()
        event_page = main.admin_event_detail(event_id, request)
        assert "分工結果" in event_page.body.decode()
        worksheet = main.admin_work_sheet(event_id, request)
        assert "音控主控" in worksheet.body.decode()

        rendered = main.page_shell("測試", "<p>管理頁</p>")
        assert 'lang="zh-Hant"' in rendered
        assert "管理頁" in rendered

    print("smoke test passed")


if __name__ == "__main__":
    run()
