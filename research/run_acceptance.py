"""Run bounded Confluence acceptance checks against one explicitly selected synthetic fixture."""

from __future__ import annotations

import argparse
import asyncio
import time
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Awaitable, Callable

from acceptance_fixtures import AcceptanceFixture, new_fixture
from confluence_connector import config
from confluence_connector.bootstrap import AppContext, Services
from confluence_connector.domain.errors import Conflict, InvalidRequest
from confluence_connector.domain.models import ContentSearchResult
from confluence_connector.timezones import parse_cql_timezone

INDEX_ATTEMPTS = 18
INDEX_WAIT_SECONDS = 5.0
SEARCH_INDEX_ATTEMPTS = 4
SEARCH_INDEX_WAIT_SECONDS = 2.0
MAX_CONTINUATION_PAGES = 30


@dataclass(frozen=True)
class CheckResult:
    """One sanitized acceptance outcome; details never contain server response text."""

    name: str
    status: str
    elapsed_ms: int
    detail: str


class SkipCheck(Exception):
    """A named prerequisite is absent, so this live check has no valid target."""


class AcceptanceReport:
    """Record checks with durations while exposing only fixed labels and exception types."""

    def __init__(self) -> None:
        self.results: list[CheckResult] = []

    async def run(self, name: str, operation: Callable[[], Awaitable[None]]) -> None:
        """Run one check and retain a sanitized outcome instead of response details or page content."""
        started = time.perf_counter()
        try:
            await operation()
        except SkipCheck as exc:
            self.results.append(CheckResult(name, "SKIP", _elapsed_ms(started), str(exc)))
        except Exception as exc:
            self.results.append(CheckResult(name, "FAIL", _elapsed_ms(started), type(exc).__name__))
        else:
            self.results.append(CheckResult(name, "PASS", _elapsed_ms(started), ""))

    def skip(self, name: str, detail: str) -> None:
        """Record a prerequisite-based skip before an operation is constructed."""
        self.results.append(CheckResult(name, "SKIP", 0, detail))

    def fail(self, name: str, detail: str, elapsed_ms: int = 0) -> None:
        """Record a sanitized failure label, normally an exception type rather than its message."""
        self.results.append(CheckResult(name, "FAIL", elapsed_ms, detail))

    def print(self) -> tuple[int, int, int]:
        """Print pass/fail/skip and elapsed time without page titles, IDs, URLs or bodies."""
        for result in self.results:
            suffix = f" detail={result.detail}" if result.detail else ""
            print(f"{result.status} {result.name} elapsed_ms={result.elapsed_ms}{suffix}")
        passed = sum(result.status == "PASS" for result in self.results)
        failed = sum(result.status == "FAIL" for result in self.results)
        skipped = sum(result.status == "SKIP" for result in self.results)
        print(f"SUMMARY pass={passed} fail={failed} skipped={skipped}")
        return passed, failed, skipped


def main() -> int:
    """Validate explicit configuration and opt-in flags before constructing a network client."""
    args = _parse_args()
    settings = config.load_config(args.config)
    try:
        fixture = new_fixture(args.run_id)
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
    if not args.run_live:
        print(f"READY: fixture {fixture.run_id}; no network requests or writes were made.")
        print("Pass --run-live to execute checks. Pass --allow-writes as well to run synthetic write checks.")
        return 0
    acceptance = settings.get("acceptance") or {}
    personal_space = acceptance.get("personal_space_key")
    if not personal_space:
        print("SKIP: acceptance.personal_space_key is not configured; no requests were made.")
        return 0
    context = AppContext(settings)
    report = asyncio.run(
        _run_live_and_close(context, settings, fixture, str(personal_space), bool(args.allow_writes))
    )
    _, failed, _ = report.print()
    return 1 if failed else 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True, help="Explicit private Confluence config file.")
    parser.add_argument("--run-id", required=True, help="Fixture run ID printed by setup_acceptance.py.")
    parser.add_argument("--run-live", action="store_true", help="Send configured Confluence requests.")
    parser.add_argument("--allow-writes", action="store_true", help="Run writes against disposable synthetic pages.")
    return parser.parse_args()


