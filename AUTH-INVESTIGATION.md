# Authentication investigation findings

Updated: 2026-10-07.

The complete dated auth record is now [research/AUTHENTICATION.md](research/AUTHENTICATION.md). The route catalog, ZIP inventory, request shapes and offline Python scripts are indexed in [research/README.md](research/README.md).

User-run probes confirm PAT and form-session access to current user, both searches, spaces and content-by-ID. Immediate in-memory cookie replay also succeeds. Anonymous checks return 401. Basic and registered native OAuth remain unverified. No encrypted cross-process persistence is implemented.

Browser observations show form login, CAPTCHA failure and Microsoft SAML SSO. The ZIP supplies frontend source/resources, not a full login network trace. Directory technology, token lifetime/revocation, OAuth API rights and SSO-only persistence remain unresolved.

The connector currently supports PAT/Basic configuration; src/probe_auth.py prompts locally and saves no secrets or cookies. Deployment and probe settings now come from the same ignored confluence_config.json; no credentials are stored in it.
