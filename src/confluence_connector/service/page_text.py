"""Convert Confluence page bodies into Markdown-like text with typed source references.

Confluence renders macros on the server, so the ``view`` body is already resolved HTML. This module only
adapts it for agents: a BeautifulSoup pre-pass replaces Confluence macros, images, attachment links and
mentions with text markers, and markdownify converts the remaining HTML. References are collected from
``view`` attributes and ``storage`` elements before conversion; rendered text is never a metadata
source. Generic HTML rendering follows the library; see requirements D3 for the accepted limitations.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urljoin, urlsplit

from bs4 import BeautifulSoup, Tag
from markdownify import MarkdownConverter

from confluence_connector.domain.models import PageAttachment, PageReference

_PANELS = {"info": "Info", "note": "Note", "warning": "Warning", "tip": "Tip", "panel": "Panel"}
_DROPPED_MACROS = frozenset({"toc", "numberedheadings", "numbered-headings"})
_INCLUDE_MACROS = frozenset({"include", "excerpt-include"})
_TEXT_MACROS = frozenset({"code", "expand", "status", "toc", "view-file", *_PANELS})
_PAGE_PATH = re.compile(r"/spaces/[^/]+/pages/(\d+)(?:/|$)")
_BRUSH = re.compile(r"brush:\s*([\w+#-]+)")
_FENCE = re.compile(r"^\s*(```|~~~)")


@dataclass(frozen=True)
class PageTextBlock:
    """One text block of a converted body; blocks are separated by blank lines in the full text."""

    text: str
    references: tuple[PageReference, ...] = ()
    separator_before: str = "\n\n"
    cursor_key: str | None = None


@dataclass(frozen=True)
class RenderedPageBody:
    """Converted body blocks and the references collected from source structure."""

    blocks: tuple[PageTextBlock, ...]
    references: tuple[PageReference, ...]


@dataclass(frozen=True)
class _Macro:
    name: str
    parameters: dict[str, str]
    body: str
    page_id: str | None = None
    page_title: str | None = None


def render_page_body(
    view_html: str,
    storage_html: str,
    *,
    page_id: str,
    attachments: list[PageAttachment],
    base_url: str | None = None,
    macros_from_storage: bool = True,
) -> RenderedPageBody:
    """Convert a page's ``view`` body to text blocks and collect typed references.

    The caller validates page references and parse scope once; this converter trusts that contract.
    With ``macros_from_storage`` false, macro parameters and storage references are not used.
    """
    macros = _storage_macros(storage_html) if macros_from_storage else []
    references = _storage_references(storage_html, page_id, macros) if macros_from_storage else []
    soup = BeautifulSoup(view_html, "html.parser")
    storage_files = {ref.attachment_id: ref.filename for ref in references if ref.kind == "attachment" and ref.attachment_id and ref.filename}
    references.extend(_prepare_view(soup, macros, page_id, attachments, base_url, storage_files))
    blocks = [PageTextBlock(text) for text in _split_blocks(_to_markdown(soup))]
    for macro in macros:
        if macro.name == "html":
            blocks.append(PageTextBlock("[Unrendered macro: html]", (_macro_reference(macro, page_id),)))
    return RenderedPageBody(blocks=tuple(blocks), references=tuple(_unique(references)))


def storage_top_level_blocks(storage_html: str) -> list[str]:
    """Split storage markup between top-level elements, returning verbatim source slices.

    Joining the slices reproduces the source exactly, so ``rich`` continuation never rewrites stored
    markup. Whitespace-only slices are kept with the element before them.
    """
    soup = BeautifulSoup(storage_html, "html.parser")
    line_starts = [0, *(match.end() for match in re.finditer(r"\n", storage_html))]
    starts = sorted(
        line_starts[tag.sourceline - 1] + tag.sourcepos
        for tag in soup.contents
        if isinstance(tag, Tag) and tag.sourceline is not None
    )
    cuts = [offset for offset in starts if offset > 0]
    slices: list[str] = []
    previous = 0
    for cut in [*cuts, len(storage_html)]:
        piece = storage_html[previous:cut]
        if piece.strip() or not slices:
            slices.append(piece)
        else:
            slices[-1] += piece
        previous = cut
    return [piece for piece in slices if piece]


# View pre-pass: Confluence structure becomes markers and references before generic conversion.


def _prepare_view(
    soup: BeautifulSoup,
    macros: list[_Macro],
    page_id: str,
    attachments: list[PageAttachment],
    base_url: str | None,
    storage_files: dict[str, str],
) -> list[PageReference]:
    for tag in soup.find_all(["script", "style"]):
        tag.decompose()
    references: list[PageReference] = []
    _replace_macros(soup, macros)
    by_id = {item.attachment_id: item for item in attachments}
    by_name = {item.filename.casefold(): item for item in attachments}
    for mention in soup.select(".user-mention, a.confluence-userlink"):
        mention.replace_with(f"@{_text(mention)}")
    for image in soup.find_all("img"):
        _replace_image(image, page_id, by_id, by_name, storage_files, references)
    for link in soup.find_all("a"):
        _replace_link(link, page_id, by_id, by_name, base_url, references)
    for item in soup.select("ul.inline-task-list > li"):
        item.insert(0, "[x] " if "checked" in (item.get("class") or []) else "[ ] ")
    for marker in soup.find_all(class_="inline-comment-marker"):
        marker.unwrap()
    return references


def _replace_macros(soup: BeautifulSoup, macros: list[_Macro]) -> None:
    """Replace macro wrappers in document order, pairing each with its storage source by name and order.

    Macros inside an included page come from another page's storage, so they take no local source.
    """
    by_name: dict[str, list[_Macro]] = {}
    for macro in macros:
        by_name.setdefault(macro.name, []).append(macro)
    used: dict[str, int] = {}
    nodes = soup.select("[data-macro-name]")
    included = {
        id(node)
        for node in nodes
        if node.find_parent(attrs={"data-macro-name": lambda value: value in _INCLUDE_MACROS}) is not None
    }
    for node in nodes:
        if node.decomposed or node.parent is None:
            continue
        name = node["data-macro-name"].strip().casefold()
        source = None
        if id(node) not in included:
            index = used.get(name, 0)
            used[name] = index + 1
            candidates = by_name.get(name, [])
            source = candidates[index] if index < len(candidates) else None
        if name == "view-file":
            node.unwrap()  # the attachment link inside becomes a [File: …] marker
            continue
        _replace_macro(soup, node, name, source)


def _replace_macro(soup: BeautifulSoup, node: Tag, name: str, source: _Macro | None) -> None:
    parameters = source.parameters if source else {}
    if name in _DROPPED_MACROS or name == "html":
        node.decompose()
    elif name == "code":
        _replace_code(soup, node, parameters, source)
    elif name in _PANELS:
        label = _PANELS[name]
        title = parameters.get("title", "")
        body = node.find(class_=re.compile(r"(confluence-information-macro-body|panelContent)")) or node
        _replace_with_container(soup, node, body, f"[{label}{': ' + title if title else ''}]", "blockquote")
    elif name == "expand":
        title = parameters.get("title") or _text(node.find(class_="expand-control-text")) or "untitled"
        body = node.find(class_="expand-content") or node
        _replace_with_container(soup, node, body, f"[Expand: {title}]", "div")
    elif name == "status":
        value = parameters.get("title") or _text(node)
        node.replace_with(f"[Status: {value}]" if value else "[Status]")
    elif name == "jira":
        key = parameters.get("key")
        node.replace_with(f"[Jira: {key}]" if key else "[Unrendered macro: jira]")
    elif name == "plantuml":
        _replace_with_code(soup, node, "[Diagram: plantuml]", source.body if source else "", "plantuml")
    elif name in _INCLUDE_MACROS:
        title = source.page_title if source and source.page_title else "unknown page"
        _replace_with_container(soup, node, node, f"[Included from: {title}]", "div")
    elif not _text(node):
        node.replace_with(f"[Unrendered macro: {name}]")
    else:
        node.unwrap()


def _replace_code(soup: BeautifulSoup, node: Tag, parameters: dict[str, str], source: _Macro | None) -> None:
    pre = node.find("pre")
    code = pre.get_text() if pre is not None else ""
    if not code.strip() and source is not None:
        code = source.body
    language = parameters.get("language", "")
    if not language and pre is not None:
        match = _BRUSH.search(pre.get("data-syntaxhighlighter-params") or "")
        language = match.group(1) if match else ""
    title = parameters.get("title", "")
    _replace_with_code(soup, node, f"[Code: {title}]" if title else None, code, language)


def _replace_with_code(soup: BeautifulSoup, node: Tag, label: str | None, code: str, language: str) -> None:
    holder = soup.new_tag("div")
    if label:
        paragraph = soup.new_tag("p")
        paragraph.string = label
        holder.append(paragraph)
    if code.strip():
        pre = soup.new_tag("pre", attrs={"data-language": language})
        pre.string = code.rstrip("\n")
        holder.append(pre)
    node.replace_with(holder)


def _replace_with_container(soup: BeautifulSoup, node: Tag, body: Tag, label: str, container: str) -> None:
    holder = soup.new_tag(container)
    paragraph = soup.new_tag("p")
    paragraph.string = label
    holder.append(paragraph)
    for child in list(body.children):
        holder.append(child.extract())
    node.replace_with(holder)


def _replace_image(
    image: Tag,
    page_id: str,
    by_id: dict[str, PageAttachment],
    by_name: dict[str, PageAttachment],
    storage_files: dict[str, str],
    references: list[PageReference],
) -> None:
    attachment_id = image.get("data-linked-resource-id") or image.get("data-attachment-id")
    filename = image.get("data-linked-resource-default-alias") or storage_files.get(attachment_id or "", "")
    attachment = by_id.get(attachment_id or "") or by_name.get(filename.casefold())
    if attachment is not None:
        filename = attachment.filename
    if image.get("class") and "emoticon" in image["class"]:
        image.replace_with(image.get("alt") or "")
        return
    alt = (image.get("alt") or "").strip()
    shown = filename or "image"
    image.replace_with(f"[Image: {shown} — {alt}]" if alt and alt != filename else f"[Image: {shown}]")
    references.append(
        PageReference(
            kind="image",
            attachment_id=attachment.attachment_id if attachment else attachment_id,
            filename=filename or None,
            media_type=attachment.media_type if attachment else None,
            size=attachment.file_size if attachment else None,
            owner_page_id=page_id or None,
            url=attachment.download_url if attachment else None,
            alt_text=alt or None,
        )
    )


def _replace_link(
    link: Tag,
    page_id: str,
    by_id: dict[str, PageAttachment],
    by_name: dict[str, PageAttachment],
    base_url: str | None,
    references: list[PageReference],
) -> None:
    resource_type = (link.get("data-linked-resource-type") or "").casefold()
    is_attachment = (
        resource_type == "attachment"
        or (resource_type != "page" and bool(link.get("data-linked-resource-default-alias")))
        or (resource_type != "page" and link.get("data-linked-resource-id") in by_id)
        or "/download/attachments/" in (link.get("href") or "")
    )
    if is_attachment:
        attachment_id = link.get("data-linked-resource-id") or ""
        filename = link.get("data-linked-resource-default-alias") or _text(link)
        attachment = by_id.get(attachment_id) or by_name.get(filename.casefold())
        link.replace_with(f"[File: {attachment.filename if attachment else filename}]")
        references.append(
            PageReference(
                kind="attachment",
                attachment_id=attachment.attachment_id if attachment else attachment_id or None,
                filename=attachment.filename if attachment else filename or None,
                media_type=attachment.media_type if attachment else None,
                size=attachment.file_size if attachment else None,
                owner_page_id=page_id or None,
                url=attachment.download_url if attachment else None,
            )
        )
        return
    href = link.get("href")
    if not href:
        link.unwrap()
        return
    absolute = _instance_url(href, base_url)
    if absolute is None:
        return  # an external link stays as written
    link["href"] = absolute
    target = link.get("data-linked-resource-id") if resource_type == "page" else None
    target = target or link.get("data-content-id") or _page_id_from_url(absolute)
    if target and target.isdecimal():
        references.append(
            PageReference(kind="page", page_id=target, title=_text(link) or None, owner_page_id=page_id or None, url=absolute)
        )


def _instance_url(href: str, base_url: str | None) -> str | None:
    """Return ``href`` as an absolute URL on the configured instance, or ``None`` for other hosts.

    Root-relative links gain the configured context path (for example ``/wiki``) when they lack it.
    Without a configured base URL, only relative links count as same-instance and stay relative.
    """
    parts = urlsplit(href)
    if base_url is None:
        return href if not parts.scheme and not parts.netloc else None
    base = urlsplit(base_url)
    context = base.path.rstrip("/")
    if not parts.scheme and not parts.netloc and href.startswith("/") and context:
        if parts.path != context and not parts.path.startswith(context + "/"):
            href = context + href
    absolute = urljoin(base_url.rstrip("/") + "/", href)
    target = urlsplit(absolute)
    same_host = (target.scheme, target.netloc.casefold()) == (base.scheme, base.netloc.casefold())
    in_context = not context or target.path == context or target.path.startswith(context + "/")
    return absolute if same_host and in_context else None


def _page_id_from_url(url: str) -> str | None:
    parts = urlsplit(url)
    match = _PAGE_PATH.search(parts.path)
    if match:
        return match.group(1)
    values = parse_qs(parts.query).get("pageId", [])
    return values[0] if len(values) == 1 else None


# Generic conversion: markdownify with the D3 table rule.


class _ConfluenceMarkdown(MarkdownConverter):
    """markdownify with the D3 table rule: repeated merged cells, minimal HTML for complex tables."""

    def convert_table(self, el, text, parent_tags):
        if any(cell.find(["table", "pre"]) for cell in el.find_all(["td", "th"])):
            return "\n\n" + _minimal_table_html(el) + "\n\n"
        grid = _table_grid(el)
        if not grid:
            return ""
        width = max(len(row) for row in grid)
        rows = [row + [""] * (width - len(row)) for row in grid]
        lines = ["| " + " | ".join(rows[0]) + " |", "|" + " --- |" * width]
        lines.extend("| " + " | ".join(row) + " |" for row in rows[1:])
        if any(cell.get("colspan") or cell.get("rowspan") for cell in el.find_all(["td", "th"])):
            lines.append("[Table: merged cells repeated]")
        return "\n\n" + "\n".join(lines) + "\n\n"


def _to_markdown(soup: BeautifulSoup) -> str:
    return _converter().convert_soup(soup)


def _converter() -> _ConfluenceMarkdown:
    return _ConfluenceMarkdown(
        heading_style="ATX",
        bullets="-",
        escape_underscores=False,
        escape_asterisks=False,
        escape_misc=False,
        code_language_callback=lambda element: element.get("data-language") or None,
    )


def _table_grid(table: Tag) -> list[list[str]]:
    """Lay a table out as rows of cell text, repeating merged cells into every covered position."""
    grid: list[list[str]] = []
    carried: dict[int, tuple[str, int]] = {}
    for row in (tr for tr in table.find_all("tr") if tr.find_parent("table") is table):
        cells = iter(row.find_all(["td", "th"], recursive=False))
        values: list[str] = []
        column = 0
        cell = next(cells, None)
        while cell is not None or column in carried:
            if column in carried:
                value, remaining = carried.pop(column)
                if remaining > 1:
                    carried[column] = (value, remaining - 1)
                values.append(value)
                column += 1
                continue
            value = _cell_text(cell)
            down = _span(cell.get("rowspan"))
            for _ in range(_span(cell.get("colspan"))):
                values.append(value)
                if down > 1:
                    carried[column] = (value, down - 1)
                column += 1
            cell = next(cells, None)
        grid.append(values)
    return grid


def _cell_text(cell: Tag) -> str:
    text = _converter().convert_soup(BeautifulSoup(cell.decode_contents(), "html.parser"))
    lines = [line.strip() for line in text.strip().splitlines() if line.strip()]
    return " <br> ".join(lines).replace("|", "\\|")


def _minimal_table_html(table: Tag) -> str:
    """Keep only table structure, spans and simple text formatting from a complex table."""
    kept_tags = {"table", "tr", "th", "td", "pre", "code", "p", "br", "ul", "ol", "li", "strong", "em"}
    copy = BeautifulSoup(str(table), "html.parser")
    for tag in copy.find_all(True):
        if tag.name not in kept_tags:
            tag.unwrap()
        else:
            tag.attrs = {key: value for key, value in tag.attrs.items() if key in {"colspan", "rowspan"}}
    return str(copy)


def _span(value: str | None) -> int:
    try:
        return max(1, min(int(value or 1), 1000))
    except ValueError:
        return 1


# Block splitting for budgets: blank lines outside fenced code separate blocks.


def _split_blocks(markdown: str) -> list[str]:
    blocks: list[str] = []
    current: list[str] = []
    fenced = False
    for line in markdown.splitlines():
        line = line.rstrip()
        if _FENCE.match(line):
            fenced = not fenced
        if not line and not fenced:
            if current:
                blocks.append("\n".join(current))
                current = []
            continue
        current.append(line)
    if current:
        blocks.append("\n".join(current))
    return blocks


# Storage source: macro parameters and references, read from storage elements.


def _storage_macros(storage_html: str) -> list[_Macro]:
    soup = BeautifulSoup(storage_html, "html.parser")
    macros: list[_Macro] = []
    for node in soup.find_all("ac:structured-macro"):
        name = (node.get("ac:name") or "").strip().casefold()
        if not name:
            continue
        own = [child for child in node.find_all(True) if child.find_parent("ac:structured-macro") is node]
        parameters = {
            (child.get("ac:name") or "").casefold(): child.get_text().strip()
            for child in own
            if child.name == "ac:parameter" and child.get("ac:name")
        }
        body = next((child.get_text() for child in own if child.name == "ac:plain-text-body"), "")
        page = next((child for child in own if child.name == "ri:page"), None)
        macros.append(
            _Macro(
                name=name,
                parameters=parameters,
                body=body.strip("\n"),
                page_id=(page.get("ri:content-id") or page.get("ri:page-id")) if page else None,
                page_title=page.get("ri:content-title") if page else None,
            )
        )
    return macros


def _storage_references(storage_html: str, page_id: str, macros: list[_Macro]) -> list[PageReference]:
    soup = BeautifulSoup(storage_html, "html.parser")
    references: list[PageReference] = []
    for node in soup.find_all(["ri:page", "ri:attachment", "ac:inline-comment-marker"]):
        if node.name == "ri:page" and (node.get("ri:content-id") or node.get("ri:content-title")):
            references.append(
                PageReference(
                    kind="page",
                    page_id=node.get("ri:content-id") or node.get("ri:page-id"),
                    title=node.get("ri:content-title"),
                    owner_page_id=page_id,
                )
            )
        elif node.name == "ri:attachment" and (node.get("ri:filename") or node.get("ri:content-id")):
            references.append(
                PageReference(
                    kind="attachment",
                    attachment_id=node.get("ri:content-id"),
                    filename=node.get("ri:filename"),
                    owner_page_id=page_id,
                )
            )
        elif node.name == "ac:inline-comment-marker":
            references.append(PageReference(kind="inline_comment", comment_id=node.get("ac:ref"), owner_page_id=page_id))
    references.extend(
        reference
        for macro in macros
        if macro.name not in _TEXT_MACROS
        for reference in [_macro_reference(macro, page_id)]
        if reference is not None
    )
    return references


def _macro_reference(macro: _Macro, page_id: str) -> PageReference | None:
    if macro.name in _INCLUDE_MACROS:
        return PageReference(kind="page", page_id=macro.page_id, title=macro.page_title, owner_page_id=page_id)
    if macro.name == "jira":
        key = macro.parameters.get("key")
        return PageReference(kind="jira", key=key, macro_name="jira") if key else None
    if macro.name in _TEXT_MACROS:
        return None
    return PageReference(kind="macro", macro_name=macro.name, owner_page_id=page_id or None)


def _text(node: Tag | None) -> str:
    return " ".join(node.get_text(" ").split()) if node is not None else ""


def _unique(references: list[PageReference]) -> list[PageReference]:
    seen: set[str] = set()
    result: list[PageReference] = []
    for reference in references:
        key = reference.model_dump_json()
        if key not in seen:
            seen.add(key)
            result.append(reference)
    return result
