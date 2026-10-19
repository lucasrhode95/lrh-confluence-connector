"""Convert safe rich or plain-text input into Confluence storage markup."""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from html import escape
from html.parser import HTMLParser

from confluence_connector.domain.errors import InvalidRequest

_VOID_TAGS = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
)
_ACTIVE_TAGS = frozenset({"script", "style", "iframe", "object", "embed"})
_URL_ATTRIBUTES = frozenset(
    {"href", "src", "action", "formaction", "xlink:href", "data", "poster", "cite", "background", "longdesc"}
)
_LANGUAGE_CLASS = re.compile(r"(?:^|\s)language-([a-zA-Z0-9_+#.-]+)(?:\s|$)")
_JAVASCRIPT_SCHEME = re.compile(r"^javascript\s*:", re.IGNORECASE)


@dataclass
class _MarkupNode:
    tag: str
    attributes: dict[str, str | None]
    start: int
    start_tag_end: int
    end_tag_start: int
    end: int
    self_closing: bool
    children: list[_MarkupNode] = field(default_factory=list)


class _MarkupParser(HTMLParser):
    """Record source spans so ordinary user markup remains byte-for-byte intact."""

    def __init__(self, source: str) -> None:
        super().__init__(convert_charrefs=False)
        self.source = source
        self.line_starts = [0]
        self.line_starts.extend(index + 1 for index, char in enumerate(source) if char == "\n")
        self.root = _MarkupNode("", {}, 0, 0, len(source), len(source), False)
        self.stack = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        raw = self.get_starttag_text() or ""
        start = self._offset()
        node = _MarkupNode(
            tag.casefold(),
            {name.casefold(): value for name, value in attrs},
            start,
            start + len(raw),
            start + len(raw),
            start + len(raw),
            False,
        )
        self.stack[-1].children.append(node)
        if node.tag not in _VOID_TAGS:
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        raw = self.get_starttag_text() or ""
        start = self._offset()
        end = start + len(raw)
        self.stack[-1].children.append(
            _MarkupNode(
                tag.casefold(),
                {name.casefold(): value for name, value in attrs},
                start,
                end,
                end,
                end,
                True,
            )
        )

    def handle_endtag(self, tag: str) -> None:
        wanted = tag.casefold()
        matching = next(
            (index for index in range(len(self.stack) - 1, 0, -1) if self.stack[index].tag == wanted),
            None,
        )
        if matching is None:
            return
        start = self._offset()
        close = self.source.find(">", start)
        end = len(self.source) if close < 0 else close + 1
        for node in self.stack[matching:]:
            node.end_tag_start = start
            node.end = end
        del self.stack[matching:]

    def finish(self) -> None:
        """Close source spans for tolerated but unclosed HTML nodes."""
        for node in self.stack[1:]:
            node.end_tag_start = len(self.source)
            node.end = len(self.source)

    def _offset(self) -> int:
        line, column = self.getpos()
        if line - 1 >= len(self.line_starts):
            return len(self.source)
        return self.line_starts[line - 1] + column


