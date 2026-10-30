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
from urllib.parse import parse_qs, urlsplit

from bs4 import BeautifulSoup, Tag
from markdownify import MarkdownConverter

from confluence_connector.domain.models import PageAttachment, PageReference
from confluence_connector.domain.urls import same_instance_link

_PANELS = {"info": "Info", "note": "Note", "warning": "Warning", "tip": "Tip", "panel": "Panel"}
_DROPPED_MACROS = frozenset({"toc", "numberedheadings", "numbered-headings"})
_INCLUDE_MACROS = frozenset({"include", "excerpt-include"})
_TEXT_MACROS = frozenset({"code", "expand", "status", "toc", "view-file", *_PANELS})
_PAGE_PATH = re.compile(r"/spaces/[^/]+/pages/(\d+)(?:/|$)")
_BRUSH = re.compile(r"brush:\s*([\w+#-]+)")
_FENCE = re.compile(r"^\s*(?:>\s*)*(`{3,}|~{3,})(.*)$")


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


@dataclass(frozen=True)
class ParsedStorage:
    """Macro facts, typed references and source slices extracted from one storage parse."""

    macros: tuple[_Macro, ...] = ()
    references: tuple[PageReference, ...] = ()
    top_level_blocks: tuple[str, ...] = ()


def render_page_body(
    view_html: str,
    storage_html: str,
    *,
    page_id: str,
    attachments: list[PageAttachment],
    base_url: str | None = None,
    macros_from_storage: bool = True,
    parsed_storage: ParsedStorage | None = None,
    convert_markdown: bool = True,
) -> RenderedPageBody:
    """Convert a page's ``view`` body to text blocks and collect typed references.

    The caller validates page references and parse scope once; this converter trusts that contract.
    With ``macros_from_storage`` false, macro parameters and storage references are not used. Callers
    reading one page can pass storage parsed for that page ID and body so macros, references and
    rich-body slices share a parse.
    """
    storage = parsed_storage
    if macros_from_storage and storage is None:
        storage = parse_storage(storage_html, page_id=page_id, include_metadata=True)
    macros = list(storage.macros) if macros_from_storage and storage is not None else []
    references = list(storage.references) if macros_from_storage and storage is not None else []
    soup = BeautifulSoup(view_html, "html.parser")
    storage_files = {ref.attachment_id: ref.filename for ref in references if ref.kind == "attachment" and ref.attachment_id and ref.filename}
    references.extend(_prepare_view(soup, macros, page_id, attachments, base_url, storage_files))
    blocks: list[PageTextBlock] = []
    if convert_markdown:
        blocks = [PageTextBlock(text) for text in _split_blocks(_to_markdown(soup))]
        for macro in macros:
            if macro.name == "html":
                blocks.append(PageTextBlock("[Unrendered macro: html]", (_macro_reference(macro, page_id),)))
    return RenderedPageBody(blocks=tuple(blocks), references=tuple(unique_references(references)))


def parse_storage(
    storage_html: str,
    *,
    page_id: str,
    include_metadata: bool = True,
    include_top_level_blocks: bool = False,
) -> ParsedStorage:
    """Extract requested facts and source slices from one parsed storage document."""
    soup = BeautifulSoup(storage_html, "html.parser")
    macros = tuple(_storage_macros(soup)) if include_metadata else ()
    references = (
        tuple(_storage_references(soup, page_id, list(macros))) if include_metadata else ()
    )
    blocks = tuple(_storage_top_level_blocks(storage_html, soup)) if include_top_level_blocks else ()
    return ParsedStorage(macros=macros, references=references, top_level_blocks=blocks)


def unique_references(references: list[PageReference]) -> list[PageReference]:
    seen: set[str] = set()
    result: list[PageReference] = []
    for reference in references:
        key = reference.model_dump_json()
        if key not in seen:
            seen.add(key)
            result.append(reference)
    return result


