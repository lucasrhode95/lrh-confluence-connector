# Research validation

Date: 2026-10-07.

- All four HARs and all 50 ZIP members processed; input hashes match the original files after analysis.
- 24 synthetic offline tests passed, including default file location, all-command overrides, shared cached settings, configured app paths, context-path redaction and real-source provenance.
- Python sources compile and unused-import checks pass; all endpoint source IDs resolve to sources.json.
- All local Markdown links resolve.
- Catalog regeneration with the shared JSON settings produces identical bytes for all eight generated inventory files. Source hashes are unchanged; capture and host labels are anonymized.
- Offline redaction audit passed: 363 known captured secret/identity/title/domain/filename values checked; no match or email-shaped value found in the research artifacts.
- Catalog counts checked: 381 endpoint-shaped records, 292 with ZIP evidence, 46 with HAR request evidence; all 11 reviewed TSV paths have source-derived methods.
- The local confluence_config.json contains deployment-specific values and is Gitignored; only the generic example is published.
- No live Wiki request or content mutation was made. Auth success records are the user-supplied results from this conversation, not new executions.

The redaction audit is a best-effort check, not a guarantee against all possible sensitive values. Unknown dynamic URL assembly, plugin/API capability, token lifetime and persistent storage remain explicitly unproven.
