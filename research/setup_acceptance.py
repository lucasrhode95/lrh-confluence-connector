"""Create or refresh one synthetic acceptance fixture tree after an explicit operator opt-in."""

from __future__ import annotations

import argparse
import asyncio
import re
from pathlib import Path
from typing import Any
from urllib.parse import quote


from acceptance_fixtures import (
    AcceptanceFixture,
    attachment_text,
    child_text,
    image_png,
    new_fixture,
    root_storage,
)
from confluence_connector import config
from confluence_connector.auth.store import TokenStore
from confluence_connector.bootstrap import AppContext, Services
from confluence_connector.domain.errors import ConnectorError

from confluence_connector.remote.transport import ConfluenceTransport


def main() -> int:
    """Validate private settings before any prompt or request; writes require ``--apply``."""
    args = _parse_args()
    settings = config.load_config(args.config)
    try:
        fixture = new_fixture(args.run_id)
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
    acceptance = settings.get("acceptance") or {}
    personal_space = acceptance.get("personal_space_key")
    cross_space = acceptance.get("cross_space_key")
    if personal_space is not None and cross_space == personal_space:
        raise SystemExit("acceptance.cross_space_key must differ from personal_space_key.")
    if not args.apply:
        print(
            f"READY: fixture {fixture.run_id}; configured personal space="
            f"{'yes' if personal_space else 'no'}. No network requests or writes were made."
        )
        print("Pass --apply to create or refresh the synthetic fixture.")
        return 0
    if not personal_space:
        print("SKIP: acceptance.personal_space_key is not configured; no requests or writes were made.")
        return 0
    context = AppContext(settings)
    try:
        summary = asyncio.run(_setup_and_close(context, settings, fixture, str(personal_space)))
    except ConnectorError as exc:
        print(f"FAIL: fixture setup stopped after {type(exc).__name__}; inspect the synthetic run before retrying.")
        return 1
    print(
        f"PASS: fixture {fixture.run_id}; pages={summary['pages']}; "
        f"attachments={summary['attachments']}; comments={summary['comments']}; "
        f"label={'present' if summary['label'] else 'missing'}"
    )
    print(f"Manual step: add one inline comment to the page titled {fixture.root_title!r} in Confluence.")
    print("Use the same --run-id with run_acceptance.py; the live suite reports outcomes and timings only.")
    return 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True, help="Explicit private Confluence config file.")
    parser.add_argument("--run-id", help="Reuse an 8-16 character lowercase hexadecimal fixture run ID.")
    parser.add_argument("--apply", action="store_true", help="Create or refresh fixture pages and attachments.")
    return parser.parse_args()


async def _setup_and_close(
    context: AppContext,
    settings: dict[str, Any],
    fixture: AcceptanceFixture,
    personal_space: str,
) -> dict[str, int | bool]:
    """Run setup and close its async client on the same event loop, including after partial failure."""
    try:
        return await _setup(context, settings, fixture, personal_space)
    finally:
        await context.aclose()


async def _setup(
    context: AppContext,
    settings: dict[str, Any],
    fixture: AcceptanceFixture,
    personal_space: str,
) -> dict[str, int | bool]:
    """Verify target space and account before upserting only pages bearing this unique run ID."""
    status = await context.check_auth_status()
    if not status.checked or status.username is None:
        raise SystemExit("The configured Confluence credential could not be verified.")
    services = await context.services()
    if services.username.casefold() != status.username.casefold():
        raise SystemExit("The Confluence account changed after verification; no fixture writes were started.")
    spaces = await services.spaces.list_spaces()
    target = next(
        (space for space in spaces.spaces if space.key == personal_space and space.type == "personal"),
        None,
    )
    if target is None:
        raise SystemExit("The configured personal acceptance space is not a visible personal space.")
    cross_space = (settings.get("acceptance") or {}).get("cross_space_key")
    if cross_space is not None and not any(space.key == cross_space for space in spaces.spaces):
        raise SystemExit("The configured cross-space acceptance target is not visible to this account.")

    root = await _ensure_page(
        services,
        fixture,
        title=fixture.root_title,
        body=root_storage(fixture),
        space_key=personal_space,
        parent_id=None,
        position=None,
    )
    child_one = await _ensure_page(
        services,
        fixture,
        title=fixture.child_one_title,
        body=f"<p>{child_text(fixture, 'firstchild')}</p>",
        space_key=personal_space,
        parent_id=root,
        position="first",
    )
    await _ensure_page(
        services,
        fixture,
        title=fixture.child_two_title,
        body=f"<p>{child_text(fixture, 'secondchild')}</p>",
        space_key=personal_space,
        parent_id=root,
        position="last",
    )
    await _ensure_page(
        services,
        fixture,
        title=fixture.grandchild_title,
        body=f"<p>{child_text(fixture, 'grandchild')}</p>",
        space_key=personal_space,
        parent_id=child_one,
        position="last",
    )
    attachments = await services.api.list_page_attachments(root, expected_username=services.username)
    attachment_names = {item.filename for item in attachments}
    uploaded = 0
    if fixture.text_filename not in attachment_names:
        await _upload_file(
            context.store,
            str(settings["base_url"]),
            _attachment_route(settings, root),
            fixture.text_filename,
            attachment_text(fixture),
            "text/plain; charset=utf-8",
            expected_username=services.username,
        )
        uploaded += 1
    if fixture.image_filename not in attachment_names:
        await _upload_file(
            context.store,
            str(settings["base_url"]),
            _attachment_route(settings, root),
            fixture.image_filename,
            image_png(),
            "image/png",
            expected_username=services.username,
        )
        uploaded += 1

    await services.writes.set_labels(root, add=[fixture.label])
    await _ensure_versions(services, fixture, root)
    await _ensure_footer_comment(services, fixture, root)
    return {"pages": 4, "attachments": len(attachment_names) + uploaded, "comments": 2, "label": True}


