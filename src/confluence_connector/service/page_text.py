"""Convert Confluence's rendered view and storage source into typed, block-oriented page text."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html import escape
from html.parser import HTMLParser
from urllib.parse import SplitResult, urljoin, urlsplit, urlunsplit

from confluence_connector.domain.models import PageAttachment, PageReference

_VOID_TAGS = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
)
_WHITESPACE = re.compile(r"[\t\r\n\f\v ]+")
_PAGE_PATH = re.compile(r"/spaces/[^/]+/pages/(\d+)(?:/|$)")


@dataclass(frozen=True)
class PageTextBlock:
    """One indivisible text block and the source identities visible inside it."""

    text: str
    references: tuple[PageReference, ...] = ()


@dataclass(frozen=True)
class RenderedPageBody:
    """Converted page blocks and references collected from source structure."""

    blocks: tuple[PageTextBlock, ...]
    references: tuple[PageReference, ...]


@dataclass
class _Node:
    tag: str | None = None
    attrs: dict[str, str | None] = field(default_factory=dict)
    children: list[_Node | str] = field(default_factory=list)


@dataclass(frozen=True)
class _Macro:
    name: str
    parameters: dict[str, str]
    body: str
    page_id: str | None = None
    page_title: str | None = None
    jira_key: str | None = None


@dataclass
class _RenderContext:
    page_id: str
    base_url: str | None
    attachments_by_id: dict[str, PageAttachment]
    attachments_by_name: dict[str, PageAttachment]
    macros_by_name: dict[str, list[_Macro]]
    macro_indexes: dict[str, int] = field(default_factory=dict)
    pending_html_macros: list[_Macro] = field(default_factory=list)


class _TreeParser(HTMLParser):
    """Build a small source tree; rendered HTML is input data, never a source for page metadata."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node()
        self.stack = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._start(tag, attrs, self_closing=False)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._start(tag, attrs, self_closing=True)

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                return

    def handle_data(self, data: str) -> None:
        if data:
            self.stack[-1].children.append(data)

    def _start(
        self, tag: str, attrs: list[tuple[str, str | None]], *, self_closing: bool
    ) -> None:
        node = _Node(tag=tag.casefold(), attrs={key.casefold(): value for key, value in attrs})
        self.stack[-1].children.append(node)
        if not self_closing and node.tag not in _VOID_TAGS:
            self.stack.append(node)


def render_page_body(
    view_html: str,
    storage_html: str,
    *,
    page_id: str,
    attachments: list[PageAttachment],
    base_url: str | None = None,
    macros_from_storage: bool = True,
) -> RenderedPageBody:
    """Render the server's view body and gather identity from view/storage source structure.

    The caller validates page references and parse scope once. This converter trusts that contract;
    it never derives an attachment, comment, page or macro identity from the rendered marker text.
    """
    macros = _storage_macros(storage_html) if macros_from_storage else []
    source_references = _storage_references(storage_html, page_id, macros) if macros_from_storage else []
    attachment_by_id = {item.attachment_id: item for item in attachments}
    attachment_by_name = {item.filename.casefold(): item for item in attachments}
    macros_by_name: dict[str, list[_Macro]] = {}
    for macro in macros:
        macros_by_name.setdefault(macro.name, []).append(macro)
    context = _RenderContext(
        page_id=page_id,
        base_url=base_url,
        attachments_by_id=attachment_by_id,
        attachments_by_name=attachment_by_name,
        macros_by_name=macros_by_name,
        pending_html_macros=[macro for macro in macros if macro.name == "html"],
    )
    parser = _TreeParser()
    parser.feed(view_html)
    parser.close()

    blocks: list[PageTextBlock] = []
    for child in parser.root.children:
        blocks.extend(_render_blocks(child, context))
    for macro in context.pending_html_macros:
        reference = _macro_reference(macro, page_id)
        blocks.append(_block("[Unrendered macro: html]", (reference,) if reference else ()))
    blocks = _coalesce_blocks(blocks)
    references = _unique_references(
        [*source_references, *(reference for block in blocks for reference in block.references)]
    )
    return RenderedPageBody(blocks=tuple(blocks), references=tuple(references))