async def _run_live_and_close(
    context: AppContext,
    settings: dict[str, Any],
    fixture: AcceptanceFixture,
    personal_space: str,
    allow_writes: bool,
) -> AcceptanceReport:
    """Run checks and close their async client on the same event loop."""
    try:
        return await _run_live(context, settings, fixture, personal_space, allow_writes)
    finally:
        await context.aclose()


async def _run_live(
    context: AppContext,
    settings: dict[str, Any],
    fixture: AcceptanceFixture,
    personal_space: str,
    allow_writes: bool,
) -> AcceptanceReport:
    """Verify authentication, discover this run's pages, and run independent read/write acceptance groups."""
    report = AcceptanceReport()
    started = time.perf_counter()
    try:
        status = await context.check_auth_status()
        services = await context.services()
    except Exception as exc:
        report.fail("authentication", type(exc).__name__, _elapsed_ms(started))
        return report
    if not status.checked or status.username is None:
        report.fail("authentication", "AuthenticationRequired", _elapsed_ms(started))
        return report
    if services.username.casefold() != status.username.casefold():
        report.fail("authentication", "AccountChanged", _elapsed_ms(started))
        return report
    await report.run("authentication", _pass())
    started = time.perf_counter()
    try:
        page_ids = await _locate_fixture(services, fixture, personal_space)
    except SkipCheck as exc:
        report.skip("fixture", str(exc))
        for name in (
            "spaces",
            "search",
            "search_filters",
            "search_date_filters",
            "search_cursor_and_users",
            "tree_and_metadata",
            "page_reads_and_versions",
            "inline_comment_marker",
            "downloads_and_exports",
            "concurrent_reads",
            "page_and_draft_writes",
            "comment_loss_refusal",
            "move_and_order",
            "cross_space_move",
            "delete_and_restore",
        ):
            report.skip(name, "fixture prerequisite is unavailable")
        return report
    except Exception as exc:
        report.fail("fixture", type(exc).__name__, _elapsed_ms(started))
        for name in (
            "spaces",
            "search",
            "search_filters",
            "search_date_filters",
            "search_cursor_and_users",
            "tree_and_metadata",
            "page_reads_and_versions",
            "inline_comment_marker",
            "downloads_and_exports",
            "concurrent_reads",
            "page_and_draft_writes",
            "comment_loss_refusal",
            "move_and_order",
            "cross_space_move",
            "delete_and_restore",
        ):
            report.skip(name, "fixture lookup failed")
        return report

    await report.run("fixture", _pass())
    cross_space_key = (settings.get("acceptance") or {}).get("cross_space_key")
    await report.run(
        "spaces",
        lambda: _check_spaces(services, personal_space, cross_space_key),
    )
    await report.run("search", lambda: _check_search_modes(services, fixture, personal_space, page_ids.root))
    await report.run("search_filters", lambda: _check_search_filters(services, fixture, personal_space, page_ids))
    await report.run(
        "search_date_filters",
        lambda: _check_search_date_filters(
            services, fixture, personal_space, page_ids.root, settings.get("cql_timezone")
        ),
    )
    await report.run("search_cursor_and_users", lambda: _check_cursor_and_user(services, fixture, page_ids))
    await report.run("tree_and_metadata", lambda: _check_tree_and_metadata(services, fixture, page_ids))
    await report.run(
        "page_reads_and_versions",
        lambda: _check_page_reads(services, fixture, page_ids),
    )
    await report.run("inline_comment_marker", lambda: _check_inline_comment_marker(services, page_ids.root))
    await report.run("downloads_and_exports", lambda: _check_downloads_and_exports(services, fixture, page_ids))
    await report.run("concurrent_reads", lambda: _check_concurrent_reads(services, page_ids.root))
    if allow_writes:
        await report.run(
            "comment_loss_refusal",
            lambda: _check_inline_comment_loss_refusal(services, fixture, page_ids.root),
        )
        await report.run(
            "page_and_draft_writes",
            lambda: _check_page_and_draft_writes(services, fixture, page_ids.root, page_ids.personal_space),
        )
        await report.run(
            "move_and_order",
            lambda: _check_move_and_order(services, fixture, page_ids),
        )
        if cross_space_key is None:
            report.skip("cross_space_move", "acceptance.cross_space_key is not configured")
        else:
            await report.run(
                "cross_space_move",
                lambda: _check_cross_space_move(services, fixture, page_ids, str(cross_space_key)),
            )
        await report.run("delete_and_restore", lambda: _check_delete_and_restore(services, fixture, page_ids))
    else:
        for name in (
            "page_and_draft_writes",
            "comment_loss_refusal",
            "move_and_order",
            "cross_space_move",
            "delete_and_restore",
        ):
            report.skip(name, "rerun with --allow-writes; writes target disposable synthetic pages")
    return report


