"""Offline tests for user lookup, ordered trees and batched metadata services."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from confluence_connector.domain.errors import InvalidRequest, NotFound
from confluence_connector.domain.models import (
    PageMetadata,
    PageSummary,
    PageVersion,
    PersonSummary,
    RemoteContentSearchPage,
    RemoteUserSearchPage,
)
from confluence_connector.service.metadata import PageMetadataService
from confluence_connector.service.tree import PageTree
from confluence_connector.service.users import UserSearch


class UserSearchChecks(unittest.IsolatedAsyncioTestCase):
    async def test_display_name_search_pages_by_actual_size(self) -> None:
        api = _ReadApi(users=[PersonSummary(username="sample-user-a"), PersonSummary(username="sample-user-b")])
        service = UserSearch(api, "sample-user")  # type: ignore[arg-type]
        first = await service.find_users("  Sample   User ", limit=1)
        self.assertEqual(first.query, "Sample User")
        self.assertEqual(first.next_cursor is not None, True)
        second = await service.find_users(cursor=first.next_cursor)
        self.assertEqual(second.start, 1)
        self.assertEqual([user.username for user in second.users], ["sample-user-b"])
        self.assertEqual([call[1] for call in api.user_calls], [0, 1])

    async def test_exact_username_path_supports_former_user(self) -> None:
        api = _ReadApi(exact_user=PersonSummary(username="former-user", user_key="former-user"))
        result = await UserSearch(api, "sample-user").find_users(username="former-user")  # type: ignore[arg-type]
        self.assertEqual(result.users[0].username, "former-user")
        self.assertEqual(api.exact_user_calls, ["former-user"])
        self.assertEqual(api.user_calls, [])

    async def test_cursor_and_search_modes_are_exclusive(self) -> None:
        service = UserSearch(_ReadApi(), "sample-user")  # type: ignore[arg-type]
        with self.assertRaisesRegex(InvalidRequest, "Pass only the cursor"):
            await service.find_users("other", cursor="eyJraW5kIjoiZmluZF91c2VycyJ9")
        with self.assertRaisesRegex(InvalidRequest, "username alone"):
            await service.find_users("other", username="sample-user")


class PageTreeChecks(unittest.IsolatedAsyncioTestCase):
    async def test_page_tree_keeps_source_order_and_ancestors(self) -> None:
        api = _ReadApi(
            children={
                "1": [_hit("2", "First"), _hit("3", "Second")],
                "2": [_hit("4", "Nested")],
            },
            metadata={
                "1": PageMetadata(
                    page_id="1",
                    title="Root",
                    content_type="page",
                    space_key="SYNTH",
                    ancestors=[PageSummary(page_id="0", title="Ancestor")],
                )
            },
        )
        result = await PageTree(api, _Links("1"), "sample-user").get_page_tree(page="1", max_depth=3)  # type: ignore[arg-type]
        self.assertEqual(result.root.title, "Root")
        self.assertEqual([node.title for node in result.children], ["First", "Second"])
        self.assertEqual([node.title for node in result.children[0].children], ["Nested"])
        self.assertEqual(result.ancestors[0].title, "Ancestor")
        self.assertEqual(result.total_pages, 4)
        self.assertTrue(result.complete)

    async def test_space_root_depth_and_page_cap_are_reported(self) -> None:
        api = _ReadApi(space_roots={"SYNTH": [_hit("1", "Top"), _hit("2", "Next"), _hit("3", "Third")]})
        with patch("confluence_connector.service.tree.MAX_TREE_PAGES", 2):
            result = await PageTree(api, _Links("1"), "sample-user").get_page_tree(
                space_key="SYNTH", max_depth=1
            )  # type: ignore[arg-type]
        self.assertEqual([node.page_id for node in result.children], ["1", "2"])
        self.assertFalse(result.complete)
        self.assertIn("safety cap", result.notes[-1])
        self.assertEqual(result.total_pages, 2)

    async def test_invalid_tree_roots_fail_before_api_calls(self) -> None:
        api = _ReadApi()
        service = PageTree(api, _Links("1"), "sample-user")  # type: ignore[arg-type]
        for kwargs in ({}, {"page": "1", "space_key": "SYNTH"}, {"space_key": "BAD/KEY"}, {"page": "1", "max_depth": 6}):
            with self.subTest(kwargs=kwargs), self.assertRaises(InvalidRequest):
                await service.get_page_tree(**kwargs)  # type: ignore[arg-type]
        self.assertEqual(api.child_calls, [])
        self.assertEqual(api.space_root_calls, [])


class PageMetadataChecks(unittest.IsolatedAsyncioTestCase):
    async def test_metadata_is_ordered_with_per_reference_failures(self) -> None:
        api = _ReadApi(
            metadata={"1": PageMetadata(page_id="1", title="One", content_type="page")},
            failures={"2": NotFound("Synthetic page not found")},
            versions={"1": [PageVersion(number=1), PageVersion(number=2)]},
        )
        result = await PageMetadataService(api, _Links(), "sample-user").get_page_metadata(
            ["1", "2"]
        )  # type: ignore[arg-type]
        self.assertEqual([page.page_id for page in result.pages], ["1"])
        self.assertEqual([version.number for version in result.pages[0].versions], [1, 2])
        self.assertEqual(result.failures[0].reference, "2")
        self.assertIn("NotFound", result.failures[0].error)

    async def test_metadata_rejects_versioned_page_references(self) -> None:
        class VersionedLinks:
            async def resolve(self, reference: str, *, expected_username: str):
                return type("Resolved", (), {"page_id": "1", "version": 2})()

        api = _ReadApi()
        result = await PageMetadataService(api, VersionedLinks(), "sample-user").get_page_metadata("1")  # type: ignore[arg-type]
        self.assertEqual(result.pages, [])
        self.assertIn("omit pageVersion", result.failures[0].error)
        self.assertEqual(api.metadata_calls, [])

    async def test_metadata_rejects_too_many_references_before_network(self) -> None:
        api = _ReadApi()
        service = PageMetadataService(api, _Links(), "sample-user")  # type: ignore[arg-type]
        with self.assertRaisesRegex(InvalidRequest, "1 to 20"):
            await service.get_page_metadata([str(number) for number in range(21)])
        self.assertEqual(api.metadata_calls, [])


class _Links:
    def __init__(self, page_id: str = "1") -> None:
        self.page_id = page_id

    async def resolve(self, reference: str, *, expected_username: str):
        if reference == "bad":
            raise InvalidRequest("Synthetic invalid page reference")
        return type("Resolved", (), {"page_id": reference, "version": None})()


class _ReadApi:
    def __init__(
        self,
        *,
        users: list[PersonSummary] | None = None,
        exact_user: PersonSummary | None = None,
        children: dict[str, list] | None = None,
        space_roots: dict[str, list] | None = None,
        metadata: dict[str, PageMetadata] | None = None,
        versions: dict[str, list[PageVersion]] | None = None,
        failures: dict[str, Exception] | None = None,
    ) -> None:
        self.users = users or []
        self.exact_user = exact_user
        self.children = children or {}
        self.space_roots = space_roots or {}
        self.metadata = metadata or {}
        self.versions = versions or {}
        self.failures = failures or {}
        self.user_calls: list[tuple[str, int, int]] = []
        self.exact_user_calls: list[str] = []
        self.child_calls: list[tuple[str, int]] = []
        self.space_root_calls: list[tuple[str, int]] = []
        self.metadata_calls: list[str] = []

    async def search_users_page(self, name: str, *, start: int, limit: int, expected_username: str):
        self.user_calls.append((name, start, limit))
        page_users = self.users[start : start + limit]
        return RemoteUserSearchPage(users=page_users, start=start, size=len(page_users), total_size=len(self.users))

    async def get_user(self, username: str, *, expected_username: str):
        self.exact_user_calls.append(username)
        if self.exact_user is None:
            raise NotFound("Synthetic user not found")
        return self.exact_user

    async def get_page_metadata(self, page_id: str, *, expected_username: str):
        self.metadata_calls.append(page_id)
        if page_id in self.failures:
            raise self.failures[page_id]
        return self.metadata[page_id].model_copy(deep=True)

    async def list_page_versions(self, page_id: str, *, expected_username: str):
        return self.versions.get(page_id, []), False

    async def list_child_pages(self, page_id: str, *, start: int, expected_username: str):
        self.child_calls.append((page_id, start))
        items = self.children.get(page_id, [])
        page_hits = items[start : start + 500]
        return RemoteContentSearchPage(
            hits=page_hits,
            start=start,
            size=len(page_hits),
            total_size=len(items),
        )

    async def list_space_root_pages(self, space_key: str, *, start: int, expected_username: str):
        self.space_root_calls.append((space_key, start))
        items = self.space_roots.get(space_key, [])
        page_hits = items[start : start + 500]
        return RemoteContentSearchPage(
            hits=page_hits,
            start=start,
            size=len(page_hits),
            total_size=len(items),
        )


def _hit(page_id: str, title: str):
    from confluence_connector.domain.models import ContentSearchHit

    return ContentSearchHit(content_id=page_id, content_type="page", title=title)


if __name__ == "__main__":
    unittest.main()