def html_to_text(value: str) -> str:
    """Convert a comment's rendered HTML into compact plain text."""
    parser = _TreeParser()
    parser.feed(value)
    parser.close()
    blocks = [block.text for child in parser.root.children for block in _render_blocks(child, _empty_context())]
    return "\n\n".join(block for block in blocks if block).strip()


def storage_top_level_blocks(storage_html: str) -> list[str]:
    """Split storage markup into raw, unchanged top-level element fragments for rich cursors.

    HTMLParser positions identify source spans while the stack identifies balanced top-level blocks.
    The source substrings are returned verbatim so rich reads never normalize stored markup.
    """
    parser = _StorageBlockParser(storage_html)
    parser.feed(storage_html)
    parser.close()
    return parser.blocks


def _render_blocks(value: _Node | str, context: _RenderContext) -> list[PageTextBlock]:
    if isinstance(value, str):
        text = _normalize_inline(value)
        return [PageTextBlock(text)] if text else []
    tag = value.tag or ""
    if tag == "script":
        return _render_html_macro_or_drop(value, context)
    if tag in {"style", "noscript", "template"}:
        return []
    if _generated_number(value):
        return []
    macro_name = _macro_name(value)
    if macro_name is not None:
        macro = _source_macro(macro_name, context)
        return _render_macro(value, macro_name, macro, context)
    html_macro = _match_html_macro(value, context)
    if html_macro is not None:
        reference = _macro_reference(html_macro, context.page_id)
        return [_block("[Unrendered macro: html]", (reference,) if reference else ())]
    if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
        text, refs = _inline(value.children, context)
        level = int(tag[1])
        return [_block(f"{'#' * level} {text.strip()}", refs)] if text.strip() else []
    if tag == "pre":
        text, refs = _inline(value.children, context, preserve_whitespace=True)
        language = _class_language(value.attrs.get("class"))
        return [_block(f"```{language}\n{text.rstrip()}\n```", refs)]
    if tag in {"ul", "ol"}:
        return _render_list(value, context, ordered=tag == "ol")
    if tag == "table":
        return _render_table(value, context)
    if tag == "blockquote":
        children = [block for child in value.children for block in _render_blocks(child, context)]
        return [_block("\n".join(f"> {line}" for line in block.text.splitlines()), block.references) for block in children]
    if tag == "hr":
        return [PageTextBlock("---")]
    if tag in {"a", "img"}:
        text, refs = _inline([value], context)
        return [_block(text.strip(), refs)] if text.strip() else []
    if tag == "p":
        text, refs = _inline(value.children, context)
        return [_block(text.strip(), refs)] if text.strip() else []
    if tag == "li":
        text, refs = _inline(value.children, context)
        return [_block(f"• {text.strip()}", refs)] if text.strip() else []

    child_blocks = [block for child in value.children for block in _render_blocks(child, context)]
    if child_blocks:
        return child_blocks
    text, refs = _inline(value.children, context)
    return [_block(text.strip(), refs)] if text.strip() else []


