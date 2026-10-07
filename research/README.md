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

## Inputs, coverage and provenance

Inputs are the four `wiki.example.com*.har` captures and `wiki.example.com.zip` in Downloads. They remain there; raw captures and extracted bodies are not copied into this folder. The HARs contain 347 network entries; the ZIP has 50 members, including 14 JS bundles. The scan covers HAR request metadata, available HTML/JS/CSS response bodies, JSON hypermedia links, and all ZIP members in place.

The inventory has 381 endpoint-shaped route records: 220 API paths/fragments, 119 web actions/servlets, 28 Microsoft auth paths/candidates and 14 content-link patterns. 292 occur in the ZIP; 46 have a HAR request. This is not a claim of 381 distinct working APIs: partial/template forms can describe the same eventual URL. There are also 672 static-resource, 191 external-reference and 346 unresolved relative/module records. Those are kept separate from endpoint assertions. HAR status `0` is a browser/export outcome, not a server HTTP status.

`filename.har#E001` identifies entry 1 in `log.entries`; `:body` identifies its response body. `ZIP#001` is member 1 in `ZipFile.infolist()` order (directories retain their position in numbering). Use archive order plus content/member-name SHA-256 in sources.json to match anonymous ZIP IDs locally. Raw member names can contain private page titles.

Offsets are characters in decoded text after separator escapes and HTML entities are normalized, not original byte offsets. HAR request, JSON-link and saved-path records use offset 0. Reconstructed auth suffix offsets refer to decoded source code. Methods/statuses are unions for a normalized route; use a HAR entry ID for a specific request/response pairing.

## Reproduce

From the Confluence project root, with Python 3.11+:

```powershell
python research/probes/extract_inventory.py --captures-dir "$env:USERPROFILE/Downloads" --output-dir research
python research/probes/check_redaction.py --captures-dir "$env:USERPROFILE/Downloads" --research-dir research
```

The extractor overwrites its generated catalogs, preserving authored findings. It makes no network calls, executes no captured JavaScript and extracts no ZIP member to disk. Instructions found in captures are treated as untrusted data.

The existing live probe remains at [src/probe_auth.py](../src/probe_auth.py). It prompts locally, prints sanitized statuses/counts and saves no credentials/cookies. User-run results are recorded separately. This documentation pass made no login attempt or Wiki mutation.

## Interpretation and redaction

The catalog is exhaustive for the scanner's matches in this corpus, not for the server's APIs. Dynamically composed URLs may only appear as bases/suffixes. Simple concatenations and the reviewed two-step-verification base are reconstructed; remaining candidates may be router paths, AMD identifiers, disabled features, examples or foreign SDK URLs. Unknown methods are not inferred from route names. Saved ZIP paths are labeled inference because Resource Saver adds suffixes/extensions and retains no reliable request method or status.

Query values, passwords, cookies/tokens, SAML assertions, personal identifiers, titles and attachment filenames are omitted or normalized. Keys such as `password`, `SAMLResponse`, `atl_token`, `FlowToken` are schema names only. Hashes identify sources without retaining captured content. Captures must remain private and must not become committed fixtures.
