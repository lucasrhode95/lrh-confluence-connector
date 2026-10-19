# Confluence connector requirements

Decisions recorded 2026-10-18. These describe the intended product, not completed implementation.
See [architecture](architecture.md) for the current proof of concept.

## 1. Product and ownership

A local MCP connector for one user's account on one Confluence Data Center instance. Multiple MCP
processes may run concurrently. No hosted service, web UI, scheduler or background crawler is requested.

The local connector accesses an organization-hosted Confluence deployment. Unlike the Microsoft 365
SaaS services used by the other connectors, its server URL, installation context and endpoint settings
are deployment-specific. Keep them in explicit private configuration rather than assuming a shared
vendor service address. This is the reason for the required configuration file.

The connector owns Confluence pages, drafts, trees, comments, attachment references and retrieval.
Agents own interpretation, multilingual query formulation, assessing documentation quality and
cross-checking evidence against other connectors or source code. External product links stay links;
handling a genuinely embedded component requires investigation of its source and ownership.

Prefer documented APIs. When they cannot provide a required capability, investigate undocumented
browser/plugin methods. Source presence alone is not proof of a usable endpoint.

## 2. Agent use cases

- Research an end-to-end operational process across Confluence and other products.
- Read a documentation subtree and propose a clearer operational handoff with fewer duplicates.
- Use documentation to explain the context of a conversation from another connector.
- Find a topic using a remembered contributor and date range rather than a known page title.
- Find, explain, summarize and compare pages; identify documented decisions and maintainers.

The wiki can contain wrong, incomplete, duplicated and outdated material. Retrieval must preserve
provenance and available metadata without treating a search hit or recent edit as proof of authority.

## 3. Search and navigation

Search by page title and textual content, across all accessible spaces or selected spaces. Include
archived content and personal spaces by default. Use a structured `scope` object for space, title,
labels, author/creator, contributor, creation/modification dates and parent/ancestor filters; do not
require agents to compose raw CQL. Preserve distinctions between filters that the server treats
differently. Exact field names, matching rules and pagination remain to be established.

Do not silently interpret ordinary query words as boolean operators or drop unsupported scope fields.
Attachment filenames and server-provided alt text are desirable searchable context where available.

Support direct page references and topic discovery, complete page reads, and page-tree navigation.
Assess live search before choosing a local index, ranking algorithm, traversal strategy or cache.
Search coverage, latency and exhaustive-search behavior are not settled.

## 4. Content reading and parsing

Support plain text and Confluence's enriched representation for reads and writes. HTML is preferred
if supported; the exact representation and plain-text update semantics need verification.

Text extraction preserves headings, tables, code blocks, lists, panels and other textual structure.
Investigate resolving textual macros/includes or authoritative server-provided alternative text.
Report unresolved content explicitly. Do not emit binary bodies or base64 strings into agent text.

A structured parsing/fetching scope controls optional content. Include page comments, inline comments
and resolved discussions by default. Place inline attachment/image markers at their source positions;
append other attachment references after the body and before comments. Include filenames and useful
source-provided descriptions. Markers let an agent request a particular attachment later.

Extract metadata and ordered content references from source fields/structure before rendering. Carry
them through typed models; never recover attachment identity or ownership from exported text markers.

## 5. Metadata, versions and exports

Default page reads show the latest published version. Include concise provenance: page identity/link,
displayed version, last-modified time, extraction time and total available versions where obtainable.
Do not assume a version number equals the number of retained versions.

Provide fuller metadata for one page or a batch. Historical reads and comparisons are later work;
batch exports should accept selected pages and versions without silently substituting another version.

Provide direct agent-readable results and a separate batch extraction operation. The latter accepts
pages/versions and a parsing/fetching scope, and produces one root ZIP with text files and optional
attachment folders, following the established Teams/Outlook export conventions. Exact schemas and
limits remain open. Byte-by-byte attachment deduplication is not required.

Attachments can be explicitly retrieved or included by export scope. File parsing, transcription,
OCR and multimedia interpretation are out of scope for now. Ordinary direct reads do not
automatically download attachments.

## 6. Page and draft management

Daily-use readiness requires search, full reads, tree access, page moves, and create/read/update/delete
operations for drafts and published pages. This is broader than the current read-only implementation.

Investigate new drafts separately from unpublished edits to existing pages. Establish publishing,
whole-body versus partial updates, version conflicts, child-page behavior, trash versus permanent
deletion and permissions before exposing mutations. These semantics are not settled by this document.
Do not implement write operations against real content merely to normalize the repository.

## 7. Authentication and concurrent use

Use a user-generated Confluence token without requiring administrator registration or assistance.
Existing PAT probe success is the starting evidence; exact lifecycle and policy must be verified.
Confluence authentication is separate from Microsoft OAuth; shared Microsoft sign-in is not assumed.

Provide persistent encrypted authentication and explicit logout, with no plaintext fallback. Before
logout, explain local token deletion and any need to generate a replacement when the original cannot
be recovered. Distinguish local removal from server revocation; revocation behavior remains open.

Coordinate shared authentication state and output files across processes. Preserve account boundaries
and ensure running processes respect logout/replacement before their next acquisition. Storage and
locking mechanisms have not been selected.

## 8. Reliability and acceptance

Report inaccessible pages, unsupported elements, failed downloads and partial coverage explicitly.
Retry reads a bounded number of times. Retry writes only when known not to have applied, honoring
`Retry-After` for 429/503. Never resend an uncertain write; report it for inspection. Keep error paths
short and readable, with no private content in operational logs.

Document potentially slow operations so agents can choose them knowingly; do not add a permission
prompt solely because a query is broad. Cancellation behavior still needs verification, especially
when a remote write may already have completed.

Use synthetic offline fixtures and iterative, explicitly authorized UI/API checks. Captures and UI
resource exports are temporary private investigation inputs outside the repository. Release evidence
must cover representative searches/reads, trees, draft/page operations and moves, authentication,
concurrent use and exports. Exact acceptance cases remain to be chosen.

Version comparisons, activity feeds, file interpretation and automated documentation-quality analysis
are follow-ups. The agent, rather than the connector, decides how to reconcile conflicting sources.