def _render_macro(node: _Node, name: str, macro: _Macro | None, context: _RenderContext) -> list[PageTextBlock]:
    reference = _macro_reference(macro, context.page_id) if macro is not None else None
    refs = (reference,) if reference is not None else ()
    if name == "html":
        if macro is not None and macro in context.pending_html_macros:
            context.pending_html_macros.remove(macro)
        return [_block("[Unrendered macro: html]", refs)]
    if name == "jira":
        key = macro.jira_key if macro is not None else None
        label = f"[Jira: {key}]" if key else "[Unrendered macro: jira]"
        return [_block(label, refs)]
    if name == "plantuml":
        source = macro.body.strip() if macro is not None else ""
        label = f"[Diagram: plantuml]\n```plantuml\n{source}\n```" if source else "[Diagram: plantuml]"
        return [_block(label, refs)]
    if name in {"include", "excerpt-include"}:
        title = macro.page_title if macro is not None else None
        marker = _block(f"[Included from: {title or 'unknown page'}]", refs)
        children = [block for child in node.children for block in _render_blocks(child, context)]
        return [marker, *children]
    if name == "toc" or name in {"numberedheadings", "numbered-headings"}:
        return []
    if name == "status":
        text, child_refs = _inline(node.children, context)
        value = macro.parameters.get("title", "").strip() if macro is not None else ""
        value = value or text.strip()
        return [_block(f"[Status: {value}]" if value else "[Status]", (*refs, *child_refs))]
    if name == "expand":
        title = macro.parameters.get("title", "") if macro is not None else ""
        children = [block for child in node.children for block in _render_blocks(child, context)]
        marker = _block(f"[Expand: {title or 'untitled'}]", refs)
        return [marker, *children]
    if name == "code":
        text, child_refs = _inline(node.children, context, preserve_whitespace=True)
        if not text.strip() and macro is not None:
            text = macro.body
        language = macro.parameters.get("language", "") if macro is not None else ""
        return [_block(f"```{language.strip()}\n{text.rstrip()}\n```", (*refs, *child_refs))]
    if name in {"info", "note", "warning", "tip", "panel"}:
        text, child_refs = _inline(node.children, context)
        heading = name.capitalize()
        if name == "panel" and macro is not None:
            heading = macro.parameters.get("title", "Panel") or "Panel"
        content = f"[{heading}] {text.strip()}".rstrip()
        return [_block("\n".join(f"> {line}" for line in content.splitlines()), (*refs, *child_refs))]
    text, child_refs = _inline(node.children, context)
    if not text.strip() or _looks_unrendered(text):
        return [_block(f"[Unrendered macro: {name}]", refs)]
    return [_block(text.strip(), (*refs, *child_refs))]


def _render_html_macro_or_drop(node: _Node, context: _RenderContext) -> list[PageTextBlock]:
    if context.pending_html_macros:
        macro = context.pending_html_macros.pop(0)
        reference = _macro_reference(macro, context.page_id)
        return [_block("[Unrendered macro: html]", (reference,) if reference else ())]
    return []


def _match_html_macro(node: _Node, context: _RenderContext) -> _Macro | None:
    visible = _normalize_inline(_node_text(node)).strip().casefold()
    if not visible:
        return None
    for macro in context.pending_html_macros:
        source_text = _normalize_inline(_plain_fragment(macro.body)).strip().casefold()
        if source_text and (visible == source_text or source_text in visible):
            context.pending_html_macros.remove(macro)
            return macro
    return None


def _plain_fragment(value: str) -> str:
    parser = _TreeParser()
    parser.feed(value)
    parser.close()
    return _node_text(parser.root) or value


def _render_list(node: _Node, context: _RenderContext, *, ordered: bool) -> list[PageTextBlock]:
    blocks: list[PageTextBlock] = []
    index = 1
    for child in node.children:
        if not isinstance(child, _Node) or child.tag != "li":
            blocks.extend(_render_blocks(child, context))
            continue
        nested_lists = [item for item in child.children if isinstance(item, _Node) and item.tag in {"ol", "ul"}]
        text, refs = _inline([item for item in child.children if item not in nested_lists], context)
        marker = f"{index}. " if ordered else "• "
        if text.strip():
            blocks.append(_block(marker + text.strip(), refs))
        for nested in nested_lists:
            nested_blocks = _render_list(nested, context, ordered=nested.tag == "ol")
            blocks.extend(
                _block("  " + line, item.references)
                for item in nested_blocks
                for line in item.text.splitlines()
            )
        index += 1
    return blocks