@dataclass(frozen=True)
class _FixturePages:
    """Resolved page IDs for one fixture run, retained only for this invocation."""

    personal_space: str
    root: str
    child_one: str
    child_two: str
    grandchild: str


async def _locate_fixture(
    services: Services, fixture: AcceptanceFixture, personal_space: str
) -> _FixturePages:
    """Resolve exact synthetic titles, waiting for indexing only while locating the fixture root."""
    root_id: str | None = None
    for attempt in range(INDEX_ATTEMPTS):
        root_id = await services.api.find_page_id_by_title(
            personal_space, fixture.root_title, expected_username=services.username
        )
        if root_id is not None:
            break
        if attempt + 1 < INDEX_ATTEMPTS:
            await asyncio.sleep(INDEX_WAIT_SECONDS)
    if root_id is None:
        raise SkipCheck("fixture title was not found after the indexing wait")
    child_one = await _find_page(services, personal_space, fixture.child_one_title)
    child_two = await _find_page(services, personal_space, fixture.child_two_title)
    grandchild = await _find_page(services, personal_space, fixture.grandchild_title)
    if child_one is None or child_two is None or grandchild is None:
        raise SkipCheck("fixture tree is incomplete; rerun setup_acceptance.py with this run ID")
    return _FixturePages(personal_space, root_id, child_one, child_two, grandchild)


async def _find_page(services: Services, space_key: str, title: str) -> str | None:
    return await services.api.find_page_id_by_title(space_key, title, expected_username=services.username)


async def _search_until_hit(
    services: Services,
    query: str,
    expected_id: str,
    *,
    failure: str,
    **options: Any,
) -> ContentSearchResult:
    """Retry a fixture search briefly to allow the instance index to catch up after setup."""
    for attempt in range(SEARCH_INDEX_ATTEMPTS):
        result = await services.search.search_content(query, **options)
        if expected_id in {hit.content_id for hit in result.hits}:
            return result
        if attempt + 1 < SEARCH_INDEX_ATTEMPTS:
            await asyncio.sleep(SEARCH_INDEX_WAIT_SECONDS)
    raise AssertionError(failure)


async def _check_spaces(
    services: Services, personal_space: str, cross_space_key: str | None
) -> None:
    spaces = await services.spaces.list_spaces()
    if not any(space.key == personal_space and space.type == "personal" for space in spaces.spaces):
        raise AssertionError("configured personal space was not returned")
    if cross_space_key is not None:
        if cross_space_key == personal_space:
            raise AssertionError("acceptance.cross_space_key must differ from personal_space_key")
        if not any(space.key == cross_space_key for space in spaces.spaces):
            raise AssertionError("configured cross-space target was not returned")


