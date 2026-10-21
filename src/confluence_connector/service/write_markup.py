"""Convert safe rich or plain-text input into Confluence storage markup."""

from __future__ import annotations

import json
import re
from collections import Counter
from html import escape

from bs4 import CData, BeautifulSoup, Tag

from confluence_connector.domain.errors import InvalidRequest

_VOID_TAGS = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)
_ACTIVE_TAGS = frozenset({"script", "style", "iframe", "object", "embed"})
_URL_ATTRIBUTES = frozenset(
    {
        "href",
        "src",
        "action",
        "formaction",
        "xlink:href",
        "data",
        "poster",
        "cite",
        "background",
        "longdesc",
    }
)
_LANGUAGE_CLASS = re.compile(r"(?:^|\s)language-([a-zA-Z0-9_+#.-]+)(?:\s|$)")
_JAVASCRIPT_SCHEME = re.compile(r"^javascript\s*:", re.IGNORECASE)
MAX_RICH_MARKUP_DEPTH = 128


def to_storage(body: str, page_format: str) -> str:
    """Convert one validated body format; rich HTML is checked before any write request."""
    if page_format == "text":
        return _plain_text_to_storage(body)
    if page_format != "rich":
        raise InvalidRequest("format must be `rich` or `text`.")

    soup = BeautifulSoup(body, "html.parser")
    _check_active_content(soup)
    _check_markup_depth(soup, MAX_RICH_MARKUP_DEPTH)
    _replace_code_blocks(soup)
    _replace_expands(soup)
    return str(soup)


def content_loss(previous: str, replacement: str) -> tuple[bool, list[str]]:
    """Describe removed source anchors, macros and attachments without exposing their content."""
    old_counts = Counter(_source_items(previous))
    new_counts = Counter(_source_items(replacement))
    anchor_loss = sum(
        count - new_counts[item]
        for item, count in old_counts.items()
        if item[0] == "inline-comment" and count > new_counts[item]
    )
    macro_losses: Counter[str] = Counter()
    attachment_loss = 0
    for item, count in old_counts.items():
        removed = count - new_counts[item]
        if removed <= 0:
            continue
        if item[0] == "macro":
            macro_losses[item[1]] += removed
        elif item[0] == "attachment":
            attachment_loss += removed
    warnings: list[str] = []
    if anchor_loss:
        warnings.append(f"{anchor_loss} inline-comment anchor(s) would be detached.")
    for name, count in sorted(macro_losses.items()):
        warnings.append(f"{count} {name} macro(s) would be removed.")
    if attachment_loss:
        warnings.append(f"{attachment_loss} attachment reference(s) would be removed.")
    return anchor_loss > 0, warnings


def _plain_text_to_storage(body: str) -> str:
    normalized = body.replace("\r\n", "\n").replace("\r", "\n")
    paragraphs = re.split(r"\n[ \t]*\n+", normalized)
    return "".join(
        f"<p>{escape(paragraph.strip(), quote=False).replace(chr(10), '<br/>')}</p>"
        for paragraph in paragraphs
        if paragraph.strip()
    )


def _check_active_content(soup: BeautifulSoup) -> None:
    for node in soup.find_all(True):
        if node.name in _ACTIVE_TAGS:
            raise InvalidRequest(f"Rich page content cannot contain <{node.name}> elements.")
        if node.name == "ac:structured-macro" and (
            node.get("ac:name") or node.get("name") or ""
        ).casefold() == "html":
            raise InvalidRequest("Rich page content cannot contain the Confluence HTML macro.")
        for name, value in node.attrs.items():
            if name.startswith("on"):
                raise InvalidRequest("Rich page content cannot contain event-handler attributes.")
            if name in _URL_ATTRIBUTES and value is not None:
                compact = re.sub(r"[\x00-\x20\x7f]+", "", str(value))
                if _JAVASCRIPT_SCHEME.match(compact):
                    raise InvalidRequest("Rich page content cannot contain javascript: URLs.")