def _render_table(node: _Node, context: _RenderContext) -> list[PageTextBlock]:
    rows = [item for item in _walk(node) if item.tag == "tr"]
    if not rows:
        return []
    if any(_contains(cell, {"table", "pre"}) for row in rows for cell in _cells(row)):
        return [_block(_minimal_table(node), _references_in(node, context))]

    rendered_rows: list[dict[int, tuple[str, tuple[PageReference, ...], str, int]]] = []
    merged = False
    occupied: dict[int, tuple[int, str, tuple[PageReference, ...], str]] = {}
    for row in rows:
        cells: dict[int, tuple[str, tuple[PageReference, ...], str, int]] = {}
        next_occupied: dict[int, tuple[int, str, tuple[PageReference, ...], str]] = {}
        column = 0
        for occupied_column, (remaining, text, refs, tag) in occupied.items():
            cells[occupied_column] = (text, refs, tag, remaining)
            if remaining > 1:
                next_occupied[occupied_column] = (remaining - 1, text, refs, tag)
        for cell in _cells(row):
            colspan = _positive_attr(cell.attrs.get("colspan"), default=1)
            while any(index in cells for index in range(column, column + colspan)):
                column += 1
            text, refs = _inline(cell.children, context, cell_mode=True)
            rowspan = _positive_attr(cell.attrs.get("rowspan"), default=1)
            merged = merged or colspan > 1 or rowspan > 1
            for offset in range(colspan):
                cell_tag = "th" if cell.tag == "th" else "td"
                cells[column + offset] = (text, refs, cell_tag, rowspan)
                if rowspan > 1:
                    next_occupied[column + offset] = (rowspan - 1, text, refs, cell_tag)
            column += colspan
        occupied = next_occupied
        rendered_rows.append(cells)
    width = max(max(row, default=-1) for row in rendered_rows) + 1
    lines: list[PageTextBlock] = []
    if merged:
        lines.append(PageTextBlock("[Table: merged cells repeated]"))
    head_cells = rendered_rows[0]
    head_text = [head_cells[index][0] if index in head_cells else "" for index in range(width)]
    header_refs = tuple(reference for cell in head_cells.values() for reference in cell[1])
    lines.append(_block(_pipe_row(head_text), header_refs))
    lines.append(PageTextBlock(_pipe_row(["---"] * width)))
    for row in rendered_rows[1:]:
        cells = [row[index] for index in range(width) if index in row]
        row_text = [row[index][0] if index in row else "" for index in range(width)]
        lines.append(_block(_pipe_row(row_text), tuple(ref for cell in cells for ref in cell[1])))
    return lines


def _inline(
    values: list[_Node | str],
    context: _RenderContext,
    *,
    preserve_whitespace: bool = False,
    cell_mode: bool = False,
) -> tuple[str, tuple[PageReference, ...]]:
    parts: list[str] = []
    references: list[PageReference] = []
    for value in values:
        if isinstance(value, str):
            parts.append(value if preserve_whitespace else _normalize_inline(value))
            continue
        tag = value.tag or ""
        if tag in {"script", "style", "noscript", "template", "nav"}:
            if tag == "script":
                marker = _render_html_macro_or_drop(value, context)
                if marker:
                    parts.extend(block.text for block in marker)
                    references.extend(marker[0].references)
            continue
        macro_name = _macro_name(value)
        if macro_name is not None:
            blocks = _render_macro(value, macro_name, _source_macro(macro_name, context), context)
            parts.extend(block.text.replace("\n", " ") for block in blocks)
            references.extend(reference for block in blocks for reference in block.references)
            continue
        html_macro = _match_html_macro(value, context)
        if html_macro is not None:
            parts.append("[Unrendered macro: html]")
            reference = _macro_reference(html_macro, context.page_id)
            if reference is not None:
                references.append(reference)
            continue
        if tag == "img":
            marker, refs = _image_marker(value, context)
            parts.append(marker)
            references.extend(refs)
            continue
        if tag == "input" and (value.attrs.get("type") or "").casefold() == "checkbox":
            checked = value.attrs.get("checked") is not None
            parts.append("[x] " if checked else "[ ] ")
            continue
        if _generated_number(value):
            continue
        classes = set((value.attrs.get("class") or "").split())
        if "user-mention" in classes or "confluence-userlink" in classes:
            text, refs = _inline(value.children, context, preserve_whitespace=preserve_whitespace, cell_mode=cell_mode)
            parts.append("@" + text.strip().lstrip("@"))
            references.extend(refs)
            continue
        if tag == "br":
            parts.append("<br>" if cell_mode else "\n")
            continue
        if tag == "a":
            attachment_id = value.attrs.get("data-linked-resource-id") or value.attrs.get("data-attachment-id")
            if attachment_id:
                filename = (
                    value.attrs.get("data-linked-resource-default-alias")
                    or value.attrs.get("data-linked-resource-filename")
                    or "unknown"
                )
                attachment = context.attachments_by_id.get(attachment_id)
                if attachment is not None:
                    filename = attachment.filename
                parts.append(f"[File: {filename}]")
                references.append(
                    PageReference(
                        kind="attachment",
                        attachment_id=attachment.attachment_id if attachment else attachment_id,
                        filename=filename if filename != "unknown" else None,
                        media_type=attachment.media_type if attachment else None,
                        size=attachment.file_size if attachment else None,
                        owner_page_id=context.page_id,
                        url=attachment.download_url if attachment else value.attrs.get("href"),
                    )
                )
                continue
            text, refs = _inline(value.children, context, preserve_whitespace=preserve_whitespace, cell_mode=cell_mode)
            link_reference = _link_reference(value, context.page_id, context.base_url)
            references.extend(refs)
            if link_reference is not None:
                references.append(link_reference)
            href = value.attrs.get("href")
            if href and text.strip():
                parts.append(f"[{text.strip()}]({href})")
            else:
                parts.append(text)
            continue
        if tag == "p" and cell_mode and parts:
            parts.append("<br>")
        if tag == "code" and not preserve_whitespace:
            text, refs = _inline(value.children, context, cell_mode=cell_mode)
            parts.append(f"`{text.strip()}`" if text.strip() else "")
            references.extend(refs)
            continue
        if tag in {"ul", "ol"} and cell_mode:
            list_items = [item for item in value.children if isinstance(item, _Node) and item.tag == "li"]
            for index, item in enumerate(list_items):
                text, refs = _inline(item.children, context, cell_mode=True)
                if index:
                    parts.append("<br>")
                parts.append("• " + text.strip())
                references.extend(refs)
            continue
        text, refs = _inline(
            value.children,
            context,
            preserve_whitespace=preserve_whitespace,
            cell_mode=cell_mode,
        )
        references.extend(refs)
        if tag in {"strong", "b"} and text.strip():
            parts.append(f"**{text.strip()}**")
        elif tag in {"em", "i"} and text.strip():
            parts.append(f"*{text.strip()}*")
        elif tag == "s" and text.strip():
            parts.append(f"~~{text.strip()}~~")
        else:
            parts.append(text)
    value = "".join(parts)
    if not preserve_whitespace:
        value = _WHITESPACE.sub(" ", value).replace(" \n", "\n").replace("\n ", "\n")
    return value, _unique_references(references)