async def _check_search_modes(
    services: Services, fixture: AcceptanceFixture, personal_space: str, root_id: str
) -> None:
    scope = {"space_keys": [personal_space], "content_types": ["page"]}
    cases = (
        (f"{fixture.marker} violetquartz{fixture.run_id}", "all"),
        (f"{fixture.marker} missingterm{fixture.run_id}", "any"),
        (f"{fixture.marker} punctuated", "phrase"),
    )
    for query, match in cases:
        await _search_until_hit(
            services,
            query,
            root_id,
            failure=f"{match} search did not return the fixture root",
            scope=scope,
            match=match,
            limit=25,
        )
    await _search_until_hit(
        services,
        fixture.code_identifier,
        root_id,
        failure="code identifier search did not return the fixture root",
        scope={"space_keys": [personal_space]},
        limit=25,
    )
    await _search_until_hit(
        services,
        "café coração",
        root_id,
        failure="accented-word search did not return the fixture root",
        scope={"space_keys": [personal_space]},
        match="all",
        limit=25,
    )
    title = await _search_until_hit(
        services,
        f"Acceptance {fixture.run_id}",
        root_id,
        failure="title search did not return the fixture root",
        fields="title",
        limit=25,
    )
    if not any(query.source == "quicknav" for query in title.queries_sent):
        raise AssertionError("unscoped title search did not include quick navigation")


async def _check_search_filters(
    services: Services,
    fixture: AcceptanceFixture,
    personal_space: str,
    pages: _FixturePages,
) -> None:
    base_query = fixture.marker
    cases: tuple[tuple[str, dict[str, Any], str], ...] = (
        ("space", {"space_keys": [personal_space]}, pages.root),
        ("space_type", {"space_type": "personal"}, pages.root),
        ("content_type", {"content_types": ["page"]}, pages.root),
        ("label", {"labels": [fixture.label]}, pages.root),
        ("creator", {"creator_usernames": [services.username]}, pages.root),
        ("contributor", {"contributor_usernames": [services.username]}, pages.root),
        ("parent", {"parent": pages.root}, pages.child_one),
        ("ancestor", {"ancestor": pages.root}, pages.grandchild),
        ("archived_default", {"include_archived": True}, pages.root),
        ("archived_false", {"include_archived": False}, pages.root),
    )
    for name, scope, expected_id in cases:
        await _search_until_hit(
            services,
            base_query,
            expected_id,
            failure=f"{name} filter did not return its fixture page",
            scope={"space_keys": [personal_space], **scope},
            limit=100,
        )


async def _check_search_date_filters(
    services: Services,
    fixture: AcceptanceFixture,
    personal_space: str,
    root_id: str,
    cql_timezone: str | None,
) -> None:
    """Check date-only calendar scopes when the configured zone can identify the fixture's day."""
    if cql_timezone is None:
        raise SkipCheck("timestamp-derived date filters need configured cql_timezone")
    metadata = await services.api.get_page_metadata(root_id, expected_username=services.username)
    created_day = _local_day(metadata.created_at, cql_timezone)
    modified_day = _local_day(metadata.modified_at, cql_timezone)
    await _search_until_hit(
        services,
        fixture.marker,
        root_id,
        failure="date scope did not return the fixture root",
        scope={
            "space_keys": [personal_space],
            "created_after": created_day,
            "created_before": created_day,
            "modified_after": modified_day,
            "modified_before": modified_day,
        },
        limit=100,
    )


async def _check_cursor_and_user(
    services: Services, fixture: AcceptanceFixture, pages: _FixturePages
) -> None:
    first = await services.search.search_content(fixture.marker, limit=1)
    if first.next_cursor is None:
        raise AssertionError("fixture search did not produce a continuation cursor")
    second = await services.search.search_content(cursor=first.next_cursor)
    first_ids = {hit.content_id for hit in first.hits}
    if not second.hits or first_ids & {hit.content_id for hit in second.hits}:
        raise AssertionError("search cursor did not advance to a distinct fixture hit")
    exact = await services.users.find_users(username=services.username)
    if not exact.users or exact.users[0].username is None:
        raise AssertionError("exact current-user lookup returned no user")
    if not pages.root.isdecimal():
        raise AssertionError("fixture root ID was not numeric")


