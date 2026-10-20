"""Typed, compact results returned through MCP and the command-line status surface."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ContentType = Literal["page", "blogpost", "comment", "attachment", "space", "user"]
SearchMatch = Literal["all", "any", "phrase"]
SearchFields = Literal["all", "title"]
SearchSource = Literal["cql", "quicknav"]
PageTextFormat = Literal["text", "rich"]
PageWriteStatus = Literal["current", "draft"]


class CurrentUser(BaseModel):
    model_config = ConfigDict(extra="ignore")

    username: str


class AuthStatus(BaseModel):
    model_config = ConfigDict(extra="ignore")

    configured: bool
    token_stored: bool
    username: str | None = None
    base_url: str
    checked: bool = False


class SpaceSummary(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str | None = None
    key: str
    name: str
    type: str | None = None
    status: str | None = None
    url: str | None = None


class SpaceList(BaseModel):
    model_config = ConfigDict(extra="ignore")

    spaces: list[SpaceSummary]
    total_count: int
    truncated: bool


class SearchScope(BaseModel):
    """Structured, validated filters accepted by the public content-search tool."""

    model_config = ConfigDict(extra="forbid")

    space_keys: list[str] | None = Field(default=None, description="Include only these Confluence space keys.")
    space_type: Literal["global", "personal"] | None = Field(
        default=None, description="Include global spaces or personal spaces."
    )
    content_types: list[ContentType] | None = Field(
        default=None, description="Content types such as page, blogpost, comment or attachment."
    )
    labels: list[str] | None = Field(default=None, description="Include content with any of these labels.")
    creator_usernames: list[str] | None = Field(
        default=None, description="Include content created by any of these usernames."
    )
    contributor_usernames: list[str] | None = Field(
        default=None, description="Include content contributed to by any of these usernames."
    )
    created_after: str | None = Field(
        default=None,
        description="Inclusive lower bound: date in Confluence's zone or ISO instant (no offset means UTC).",
    )
    created_before: str | None = Field(
        default=None,
        description="Inclusive upper bound: date in Confluence's zone or ISO instant (no offset means UTC).",
    )
    modified_after: str | None = Field(
        default=None,
        description="Inclusive lower bound: date in Confluence's zone or ISO instant (no offset means UTC).",
    )
    modified_before: str | None = Field(
        default=None,
        description="Inclusive upper bound: date in Confluence's zone or ISO instant (no offset means UTC).",
    )
    parent: str | None = Field(
        default=None, description="Direct-child page ID or URL (up to 2,048 characters)."
    )
    ancestor: str | None = Field(
        default=None, description="Ancestor page ID or URL (up to 2,048 characters)."
    )
    include_archived: bool = Field(
        default=True, description="Include archived spaces (default true)."
    )


class SearchQuery(BaseModel):
    model_config = ConfigDict(extra="ignore")

    source: SearchSource
    value: str


class PageSummary(BaseModel):
    model_config = ConfigDict(extra="ignore")

    page_id: str
    title: str
    space_key: str | None = None
    url: str | None = None
    position: int | None = None


class ContentSearchHit(BaseModel):
    model_config = ConfigDict(extra="ignore")

    content_id: str | None = None
    content_type: str | None = None
    title: str
    space_key: str | None = None
    space_name: str | None = None
    url: str | None = None
    excerpt: str | None = None
    modified_at: str | None = None
    parent_id: str | None = None
    ancestors: list[PageSummary] = Field(default_factory=list)
    position: int | None = None
    sources: list[SearchSource] = Field(default_factory=list)


class RemoteContentSearchPage(BaseModel):
    """One mapped Confluence search page before service-level cursor construction."""

    model_config = ConfigDict(extra="ignore")

    hits: list[ContentSearchHit]
    start: int
    size: int
    total_size: int | None = None


class ContentSearchResult(BaseModel):
    model_config = ConfigDict(extra="ignore")

    query: str
    match: SearchMatch
    fields: SearchFields
    scope: SearchScope
    queries_sent: list[SearchQuery]
    hits: list[ContentSearchHit]
    start: int
    size: int
    approximate_total: int | None = None
    more_results_available: bool
    next_cursor: str | None = None
    notes: list[str] = Field(default_factory=list)


class PersonSummary(BaseModel):
    model_config = ConfigDict(extra="ignore")

    username: str | None = None
    user_key: str | None = None
    display_name: str | None = None
    user_type: str | None = None


class RemoteUserSearchPage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    users: list[PersonSummary]
    start: int
    size: int
    total_size: int | None = None


class UserSearchResult(BaseModel):
    model_config = ConfigDict(extra="ignore")

    query: str
    users: list[PersonSummary]
    approximate_total: int | None = None
    start: int
    size: int
    next_cursor: str | None = None
    notes: list[str] = Field(default_factory=list)


class PageTreeNode(BaseModel):
    model_config = ConfigDict(extra="ignore")

    page_id: str
    title: str
    url: str | None = None
    position: int | None = None
    children: list[PageTreeNode] = Field(default_factory=list)


class PageTreeResult(BaseModel):
    model_config = ConfigDict(extra="ignore")

    root: PageSummary | None = None
    space_key: str | None = None
    ancestors: list[PageSummary] = Field(default_factory=list)
    children: list[PageTreeNode] = Field(default_factory=list)
    max_depth: int
    total_pages: int
    complete: bool
    notes: list[str] = Field(default_factory=list)


class PageVersion(BaseModel):
    model_config = ConfigDict(extra="ignore")

    number: int
    when: str | None = None
    author: PersonSummary | None = None
    message: str | None = None
    minor_edit: bool | None = None


class PageMetadata(BaseModel):
    model_config = ConfigDict(extra="ignore")

    page_id: str
    title: str
    content_type: str
    status: str | None = None
    url: str | None = None
    space_key: str | None = None
    space_name: str | None = None
    created_at: str | None = None
    created_by: PersonSummary | None = None
    modified_at: str | None = None
    modified_by: PersonSummary | None = None
    version: int | None = None
    ancestors: list[PageSummary] = Field(default_factory=list)
    labels: list[str] = Field(default_factory=list)
    contributors: list[PersonSummary] = Field(default_factory=list)
    versions: list[PageVersion] = Field(default_factory=list)
    versions_truncated: bool = False


class PageMetadataFailure(BaseModel):
    model_config = ConfigDict(extra="ignore")

    reference: str
    error: str


class PageMetadataResult(BaseModel):
    model_config = ConfigDict(extra="ignore")

    pages: list[PageMetadata]
    failures: list[PageMetadataFailure] = Field(default_factory=list)


class ResolvedPageReference(BaseModel):
    model_config = ConfigDict(extra="ignore")

    page_id: str
    version: int | None = None


class PageParseScope(BaseModel):
    """Validated switches for optional page-read sections and source metadata."""

    model_config = ConfigDict(extra="forbid")

    comments: bool = Field(
        default=True, description="Include the threaded comment section and comment bodies."
    )
    attachments: bool = Field(default=True, description="Include the page's attachment list and references.")
    macros_from_storage: bool = Field(
        default=True, description="Read storage markup to identify macros and typed references."
    )
    header: bool = Field(default=True, description="Include page title, ID, URL, space and timestamps.")


class PageReference(BaseModel):
    """Typed source identity carried beside rendered page text."""

    model_config = ConfigDict(extra="ignore")

    kind: Literal["page", "attachment", "image", "comment", "jira", "macro", "inline_comment"]
    page_id: str | None = None
    version: int | None = None
    title: str | None = None
    attachment_id: str | None = None
    comment_id: str | None = None
    filename: str | None = None
    media_type: str | None = None
    size: int | None = None
    owner_page_id: str | None = None
    url: str | None = None
    alt_text: str | None = None
    key: str | None = None
    macro_name: str | None = None


class PageAttachment(BaseModel):
    model_config = ConfigDict(extra="ignore")

    attachment_id: str
    filename: str
    media_type: str | None = None
    file_size: int | None = None
    download_url: str | None = None


class SavedAttachment(BaseModel):
    model_config = ConfigDict(extra="ignore")

    attachment_id: str
    filename: str
    path: str
    media_type: str | None = None
    size_bytes: int


class AttachmentDownloadFailure(BaseModel):
    model_config = ConfigDict(extra="ignore")

    attachment_id: str
    filename: str | None = None
    error: str


class AttachmentDownloadResult(BaseModel):
    model_config = ConfigDict(extra="ignore")

    page_id: str
    files: list[SavedAttachment] = Field(default_factory=list)
    failures: list[AttachmentDownloadFailure] = Field(default_factory=list)


class PageExportResult(BaseModel):
    model_config = ConfigDict(extra="ignore")

    path: str
    filename: str
    content_type: str
    size_bytes: int
    pages_exported: int
    pages_failed: int
    attachments_exported: int = 0
    attachments_failed: int = 0


class PageWriteResult(BaseModel):
    model_config = ConfigDict(extra="ignore")

    page_id: str
    title: str
    status: PageWriteStatus
    version: int | None = None
    url: str | None = None
    warnings: list[str] = Field(default_factory=list)


class PagePlacement(BaseModel):
    """Validated target placement carried into move and restore operations."""

    model_config = ConfigDict(extra="forbid")

    space_key: str
    parent_id: str | None = None
    position: Literal["first", "last", "before", "after"] = "last"
    sibling_id: str | None = None


class PageMoveResult(BaseModel):
    model_config = ConfigDict(extra="ignore")

    page_id: str
    title: str
    space_key: str
    parent_id: str | None = None
    moved: bool
    position_applied: bool | None = None
    warnings: list[str] = Field(default_factory=list)


class PageStructureOutcome(BaseModel):
    model_config = ConfigDict(extra="ignore")

    page_id: str
    title: str
    former_space_key: str
    former_parent_id: str | None = None
    former_position: int | None = None
    status: Literal["trashed", "moved_up", "not_attempted", "unknown"]
    detail: str | None = None


class PageDeleteResult(BaseModel):
    model_config = ConfigDict(extra="ignore")

    page_id: str
    title: str
    children_mode: Literal["refuse", "move_up", "trash"]
    complete: bool
    pages: list[PageStructureOutcome]
    warnings: list[str] = Field(default_factory=list)
    failure: str | None = None


class TrashPageSummary(BaseModel):
    model_config = ConfigDict(extra="ignore")

    page_id: str
    title: str
    space_key: str | None = None
    space_name: str | None = None
    url: str | None = None
    modified_at: str | None = None


class TrashListResult(BaseModel):
    model_config = ConfigDict(extra="ignore")

    pages: list[TrashPageSummary]
    start: int
    size: int
    approximate_total: int | None = None
    more_results_available: bool
    truncated: bool = False
    next_cursor: str | None = None


class PageRestoreResult(BaseModel):
    model_config = ConfigDict(extra="ignore")

    page_id: str
    title: str
    space_key: str | None = None
    parent_id: str | None = None
    placement_source: Literal["caller", "saved_property", "space_root"]
    placement_status: Literal["applied", "root", "partial", "unknown"]
    warnings: list[str] = Field(default_factory=list)


class DraftSummary(BaseModel):
    model_config = ConfigDict(extra="ignore")

    page_id: str
    title: str
    space_key: str | None = None
    space_name: str | None = None
    url: str | None = None
    modified_at: str | None = None


class DraftListResult(BaseModel):
    model_config = ConfigDict(extra="ignore")

    drafts: list[DraftSummary]
    start: int
    size: int
    approximate_total: int | None = None
    more_results_available: bool
    truncated: bool = False
    next_cursor: str | None = None


class DraftDeletionResult(BaseModel):
    model_config = ConfigDict(extra="ignore")

    page_id: str
    title: str
    deleted: bool
    permanent: bool = True


class RemoteDraftPage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    drafts: list[DraftSummary]
    start: int
    size: int
    total_size: int | None = None
    has_more: bool | None = None


class RemoteTrashPage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    pages: list[TrashPageSummary]
    start: int
    size: int
    total_size: int | None = None
    has_more: bool | None = None


class LabelChangeFailure(BaseModel):
    model_config = ConfigDict(extra="ignore")

    action: Literal["add", "remove"]
    label: str
    error: str


class LabelChangeResult(BaseModel):
    model_config = ConfigDict(extra="ignore")

    page_id: str
    added: list[str] = Field(default_factory=list)
    removed: list[str] = Field(default_factory=list)
    unchanged: list[str] = Field(default_factory=list)
    failures: list[LabelChangeFailure] = Field(default_factory=list)
    not_attempted: list[LabelChangeFailure] = Field(default_factory=list)


class PageComment(BaseModel):
    model_config = ConfigDict(extra="ignore")

    comment_id: str
    parent_comment_id: str | None = None
    author: PersonSummary | None = None
    created_at: str | None = None
    body: str = ""
    location: str | None = None
    resolution: str | None = None
    selected_text: str | None = None
    references: list[PageReference] = Field(default_factory=list)


class RemotePageComment(BaseModel):
    """Mapped comment source fields before its rendered HTML body becomes plain text."""

    model_config = ConfigDict(extra="ignore")

    comment_id: str
    parent_comment_id: str | None = None
    author: PersonSummary | None = None
    created_at: str | None = None
    body_view: str
    location: str | None = None
    resolution: str | None = None
    selected_text: str | None = None


class RemotePageContent(BaseModel):
    """Mapped page source fields required to render one current or historical version."""

    model_config = ConfigDict(extra="ignore")

    metadata: PageMetadata
    view_body: str
    storage_body: str
    version: int
    total_versions: int | None = None


class PageReadRecord(BaseModel):
    model_config = ConfigDict(extra="ignore")

    page_id: str
    title: str
    url: str | None = None
    version: int
    total_versions: int | None = None
    modified_at: str | None = None
    extracted_at: str
    format: PageTextFormat
    text: str
    header: str | None = None
    references: list[PageReference] = Field(default_factory=list)
    attachments: list[PageAttachment] = Field(default_factory=list)
    comments: list[PageComment] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class PageReadFailure(BaseModel):
    model_config = ConfigDict(extra="ignore")

    reference: str
    error: str


class PageReadResult(BaseModel):
    model_config = ConfigDict(extra="ignore")

    pages: list[PageReadRecord]
    remaining: list[str] = Field(default_factory=list)
    failures: list[PageReadFailure] = Field(default_factory=list)
    next_cursor: str | None = None
    notes: list[str] = Field(default_factory=list)