def _storage_macros(storage_html: str) -> list[_Macro]:
    parser = _TreeParser()
    parser.feed(storage_html)
    parser.close()
    macros: list[_Macro] = []
    for node in _walk(parser.root):
        if node.tag != "ac:structured-macro":
            continue
        name = (node.attrs.get("ac:name") or node.attrs.get("name") or "").strip().casefold()
        if not name:
            continue
        parameters: dict[str, str] = {}
        body = ""
        page_id = None
        page_title = None
        jira_key = None
        for child in _walk(node):
            if child.tag == "ac:parameter":
                parameter_name = (child.attrs.get("ac:name") or child.attrs.get("name") or "").casefold()
                if parameter_name:
                    parameters[parameter_name] = _node_text(child).strip()
            elif child.tag in {"ac:plain-text-body", "ac:rich-text-body"}:
                body = _node_text(child).strip()
            elif child.tag == "ri:page":
                page_id = child.attrs.get("ri:content-id") or child.attrs.get("ri:page-id")
                page_title = child.attrs.get("ri:content-title") or _node_text(child).strip() or None
        jira_key = parameters.get("key")
        macros.append(
            _Macro(
                name=name,
                parameters=parameters,
                body=body,
                page_id=page_id,
                page_title=page_title,
                jira_key=jira_key,
            )
        )
    return macros


def _storage_references(
    storage_html: str, page_id: str, macros: list[_Macro]
) -> list[PageReference]:
    parser = _TreeParser()
    parser.feed(storage_html)
    parser.close()
    references: list[PageReference] = []
    for node in _walk(parser.root):
        if node.tag == "ri:page":
            identifier = node.attrs.get("ri:content-id") or node.attrs.get("ri:page-id")
            title = node.attrs.get("ri:content-title")
            if identifier or title:
                references.append(PageReference(kind="page", page_id=identifier, title=title))
        elif node.tag == "ri:attachment":
            filename = node.attrs.get("ri:filename")
            identifier = node.attrs.get("ri:content-id")
            if filename or identifier:
                references.append(
                    PageReference(
                        kind="attachment",
                        attachment_id=identifier,
                        filename=filename,
                        owner_page_id=page_id,
                    )
                )
        elif node.tag == "ac:inline-comment-marker":
            marker_ref = node.attrs.get("ac:ref")
            references.append(
                PageReference(kind="inline_comment", comment_id=marker_ref, owner_page_id=page_id)
            )
    for macro in macros:
        reference = _macro_reference(macro, page_id)
        if reference is not None:
            references.append(reference)
    return _unique_references(references)