async def _check_tree_and_metadata(
    services: Services, fixture: AcceptanceFixture, pages: _FixturePages
) -> None:
    tree = await services.tree.get_page_tree(page=pages.root, max_depth=3)
    if [item.page_id for item in tree.children] != [pages.child_one, pages.child_two]:
        raise AssertionError("child order differs from the fixture's requested order")
    if [item.page_id for item in tree.children[0].children] != [pages.grandchild]:
        raise AssertionError("grandchild placement differs from the fixture")
    result = await services.metadata.get_page_metadata(pages.root)
    if result.failures or not result.pages:
        raise AssertionError("page metadata returned a failure")
    metadata = result.pages[0]
    if (metadata.version or 0) < 3 or len(metadata.versions) < 3:
        raise AssertionError("fixture root does not expose several versions")
    if fixture.label not in metadata.labels:
        raise AssertionError("fixture label was not returned in metadata")


async def _check_page_reads(
    services: Services,
    fixture: AcceptanceFixture,
    pages: _FixturePages,
) -> None:
    text_result = await services.pages.get_pages(pages.root, format="text", max_chars=40_000)
    if text_result.failures or not text_result.pages:
        raise AssertionError("text page read returned a failure")
    text_page = text_result.pages[0]
    if fixture.marker not in text_page.text or "## Attachments" not in text_page.text or "## Comments" not in text_page.text:
        raise AssertionError("text output omitted required body or section markers")
    kinds = {reference.kind for reference in text_page.references}
    if "image" not in kinds or "attachment" not in kinds:
        raise AssertionError("typed image or attachment references were not returned")
    if len(text_page.comments) < 2 or len(text_page.attachments) < 2:
        raise AssertionError("footer comment/reply or attachment records were incomplete")
    rich_result = await services.pages.get_pages(pages.root, format="rich", max_chars=40_000)
    if rich_result.failures or not rich_result.pages or "<ac:structured-macro" not in rich_result.pages[0].text:
        raise AssertionError("rich page read did not preserve storage markup")
    paged = await services.pages.get_pages(pages.root, format="text", max_chars=300)
    if paged.next_cursor is None or not paged.pages:
        raise AssertionError("small character budget did not return a continuation")
    current_version = paged.pages[0].version
    continuation_count = 0
    cursor = paged.next_cursor
    while cursor is not None and continuation_count < MAX_CONTINUATION_PAGES:
        continued = await services.pages.get_pages(cursor=cursor)
        if continued.failures or not continued.pages or continued.pages[0].version != current_version:
            raise AssertionError("page continuation lost its pinned version")
        cursor = continued.next_cursor
        continuation_count += 1
    if cursor is not None:
        raise AssertionError("page continuation exceeded the acceptance loop bound")
    packed = await services.pages.get_pages(
        [pages.root, pages.child_one], format="text", max_chars=300
    )
    if pages.child_one not in packed.remaining:
        raise AssertionError("a page that did not fit the character budget was not reported as remaining")
    metadata = await services.api.get_page_metadata(pages.root, expected_username=services.username)
    if metadata.url is None:
        raise SkipCheck("page URL is unavailable for version-link resolution")
    reference = metadata.url + ("&" if "?" in metadata.url else "?") + "pageVersion=1"
    historical = await services.pages.get_pages(reference, format="rich", max_chars=40_000)
    if historical.failures or not historical.pages or historical.pages[0].version != 1:
        raise AssertionError("versioned link did not resolve to the first page version")


async def _check_inline_comment_marker(services: Services, root_id: str) -> None:
    """Check typed inline-comment references when the operator completed the manual UI step."""
    result = await services.pages.get_pages(root_id, format="text", max_chars=40_000)
    if result.failures or not result.pages:
        raise AssertionError("inline-comment page read returned a failure")
    if not any(reference.kind == "inline_comment" for reference in result.pages[0].references):
        raise SkipCheck("add an inline comment to the fixture root in the UI to check its marker")


async def _check_inline_comment_loss_refusal(
    services: Services, fixture: AcceptanceFixture, root_id: str
) -> None:
    """Confirm a destructive body replacement is refused when the disposable fixture has an inline anchor."""
    content = await services.api.get_page_content(
        root_id, expected_username=services.username, include_storage=True
    )
    if "<ac:inline-comment-marker" not in content.storage_body:
        raise SkipCheck("add an inline comment to the fixture root in the UI to check loss refusal")
    if content.version is None:
        raise AssertionError("fixture root version was not returned for the loss-refusal check")
    try:
        await services.writes.update_page(
            root_id,
            f"<p>losscheck{fixture.run_id}</p>",
            expected_version=content.version,
            format="rich",
        )
    except InvalidRequest:
        return
    raise AssertionError("body replacement was not refused despite removing an inline-comment anchor")


