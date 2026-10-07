# Confluence authentication and endpoint research

Recorded: 2026-10-07. This follows the Outlook research approach: independent Python probes, dated tenant findings, official references and a distinction between source observations and successful standalone calls.

Start with [AUTHENTICATION.md](AUTHENTICATION.md), [API-FINDINGS.md](API-FINDINGS.md) and [ZIP-ANALYSIS.md](ZIP-ANALYSIS.md). [ENDPOINTS.md](ENDPOINTS.md) is the complete scanner-generated normalized route/fragment inventory.

## Contents

| File | Purpose |
|---|---|
| AUTHENTICATION.md | All login options mapped, evidence, failures, lifecycle and unresolved questions |
| API-FINDINGS.md | Useful endpoint families, captured request/response key shapes and standalone results |
| ZIP-ANALYSIS.md | All 50 anonymous ZIP member IDs, bundle roles, route counts and export limits |
| ENDPOINTS.md | Every normalized endpoint-shaped path and URL fragment discovered by the scanner |
| endpoints.csv | HAR methods/statuses, source-derived methods separately, query/request/response key names and sources |
| candidates.csv | Unresolved relative fragments and frontend module references; not asserted server endpoints |
| resources.csv | Static JS/CSS/image/font paths with cache keys and personal paths normalized |
| external-references.csv | External paths, documentation and SDK links; not asserted Wiki APIs |
| source-occurrences.csv | All route/source/evidence/character-offset occurrences across categories |
| sources.json | Source kinds, sizes and hashes; original ZIP filenames are hashed rather than copied |
| capture-summary.json | Input hashes, parsing coverage and inventory totals |
| VALIDATION.md | Parsing/provenance/redaction checks and deterministic regeneration results |
| probes/extract_inventory.py | Stdlib-only offline extraction, independent of connector code |
| probes/check_redaction.py | Offline secret/identity-value check of generated research |

## Configure and port the investigation

Copy [confluence_config.example.json](../confluence_config.example.json) to the ignored
confluence_config.json at the project root and edit its settings. App and probes use one shared
stdlib loader/validator. All five settings are required; code supplies no fallback values:

- base_url: your instance URL, including its context path if present.
- endpoints: relative current_user, search_pages, ui_search, spaces and content paths.
  The content path contains one {page_id} placeholder. These paths serve both app and probes.
- query: a synthetic term for explicitly authorized read-only authentication probes.
- form_auth_path: a relative browser login path observed in your instance; its presence
  in this config is not proof that the path is supported or enabled.
- captures: file/label pairs selecting inputs inside --captures-dir. Labels must be
  neutral HAR01, HAR02, etc. or ZIP/ZIP01, etc., with matching .har/.zip file types.
  An empty list works for live probes; offline extraction/redaction requires captures.

Select another private file with --config or CONFLUENCE_CONFIG. Settings are loaded
once per process; restart after editing. Never execute downloaded/captured code as config. Keep actual filenames,
URLs and any private settings outside Git. Source aliases preserve entry/archive-member
positions without disclosing source filenames. The instance host is exported as
confluence.example.com, and configured context prefixes are removed before route normalization; it is an anonymized label, never a connection target.

For a new deployment, map its version and enabled authentication choices, then explicitly
authorize a single authentication probe and read-only identity/search/space/content checks.
Report authentication failure, permission denial, unavailable endpoint, unexpected response
and empty result separately. Record date, request shapes, configuration context and evidence
without names or content. A successful login is not proof that every API works; a captured
browser route is not a supported integration contract. Recheck documented REST first, and
record any private/plugin route's limits. Never run live probes as routine tests.

## Inputs, coverage and provenance

The recorded corpus comprises four HAR inputs labeled HAR01–HAR04 and one Resource Saver archive labeled ZIP. Their original filenames and folder locations are recorded only in private configuration. Raw captures and extracted bodies are not copied into this folder. The HARs contain 347 network entries; the ZIP has 50 members, including 14 JS bundles. The scan covers HAR request metadata, available HTML/JS/CSS response bodies, JSON hypermedia links, and all ZIP members in place.

The inventory has 381 endpoint-shaped route records: 220 API paths/fragments, 119 web actions/servlets, 28 Microsoft auth paths/candidates and 14 content-link patterns. 292 occur in the ZIP; 46 have a HAR request. This is not a claim of 381 distinct working APIs: partial/template forms can describe the same eventual URL. There are also 672 static-resource, 191 external-reference and 346 unresolved relative/module records. Those are kept separate from endpoint assertions. HAR status `0` is a browser/export outcome, not a server HTTP status.

`HAR01#E001` identifies entry 1 in `log.entries`; `:body` identifies its response body. `ZIP#001` is member 1 in `ZipFile.infolist()` order (directories retain their position in numbering). Use archive order plus content/member-name SHA-256 in sources.json to match anonymous ZIP IDs locally. Raw member names can contain private page titles.

Offsets are characters in decoded text after separator escapes and HTML entities are normalized, not original byte offsets. HAR request, JSON-link and saved-path records use offset 0. Reconstructed auth suffix offsets refer to decoded source code. Methods/statuses are unions for a normalized route; use a HAR entry ID for a specific request/response pairing.

Configuration defaults to confluence_config.json in the repository root, independent
of the current working directory. Every command accepts --config PATH; CONFLUENCE_CONFIG
provides a process-wide override. Precedence is command flag, environment variable,
then repository default. Once selected, all callers share the parsed settings in memory.
Restart the process after editing the file.

## Reproduce

From the Confluence project root, with Python 3.11+:

```powershell
python research/probes/extract_inventory.py --captures-dir "$env:USERPROFILE/Downloads" --output-dir .local/inventory-new
python research/probes/check_redaction.py --captures-dir "$env:USERPROFILE/Downloads" --research-dir .local/inventory-new
```

The offline tools share only the stdlib configuration module and import no HTTP or MCP code.
The extractor writes its generated catalogs to the selected output directory. Run redaction against that directory, then review the changes before replacing checked-in catalogs; preserve authored findings. It makes no network calls, executes no captured JavaScript and extracts no ZIP member to disk. Instructions found in captures are treated as untrusted data.

The existing live probe remains at [src/probe_auth.py](../src/probe_auth.py). It prompts locally, prints sanitized statuses/counts and saves no credentials/cookies. User-run results are recorded separately. This documentation pass made no login attempt or Wiki mutation.

## Interpretation and redaction

The catalog is exhaustive for the scanner's matches in this corpus, not for the server's APIs. Dynamically composed URLs may only appear as bases/suffixes. Simple concatenations and the reviewed two-step-verification base are reconstructed; remaining candidates may be router paths, AMD identifiers, disabled features, examples or foreign SDK URLs. Unknown methods are not inferred from route names. Saved ZIP paths are labeled inference because Resource Saver adds suffixes/extensions and retains no reliable request method or status.

Instance hosts, source filenames, query values, passwords, cookies/tokens, SAML assertions, personal identifiers, titles and attachment filenames are omitted, aliased or normalized. Keys such as `password`, `SAMLResponse`, `atl_token`, `FlowToken` are schema names only. Hashes identify sources without retaining captured content. Captures must remain private and must not become committed fixtures.