def _macro_reference(macro: _Macro | None, page_id: str) -> PageReference | None:
    if macro is None:
        return None
    if macro.name in {"include", "excerpt-include"}:
        return PageReference(kind="page", page_id=macro.page_id, title=macro.page_title)
    if macro.name == "jira" and macro.jira_key:
        return PageReference(kind="jira", key=macro.jira_key, macro_name=macro.name)
    if macro.name in {"plantuml", "html"}:
        return PageReference(kind="macro", macro_name=macro.name, owner_page_id=page_id)
    if macro.name not in {"code", "expand", "status", "info", "note", "warning", "tip", "panel", "toc"}:
        return PageReference(kind="macro", macro_name=macro.name, owner_page_id=page_id)
    return None


def _source_macro(name: str, context: _RenderContext) -> _Macro | None:
    values = context.macros_by_name.get(name.casefold(), [])
    index = context.macro_indexes.get(name.casefold(), 0)
    context.macro_indexes[name.casefold()] = index + 1
    return values[index] if index < len(values) else None


def _macro_name(node: _Node) -> str | None:
    name = node.attrs.get("data-macro-name")
    return name.strip().casefold() if name else None


def _image_marker(node: _Node, context: _RenderContext) -> tuple[str, tuple[PageReference, ...]]:
    attachment_id = node.attrs.get("data-linked-resource-id") or node.attrs.get("data-attachment-id")
    filename = (
        node.attrs.get("data-linked-resource-default-alias")
        or node.attrs.get("data-linked-resource-filename")
        or "unknown"
    )
    attachment = context.attachments_by_id.get(attachment_id or "") or context.attachments_by_name.get(filename.casefold())
    if attachment is not None:
        filename = attachment.filename
    alt = (node.attrs.get("alt") or "").strip()
    label = f"[Image: {filename} — {alt}]" if alt else f"[Image: {filename}]"
    reference = PageReference(
        kind="image",
        attachment_id=attachment.attachment_id if attachment else attachment_id,
        filename=filename if filename != "unknown" else None,
        media_type=attachment.media_type if attachment else None,
        size=attachment.file_size if attachment else None,
        owner_page_id=context.page_id,
        url=attachment.download_url if attachment else node.attrs.get("src"),
        alt_text=alt or None,
    )
    return label, (reference,)


def _link_reference(node: _Node, page_id: str, base_url: str | None) -> PageReference | None:
    href = node.attrs.get("href") or ""
    if href and not _same_instance_href(href, base_url):
        return None
    target = node.attrs.get("data-content-id") or node.attrs.get("data-page-id")
    if target is None:
        match = _PAGE_PATH.search(urlsplit(href).path)
        target = match.group(1) if match else None
    if target and target.isdecimal():
        return PageReference(kind="page", page_id=target, owner_page_id=page_id, url=href or None)
    return None


def _same_instance_href(href: str, base_url: str | None) -> bool:
    """Accept a linked page path only under the configured Confluence origin and context path."""
    if base_url is None:
        parsed = urlsplit(href)
        return not parsed.scheme and not parsed.netloc
    base = urlsplit(base_url)
    base_path = base.path.rstrip("/")
    if href.startswith("/") and base_path and not href.startswith(base_path + "/"):
        target = urlunsplit((base.scheme, base.netloc, base_path + href, "", ""))
    else:
        target = urljoin(base_url.rstrip("/") + "/", href)
    linked = urlsplit(target)
    try:
        same_origin = (
            base.scheme.casefold() == linked.scheme.casefold()
            and (base.hostname or "").casefold() == (linked.hostname or "").casefold()
            and _url_port(base) == _url_port(linked)
        )
    except ValueError:
        return False
    in_context = not base_path or linked.path == base_path or linked.path.startswith(base_path + "/")
    return same_origin and in_context and linked.username is None and linked.password is None


