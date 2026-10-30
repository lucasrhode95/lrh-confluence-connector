"""Read and update the signed-in user's email notification settings."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from confluence_connector.domain.errors import (
    AccountMismatch,
    AuthenticationRequired,
    ConnectorError,
    InvalidRequest,
    UncertainWrite,
    Upstream,
)
from confluence_connector.domain.models import (
    EmailNotificationSettings,
    EmailNotificationSettingsUpdate,
    EmailNotificationSettingsUpdateResult,
)
from confluence_connector.remote.transport import ConfluenceTransport

VIEW_SETTINGS_ROUTE = "users/viewmyemailsettings.action"
UPDATE_SETTINGS_ROUTE = "users/editmyemailsettings.action"
SETTING_FIELDS = {
    "autowatch": "watchMyOwnContent",
    "daily_updates": "notifyByEmail",
    "all_blog_posts": "siteBlogWatchForUser",
    "network": "watchingNetwork",
    "new_followers": "notifyOnNewFollowers",
    "notify_on_my_actions": "notifyForMyOwnActions",
    "show_changed_content": "showDiffInEmailNotifications",
    "recommended_updates": "receiveRecommendedEmail",
}


class EmailNotificationSettingsService:
    """Own the isolated undocumented UI form and verify each saved setting by rereading it."""

    def __init__(self, transport: ConfluenceTransport, username: str) -> None:
        self._transport = transport
        self._username = username

    async def get_settings(self) -> EmailNotificationSettings:
        form = await self._read_form()
        return form.settings

    async def update_settings(
        self, changes: EmailNotificationSettingsUpdate
    ) -> EmailNotificationSettingsUpdateResult:
        requested = changes.model_dump(exclude_none=True)
        if not requested:
            raise InvalidRequest("Set at least one email notification setting.")
        form = await self._read_form()
        desired = form.settings.model_copy(update=requested)
        changed = [
            name
            for name in SETTING_FIELDS
            if getattr(form.settings, name) != getattr(desired, name)
        ]
        if not changed:
            raise InvalidRequest("The selected settings already have those values; nothing was sent.")
        response = await self._transport.send_form(
            UPDATE_SETTINGS_ROUTE,
            form_fields=form.submission_fields(desired),
            expected_username=self._username,
        )
        try:
            actual = await self.get_settings()
        except (AuthenticationRequired, AccountMismatch):
            raise
        except ConnectorError as exc:
            raise UncertainWrite(
                f"The settings form returned HTTP {response.status_code}, but its saved values could not be reread; "
                "inspect the settings before retrying."
            ) from exc
        mismatches = [
            name
            for name in SETTING_FIELDS
            if getattr(actual, name) != getattr(desired, name)
        ]
        return EmailNotificationSettingsUpdateResult(
            settings=actual,
            changed_settings=changed,
            mismatches=mismatches,
            response_status=response.status_code,
        )

    async def _read_form(self) -> _EmailSettingsForm:
        html = await self._transport.get_text(VIEW_SETTINGS_ROUTE, expected_username=self._username)
        return _email_settings_form(html)


@dataclass(frozen=True, slots=True)
class _EmailSettingsForm:
    settings: EmailNotificationSettings
    hidden_fields: dict[str, str]
    csrf_token: str
    submit_name: str
    submit_value: str
    checkbox_values: dict[str, str]

    def submission_fields(self, settings: EmailNotificationSettings) -> dict[str, str]:
        fields = dict(self.hidden_fields)
        fields["atl_token"] = self.csrf_token
        for setting_name, form_name in SETTING_FIELDS.items():
            if getattr(settings, setting_name):
                fields[form_name] = self.checkbox_values[form_name]
            else:
                fields.pop(form_name, None)
        fields[self.submit_name] = self.submit_value
        return fields


def _email_settings_form(html: str) -> _EmailSettingsForm:
    soup = BeautifulSoup(html, "html.parser")
    forms = [
        form
        for form in soup.find_all("form")
        if urlsplit(form.get("action") or "").path.casefold().endswith("editmyemailsettings.action")
    ]
    if len(forms) != 1:
        raise Upstream("Confluence returned no unique email settings form.")
    form = forms[0]
    checkboxes: dict[str, bool] = {}
    checkbox_values: dict[str, str] = {}
    hidden_fields: dict[str, str] = {}
    csrf_token: str | None = None
    submit: tuple[str, str] | None = None
    known_form_names = set(SETTING_FIELDS.values())
    for control in form.find_all("input"):
        name = control.get("name")
        kind = (control.get("type") or "text").casefold()
        if not isinstance(name, str) or not name:
            continue
        if kind == "checkbox":
            if name not in known_form_names or name in checkboxes:
                raise Upstream("Confluence returned an unexpected email notification checkbox.")
            checkboxes[name] = control.has_attr("checked")
            checkbox_values[name] = str(control.get("value") or "on")
        elif kind == "hidden":
            value = str(control.get("value") or "")
            hidden_fields[name] = value
            if name == "atl_token":
                if csrf_token is not None or not value:
                    raise Upstream("Confluence returned an invalid email settings form token.")
                csrf_token = value
        elif kind == "submit" and submit is None:
            submit = (name, str(control.get("value") or ""))
    if set(checkboxes) != known_form_names:
        raise Upstream("Confluence returned an incomplete email notification settings form.")
    if csrf_token is None or submit is None:
        raise Upstream("Confluence returned an incomplete email settings form.")
    settings_by_name = {
        setting_name: checkboxes[form_name]
        for setting_name, form_name in SETTING_FIELDS.items()
    }
    return _EmailSettingsForm(
        settings=EmailNotificationSettings(**settings_by_name),
        hidden_fields=hidden_fields,
        csrf_token=csrf_token,
        submit_name=submit[0],
        submit_value=submit[1],
        checkbox_values=checkbox_values,
    )
