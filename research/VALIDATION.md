# Research validation

Date: 2026-10-07.

- All four HARs and all 50 ZIP members processed; input hashes match the original files after analysis.
- Python scripts parse successfully; all endpoint source IDs resolve to sources.json.
- All local Markdown links resolve.
- Catalog regeneration produces identical bytes for all eight generated inventory files.
- Offline redaction audit passed: 354 known captured secret/identity/title values checked; no match or email-shaped value found in the research artifacts.
- Catalog counts checked: 381 endpoint-shaped records, 292 with ZIP evidence, 46 with HAR request evidence; all 11 reviewed TSV paths have source-derived methods.
- No live Wiki request or content mutation was made. Auth success records are the user-supplied results from this conversation, not new executions.

The redaction audit is a best-effort check, not a guarantee against all possible sensitive values. Unknown dynamic URL assembly, plugin/API capability, token lifetime and persistent storage remain explicitly unproven.
