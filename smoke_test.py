import tempfile
from pathlib import Path

import main


def run():
    with tempfile.TemporaryDirectory() as temp_dir:
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

        event_id = main.create_event("group", "group-1", "校慶", "user-1")
        main.save_availability(event_id, "user-1", member["real_name"], "available")
        summary = main.format_availability(main.get_latest_event("group", "group-1"), main.get_availability(event_id))
        assert "有空（1）" in summary and "王小明" in summary
        assert "availability" in main.build_event_message(event_id, "校慶").to_json()

        token = main.session_cookie("admin", expires=2_000_000_000)
        assert token

        rendered = main.page_shell("測試", "<p>管理頁</p>")
        assert 'lang="zh-Hant"' in rendered
        assert "管理頁" in rendered

    print("smoke test passed")


if __name__ == "__main__":
    run()
