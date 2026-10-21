"""Account-bound page moves, trash listing, deletion and restoration."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Literal

from confluence_connector.domain.errors import (
    AccountMismatch,
    AuthenticationRequired,
    ConnectorError,
    InvalidRequest,
    NotFound,
    PermissionDenied,
    UncertainWrite,
    Upstream,
)
from confluence_connector.domain.models import (
    ContentSearchHit,
    PageDeleteResult,
    PageMetadata,
    PageMoveResult,
    PagePlacement,
    PageRestoreResult,
    PageStructureOutcome,
    RemoteTrashPage,
    ResolvedPageReference,
    TrashListResult,
)
from confluence_connector.domain.validation import (
    optional_space_key,
    page_reference,
    require_current_page,
    placement_inputs,
)
from confluence_connector.remote.confluence import (
    CHILD_PAGE_SIZE,
    PLACEMENT_PROPERTY_KEY,
    TRASH_PAGE_SIZE,
    ConfluenceApi,
)
from confluence_connector.remote.links import PageLinkResolver
from confluence_connector.service import cursors
from confluence_connector.service.placement import PagePlacementResolver

log = logging.getLogger(__name__)

DEFAULT_TRASH_LIMIT = 50
MAX_TRASH_OFFSET = 1_000_000
MAX_TRASH_LIMIT = TRASH_PAGE_SIZE
MAX_DELETE_SUBTREE_PAGES = 100
MAX_HIERARCHY_READ_PAGES = 5_000
_TRASH_CURSOR_KIND = "list_trash"


class PageStructure:
    """Resolve public page references, then coordinate bounded Confluence structure operations."""

    def __init__(self, api: ConfluenceApi, links: PageLinkResolver, username: str) -> None:
        self._api = api
        self._links = links
        self._username = username
        self._placement_resolver = PagePlacementResolver(api, username)

    async def move_page(
        self,
        page: str,
        *,
        parent_id: str | None = None,
        space_key: str | None = None,
        position: str | None = None,
        sibling: str | None = None,
    ) -> PageMoveResult:
        """Move a current page to one parent or space root, then apply its requested sibling position.

        This public boundary validates all references and placement options before writes. Internal
        routines receive numeric current-page IDs, a current page metadata record and a resolved
        target placement.
        """
        reference = page_reference(page)
        selected_position, parent_reference, selected_space, sibling_reference = placement_inputs(
            parent_id=parent_id,
            space_key=space_key,
            position=position,
            sibling=sibling,
        )
        source = await self._resolve_page(reference)
        metadata = await self._api.get_page_metadata(
            source.page_id, expected_username=self._username
        )
        require_current_page(metadata, allow_missing_status=True)
        target = await self._resolve_placement(
            parent_reference=parent_reference,
            space_key=selected_space,
            position=selected_position,
            sibling_reference=sibling_reference,
            moving_page_id=metadata.page_id,
        )
        return await self._move_resolved(metadata, target)

    async def delete_page(
        self, page: str, *, children: Literal["refuse", "move_up", "trash"] = "refuse"
    ) -> PageDeleteResult:
        """Preflight permissions, then trash one page with the requested direct-child policy.

        The service performs every read-only tree and delete-operation check before its first write.
        After writes begin, it stops at the first error and returns confirmed, unknown and unattempted
        pages separately; it never compensates a completed move or trash operation.
        """
        reference = page_reference(page)
        mode = _children_mode(children)
        resolved = await self._resolve_page(reference)
        metadata = await self._api.get_page_metadata(
            resolved.page_id, expected_username=self._username
        )
        require_current_page(metadata, allow_missing_status=True)
        if metadata.space_key is None:
            raise Upstream("Confluence did not return the page's space.")

        direct_children = await self._read_children(metadata.page_id, metadata.space_key)
        if mode == "refuse" and direct_children:
            summary = ", ".join(f"{row.title} (ID {row.content_id})" for row in direct_children)
            raise InvalidRequest(f"The page has child pages; choose children=move_up or children=trash: {summary}")

        nodes = await self._subtree(metadata) if mode == "trash" else [
            await self._node_for_metadata(metadata)
        ]
        if mode == "move_up":
            nodes.extend(
                self._nodes_for_children(direct_children, metadata.page_id, metadata.space_key)
            )
        if mode == "trash" and len(nodes) > MAX_DELETE_SUBTREE_PAGES:
            raise InvalidRequest(
                f"The subtree contains {len(nodes)} pages, exceeding the {MAX_DELETE_SUBTREE_PAGES}-page trash limit."
            )

        await self._preflight_delete(nodes)
        warnings = await self._restore_warning(metadata.space_key)
        outcomes = {node.page_id: _outcome(node, "not_attempted") for node in nodes}
        try:
            saved_count = await self._save_placements(nodes if mode == "trash" else nodes[:1])
        except (AuthenticationRequired, AccountMismatch) as exc:
            warnings.append(
                "Account state changed while saving placement records; some records may have been written, and no page was moved or trashed."
            )
            return _delete_result(metadata, mode, outcomes, warnings, _error_text(exc))
        if saved_count:
            warnings.append(
                f"Could not save placement for {saved_count} page(s); restoring them may leave them at the space root."
            )

        if mode == "move_up":
            new_parent_id = metadata.ancestors[-1].page_id if metadata.ancestors else None
            for index, node in enumerate(nodes[1:]):
                try:
                    child_metadata = await self._api.get_page_metadata(
                        node.page_id, expected_username=self._username
                    )
                    require_current_page(child_metadata, allow_missing_status=True)
                    current_parent = child_metadata.ancestors[-1].page_id if child_metadata.ancestors else None
                    if current_parent != node.parent_id or child_metadata.space_key != node.space_key:
                        outcomes[node.page_id] = _outcome(node, "skipped", "Parent or space changed after preflight.")
                        continue
                    if child_metadata.version is None:
                        raise Upstream("Confluence did not return a child page version for the move.")
                    await self._change_parent(child_metadata, new_parent_id)
                    outcomes[node.page_id] = _outcome(node, "moved_up")
                except ConnectorError as exc:
                    status = "unknown" if isinstance(exc, UncertainWrite) else "failed"
                    outcomes[node.page_id] = _outcome(node, status, _error_text(exc))
                    for pending in nodes[index + 2 :]:
                        outcomes[pending.page_id] = _outcome(
                            pending, "not_attempted", "Not attempted after an earlier failure."
                        )
                    outcomes[metadata.page_id] = _outcome(
                        nodes[0], "not_attempted", "Not trashed because a child move failed."
                    )
                    return _delete_result(metadata, mode, outcomes, warnings, _error_text(exc))

        delete_order = list(reversed(nodes)) if mode == "trash" else nodes[:1]
        for index, node in enumerate(delete_order):
            try:
                await self._api.trash_page(node.page_id, expected_username=self._username)
                outcomes[node.page_id] = _outcome(node, "trashed")
            except ConnectorError as exc:
                status = "unknown" if isinstance(exc, UncertainWrite) else "failed"
                outcomes[node.page_id] = _outcome(node, status, _error_text(exc))
                for pending in delete_order[index + 1 :]:
                    outcomes[pending.page_id] = _outcome(
                        pending, "not_attempted", "Not attempted after an earlier failure."
                    )
                return _delete_result(metadata, mode, outcomes, warnings, _error_text(exc))
        return _delete_result(metadata, mode, outcomes, warnings, None)

    async def list_trash(
        self,
        *,
        space_key: str | None = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> TrashListResult:
        """List one bounded page of visible trashed pages; cursors retain the space and page size."""
        if cursor is not None:
            if space_key is not None or limit is not None:
                raise InvalidRequest("Pass only cursor; it already contains the trash scope and limit.")
            selected_space, start, page_limit = _decode_trash_cursor(cursor)
        else:
            selected_space = optional_space_key(space_key)
            start = 0
            page_limit = _trash_limit(limit)
        page: RemoteTrashPage = await self._api.list_trash_page(
            space_key=selected_space,
            start=start,
            limit=page_limit,
            expected_username=self._username,
        )
        next_start = page.next_start(page_limit)
        server_has_more = next_start is not None
        more = server_has_more and next_start <= MAX_TRASH_OFFSET
        return TrashListResult(
            pages=page.pages,
            start=page.start,
            size=page.size,
            approximate_total=page.total_size,
            more_results_available=more,
            truncated=page.size > 0 and server_has_more and not more,
            next_cursor=(
                cursors.encode(
                    _TRASH_CURSOR_KIND,
                    space_key=selected_space,
                    start=next_start,
                    limit=page_limit,
                )
                if more
                else None
            ),
        )

    async def restore_page(
        self,
        page: str,
        *,
        parent_id: str | None = None,
        space_key: str | None = None,
        position: str | None = None,
        sibling: str | None = None,
    ) -> PageRestoreResult:
        """Restore one trashed page, then apply caller placement, saved placement or the space root."""
        reference = page_reference(page)
        has_caller_placement = any(
            value is not None for value in (parent_id, space_key, position, sibling)
        )
        if has_caller_placement:
            selected_position, parent_reference, selected_space, sibling_reference = placement_inputs(
                parent_id=parent_id,
                space_key=space_key,
                position=position,
                sibling=sibling,
            )
        else:
            selected_position, parent_reference, selected_space, sibling_reference = (
                "last",
                None,
                None,
                None,
            )

        resolved = await self._resolve_page(reference, status="trashed")
        metadata = await self._api.get_page_metadata(
            resolved.page_id, status="trashed", expected_username=self._username
        )
        if metadata.content_type != "page" or metadata.status != "trashed":
            raise InvalidRequest("restore_page accepts trashed pages only.")
        if metadata.space_key is None or metadata.version is None:
            raise Upstream("Confluence did not return the trashed page's space and version.")
        await self._api.restore_trashed_page(
            metadata.page_id,
            title=metadata.title,
            space_key=metadata.space_key,
            next_version=metadata.version + 1,
            expected_username=self._username,
        )

        warnings: list[str] = []
        chosen: PagePlacement | None = None
        placement_source: Literal["caller", "saved_property", "space_root"] = "space_root"
        skip_property_cleanup = False
        if has_caller_placement:
            try:
                chosen = await self._resolve_placement(
                    parent_reference=parent_reference,
                    space_key=selected_space,
                    position=selected_position,
                    sibling_reference=sibling_reference,
                    moving_page_id=metadata.page_id,
                )
                placement_source = "caller"
            except (AuthenticationRequired, AccountMismatch) as exc:
                skip_property_cleanup = True
                warnings.append(
                    f"The page was restored at the space root, but account state changed before placement could be checked ({type(exc).__name__}); no further writes were attempted."
                )
            except (ConnectorError, ValueError) as exc:
                warnings.append(
                    f"The requested placement could not be resolved; the restored page remains at the space root ({type(exc).__name__})."
                )
        else:
            try:
                saved, property_warning = await self._saved_placement(metadata)
                if property_warning:
                    warnings.append(property_warning)
            except (AuthenticationRequired, AccountMismatch) as exc:
                saved = None
                skip_property_cleanup = True
                warnings.append(
                    f"The page was restored at the space root, but account state changed before saved placement could be read ({type(exc).__name__}); no further writes were attempted."
                )
            if saved is not None:
                try:
                    chosen = await self._resolve_placement(
                        parent_reference=saved.parent_id,
                        space_key=None if saved.parent_id is not None else saved.space_key,
                        position=saved.position,
                        sibling_reference=saved.sibling_id,
                        moving_page_id=metadata.page_id,
                    )
                except ConnectorError as exc:
                    if isinstance(exc, (AuthenticationRequired, AccountMismatch)):
                        skip_property_cleanup = True
                        warnings.append(
                            f"The page was restored at the space root, but account state changed before saved placement could be checked ({type(exc).__name__}); no further writes were attempted."
                        )
                    else:
                        log.info("Saved page placement could not be applied (%s)", type(exc).__name__)
                        warnings.append(
                            f"The saved placement could not be applied; the page remains at the space root ({type(exc).__name__})."
                        )
                    chosen = None
                else:
                    if chosen.space_key != saved.space_key:
                        chosen = None
                        warnings.append("The saved placement's space did not match the restored page.")
                    else:
                        placement_source = "saved_property"

        placement_status: Literal["applied", "root", "unknown"] = "root"
        actual_parent_id: str | None = None
        actual_space_key: str | None = metadata.space_key
        if chosen is not None:
            try:
                current = await self._api.get_page_metadata(
                    metadata.page_id, expected_username=self._username
                )
                require_current_page(current, allow_missing_status=True)
                move_result = await self._move_resolved(current, chosen)
            except UncertainWrite as exc:
                placement_status = "unknown"
                skip_property_cleanup = True
                if chosen.space_key != metadata.space_key:
                    actual_space_key = None
                warnings.append(
                    f"The restore completed, but Confluence may still be applying the placement; inspect the page ({type(exc).__name__})."
                )
                actual_parent_id = None
            except (AuthenticationRequired, AccountMismatch) as exc:
                skip_property_cleanup = True
                if chosen.space_key != metadata.space_key:
                    actual_space_key = None
                warnings.append(
                    f"The page was restored, but account state changed before placement could be confirmed ({type(exc).__name__}); no further writes were attempted."
                )
                placement_status = "unknown"
                actual_parent_id = None
            except ConnectorError as exc:
                warnings.append(
                    f"The requested placement was refused; the restored page remains at the space root ({type(exc).__name__})."
                )
            else:
                actual_parent_id = move_result.parent_id
                if move_result.position_applied is False:
                    warnings.extend(move_result.warnings)
                    placement_status = "root"
                elif move_result.position_applied is None:
                    placement_status = "unknown"
                    skip_property_cleanup = True
                    if chosen.space_key != metadata.space_key:
                        actual_space_key = None
                    warnings.extend(move_result.warnings)
                else:
                    placement_status = "applied"
                    actual_space_key = move_result.space_key

        if not skip_property_cleanup:
            try:
                await self._api.remove_page_property(
                    metadata.page_id, PLACEMENT_PROPERTY_KEY, expected_username=self._username
                )
            except (AuthenticationRequired, AccountMismatch):
                warnings.append(
                    "The page was restored, but its saved placement record could not be removed after an account change."
                )
            except NotFound:
                pass
            except ConnectorError as exc:
                warnings.append(
                    f"The restored page's saved placement record could not be removed ({type(exc).__name__})."
                )

        if placement_status == "root" and placement_source != "space_root":
            placement_source = "space_root"
        return PageRestoreResult(
            page_id=metadata.page_id,
            title=metadata.title,
            space_key=actual_space_key,
            parent_id=actual_parent_id,
            placement_source=placement_source,
            placement_status=placement_status,
            warnings=warnings,
        )

    async def _move_resolved(self, metadata: PageMetadata, target: PagePlacement) -> PageMoveResult:
        """Apply a validated target with one action for each same-space move."""
        if metadata.space_key is None:
            raise Upstream("Confluence did not return the page's current space.")
        old_parent_id = metadata.ancestors[-1].page_id if metadata.ancestors else None
        cross_space = metadata.space_key != target.space_key
        action_position, target_id = self._move_action_target(target)
        if cross_space:
            await self._api.move_page_action(
                metadata.page_id,
                space_key=target.space_key,
                position=action_position,
                target_id=target_id,
                asynchronous=True,
                expected_username=self._username,
            )
            return PageMoveResult(
                page_id=metadata.page_id,
                title=metadata.title,
                space_key=target.space_key,
                parent_id=target.parent_id,
                moved=True,
                position_applied=True,
            )

        try:
            await self._api.move_page_action(
                metadata.page_id,
                space_key=target.space_key,
                position=action_position,
                target_id=target_id,
                asynchronous=False,
                expected_username=self._username,
            )
        except (AuthenticationRequired, AccountMismatch):
            raise
        except ConnectorError as exc:
            uncertain = isinstance(exc, UncertainWrite)
            return PageMoveResult(
                page_id=metadata.page_id,
                title=metadata.title,
                space_key=target.space_key,
                parent_id=None if uncertain else old_parent_id,
                moved=False,
                position_applied=None if uncertain else False,
                warnings=[
                    f"The requested same-space move was not confirmed ({type(exc).__name__}); inspect the page before retrying."
                    if uncertain
                    else f"The requested same-space move was refused ({type(exc).__name__}); the page's previous placement remains in effect."
                ],
            )
        return PageMoveResult(
            page_id=metadata.page_id,
            title=metadata.title,
            space_key=target.space_key,
            parent_id=target.parent_id,
            moved=True,
            position_applied=True,
        )

    def _move_action_target(self, target: PagePlacement) -> tuple[str, str | None]:
        """Translate public placement into the UI action's documented position/target pair."""
        if target.position == "before":
            return "above", target.sibling_id
        if target.position == "after":
            return "below", target.sibling_id
        if target.position == "first":
            if target.sibling_id is not None:
                return "above", target.sibling_id
        if target.parent_id is None:
            if target.position == "last":
                if target.sibling_id is not None:
                    return "below", target.sibling_id
            return "topLevel", None
        return "append", target.parent_id

    async def _change_parent(self, metadata: PageMetadata, parent_id: str | None) -> None:
        """Reparent a current same-space page without creating a content version."""
        if metadata.space_key is None:
            raise Upstream("Confluence did not return the page space for its parent change.")
        if parent_id is None:
            target = await self._placement_resolver.resolve(
                parent_reference=None,
                space_key=metadata.space_key,
                position="last",
                sibling_reference=None,
                resolve_page=self._resolve_page,
                allow_missing_status=True,
                moving_page_id=metadata.page_id,
                resolve_root_last=True,
            )
        else:
            target = PagePlacement(space_key=metadata.space_key, parent_id=parent_id)
        position, target_id = self._move_action_target(target)
        await self._api.move_page_action(
            metadata.page_id,
            space_key=metadata.space_key,
            position=position,
            target_id=target_id,
            asynchronous=False,
            expected_username=self._username,
        )

    async def _resolve_placement(
        self,
        *,
        parent_reference: str | None,
        space_key: str | None,
        position: str,
        sibling_reference: str | None,
        moving_page_id: str,
    ) -> PagePlacement:
        """Delegate validated placement inputs to the shared resolver."""
        return await self._placement_resolver.resolve(
            parent_reference=parent_reference,
            space_key=space_key,
            position=position,  # type: ignore[arg-type]
            sibling_reference=sibling_reference,
            resolve_page=self._resolve_page,
            allow_missing_status=True,
            moving_page_id=moving_page_id,
            resolve_root_last=True,
        )

    async def _saved_placement(
        self, metadata: PageMetadata
    ) -> tuple[PagePlacement | None, str | None]:
        """Load a saved placement after restoration, validating the JSON at this remote boundary."""
        try:
            value = await self._api.get_page_property(
                metadata.page_id, PLACEMENT_PROPERTY_KEY, expected_username=self._username
            )
        except (AuthenticationRequired, AccountMismatch):
            raise
        except ConnectorError as exc:
            log.info("Saved page placement unavailable (%s)", type(exc).__name__)
            return None, "No readable saved placement was available; the page remains at the space root."
        try:
            if not isinstance(value, dict):
                raise ValueError("not an object")
            placement = PagePlacement.model_validate(value)
            if placement.space_key != metadata.space_key:
                raise ValueError("space does not match")
            return placement, None
        except (ValueError, TypeError):
            log.warning("Saved page placement was malformed; page remains at the space root")
            return None, "The saved placement record was invalid; the page remains at the space root."

    async def _preflight_delete(self, nodes: list[_PageNode]) -> None:
        """Require delete operation on every page affected before the first mutation."""
        for node in nodes:
            if not await self._api.page_supports_delete(
                node.page_id, expected_username=self._username
            ):
                raise PermissionDenied(
                    f"The current account cannot delete every affected page; permission is missing for {node.title} (ID {node.page_id}). No changes were made."
                )

    async def _restore_warning(self, space_key: str) -> list[str]:
        """Best-effort read of space permissions before a one-way trash operation."""
        try:
            readable = await self._api.space_permissions_readable(
                space_key, expected_username=self._username
            )
        except (AuthenticationRequired, AccountMismatch):
            raise
        except (PermissionDenied, Upstream) as exc:
            log.info("Space permissions could not be read (%s)", type(exc).__name__)
            return ["Space permissions could not be read; restoring this page may require a space administrator."]
        if not readable:
            return ["Space permissions could not be read; restoring this page may require a space administrator."]
        return []

    async def _save_placements(self, nodes: list[_PageNode]) -> int:
        """Persist placement metadata as best effort before trashing pages."""
        failures = 0
        for node in nodes:
            try:
                await self._api.set_page_property(
                    node.page_id,
                    PLACEMENT_PROPERTY_KEY,
                    {
                        "space_key": node.space_key,
                        "parent_id": node.parent_id,
                        "position": "before" if node.next_sibling_id is not None else "last",
                        "sibling_id": node.next_sibling_id,
                    },
                    expected_username=self._username,
                )
            except (AuthenticationRequired, AccountMismatch):
                raise
            except ConnectorError as exc:
                failures += 1
                log.warning("Could not save a page placement property (%s)", type(exc).__name__)
        return failures

    async def _subtree(self, root: PageMetadata) -> list[_PageNode]:
        """Read a complete page subtree before a trash-mode write, retaining source order and placement."""
        root_node = await self._node_for_metadata(root)
        nodes = [root_node]
        seen = {root_node.page_id}
        pending = [root_node]
        while pending:
            parent = pending.pop()
            children = await self._read_children(parent.page_id, parent.space_key)
            child_nodes = self._nodes_for_children(children, parent.page_id, parent.space_key)
            if len(nodes) + len(child_nodes) > MAX_HIERARCHY_READ_PAGES:
                raise InvalidRequest(
                    f"The page hierarchy exceeds the {MAX_HIERARCHY_READ_PAGES}-page read safety cap; no changes were made."
                )
            for child in child_nodes:
                if child.page_id in seen:
                    raise Upstream("Confluence repeated a page while reading its hierarchy.")
                seen.add(child.page_id)
            nodes.extend(child_nodes)
            pending.extend(reversed(child_nodes))
        return nodes

    def _nodes_for_children(
        self, children: list[ContentSearchHit], parent_id: str, space_key: str
    ) -> list[_PageNode]:
        nodes: list[_PageNode] = []
        for index, hit in enumerate(children):
            page_id = _hit_page_id(hit)
            next_id = _hit_page_id(children[index + 1]) if index + 1 < len(children) else None
            nodes.append(
                _PageNode(
                    page_id=page_id,
                    title=hit.title,
                    space_key=hit.space_key or space_key,
                    parent_id=parent_id,
                    position=hit.position if hit.position is not None else index,
                    next_sibling_id=next_id,
                )
            )
        return nodes

    async def _node_for_metadata(self, metadata: PageMetadata) -> _PageNode:
        if metadata.space_key is None:
            raise Upstream("Confluence did not return a page's space.")
        parent_id = metadata.ancestors[-1].page_id if metadata.ancestors else None
        siblings = await self._read_children(parent_id, metadata.space_key)
        ids = [_hit_page_id(hit) for hit in siblings]
        try:
            index = ids.index(metadata.page_id)
        except ValueError:
            return _PageNode(metadata.page_id, metadata.title, metadata.space_key, parent_id, None, None)
        return _PageNode(
            metadata.page_id,
            metadata.title,
            metadata.space_key,
            parent_id,
            siblings[index].position if siblings[index].position is not None else index,
            ids[index + 1] if index + 1 < len(ids) else None,
        )

    async def _read_children(self, parent_id: str | None, space_key: str) -> list[ContentSearchHit]:
        """Read every ordered direct child while bounding page counts and validating server offsets."""
        rows: list[ContentSearchHit] = []
        start = 0
        while True:
            if parent_id is None:
                page = await self._api.list_space_root_pages(
                    space_key, start=start, expected_username=self._username
                )
            else:
                page = await self._api.list_child_pages(
                    parent_id, start=start, expected_username=self._username
                )
            rows.extend(page.hits)
            if len(rows) > MAX_HIERARCHY_READ_PAGES:
                raise InvalidRequest(
                    f"A page's child list exceeds the {MAX_HIERARCHY_READ_PAGES}-page read safety cap."
                )
            next_start = page.next_start(CHILD_PAGE_SIZE)
            if next_start is None:
                return rows
            start = next_start

    async def _resolve_page(self, reference: str, *, status: str | None = None) -> ResolvedPageReference:
        """Resolve one reference already sanitized by a public structure-tool boundary."""
        resolved = await self._links.resolve(reference, expected_username=self._username, status=status)
        if resolved.version is not None:
            raise InvalidRequest("Page structure operations target the current page; omit pageVersion from the reference.")
        if not resolved.page_id.isdecimal():
            raise InvalidRequest("The selected page reference did not resolve to a numeric page ID.")
        return resolved