def _storage_top_level_blocks(storage_html: str, soup: BeautifulSoup) -> list[str]:
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
    for node in soup.find_all(["img", "a"]):
        if node.find_parent(attrs={"data-macro-name": lambda value: value in _INCLUDE_MACROS}):
            node["data-connector-included"] = "true"
    _replace_macros(soup, macros)
    by_id = {item.attachment_id: item for item in attachments}
    by_name = {item.filename.casefold(): item for item in attachments}
    for mention in soup.select(".user-mention, a.confluence-userlink"):
        mention.replace_with(f"@{_text(mention)}")
    for image in soup.find_all("img"):
        _replace_image(image, page_id, by_id, by_name, storage_files, base_url, references)
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
        if key:
            node.replace_with(f"[Jira: {key}]")
        elif _text(node):
            node.unwrap()
        else:
            node.replace_with("[Unrendered macro: jira]")
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
    base_url: str | None,
    references: list[PageReference],
) -> None:
    attachment_id = image.get("data-linked-resource-id") or image.get("data-attachment-id")
    filename = image.get("data-linked-resource-filename") or image.get("data-linked-resource-default-alias") or storage_files.get(attachment_id or "", "")
    source_url = image.get("src") or ""
    absolute = same_instance_link(base_url, source_url) if source_url else None
    owner = _attachment_owner(image, page_id, absolute)
    local = owner == page_id and (not source_url or absolute is not None)
    attachment = (by_id.get(attachment_id) if attachment_id else by_name.get(filename.casefold())) if local else None
    if source_url and absolute is None:
        attachment_id = None
        owner = None
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
            owner_page_id=owner,
            url=attachment.download_url if attachment else absolute or source_url or None,
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
    href = link.get("href")
    absolute = same_instance_link(base_url, href) if href else None
    if href and absolute is None:
        return
    resource_type = (link.get("data-linked-resource-type") or "").casefold()
    owner = _attachment_owner(link, page_id, absolute)
    is_attachment = (
        resource_type == "attachment"
        or (resource_type != "page" and bool(link.get("data-linked-resource-default-alias")))
        or (resource_type != "page" and link.get("data-linked-resource-id") in by_id)
        or "/download/attachments/" in (link.get("href") or "")
    )
    if is_attachment:
        attachment_id = link.get("data-linked-resource-id") or ""
        filename = link.get("data-linked-resource-default-alias") or _text(link)
        attachment = (by_id.get(attachment_id) if attachment_id else by_name.get(filename.casefold())) if owner == page_id else None
        link.replace_with(f"[File: {attachment.filename if attachment else filename}]")
        references.append(
            PageReference(
                kind="attachment",
                attachment_id=attachment.attachment_id if attachment else attachment_id or None,
                filename=attachment.filename if attachment else filename or None,
                media_type=attachment.media_type if attachment else None,
                size=attachment.file_size if attachment else None,
                owner_page_id=owner,
                url=attachment.download_url if attachment else absolute,
            )
        )
        return
    href = link.get("href")
    if not href:
        link.unwrap()
        return
    absolute = same_instance_link(base_url, href)
    if absolute is None:
        return  # an external link stays as written
    link["href"] = absolute
    target = link.get("data-linked-resource-id") if resource_type == "page" else None
    target = target or link.get("data-content-id") or link.get("data-page-id") or _page_id_from_url(absolute)
    if target and target.isdecimal():
        references.append(
            PageReference(kind="page", page_id=target, title=_text(link) or None, owner_page_id=page_id or None, url=absolute)
        )


def _attachment_owner(node: Tag, page_id: str, url: str | None) -> str | None:
    """Read attachment ownership from source attributes/URL; includes never inherit local ownership."""
    owner = node.get("data-linked-resource-container-id")
    if owner and owner.isdecimal():
        return owner
    if url:
        match = re.search(r"/download/attachments/(\d+)/", urlsplit(url).path)
        if match:
            return match.group(1)
    return None if node.get("data-connector-included") else page_id or None


def _page_id_from_url(url: str) -> str | None:
    parts = urlsplit(url)
    match = _PAGE_PATH.search(parts.path)
    if match:
        return match.group(1)
    values = parse_qs(parts.query).get("pageId", [])
    return values[0] if len(values) == 1 else None


# Generic conversion: markdownify with the D3 table rule.


