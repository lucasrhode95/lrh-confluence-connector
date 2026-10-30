"""Synthetic failure paths for disposable acceptance writes; no configured instance is used."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "research"))

import run_acceptance
from acceptance_fixtures import new_fixture
from confluence_connector.domain.errors import AccountMismatch, NotFound, UncertainWrite, Upstream
from confluence_connector.domain.models import PageMetadata, PageWriteResult


class ScratchCleanupChecks(unittest.IsolatedAsyncioTestCase):
    def services(self):
        return SimpleNamespace(
            username="sample-user",
            api=SimpleNamespace(get_page_metadata=AsyncMock(return_value=PageMetadata(
                page_id="42", title="Scratch", content_type="page", status="current", space_key="HOME", version=1,
            ))),
            writes=SimpleNamespace(
                create_page=AsyncMock(return_value=PageWriteResult(page_id="42", title="Scratch", status="current", version=1)),
                delete_draft=AsyncMock(return_value=SimpleNamespace(deleted=True)),
            ),
            structure=SimpleNamespace(
                move_page=AsyncMock(),
                delete_page=AsyncMock(return_value=SimpleNamespace(complete=True, pages=[])),
            ),
        )

    async def test_cross_space_verification_failure_returns_home_and_trashes_immediately(self):
        services = self.services()
        home = services.api.get_page_metadata.return_value
        services.api.get_page_metadata.side_effect = [home.model_copy(update={"space_key": "FOREIGN"}), home]
        services.structure.move_page.side_effect = [
            SimpleNamespace(moved=True, space_key="FOREIGN", position_applied=True),
            SimpleNamespace(moved=True, space_key="HOME", position_applied=True),
        ]
        pages = SimpleNamespace(root="10", personal_space="HOME")
        with patch.object(run_acceptance, "_assert_placement", side_effect=Upstream("synthetic verification failure")):
            with self.assertRaises(Upstream):
                await run_acceptance._check_cross_space_move(services, new_fixture("deadbeef"), pages, "FOREIGN")
        self.assertEqual(services.structure.move_page.await_count, 2)
        self.assertEqual(services.structure.move_page.await_args.kwargs, {"parent_id": "10"})
        services.structure.delete_page.assert_awaited_once_with("42")

    async def test_cross_space_parent_outside_the_cross_space_fails_before_any_write(self):
        pages = SimpleNamespace(root="10", personal_space="HOME")
        for update in ({"space_key": "ELSEWHERE"}, {"status": "trashed"}, {"content_type": "blogpost"}):
            with self.subTest(update=update):
                services = self.services()
                parent = services.api.get_page_metadata.return_value.model_copy(
                    update={"page_id": "77", "space_key": "FOREIGN", **update}
                )
                services.api.get_page_metadata.return_value = parent
                with self.assertRaisesRegex(AssertionError, "cross_space_parent_id"):
                    await run_acceptance._check_cross_space_move(
                        services, new_fixture("deadbeef"), pages, "FOREIGN", "77"
                    )
                services.api.get_page_metadata.assert_awaited_once_with("77", expected_username="sample-user")
                services.writes.create_page.assert_not_awaited()
                services.structure.move_page.assert_not_awaited()

    async def test_cross_space_parent_in_the_cross_space_receives_the_move(self):
        services = self.services()
        home = services.api.get_page_metadata.return_value
        parent = home.model_copy(update={"page_id": "77", "space_key": "FOREIGN"})
        services.api.get_page_metadata.return_value = parent
        services.structure.move_page.side_effect = [
            SimpleNamespace(moved=True, space_key="FOREIGN", position_applied=True),
            SimpleNamespace(moved=True, space_key="HOME", position_applied=True),
        ]
        pages = SimpleNamespace(root="10", personal_space="HOME")
        with patch.object(run_acceptance, "_assert_placement", AsyncMock()), patch.object(
            run_acceptance._ScratchPages, "__aexit__", AsyncMock(return_value=False)
        ):
            await run_acceptance._check_cross_space_move(services, new_fixture("deadbeef"), pages, "FOREIGN", "77")
        first_move = services.structure.move_page.await_args_list[0]
        self.assertEqual(first_move.kwargs, {"parent_id": "77", "space_key": None, "position": "last"})

    async def test_failed_draft_check_deletes_confirmed_draft(self):
        services = self.services()
        services.api.get_page_metadata.side_effect = [
            NotFound("not current"), PageMetadata(page_id="42", title="Draft", content_type="page", status="draft"),
        ]
        with self.assertRaisesRegex(AssertionError, "synthetic failure"):
            async with run_acceptance._ScratchPages(services, "HOME", "10") as scratch:
                await scratch.create("Scratch", "body")
                raise AssertionError("synthetic failure")
        services.writes.delete_draft.assert_awaited_once_with("42")
        services.structure.delete_page.assert_not_awaited()

    async def test_uncertain_write_or_account_change_never_starts_compensating_writes(self):
        for error in (UncertainWrite("pending"), AccountMismatch("changed")):
            with self.subTest(error=type(error).__name__):
                services = self.services()
                with self.assertRaises(run_acceptance.ScratchCleanupRequired):
                    async with run_acceptance._ScratchPages(services, "HOME", "10") as scratch:
                        await scratch.create("Scratch", "body")
                        raise error
                services.api.get_page_metadata.assert_not_awaited()
                services.structure.delete_page.assert_not_awaited()
                services.structure.move_page.assert_not_awaited()

    async def test_cleanup_failure_is_reported_and_other_known_pages_are_still_cleaned(self):
        services = self.services()
        services.writes.create_page.side_effect = [
            PageWriteResult(page_id="41", title="One", status="current"),
            PageWriteResult(page_id="42", title="Two", status="current"),
        ]
        services.structure.delete_page.side_effect = [Upstream("refused"), SimpleNamespace(complete=True, pages=[])]
        with self.assertRaises(run_acceptance.ScratchCleanupRequired):
            async with run_acceptance._ScratchPages(services, "HOME", "10") as scratch:
                await scratch.create("One", "body")
                await scratch.create("Two", "body")
        self.assertEqual([call.args[0] for call in services.structure.delete_page.await_args_list], ["42", "41"])

    async def test_move_acceptance_detects_an_unwanted_content_version(self):
        services = self.services()
        services.api.get_page_metadata.return_value.version = 2
        with self.assertRaisesRegex(AssertionError, "created a content version"):
            await run_acceptance._assert_placement(services, "42", "HOME", None, "last", expected_version=1)