async def _check_downloads_and_exports(
    services: Services, fixture: AcceptanceFixture, pages: _FixturePages
) -> None:
    attachments = await services.api.list_page_attachments(pages.root, expected_username=services.username)
    selected = [item.attachment_id for item in attachments if item.filename in {fixture.text_filename, fixture.image_filename}]
    if len(selected) != 2:
        raise AssertionError("fixture attachment list does not contain both synthetic files")
    download = await services.attachments.download_attachments(pages.root, selected)
    if len(download.files) != 2 or download.failures:
        raise AssertionError("selected attachment download did not complete")
    downloaded = {item.filename: Path(item.path).read_bytes() for item in download.files}
    if fixture.text_filename not in downloaded or fixture.attachment_marker.encode() not in downloaded[fixture.text_filename]:
        raise AssertionError("downloaded text attachment omitted its synthetic marker")
    if fixture.image_filename not in downloaded or not downloaded[fixture.image_filename].startswith(b"\x89PNG\r\n\x1a\n"):
        raise AssertionError("downloaded image attachment was not the synthetic PNG")
    single = await services.exports.export_pages(pages.root, format="text")
    if Path(single.path).suffix != ".txt" or not Path(single.path).is_file():
        raise AssertionError("single-page export did not produce a text file")
    archive = await services.exports.export_pages(
        [pages.root, pages.child_one], format="text", include_attachments=True
    )
    if Path(archive.path).suffix != ".zip" or not zipfile.is_zipfile(archive.path):
        raise AssertionError("attachment-inclusive multi-page export did not produce a ZIP")
    with zipfile.ZipFile(archive.path) as bundle:
        members = bundle.namelist()
    unsafe = [
        name
        for name in members
        if PurePosixPath(name).is_absolute() or {".", ".."} & set(PurePosixPath(name).parts)
    ]
    member_names = {PurePosixPath(name).name for name in members}
    if len(members) < 3 or unsafe or not {fixture.text_filename, fixture.image_filename} <= member_names:
        raise AssertionError("export archive layout was unsafe or omitted fixture attachments")


async def _check_concurrent_reads(services: Services, root_id: str) -> None:
    results = await asyncio.gather(
        *(services.metadata.get_page_metadata(root_id) for _ in range(6))
    )
    if any(result.failures or not result.pages for result in results):
        raise AssertionError("one or more concurrent read calls failed")