class _ConfluenceMarkdown(MarkdownConverter):
    """Use library tables, retaining minimal HTML when Markdown would lose table structure."""

    def convert_pre(self, el, text, parent_tags):
        rendered = super().convert_pre(el, text, parent_tags)
        # Use a fence longer than any backtick run in the original code.
        runs = [len(match.group()) for match in re.finditer(r"`+", el.get_text())]
        length = max(3, max(runs, default=0) + 1)
        if length > 3:
            stripped = rendered.strip("\n")
            opening, body = stripped.split("\n", 1)
            body, _ = body.rsplit("\n", 1)
            return "\n\n" + "`" * length + opening[3:] + "\n" + body + "\n" + "`" * length + "\n\n"
        return rendered

    def convert_table(self, el, text, parent_tags):
        cells = el.find_all(["td", "th"])
        if any(cell.find(["table", "pre"]) or cell.get("colspan") or cell.get("rowspan") for cell in cells):
            return "\n\n" + _minimal_table_html(el) + "\n\n"
        return super().convert_table(el, text, parent_tags)


def _to_markdown(soup: BeautifulSoup) -> str:
    return _converter().convert_soup(soup)


def _converter() -> _ConfluenceMarkdown:
    return _ConfluenceMarkdown(
        table_infer_header=True,
        heading_style="ATX",
        bullets="-",
        escape_underscores=False,
        escape_asterisks=False,
        escape_misc=False,
        code_language_callback=lambda element: element.get("data-language") or None,
    )


def _minimal_table_html(table: Tag) -> str:
    """Keep table structure, code, formatting and passive link destinations without layout attributes."""
    kept_tags = {"table", "tr", "th", "td", "pre", "code", "p", "br", "ul", "ol", "li", "strong", "em", "b", "i", "del", "blockquote", "a"}
    copy = BeautifulSoup(str(table), "html.parser")
    for tag in copy.find_all(True):
        if tag.name not in kept_tags:
            tag.unwrap()
            continue
        allowed = {"colspan", "rowspan"} if tag.name in {"th", "td"} else set()
        if tag.name == "a":
            href = re.sub(r"[\x00-\x20\x7f]+", "", str(tag.get("href", "")))
            try:
                if urlsplit(href).scheme.casefold() in {"", "http", "https", "mailto"}:
                    allowed.add("href")
            except ValueError:
                pass  # Malformed destinations retain their visible label only.
        tag.attrs = {key: value for key, value in tag.attrs.items() if key in allowed}
    return str(copy)


# Block splitting for budgets: blank lines outside fenced code separate blocks.


def _split_blocks(markdown: str) -> list[str]:
    blocks: list[str] = []
    current: list[str] = []
    fence: str | None = None
    table_depth = 0
    for line in markdown.splitlines():
        line = line.rstrip()
        # Retained tables start their own line; table tags quoted in prose or inline code do not.
        if fence is None and (table_depth or line.startswith("<table")):
            for tag in re.finditer(r"</?table\b[^>]*>", line, re.IGNORECASE):
                table_depth += -1 if tag.group().startswith("</") else 1
        match = _FENCE.match(line) if table_depth == 0 else None
        if match:
            delimiter, suffix = match.groups()
            if fence is None:
                fence = delimiter
            elif delimiter[0] == fence[0] and len(delimiter) >= len(fence) and not suffix.strip():
                fence = None
        if not line and fence is None and table_depth == 0:
            if current:
                blocks.append("\n".join(current))
                current = []
            continue
        current.append(line)
    if current:
        blocks.append("\n".join(current))
    return blocks


# Storage source: macro parameters and references, read from storage elements.


def _storage_macros(soup: BeautifulSoup) -> list[_Macro]:
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


def _storage_references(
    soup: BeautifulSoup, page_id: str, macros: list[_Macro]
) -> list[PageReference]:
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
            owner_page = node.find("ri:page")
            owner = (owner_page.get("ri:content-id") or owner_page.get("ri:page-id")) if owner_page else page_id
            references.append(
                PageReference(
                    kind="attachment",
                    attachment_id=node.get("ri:content-id"),
                    filename=node.get("ri:filename"),
                    owner_page_id=owner,
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
