"""Reject missing or skipped offline browser coverage in the CI JUnit report."""
from __future__ import annotations

import argparse
from pathlib import Path
import xml.etree.ElementTree as ET


REQUIRED_TESTS = {
    "test_auto_generate_ui": {
        "test_auto_generate_mocked_browser",
        "test_windows_manual_display_is_separate",
    },
    "test_model_quota_ui": {
        "test_models_browser_mocked_api_desktop_mobile_and_actions",
    },
    "test_signup_form_transition": {
        "test_waits_for_actual_signup_mode_and_retries_only_toggle",
        "test_existing_form_does_not_click_or_toggle_back",
        "test_stuck_login_reports_transition_not_missing_generic_text",
    },
    "test_update_ui": {
        "test_older_published_release_never_looks_like_downgrade_offer",
        "test_download_requires_explicit_restart_and_retry_keeps_download",
        "test_close_reopen_does_not_duplicate_download_or_apply",
        "test_failed_download_can_retry_and_stale_poll_cannot_reset_ready",
        "test_check_now_is_single_flight_across_card_reopen",
        "test_backend_phase_restores_controls_after_reload",
        "test_check_now_without_available_release_and_failed_check_preserves_result",
        "test_init_is_idempotent_and_polling_is_hidden_aware",
        "test_release_links_reject_unsafe_urls_and_linux_never_downloads",
    },
    "test_v2_workspace_browser": {
        "test_v2_navigation_and_studio_handoff",
    },
}


def audit_report(path: Path) -> tuple[list[str], int]:
    required = {(module, name) for module, names in REQUIRED_TESTS.items() for name in names}
    seen = set()
    errors = []
    count = 0
    for case in ET.parse(path).iter("testcase"):
        module = case.get("classname", "").rsplit(".", 1)[-1]
        name = case.get("name", "").split("[", 1)[0]
        identity = (module, name)
        if identity not in required:
            continue
        seen.add(identity)
        count += 1
        for outcome in ("skipped", "failure", "error"):
            if case.find(outcome) is not None:
                errors.append(f"{module}.{case.get('name')}: {outcome}")
    errors.extend(f"{module}.{name}: missing" for module, name in sorted(required - seen))
    return errors, count


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    args = parser.parse_args()
    try:
        errors, count = audit_report(args.report)
    except (OSError, ET.ParseError) as exc:
        print(f"Browser coverage report unavailable: {exc}")
        return 1
    if errors:
        print("Required offline browser coverage failed:\n" + "\n".join(errors))
        return 1
    print(f"Required offline browser coverage: {count} cases passed, none skipped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