def _url_port(parts: SplitResult) -> int:
    if parts.port is not None:
        return parts.port
    return 443 if parts.scheme.casefold() == "https" else 80


def _minimal_table(node: _Node) -> str:
    rows = []
    for row in _table_rows(node):
        cells = []
        for cell in _cells(row):
            tag = "th" if cell.tag == "th" else "td"
            attrs = "".join(
                f' {name}="{escape(str(cell.attrs.get(name)), quote=True)}"'
                for name in ("colspan", "rowspan")
                if cell.attrs.get(name)
            )
            cells.append(f"<{tag}{attrs}>{_minimal_cell_content(cell)}</{tag}>")
        rows.append("<tr>" + "".join(cells) + "</tr>")
    return "<table>" + "".join(rows) + "</table>"


def _table_rows(node: _Node) -> list[_Node]:
    rows: list[_Node] = []
    for child in node.children:
        if not isinstance(child, _Node):
            continue
        if child.tag == "tr":
            rows.append(child)
        elif child.tag in {"thead", "tbody", "tfoot"}:
            rows.extend(
                item for item in child.children if isinstance(item, _Node) and item.tag == "tr"
            )
    return rows


def _minimal_cell_content(node: _Node) -> str:
    parts: list[str] = []
    for child in node.children:
        if isinstance(child, str):
            parts.append(escape(child))
        elif child.tag == "table":
            parts.append(_minimal_table(child))
        elif child.tag not in {"script", "style", "noscript"}:
            parts.append(_minimal_cell_content(child))
    return "".join(parts).strip()


def _references_in(node: _Node, context: _RenderContext) -> tuple[PageReference, ...]:
    _, references = _inline([node], context)
    return references


def _pipe_row(cells: list[str]) -> str:
    values = [cell.strip().replace("|", "\\|") for cell in cells]
    return "| " + " | ".join(values) + " |"


def _cells(row: _Node) -> list[_Node]:
    return [child for child in row.children if isinstance(child, _Node) and child.tag in {"td", "th"}]


def _contains(node: _Node, names: set[str]) -> bool:
    return any(item.tag in names for item in _walk(node) if item is not node)


def _walk(node: _Node):
    yield node
    for child in node.children:
        if isinstance(child, _Node):
            yield from _walk(child)


def _node_text(node: _Node) -> str:
    parts: list[str] = []
    for child in node.children:
        if isinstance(child, str):
            parts.append(child)
        elif child.tag not in {"script", "style"}:
            parts.append(_node_text(child))
    return "".join(parts)


def _normalize_inline(value: str) -> str:
    return _WHITESPACE.sub(" ", value.replace("\xa0", " "))


def _class_language(value: str | None) -> str:
    match = re.search(r"(?:^|\s)language-([a-zA-Z0-9_+.-]+)", value or "")
    return match.group(1) if match else ""


def _looks_unrendered(value: str) -> bool:
    lowered = value.casefold()
    return any(phrase in lowered for phrase in ("unable to render", "error rendering", "unknown macro"))


def _generated_number(node: _Node) -> bool:
    classes = set((node.attrs.get("class") or "").casefold().split())
    return bool(
        classes.intersection({"confluence-numbering", "generated-number", "auto-number", "numbered-heading"})
        or any("heading" in name and ("number" in name or "numbering" in name) for name in classes)
    )


def _positive_attr(value: str | None, *, default: int) -> int:
    if value and value.isdecimal() and int(value) > 0:
        return int(value)
    return default


def _block(text: str, references: tuple[PageReference, ...] | list[PageReference]) -> PageTextBlock:
    return PageTextBlock(text=text, references=tuple(_unique_references(list(references))))


def _coalesce_blocks(blocks: list[PageTextBlock]) -> list[PageTextBlock]:
    result: list[PageTextBlock] = []
    for block in blocks:
        text = block.text.strip()
        if not text:
            continue
        if result and result[-1].text == text:
            result[-1] = _block(text, (*result[-1].references, *block.references))
        else:
            result.append(_block(text, block.references))
    return result