@dataclass(frozen=True)
class _PageNode:
    """Validated page identity and former placement collected before a delete write."""

    page_id: str
    title: str
    space_key: str
    parent_id: str | None
    position: int | None
    next_sibling_id: str | None


def _children_mode(value: Any) -> Literal["refuse", "move_up", "trash"]:
    if not isinstance(value, str) or value not in {"refuse", "move_up", "trash"}:
        raise InvalidRequest("children must be refuse, move_up or trash.")
    return value  # type: ignore[return-value]


def _trash_limit(value: Any) -> int:
    result = DEFAULT_TRASH_LIMIT if value is None else value
    if not isinstance(result, int) or isinstance(result, bool) or not 1 <= result <= MAX_TRASH_LIMIT:
        raise InvalidRequest(f"limit must be between 1 and {MAX_TRASH_LIMIT}.")
    return result


def _decode_trash_cursor(value: str) -> tuple[str | None, int, int]:
    state = cursors.decode(value, _TRASH_CURSOR_KIND)
    if set(state) != {"space_key", "start", "limit"}:
        raise InvalidRequest("Malformed trash cursor.")
    selected_space = optional_space_key(state.get("space_key"))
    start = state.get("start")
    limit = state.get("limit")
    if (
        not isinstance(start, int)
        or isinstance(start, bool)
        or not 0 <= start <= MAX_TRASH_OFFSET
        or not isinstance(limit, int)
        or isinstance(limit, bool)
        or not 1 <= limit <= MAX_TRASH_LIMIT
    ):
        raise InvalidRequest("Malformed trash cursor.")
    return selected_space, start, limit


def _hit_page_id(hit: ContentSearchHit) -> str:
    if hit.content_id is None or not hit.content_id.isdecimal() or hit.content_type not in (None, "page"):
        raise Upstream("Confluence returned a malformed page in the hierarchy.")
    return hit.content_id


def _outcome(
    node: _PageNode,
    status: Literal["trashed", "moved_up", "skipped", "failed", "not_attempted", "unknown"],
    detail: str | None = None,
) -> PageStructureOutcome:
    return PageStructureOutcome(
        page_id=node.page_id,
        title=node.title,
        former_space_key=node.space_key,
        former_parent_id=node.parent_id,
        former_position=node.position,
        status=status,
        detail=detail,
    )


def _delete_result(
    metadata: PageMetadata,
    mode: Literal["refuse", "move_up", "trash"],
    outcomes: dict[str, PageStructureOutcome],
    warnings: list[str],
    failure: str | None,
) -> PageDeleteResult:
    return PageDeleteResult(
        page_id=metadata.page_id,
        title=metadata.title,
        children_mode=mode,
        complete=failure is None,
        pages=list(outcomes.values()),
        warnings=warnings,
        failure=failure,
    )


def _error_text(error: ConnectorError) -> str:
    return f"{type(error).__name__}: {error}"