async def _check_page_and_draft_writes(
    services: Services, fixture: AcceptanceFixture, root_id: str, space_key: str
) -> None:
    title = f"Connector Lab Write Check [{fixture.run_id}]"
    text_created = await services.writes.create_page(
        title,
        f"writecheck{fixture.run_id} initial text",
        format="text",
        parent_id=root_id,
    )
    if text_created.status != "current" or text_created.version is None:
        raise AssertionError("text page creation did not return a current version")
    try:
        await services.writes.create_page(title, "duplicate", format="text", parent_id=root_id)
    except InvalidRequest:
        pass
    else:
        raise AssertionError("duplicate-title creation was not refused")
    rich = await services.writes.update_page(
        text_created.page_id,
        f"<p>writecheck{fixture.run_id} rich update</p>",
        expected_version=text_created.version,
        format="rich",
    )
    if rich.version is None or rich.version <= text_created.version:
        raise AssertionError("rich update did not advance the page version")
    try:
        await services.writes.update_page(
            text_created.page_id,
            "stale update",
            expected_version=text_created.version,
            format="text",
        )
    except Conflict:
        pass
    else:
        raise AssertionError("stale expected_version was not refused")
    try:
        await services.writes.create_page(
            f"Connector Lab Unsafe [{fixture.run_id}]",
            "<script>synthetic</script>",
            format="rich",
            parent_id=root_id,
        )
    except InvalidRequest:
        pass
    else:
        raise AssertionError("active content was not refused")
    try:
        await services.writes.update_page(
            text_created.page_id,
            f"<p>writecheck{fixture.run_id}</p>",
            expected_version=rich.version,
            title=fixture.root_title,
            format="rich",
        )
    except InvalidRequest:
        pass
    else:
        raise AssertionError("duplicate-title rename was not refused")
    added = await services.writes.set_labels(text_created.page_id, add=[fixture.label + "_write"])
    removed = await services.writes.set_labels(text_created.page_id, remove=[fixture.label + "_write"])
    if fixture.label + "_write" not in added.added or fixture.label + "_write" not in removed.removed:
        raise AssertionError("label add/remove did not report completed changes")
    draft = await services.writes.create_page(
        f"Connector Lab Publish Draft [{fixture.run_id}]",
        f"draftcheck{fixture.run_id}",
        format="text",
        status="draft",
        parent_id=root_id,
    )
    draft_list = await services.writes.list_drafts(space_key=space_key, limit=100)
    if draft.page_id not in {item.page_id for item in draft_list.drafts}:
        raise AssertionError("new page draft was not listed")
    published = await services.writes.publish_draft(draft.page_id)
    if published.page_id != draft.page_id or published.status != "current":
        raise AssertionError("draft publish did not retain its page ID")
    disposable_draft = await services.writes.create_page(
        f"Connector Lab Delete Draft [{fixture.run_id}]",
        f"draftdelete{fixture.run_id}",
        format="text",
        status="draft",
        parent_id=root_id,
    )
    deletion = await services.writes.delete_draft(disposable_draft.page_id)
    if not deletion.deleted or not deletion.permanent:
        raise AssertionError("new-page draft deletion was not reported as permanent")
    await services.structure.delete_page(text_created.page_id)
    await services.structure.delete_page(published.page_id)


async def _check_move_and_order(
    services: Services,
    fixture: AcceptanceFixture,
    pages: _FixturePages,
) -> None:
    move_title = f"Connector Lab Move Check [{fixture.run_id}]"
    created = await services.writes.create_page(
        move_title,
        f"movecheck{fixture.run_id}",
        format="text",
        parent_id=pages.root,
    )
    moved = await services.structure.move_page(
        created.page_id, parent_id=pages.child_one, position="last"
    )
    if not moved.moved or moved.parent_id != pages.child_one:
        raise AssertionError("same-space parent move did not complete")
    returned = await services.structure.move_page(
        created.page_id, parent_id=pages.root, position="first"
    )
    if not returned.moved or returned.parent_id != pages.root:
        raise AssertionError("same-space return move did not complete")
    await services.structure.delete_page(created.page_id)


async def _check_cross_space_move(
    services: Services,
    fixture: AcceptanceFixture,
    pages: _FixturePages,
    cross_space_key: str,
) -> None:
    """Move one disposable page out and back only when a distinct visible target is configured."""
    if cross_space_key == pages.personal_space:
        raise AssertionError("acceptance.cross_space_key must differ from personal_space_key")
    created = await services.writes.create_page(
        f"Connector Lab Cross-Space Check [{fixture.run_id}]",
        f"crossspacecheck{fixture.run_id}",
        format="text",
        parent_id=pages.root,
    )
    cross = await services.structure.move_page(
        created.page_id, space_key=cross_space_key, position="last"
    )
    if not cross.moved or cross.space_key != cross_space_key:
        raise AssertionError("cross-space long task did not confirm its target")
    back = await services.structure.move_page(created.page_id, space_key=pages.personal_space)
    if not back.moved or back.space_key != pages.personal_space:
        raise AssertionError("cross-space return move did not confirm its target")
    await services.structure.delete_page(created.page_id)


