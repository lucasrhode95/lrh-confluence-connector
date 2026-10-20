"""Synthetic fixture names and bodies shared by the explicit live setup and offline checks."""

from __future__ import annotations

import base64
import re
import secrets
from dataclasses import dataclass
from html import escape

_RUN_ID = re.compile(r"^[a-f0-9]{8,16}$")
_PIXEL_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/"
    "8XcAAAAASUVORK5CYII="
)


@dataclass(frozen=True)
class AcceptanceFixture:
    """Names and synthetic markers for one isolated acceptance run."""

    run_id: str

    @property
    def root_title(self) -> str:
        return f"Connector Lab Acceptance [{self.run_id}] — Search & Structure!"

    @property
    def child_one_title(self) -> str:
        return f"Connector Lab Child A [{self.run_id}]"

    @property
    def child_two_title(self) -> str:
        return f"Connector Lab Child B [{self.run_id}]"

    @property
    def grandchild_title(self) -> str:
        return f"Connector Lab Grandchild [{self.run_id}]"

    @property
    def marker(self) -> str:
        return f"orchidbeacon{self.run_id}"

    @property
    def code_identifier(self) -> str:
        return f"beacon_x.y_{self.run_id}"

    @property
    def attachment_marker(self) -> str:
        return f"cedarattachment{self.run_id}"

    @property
    def label(self) -> str:
        return f"connector_lab_acceptance_{self.run_id}"

    @property
    def text_filename(self) -> str:
        return f"connector-lab-notes-{self.run_id}.txt"

    @property
    def image_filename(self) -> str:
        return f"connector-lab-pixel-{self.run_id}.png"


def new_fixture(run_id: str | None = None) -> AcceptanceFixture:
    """Validate an optional operator-supplied run ID or create a synthetic, collision-resistant one."""
    value = secrets.token_hex(5) if run_id is None else run_id
    if not isinstance(value, str) or not _RUN_ID.fullmatch(value):
        raise ValueError("run_id must contain 8 to 16 lowercase hexadecimal characters.")
    return AcceptanceFixture(value)


def root_storage(fixture: AcceptanceFixture, *, revision: int = 1) -> str:
    """Return a synthetic rich body containing every required macro and structure shape."""
    child_title = escape(fixture.child_one_title, quote=True)
    text_name = escape(fixture.text_filename, quote=True)
    image_name = escape(fixture.image_filename, quote=True)
    return f"""<h1>Acceptance {fixture.run_id}</h1>
<p>{fixture.marker} punctuated identifiers, café and coração; revision {revision}.</p>
<p>Unique search body: violetquartz{fixture.run_id}.</p>
<ac:structured-macro ac:name="code"><ac:parameter ac:name="language">python</ac:parameter><ac:plain-text-body><![CDATA[{fixture.code_identifier} = 'synthetic']]></ac:plain-text-body></ac:structured-macro>
<ac:structured-macro ac:name="plantuml"><ac:plain-text-body><![CDATA[@startuml\nA -> B: synthetic {fixture.run_id}\n@enduml]]></ac:plain-text-body></ac:structured-macro>
<ac:structured-macro ac:name="jira"><ac:parameter ac:name="key">SYN-{fixture.run_id.upper()}</ac:parameter><ac:rich-text-body><p>synthetic issue</p></ac:rich-text-body></ac:structured-macro>
<ac:structured-macro ac:name="html"><ac:plain-text-body><![CDATA[<p>htmlmacro{fixture.run_id}</p>]]></ac:plain-text-body></ac:structured-macro>
<ac:structured-macro ac:name="include"><ac:parameter ac:name="page"><ri:page ri:content-title="{child_title}" /></ac:parameter></ac:structured-macro>
<ac:structured-macro ac:name="excerpt-include"><ac:parameter ac:name="default">{child_title}</ac:parameter><ac:rich-text-body><ri:page ri:content-title="{child_title}" /></ac:rich-text-body></ac:structured-macro>
<ac:structured-macro ac:name="view-file"><ac:parameter ac:name="name">{text_name}</ac:parameter><ac:rich-text-body><ri:attachment ri:filename="{text_name}" /></ac:rich-text-body></ac:structured-macro>
<ac:structured-macro ac:name="info"><ac:rich-text-body><p>infomarker{fixture.run_id}</p></ac:rich-text-body></ac:structured-macro>
<ac:structured-macro ac:name="panel"><ac:rich-text-body><p>panelmarker{fixture.run_id}</p></ac:rich-text-body></ac:structured-macro>
<ac:structured-macro ac:name="expand"><ac:parameter ac:name="title">Synthetic details</ac:parameter><ac:rich-text-body><p>expandmarker{fixture.run_id}</p></ac:rich-text-body></ac:structured-macro>
<ac:structured-macro ac:name="status"><ac:parameter ac:name="title">READY</ac:parameter></ac:structured-macro>
<ac:structured-macro ac:name="toc" />
<ac:structured-macro ac:name="acceptance-unknown"><ac:rich-text-body><p>unknownmacro{fixture.run_id}</p></ac:rich-text-body></ac:structured-macro>
<p><ac:image ac:alt="synthetic one-pixel image"><ri:attachment ri:filename="{image_name}" /></ac:image></p>
<table><tbody><tr><th rowspan="2">Merged header</th><td>First cell</td></tr><tr><td>Second cell</td></tr></tbody></table>
<table><tbody><tr><td>Outer cell<table><tbody><tr><td>Nested table cell</td></tr></tbody></table></td></tr></tbody></table>"""


def child_text(fixture: AcceptanceFixture, name: str) -> str:
    """Return a unique plain-text body for a fixture page role."""
    return (
        f"{fixture.marker} {name} childterm{fixture.run_id} "
        f"{fixture.attachment_marker} {fixture.code_identifier}"
    )


def attachment_text(fixture: AcceptanceFixture) -> bytes:
    """Return a small synthetic UTF-8 attachment whose content is searchable."""
    return f"{fixture.attachment_marker}\nsynthetic attachment for {fixture.run_id}\n".encode("utf-8")


def image_png() -> bytes:
    """Return a fixed synthetic one-pixel PNG for attachment and inline-image acceptance."""
    return _PIXEL_PNG
