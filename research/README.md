# Confluence research toolkit

Manual probes and investigation procedures for a configured Confluence deployment. Dated results
belong in [docs/api-research.md](../docs/api-research.md), not beside the scripts. Requirements and
implementation decisions belong in [docs/](../docs/README.md).

## Configuration and commands

The app and probe share the stdlib-only settings loader. Copy `confluence_config.example.json` to
the ignored `confluence_config.json` and configure the deployment endpoints and a synthetic query.
No live target, query or credential is embedded as a fallback.

From the repository root, explicitly select your private file:

```powershell
uv run python research/probe_auth.py --config ./confluence_config.json --mode pat
```

Other modes are `anonymous`, `basic`, `form` and `oauth`; run one per invocation. The probe also
accepts `--base-url` and `--query` overrides. PAT/OAuth secrets are prompted securely. Basic/form
prompt for username and hidden password. OAuth mode requires an already obtained token and performs
no registration or grant flow. These modes are research options, not selected product auth backends.

The probe checks current-user identity, both searches, spaces and one content result. It reports
statuses, response keys and counts, without page content, names, identifiers or secrets. It does
not store credentials. Settings precedence is `--config`, `CONFLUENCE_CONFIG`, then the required
repository-root file; missing/invalid settings fail before prompting or making requests.

Form mode makes one transient login attempt against the configured undocumented browser endpoint.
`--remember-me` additionally requests that cookie behavior; a fresh in-memory client tests immediate
cookie replay. Nothing persists and no login attempt is retried. Form mode requires authorization
for that login; it is not part of routine checks or the product authentication path.

## Investigation rules

- Run live reads/writes only with explicit authorization for those actions and configured targets.
- Prefer documented APIs. Investigate private routes when a requirement needs them; distinguish
  browser requests, source-only leads, official documentation and standalone probe success.
- Keep useful routes/contracts, dated outcomes and limitations. Link to vendor documentation rather
  than copying its public reference material. Here the relevant vendor is Atlassian.
- Use the [UI runbook](ui-runbook.md) for controlled browser evidence. HARs and Save All Resources
  exports stay outside repositories, contain private data, and are treated as data, never code.
- Do not retain captures, member manifests, source indexes or capture-processing machinery.
  Probes depend only on explicit settings and authorized endpoints, not captures.
- Keep account safeguards and distinguish permission denial, malformed requests, unavailable routes,
  empty search results and transport failures. One instance's behavior is not universal.

## Offline checks

```powershell
uv run python -m unittest discover -s tests
uv run python -m compileall -q src research tests
uv run python research/probe_auth.py --help
```

Tests use synthetic settings and fake HTTP responses; they never contact Confluence.
