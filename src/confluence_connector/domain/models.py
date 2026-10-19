"""Typed, compact results returned through MCP and the command-line status surface."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ContentType = Literal["page", "blogpost", "comment", "attachment", "space", "user"]
SearchMatch = Literal["all", "any", "phrase"]
SearchFields = Literal["all", "title"]
SearchSource = Literal["cql", "quicknav"]


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

    space_keys: list[str] | None = None
    space_type: Literal["global", "personal"] | None = None
    content_types: list[ContentType] | None = None
    labels: list[str] | None = None
    creator_usernames: list[str] | None = None
    contributor_usernames: list[str] | None = None
    created_after: str | None = None
    created_before: str | None = None
    modified_after: str | None = None
    modified_before: str | None = None
    parent: str | None = None
    ancestor: str | None = None
    include_archived: bool = True


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
