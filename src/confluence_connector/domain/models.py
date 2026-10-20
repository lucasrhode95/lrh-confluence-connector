"""Typed, compact results returned through MCP and the command-line status surface."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


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


class ResolvedPageReference(BaseModel):
    model_config = ConfigDict(extra="ignore")

    page_id: str
    version: int | None = None
