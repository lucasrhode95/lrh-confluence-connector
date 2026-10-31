"""Convert safe rich or plain-text input into Confluence storage markup."""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from collections import Counter
from typing import Any
from html import escape
from html.entities import html5
from html.parser import HTMLParser

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
_XML_PREFIX = re.compile(r"(?<![\w.-])([A-Za-z_][\w.-]*):(?=[A-Za-z_])")
_NAMED_ENTITY = re.compile(r"&([A-Za-z][A-Za-z0-9]*);")
_XML_ENTITIES = frozenset({"amp", "lt", "gt", "quot", "apos"})
MAX_RICH_MARKUP_DEPTH = 128


def body_to_storage(value: Any, page_format: Any) -> str:
    """Entry point for page and comment bodies: validate the text, then convert its format."""
    if not isinstance(value, str):
        raise InvalidRequest("body must be text.")
    return to_storage(value, page_format)


def to_storage(body: str, page_format: str) -> str:
    """Convert rich input only when its XML fragment is well formed; let Confluence validate malformed input."""
    if page_format == "text":
        return _plain_text_to_storage(body)
    if page_format != "rich":
        raise InvalidRequest("format must be `rich` or `text`.")

    soup = BeautifulSoup(body, "html.parser")
    _check_active_content(soup)
    _check_markup_depth(soup, MAX_RICH_MARKUP_DEPTH)
    if not _is_well_formed_fragment(body):
        return body
    _replace_code_blocks(soup)
    _replace_expands(soup)
    return str(soup)


def file_to_storage(body: str, existing_storage: str | None) -> str:
    """Check a `body_file` storage body and convert only its new pre/details blocks.

    Every other byte is sent unchanged, so an unedited export round-trips exactly; markup Confluence
    cannot parse is sent as is and its 400 reports the row and column. A `<pre>` block whose source
    already appears in ``existing_storage`` is Confluence's own preformatted text and is kept.
    """
    soup = BeautifulSoup(body, "html.parser")
    _check_active_content(soup)
    _check_markup_depth(soup, MAX_RICH_MARKUP_DEPTH)
    replacements: list[tuple[int, int, str]] = []
    for start, end, name in _block_spans(body, frozenset({"pre", "details"})):
        source = body[start:end]
        if name == "pre" and existing_storage is not None and source in existing_storage:
            continue
        block = BeautifulSoup(source, "html.parser")
        _replace_code_blocks(block)
        _replace_expands(block)
        replacements.append((start, end, str(block)))
    for start, end, converted in reversed(replacements):
        body = body[:start] + converted + body[end:]
    return body


def content_loss(previous: str, replacement: str) -> tuple[bool, list[str]]:
    """Describe removed source anchors, macros and attachments without exposing their content."""
    old_items = _source_items(previous)
    new_items = _source_items(replacement)
    old_counts = Counter(old_items)
    new_counts = Counter(new_items)
    anchor_loss = sum(
        count - new_counts[item]
        for item, count in old_counts.items()
        if item[0] == "inline-comment" and count > new_counts[item]
    )
    macro_losses: Counter[str] = Counter()
    macro_changes: Counter[tuple[str, str]] = Counter()
    attachment_loss = 0
    for item, count in old_counts.items():
        if item[0] == "macro":
            continue
        removed = count - new_counts[item]
        if removed <= 0:
            continue
        if item[0] == "attachment":
            attachment_loss += removed
    old_macro_ids = {item[1] for item in old_items if item[0] == "macro" and item[1]}
    new_macro_ids = {item[1] for item in new_items if item[0] == "macro" and item[1]}
    shared_macro_ids = old_macro_ids & new_macro_ids
    old_macros = _macros_by_identity(
        old_items, shared_ids=shared_macro_ids, other_items=new_items
    )
    new_macros = _macros_by_identity(
        new_items, shared_ids=shared_macro_ids, other_items=old_items
    )
    for identity, old_shapes in old_macros.items():
        new_shapes = new_macros.get(identity, Counter())
        old_remaining = {
            shape: count - new_shapes.get(shape, 0)
            for shape, count in old_shapes.items()
            if count > new_shapes.get(shape, 0)
        }
        new_remaining = {
            shape: count - old_shapes.get(shape, 0)
            for shape, count in new_shapes.items()
            if count > old_shapes.get(shape, 0)
        }
        for old_shape, old_count in old_remaining.items():
            for new_shape, new_count in new_remaining.items():
                changed = min(old_count, new_count)
                if changed:
                    macro_changes[(old_shape[0], new_shape[0])] += changed
                    old_remaining[old_shape] -= changed
                    new_remaining[new_shape] -= changed
                    old_count -= changed
                    if old_count == 0:
                        break
        for shape, count in old_remaining.items():
            if count > 0:
                macro_losses[shape[0]] += count
    warnings: list[str] = []
    if anchor_loss:
        warnings.append(f"{anchor_loss} inline-comment anchor(s) would be detached.")
    for (old_name, new_name), count in sorted(macro_changes.items()):
        if old_name == new_name:
            warnings.append(f"{count} {old_name} macro(s) changed.")
        else:
            warnings.append(f"{count} {old_name} macro(s) changed to {new_name}.")
    for name, count in sorted(macro_losses.items()):
        warnings.append(f"{count} {name} macro(s) would be removed.")
    if attachment_loss:
        warnings.append(f"{attachment_loss} attachment reference(s) would be removed.")
    return anchor_loss > 0, warnings