def _unique_references(values: list[PageReference]) -> list[PageReference]:
    result: list[PageReference] = []
    seen: set[tuple[tuple[str, str], ...]] = set()
    for value in values:
        key = tuple(sorted((name, str(item)) for name, item in value.model_dump(exclude_none=True).items()))
        if key not in seen:
            seen.add(key)
            result.append(value)
    return result


def _empty_context() -> _RenderContext:
    return _RenderContext("", None, {}, {}, {})


class _StorageBlockParser(HTMLParser):
    """Track raw top-level element spans while preserving the storage source byte-for-character."""

    def __init__(self, source: str) -> None:
        super().__init__(convert_charrefs=False)
        self.source = source
        self.blocks: list[str] = []
        self.stack: list[str] = []
        self.top_start: int | None = None
        self.top_text_start: int | None = None
        self.pending_prefix = ""
        self.position = _source_positions(source)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._open(tag, self.get_starttag_text() or "", self_closing=False)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._open(tag, self.get_starttag_text() or "", self_closing=True)

    def handle_endtag(self, tag: str) -> None:
        if not self.stack:
            return
        index = next((i for i in range(len(self.stack) - 1, -1, -1) if self.stack[i] == tag), None)
        if index is None:
            return
        del self.stack[index:]
        if not self.stack and self.top_start is not None:
            start = self.top_start
            end_start = self._offset(self.getpos())
            end = self.source.find(">", end_start)
            if end >= 0:
                self.blocks.append(self.source[start : end + 1])
            self.top_start = None

    def handle_data(self, data: str) -> None:
        if not self.stack and data and self.top_text_start is None:
            self.top_text_start = self._offset(self.getpos())

    def handle_entityref(self, name: str) -> None:
        self._append_reference()

    def handle_charref(self, name: str) -> None:
        self._append_reference()

    def handle_comment(self, data: str) -> None:
        self._append_construct("-->")

    def handle_decl(self, decl: str) -> None:
        self._append_construct(">")

    def unknown_decl(self, data: str) -> None:
        self._append_construct("]]>")

    def handle_pi(self, data: str) -> None:
        self._append_construct("?>")

    def close(self) -> None:
        super().close()
        if self.top_start is not None:
            self.blocks.append(self.source[self.top_start :])
            self.top_start = None
            self.stack.clear()
        else:
            self._flush_top_text(len(self.source))
        if self.pending_prefix:
            if self.blocks:
                self.blocks[-1] += self.pending_prefix
            else:
                self.blocks.append(self.pending_prefix)
            self.pending_prefix = ""

    def _open(self, tag: str, source_tag: str, *, self_closing: bool) -> None:
        if not self.stack:
            start = self._offset(self.getpos())
            self._flush_top_text(start)
            self.top_start = start - len(self.pending_prefix)
            self.pending_prefix = ""
        if self_closing or tag.casefold() in _VOID_TAGS:
            if not self.stack and self.top_start is not None:
                end = self._offset(self.getpos()) + len(source_tag)
                self.blocks.append(self.source[self.top_start : end])
                self.top_start = None
            return
        self.stack.append(tag)

    def _offset(self, position: tuple[int, int]) -> int:
        return self.position[position[0] - 1] + position[1]

    def _append_construct(self, terminator: str) -> None:
        if self.stack:
            return
        start = self._offset(self.getpos())
        self._flush_top_text(start)
        start -= len(self.pending_prefix)
        self.pending_prefix = ""
        end = self.source.find(terminator, start)
        if end >= 0:
            self.blocks.append(self.source[start : end + len(terminator)])

    def _append_reference(self) -> None:
        if not self.stack and self.top_text_start is None:
            self.top_text_start = self._offset(self.getpos())

    def _flush_top_text(self, end: int) -> None:
        if self.top_text_start is None:
            return
        text = self.source[self.top_text_start : end]
        if text.isspace():
            if self.blocks:
                self.blocks[-1] += text
            else:
                self.pending_prefix += text
        else:
            self.blocks.append(self.pending_prefix + text)
            self.pending_prefix = ""
        self.top_text_start = None


def _source_positions(source: str) -> list[int]:
    starts = [0]
    for index, char in enumerate(source):
        if char == "\n":
            starts.append(index + 1)
    return starts