def _check_markup_depth(soup: BeautifulSoup, maximum_depth: int) -> None:
    def visit(container: Tag, depth: int) -> None:
        for child in container.children:
            if not isinstance(child, Tag):
                continue
            child_depth = depth + (child.name not in _VOID_TAGS)
            if child_depth > maximum_depth:
                raise InvalidRequest(
                    f"Rich page markup cannot be nested more than {maximum_depth} elements."
                )
            visit(child, child_depth)

    visit(soup, 0)


def _replace_code_blocks(soup: BeautifulSoup) -> None:
    for pre in list(soup.find_all("pre")):
        _replace_code_block(soup, pre)


def _replace_code_block(soup: BeautifulSoup, pre: Tag) -> None:
    code = pre.find("code", recursive=False)
    language = None
    source = code if code is not None else pre
    if code is not None:
        classes = code.get("class", [])
        class_text = " ".join(classes) if isinstance(classes, list) else str(classes)
        language_match = _LANGUAGE_CLASS.search(class_text)
        language = language_match.group(1) if language_match else None

    macro = soup.new_tag("ac:structured-macro", attrs={"ac:name": "code"})
    if language:
        parameter = soup.new_tag("ac:parameter", attrs={"ac:name": "language"})
        parameter.string = language
        macro.append(parameter)
    body = soup.new_tag("ac:plain-text-body")
    code_text = source.get_text()
    body.append(CData(code_text.replace("]]>", "]]]]><![CDATA[>")))
    macro.append(body)
    pre.replace_with(macro)


def _replace_expands(soup: BeautifulSoup) -> None:
    for details in list(soup.find_all("details")):
        _replace_expand(soup, details)


def _replace_expand(soup: BeautifulSoup, details: Tag) -> None:
    summary = details.find("summary", recursive=False)
    title = summary.get_text().strip() if summary is not None else ""
    if summary is not None:
        summary.decompose()

    macro = soup.new_tag("ac:structured-macro", attrs={"ac:name": "expand"})
    if title:
        parameter = soup.new_tag("ac:parameter", attrs={"ac:name": "title"})
        parameter.string = title
        macro.append(parameter)

    body = soup.new_tag("ac:rich-text-body")
    for child in list(details.contents):
        body.append(child.extract())
    macro.append(body)
    details.replace_with(macro)


def _source_items(source: str) -> list[tuple[str, ...]]:
    soup = BeautifulSoup(source, "html.parser")
    items: list[tuple[str, ...]] = []
    for node in soup.find_all(True):
        if node.name == "ac:inline-comment-marker":
            items.append(("inline-comment", node.get("ac:ref") or "unidentified"))
        elif node.name == "ri:attachment":
            identity = _attachment_identity(node)
            items.append(("attachment", identity))
        elif node.name == "ac:structured-macro":
            name, identity = _macro_identity(node)
            items.append(("macro", name, identity))
    return items


def _macro_identity(node: Tag) -> tuple[str, str]:
    name = (node.get("ac:name") or node.get("name") or "unknown").casefold()
    parameters: list[tuple[str, str]] = []
    page_references: list[tuple[str, ...]] = []
    for child in node.find_all(True):
        if child.find_parent("ac:structured-macro") is not node:
            continue
        if child.name == "ac:parameter":
            parameter_name = (child.get("ac:name") or child.get("name") or "").casefold()
            parameters.append((parameter_name, child.get_text().strip()))
        elif child.name == "ri:page":
            page_references.append(_page_identity(child))
        elif child.name == "ri:attachment":
            page_references.append(("attachment", _attachment_identity(child)))
    identity = json.dumps(
        [name, sorted(parameters), sorted(page_references)],
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return name, identity


def _page_identity(node: Tag) -> tuple[str, ...]:
    """A source page target includes its space as well as its ID/title."""
    return tuple(str(node.get(key) or "") for key in ("ri:content-id", "ri:page-id", "ri:content-title", "ri:space-key"))


def _attachment_identity(node: Tag) -> str:
    """A source attachment target includes the explicit owning page, when present."""
    owner = node.find("ri:page")
    return json.dumps([
        node.get("ri:content-id") or "", node.get("ri:filename") or "",
        _page_identity(owner) if owner is not None else None,
    ], separators=(",", ":"), ensure_ascii=False)