class _TextCollector(HTMLParser):
    """Collect decoded text for code and summary content, ignoring its markup."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def to_storage(body: str, page_format: str) -> str:
    """Convert one validated body format; rich HTML is checked before any write request."""
    if page_format == "text":
        return _plain_text_to_storage(body)
    if page_format != "rich":
        raise InvalidRequest("format must be `rich` or `text`.")
    _check_active_content(body)
    parser = _MarkupParser(body)
    parser.feed(body)
    parser.close()
    parser.finish()
    return _rewrite_region(body, 0, len(body), parser.root.children)


def _plain_text_to_storage(body: str) -> str:
    normalized = body.replace("\r\n", "\n").replace("\r", "\n")
    paragraphs = re.split(r"\n[ \t]*\n+", normalized)
    return "".join(
        f"<p>{escape(paragraph.strip(), quote=False).replace(chr(10), '<br/>')}</p>"
        for paragraph in paragraphs
        if paragraph.strip()
    )


def _check_active_content(source: str) -> None:
    class _SafetyParser(_MarkupParser):
        def __init__(self, value: str) -> None:
            super().__init__(value)
            self.violation: str | None = None

        def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
            self._check(tag, attrs)
            super().handle_starttag(tag, attrs)

        def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
            self._check(tag, attrs)
            super().handle_startendtag(tag, attrs)

        def _check(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
            tag_name = tag.casefold()
            if tag_name in _ACTIVE_TAGS:
                self.violation = f"<{tag_name}> elements"
                return
            mapped = {name.casefold(): value for name, value in attrs}
            if tag_name == "ac:structured-macro" and (
                mapped.get("ac:name") or mapped.get("name") or ""
            ).casefold() == "html":
                self.violation = "the Confluence HTML macro"
                return
            for name, value in mapped.items():
                if name.startswith("on"):
                    self.violation = "event-handler attributes"
                    return
                if name in _URL_ATTRIBUTES and value is not None:
                    compact = re.sub(r"[\x00-\x20\x7f]+", "", value)
                    if _JAVASCRIPT_SCHEME.match(compact):
                        self.violation = "javascript: URLs"
                        return

    safety = _SafetyParser(source)
    safety.feed(source)
    safety.close()
    if safety.violation is not None:
        raise InvalidRequest(f"Rich page content cannot contain {safety.violation}.")


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
        warnings.append(
            f"{anchor_loss} inline-comment anchor(s) would be detached."
        )
    for name, count in sorted(macro_losses.items()):
        warnings.append(f"{count} {name} macro(s) would be removed.")
    if attachment_loss:
        warnings.append(f"{attachment_loss} attachment reference(s) would be removed.")
    return anchor_loss > 0, warnings


def _rewrite_region(
    source: str,
    start: int,
    end: int,
    nodes: list[_MarkupNode],
    *,
    omitted: frozenset[int] = frozenset(),
) -> str:
    parts: list[str] = []
    cursor = start
    for node in nodes:
        if node.start < cursor or node.start >= end:
            continue
        parts.append(source[cursor : node.start])
        if id(node) in omitted:
            cursor = min(node.end, end)
            continue
        parts.append(_rewrite_node(source, node))
        cursor = min(node.end, end)
    parts.append(source[cursor:end])
    return "".join(parts)


def _rewrite_node(source: str, node: _MarkupNode) -> str:
    if node.tag == "pre":
        return _code_macro(source, node)
    if node.tag == "details":
        return _expand_macro(source, node)
    if node.tag in _VOID_TAGS and not node.self_closing:
        raw = source[node.start : node.start_tag_end]
        return re.sub(r"\s*>$", "/>", raw)
    if node.self_closing:
        return source[node.start : node.end]
    inner = _rewrite_region(source, node.start_tag_end, node.end_tag_start, node.children)
    return source[node.start : node.start_tag_end] + inner + source[node.end_tag_start : node.end]


def _code_macro(source: str, node: _MarkupNode) -> str:
    code_node = next((child for child in node.children if child.tag == "code"), None)
    language = None
    text_start = node.start_tag_end
    text_end = node.end_tag_start
    if code_node is not None:
        classes = code_node.attributes.get("class") or ""
        language_match = _LANGUAGE_CLASS.search(classes)
        language = language_match.group(1) if language_match else None
        text_start = code_node.start_tag_end
        text_end = code_node.end_tag_start
    text = _text_content(source[text_start:text_end])
    parameter = (
        f'<ac:parameter ac:name="language">{escape(language, quote=False)}</ac:parameter>'
        if language
        else ""
    )
    cdata = text.replace("]]>", "]]]]><![CDATA[>")
    return (
        '<ac:structured-macro ac:name="code">'
        f"{parameter}<ac:plain-text-body><![CDATA[{cdata}]]></ac:plain-text-body>"
        "</ac:structured-macro>"
    )


def _expand_macro(source: str, node: _MarkupNode) -> str:
    summary = next((child for child in node.children if child.tag == "summary"), None)
    title = None
    omitted: frozenset[int] = frozenset()
    if summary is not None:
        title = _text_content(source[summary.start_tag_end : summary.end_tag_start]).strip()
        omitted = frozenset({id(summary)})
    body = _rewrite_region(
        source,
        node.start_tag_end,
        node.end_tag_start,
        node.children,
        omitted=omitted,
    )
    parameter = f'<ac:parameter ac:name="title">{escape(title, quote=False)}</ac:parameter>' if title else ""
    return (
        '<ac:structured-macro ac:name="expand">'
        f"{parameter}<ac:rich-text-body>{body}</ac:rich-text-body>"
        "</ac:structured-macro>"
    )


def _text_content(fragment: str) -> str:
    collector = _TextCollector()
    collector.feed(fragment)
    collector.close()
    return "".join(collector.parts)


def _source_items(source: str) -> list[tuple[str, ...]]:
    parser = _MarkupParser(source)
    parser.feed(source)
    parser.close()
    parser.finish()
    items: list[tuple[str, ...]] = []
    pending = list(parser.root.children)
    while pending:
        node = pending.pop()
        if node.tag == "ac:inline-comment-marker":
            items.append(("inline-comment", node.attributes.get("ac:ref") or "unidentified"))
        elif node.tag == "ri:attachment":
            identity = node.attributes.get("ri:content-id") or node.attributes.get("ri:filename") or "unidentified"
            items.append(("attachment", identity))
        elif node.tag == "ac:structured-macro":
            name, identity = _macro_identity(node, source)
            items.append(("macro", name, identity))
        pending.extend(node.children)
    return items


def _macro_identity(node: _MarkupNode, source: str) -> tuple[str, str]:
    name = (node.attributes.get("ac:name") or node.attributes.get("name") or "unknown").casefold()
    parameters: list[tuple[str, str]] = []
    page_references: list[tuple[str, str]] = []
    pending = list(node.children)
    while pending:
        child = pending.pop()
        if child.tag == "ac:structured-macro":
            continue
        if child.tag == "ac:parameter":
            parameter_name = (child.attributes.get("ac:name") or child.attributes.get("name") or "").casefold()
            parameter_value = _text_content(source[child.start_tag_end : child.end_tag_start]).strip()
            parameters.append((parameter_name, parameter_value))
        elif child.tag == "ri:page":
            page_id = child.attributes.get("ri:content-id") or child.attributes.get("ri:page-id") or ""
            page_title = child.attributes.get("ri:content-title") or ""
            page_references.append((page_id, page_title))
        pending.extend(child.children)
    identity = json.dumps(
        [name, sorted(parameters), sorted(page_references)],
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return name, identity