def _is_well_formed_fragment(source: str) -> bool:
    """Check storage markup as XML while allowing Confluence's unbound ac:/ri: fragment prefixes."""
    prefixes = {
        prefix
        for prefix in _XML_PREFIX.findall(source)
        if prefix not in {"xml", "xmlns"}
    }
    namespaces = " ".join(f'xmlns:{prefix}="urn:{prefix}"' for prefix in sorted(prefixes))
    normalized = _numeric_html_entities(_close_html_void_elements(source))
    try:
        ET.fromstring(f"<confluence-body {namespaces}>{normalized}</confluence-body>")
    except ET.ParseError:
        return False
    return True


def _numeric_html_entities(source: str) -> str:
    """Spell HTML named entities numerically for XML validation; storage accepts them, XML does not."""

    def numeric(match: re.Match[str]) -> str:
        name = match.group(1)
        text = html5.get(f"{name};")
        if name in _XML_ENTITIES or text is None:
            return match.group(0)
        return "".join(f"&#{ord(character)};" for character in text)

    return _NAMED_ENTITY.sub(numeric, source)


def _close_html_void_elements(source: str) -> str:
    """Normalize only HTML void start tags for XML validation; do not repair other malformed markup."""
    if not any(f"<{tag}" in source.casefold() for tag in _VOID_TAGS):
        return source
    parser = _VoidTagNormalizer(source)
    parser.feed(source)
    parser.close()
    for offset in reversed(parser.insertions):
        source = source[:offset] + "/" + source[offset:]
    return source


class _VoidTagNormalizer(HTMLParser):
    def __init__(self, source: str) -> None:
        super().__init__(convert_charrefs=False)
        self._line_starts = [0, *(match.end() for match in re.finditer(r"\n", source))]
        self.insertions: list[int] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag not in _VOID_TAGS:
            return
        source_tag = self.get_starttag_text()
        if source_tag is None or re.search(r"/\s*>$", source_tag):
            return
        line, column = self.getpos()
        start = self._line_starts[line - 1] + column
        self.insertions.append(start + len(source_tag) - 1)


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


def _block_spans(text: str, names: frozenset[str]) -> list[tuple[int, int, str]]:
    """Return source offsets of outermost elements named in ``names``; CDATA content is not markup."""
    finder = _BlockSpanFinder(text, names)
    finder.feed(text)
    finder.close()
    return finder.spans


class _BlockSpanFinder(HTMLParser):
    def __init__(self, text: str, names: frozenset[str]) -> None:
        super().__init__(convert_charrefs=False)
        self._text = text
        self._names = names
        self._line_starts = [0, *(match.end() for match in re.finditer(r"\n", text))]
        self._open: tuple[str, int] | None = None
        self._depth = 0
        self.spans: list[tuple[int, int, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._open is None and tag in self._names:
            self._open = (tag, self._offset())
            self._depth = 1
        elif self._open is not None and tag == self._open[0]:
            self._depth += 1

    def handle_endtag(self, tag: str) -> None:
        if self._open is None or tag != self._open[0]:
            return
        self._depth -= 1
        if self._depth == 0:
            end = self._text.index(">", self._offset()) + 1
            self.spans.append((self._open[1], end, tag))
            self._open = None

    def _offset(self) -> int:
        line, column = self.getpos()
        return self._line_starts[line - 1] + column


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
            name, macro_id, fallback_identity = _macro_identity(node)
            items.append(("macro", macro_id or "", fallback_identity, name))
    return items


def _macros_by_identity(
    items: list[tuple[str, ...]],
    *,
    shared_ids: set[str],
    other_items: list[tuple[str, ...]],
) -> dict[str, Counter[tuple[str, str]]]:
    """Group macros by IDs shared across both bodies, or by the fallback when one lacks an ID.

    Within a group a macro's shape is its name and parameters, so a changed parameter counts as a
    change and an edited body inside the macro does not.
    """
    grouped: dict[str, Counter[tuple[str, str]]] = {}
    other_without_id = {
        item[2] for item in other_items if item[0] == "macro" and not item[1]
    }
    for item in items:
        if item[0] == "macro":
            _kind, macro_id, fallback, name = item
            if macro_id in shared_ids:
                identity = f"macro-id:{macro_id}"
            elif not macro_id or fallback in other_without_id:
                identity = f"fallback:{fallback}"
            else:
                identity = f"unmatched-id:{macro_id}"
            grouped.setdefault(identity, Counter())[(name, fallback)] += 1
    return grouped


def _macro_identity(node: Tag) -> tuple[str, str | None, str]:
    """Return the macro name, its trimmed ac:macro-id and its name-and-parameters fallback identity."""
    name = (node.get("ac:name") or node.get("name") or "unknown").casefold()
    macro_id = node.get("ac:macro-id") or node.get("macro-id")
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
    fallback_identity = json.dumps(
        [name, sorted(parameters), sorted(page_references)],
        separators=(",", ":"),
        ensure_ascii=False,
    )
    macro_id = macro_id.strip() if isinstance(macro_id, str) and macro_id.strip() else None
    return name, macro_id, fallback_identity


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
