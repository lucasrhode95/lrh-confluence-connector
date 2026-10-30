"""Synthetic checks for account email notification settings."""

from __future__ import annotations

import unittest

from confluence_connector.domain.errors import InvalidRequest, Upstream
from confluence_connector.domain.models import EmailNotificationSettingsUpdate
from confluence_connector.remote.transport import FormResponse
from confluence_connector.service.user_settings import EmailNotificationSettingsService, SETTING_FIELDS


class EmailNotificationSettingsChecks(unittest.IsolatedAsyncioTestCase):
    async def test_reads_all_eight_checked_values_from_the_view_form(self) -> None:
        transport = _Transport()

        settings = await _service(transport).get_settings()

        self.assertEqual(settings.autowatch, True)
        self.assertEqual(settings.daily_updates, False)
        self.assertEqual(settings.recommended_updates, True)
        self.assertEqual(len(SETTING_FIELDS), 8)

    async def test_partial_update_sends_full_state_and_verifies_readback(self) -> None:
        transport = _Transport()
        result = await _service(transport).update_settings(
            EmailNotificationSettingsUpdate(daily_updates=True, autowatch=False)
        )

        self.assertEqual(result.changed_settings, ["autowatch", "daily_updates"])
        self.assertEqual(result.mismatches, [])
        self.assertFalse(result.settings.autowatch)
        self.assertTrue(result.settings.daily_updates)
        self.assertTrue(result.settings.recommended_updates)
        self.assertEqual(transport.posts, 1)
        self.assertEqual(transport.submitted["atl_token"], "synthetic-csrf")
        self.assertEqual(transport.submitted["edit"], "Edit")
        self.assertNotIn("watchMyOwnContent", transport.submitted)
        self.assertEqual(transport.submitted["notifyByEmail"], "on")
        self.assertEqual(transport.submitted["siteBlogWatchForUser"], "on")

    async def test_empty_or_unchanged_update_does_not_post(self) -> None:
        transport = _Transport()
        service = _service(transport)
        for update in (EmailNotificationSettingsUpdate(), EmailNotificationSettingsUpdate(autowatch=True)):
            with self.subTest(update=update), self.assertRaises(InvalidRequest):
                await service.update_settings(update)
        self.assertEqual(transport.posts, 0)

    async def test_reports_server_values_that_differ_after_save(self) -> None:
        transport = _Transport(ignore_updates=True)
        result = await _service(transport).update_settings(
            EmailNotificationSettingsUpdate(daily_updates=True)
        )

        self.assertEqual(result.mismatches, ["daily_updates"])
        self.assertFalse(result.settings.daily_updates)

    async def test_rejects_incomplete_or_ambiguous_form(self) -> None:
        with self.assertRaises(Upstream):
            await _service(_Transport(html="<form action='editmyemailsettings.action'></form>")).get_settings()


def _service(transport: _Transport) -> EmailNotificationSettingsService:
    return EmailNotificationSettingsService(transport, "sample-user")  # type: ignore[arg-type]


class _Transport:
    def __init__(self, *, html: str | None = None, ignore_updates: bool = False) -> None:
        self.values = {name: name in {"autowatch", "all_blog_posts", "recommended_updates"} for name in SETTING_FIELDS}
        self.html = html
        self.ignore_updates = ignore_updates
        self.posts = 0
        self.submitted: dict[str, str] = {}

    async def get_text(self, path: str, *, expected_username: str) -> str:
        self.assert_account(path, expected_username)
        return self.html if self.html is not None else _form(self.values)

    async def send_form(self, path: str, *, form_fields: dict[str, str], expected_username: str) -> FormResponse:
        self.assert_account(path, expected_username)
        self.posts += 1
        self.submitted = form_fields
        if not self.ignore_updates:
            self.values = {
                name: SETTING_FIELDS[name] in form_fields
                for name in SETTING_FIELDS
            }
        return FormResponse(status_code=302, location="/users/viewmyemailsettings.action")

    @staticmethod
    def assert_account(path: str, expected_username: str) -> None:
        if expected_username != "sample-user":
            raise AssertionError(f"unexpected account for {path}")


def _form(values: dict[str, bool]) -> str:
    controls = ["<input type='hidden' name='atl_token' value='synthetic-csrf'>"]
    controls.extend(
        f"<input type='checkbox' name='{field}' value='on'{' checked' if values[name] else ''}>"
        for name, field in SETTING_FIELDS.items()
    )
    controls.append("<input type='submit' name='edit' value='Edit'>")
    return "<form action='editmyemailsettings.action'>" + "".join(controls) + "</form>"