async def _ensure_page(
    services: Services,
    fixture: AcceptanceFixture,
    *,
    title: str,
    body: str,
    space_key: str,
    parent_id: str | None,
    position: str | None,
) -> str:
    """Create a uniquely titled page or refresh its body only when its synthetic marker proves ownership."""
    page_id = await services.api.find_page_id_by_title(
        space_key, title, expected_username=services.username
    )
    if page_id is None:
        created = await services.writes.create_page(
            title,
            body,
            format="rich",
            parent_id=parent_id,
            space_key=space_key if parent_id is None else None,
            position=position,
        )
        return created.page_id

    current = await services.api.get_page_content(
        page_id, expected_username=services.username, include_storage=True
    )
    if fixture.marker not in current.storage_body:
        raise SystemExit("A page with the synthetic title exists but lacks this run's marker; it was not changed.")
    metadata = current.metadata
    current_parent = metadata.ancestors[-1].page_id if metadata.ancestors else None
    if metadata.space_key != space_key or current_parent != parent_id:
        await services.structure.move_page(
            page_id,
            parent_id=parent_id,
            space_key=space_key if parent_id is None else None,
            position=position,
        )
    elif parent_id is not None and position is not None:
        await services.structure.move_page(page_id, parent_id=parent_id, position=position)
    if _fixture_body_needs_refresh(current.storage_body, body):
        # Same-space placement preserves the content version. Pin the fresh version before updating.
        current = await services.api.get_page_content(
            page_id, expected_username=services.username, include_storage=True
        )
        if fixture.marker not in current.storage_body:
            raise SystemExit("The fixture ownership marker changed during setup; the body was not changed.")
        if not _fixture_body_needs_refresh(current.storage_body, body):
            return page_id
        await services.writes.update_page(
            page_id,
            body,
            expected_version=current.version,
            format="rich",
        )
    return page_id


def _fixture_body_needs_refresh(existing: str, desired: str) -> bool:
    """Refresh an incomplete synthetic body only when doing so cannot detach a manual inline comment."""
    if "<ac:inline-comment-marker" in existing and "<ac:inline-comment-marker" not in desired:
        return False
    desired_macros = _macro_names(desired)
    existing_macros = _macro_names(existing)
    return not desired_macros <= existing_macros or "orchidbeacon" not in existing


def _macro_names(storage: str) -> set[str]:
    """Collect macro names from storage markup without interpreting rendered page text."""
    return set(re.findall(r'<ac:structured-macro\b[^>]*\bac:name="([^"]+)"', storage))


async def _ensure_versions(services: Services, fixture: AcceptanceFixture, page_id: str) -> None:
    """Ensure the synthetic root has several versions, stopping if manual inline anchors prevent a safe edit."""
    metadata = await services.api.get_page_metadata(page_id, expected_username=services.username)
    version = metadata.version or 1
    while version < 3:
        content = await services.api.get_page_content(
            page_id, expected_username=services.username, include_storage=True
        )
        if "<ac:inline-comment-marker" in content.storage_body:
            return
        result = await services.writes.update_page(
            page_id,
            root_storage(fixture, revision=version + 1),
            expected_version=version,
            format="rich",
        )
        version = result.version or version + 1


async def _ensure_footer_comment(
    services: Services,
    fixture: AcceptanceFixture,
    page_id: str,
) -> None:
    """Create a synthetic footer comment and reply once through the route-owning remote adapter."""
    comments = await services.api.list_page_comments(page_id, expected_username=services.username)
    top = next(
        (item for item in comments if fixture.marker + "footernote" in item.body_view and item.parent_comment_id is None),
        None,
    )
    if top is None:
        comment_id = await services.api.create_page_comment(
            page_id,
            f"<p>{fixture.marker}footernote</p>",
            expected_username=services.username,
        )
    else:
        comment_id = top.comment_id
    if any(
        item.parent_comment_id == comment_id and fixture.marker + "replynote" in item.body_view
        for item in comments
    ):
        return
    await services.api.create_page_comment(
        page_id,
        f"<p>{fixture.marker}replynote</p>",
        expected_username=services.username,
        parent_comment_id=comment_id,
    )


def _attachment_route(settings: dict[str, Any], page_id: str) -> str:
    """Build a relative multipart upload route from the validated configured content template."""
    template = str(settings["endpoints"]["content"])
    return template.replace("{page_id}", quote(page_id, safe="")) + "/child/attachment"


async def _upload_file(
    store: TokenStore,
    base_url: str,
    route: str,
    filename: str,
    content: bytes,
    media_type: str,
    *,
    expected_username: str,
) -> None:
    """Upload trusted synthetic bytes using the product transport's conservative write policy."""
    transport = ConfluenceTransport({"base_url": base_url}, store)
    try:
        await transport.send_json(
            "POST", route, files={"file": (filename, content, media_type)},
            skip_atlassian_token_check=True, expected_username=expected_username,
        )
    finally:
        await transport.aclose()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (KeyboardInterrupt, EOFError):
        print("\nCancelled; inspect the synthetic fixture before retrying.")
        raise SystemExit(130)