async def _check_delete_and_restore(
    services: Services, fixture: AcceptanceFixture, pages: _FixturePages
) -> None:
    try:
        await services.structure.delete_page(pages.root, children="refuse")
    except InvalidRequest:
        pass
    else:
        raise AssertionError("children=refuse did not prevent deleting a page with children")

    move_parent = await services.writes.create_page(
        f"Connector Lab Move-Up Delete [{fixture.run_id}]",
        f"deletecheck{fixture.run_id}",
        format="text",
        parent_id=pages.root,
    )
    move_child = await services.writes.create_page(
        f"Connector Lab Move-Up Child [{fixture.run_id}]",
        f"deletechild{fixture.run_id}",
        format="text",
        parent_id=move_parent.page_id,
    )
    move_result = await services.structure.delete_page(move_parent.page_id, children="move_up")
    if not move_result.complete or not any(item.page_id == move_child.page_id and item.status == "moved_up" for item in move_result.pages):
        raise AssertionError("move_up delete did not preserve its child")
    restored = await services.structure.restore_page(move_parent.page_id)
    if restored.placement_source != "saved_property" or restored.placement_status != "applied":
        raise AssertionError("saved placement was not applied during restore")
    await services.structure.delete_page(move_child.page_id)

    trash_parent = await services.writes.create_page(
        f"Connector Lab Subtree Delete [{fixture.run_id}]",
        f"subtreecheck{fixture.run_id}",
        format="text",
        parent_id=pages.root,
    )
    trash_child = await services.writes.create_page(
        f"Connector Lab Subtree Child [{fixture.run_id}]",
        f"subtreechild{fixture.run_id}",
        format="text",
        parent_id=trash_parent.page_id,
    )
    trash_grandchild = await services.writes.create_page(
        f"Connector Lab Subtree Grandchild [{fixture.run_id}]",
        f"subtreegrandchild{fixture.run_id}",
        format="text",
        parent_id=trash_child.page_id,
    )
    subtree = await services.structure.delete_page(trash_parent.page_id, children="trash")
    if not subtree.complete or sum(item.status == "trashed" for item in subtree.pages) != 3:
        raise AssertionError("bottom-up subtree deletion did not trash all three pages")
    listed = await services.structure.list_trash(space_key=pages.personal_space, limit=100)
    if not {trash_parent.page_id, trash_child.page_id, trash_grandchild.page_id} <= {
        item.page_id for item in listed.pages
    }:
        raise AssertionError("fixture subtree was missing from the trash listing")
    for page_id in (trash_parent.page_id, trash_child.page_id, trash_grandchild.page_id):
        await services.structure.restore_page(page_id)

    caller_page = await services.writes.create_page(
        f"Connector Lab Caller Restore [{fixture.run_id}]",
        f"callerrestore{fixture.run_id}",
        format="text",
        parent_id=pages.root,
    )
    await services.structure.delete_page(caller_page.page_id)
    caller_restore = await services.structure.restore_page(
        caller_page.page_id, parent_id=pages.root, position="last"
    )
    if caller_restore.placement_source != "caller" or caller_restore.placement_status != "applied":
        raise AssertionError("caller placement was not applied during restore")
    await services.structure.delete_page(caller_page.page_id)

    root_fallback = await services.writes.create_page(
        f"Connector Lab Root Restore [{fixture.run_id}]",
        f"rootrestore{fixture.run_id}",
        format="text",
        parent_id=pages.root,
    )
    await services.api.trash_page(root_fallback.page_id, expected_username=services.username)
    fallback = await services.structure.restore_page(root_fallback.page_id)
    if fallback.placement_source != "space_root" or fallback.placement_status != "root":
        raise AssertionError("missing placement property did not fall back to space root")
    await services.structure.delete_page(root_fallback.page_id)


def _local_day(value: str | None, timezone_name: str) -> str:
    """Convert a REST UTC timestamp to its configured CQL calendar day."""
    if not value:
        raise SkipCheck("fixture metadata has no timestamp for date-scope checks")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(parse_cql_timezone(timezone_name)).date().isoformat()


async def _pass() -> None:
    return None


def _elapsed_ms(started: float) -> int:
    return max(0, round((time.perf_counter() - started) * 1_000))


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (KeyboardInterrupt, EOFError):
        print("\nCancelled. Inspect any in-progress synthetic write before retrying.")
        raise SystemExit(130)
