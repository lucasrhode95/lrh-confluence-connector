# Authentication findings

Date: 2026-10-07. Saved Wiki HTML metadata identifies Confluence Data Center 9.2.10. The separate AUI 9.12.11 asset marker is not interpreted as the server version.

## All mapped options

| Method | Evidence | Status and limit |
|---|---|---|
| Personal access token bearer | User's successful `--mode pat` run | 200 for current user, both searches, spaces and content-by-ID. Best verified integration credential. Token classification relies on the user's PAT-creation report; the probe transports PAT and OAuth bearer tokens identically. |
| Password form plus web session | HAR02 E001 failure; HAR04 E001 success; two user-run form probes | API access and immediate fresh-client cookie replay work. Two secure/HttpOnly cookies, one persistent. Cross-process/reboot reuse and cookie lifetime untested. |
| Microsoft/Entra SAML browser SSO | HAR02 E002/E004-E006; HAR03 E034/E036/E055/E062/E074-E075 | Browser sign-in returns to samlconsumer and authenticated Wiki content. Does not establish a local app OAuth grant. |
| HTTP Basic | Documented API option and `--mode basic` | No successful Basic result supplied. Form success does not establish Basic acceptance. |
| Native OAuth authorization code/PKCE | Official provider documentation; `--mode oauth` | Requires incoming application-link registration by admin. No native OAuth token/test. User expects administrators to reject this setup. |
| Anonymous | Prior anonymous live probe | 401 for current user, both searches and spaces. Protected reads unavailable anonymously. |
| CAPTCHA, TOTP/recovery, enrollment/elevation | HAR03 login JS and split MFA chunk | CAPTCHA-related failure captured. Other variants are source-only, not proven enabled/exercised. |
| Legacy application-link OAuth | App-link provider/consumer fragments in ZIP/HAR JS | Source-only, with no registration/grant/API test. Admin integration, not proven self-service. |
| Service-account client credentials | Current Atlassian documentation only | No service account or instance evidence. Availability for captured 9.2.10 unestablished. |
| Hosted Rovo/Teamwork Graph | Official docs, discussed in conversation | Admin/cloud bridge. Not configured in the tested environment; no setup attempted. |

## Form and login-options contracts

`POST /rest/tsv/1.0/authenticate` accepts the captured JSON field names `username,password,rememberMe,targetUrl,captchaId`. HAR02 E001 returns 401 and keys `message,captchaRequired`; HAR04 E001 returns 200 and `next`. Request query key `os_authType` is observed. Shared JS appends the fixed option `os_authType=none` for supported products. Credential/CAPTCHA/target values are not retained.

HAR03 E032: `GET /rest/authconfig/1.0/login-options` returns keys `results,size,start,limit,isLastPage`. There are two results, with types `LOGIN_FORM` and `IDP`; item shapes are `type,id` and `type,id,buttonText,loginLink`. This is evidence of a form and a configured identity-provider choice, not proof of OIDC.

The user ran `src/probe_auth.py --mode form --remember-me` twice. Both logins and all API reads passed. Two cookies were secure/HttpOnly; one had an expiry. An immediate copied-jar replay in a fresh HTTP client passed current user and CQL search. The probes retain cookies in memory only. They prove neither persistence after process exit nor a guaranteed remember-me duration.

The same corporate password working in Microsoft SSO and the Wiki form is consistent with a common directory. LDAP vs AD vs Crowd vs another directory remains unproven.

## Browser SAML path

Observed sequence: `GET /login.action` -> `GET /plugins/servlet/external-login/{id}` with `authDest` -> Microsoft `/{tenant}/saml2` -> interactive Microsoft requests -> `POST /plugins/servlet/samlconsumer` -> Wiki session. The consumer POST has form names `SAMLResponse,RelayState` and returns 302.

HAR03 includes Microsoft `POST /common/GetCredentialType`, `POST /{tenant}/login` and `POST /kmsi`, each 200. These are external browser-auth endpoints, not Wiki integration APIs. Microsoft shared-source device-code/telemetry/cookie routes do not prove a Confluence device-code flow.

A normal browser SAML sign-in does not automatically deliver an app token to Python. Browser-managed SSO cookie reuse was considered, but browser inspection did not complete and SSO-only persistence is unproven. HAR cookie arrays inspected are empty; they do not reveal issued cookie values or lifetimes. The cookie counts above come from user-run form probes.

## MFA and recovery source routes

HAR03 E016's JS defines `contextPath + /rest/tsv/1.0` and appends a supplied suffix in fetch. E014's split MFA chunk imports that fetch module. Full paths below are reconstructed from those sources; methods are source-derived. Only authenticate was actually observed/probed.

| Source method | Full path | Source |
|---|---|---|
| POST | `/rest/tsv/1.0/authenticate` | E016, HAR02/HAR04 requests |
| GET/default fetch | `/rest/tsv/1.0/authenticate/captcha` | E016 |
| POST | `/rest/tsv/1.0/authenticate/totp-code` | E014 + E016 base |
| POST | `/rest/tsv/1.0/authenticate/recovery-code` | E014 + E016 base |
| POST | `/rest/tsv/1.0/elevate-permissions/recovery-code` | E014 + E016 base |
| POST | `/rest/tsv/1.0/totp/start-enforced-enrollment` | E016 |
| POST | `/rest/tsv/1.0/totp/complete-enforced-enrollment` | E016 |
| POST | `/rest/tsv/1.0/totp/start-voluntary-enrollment` | E016 |
| POST | `/rest/tsv/1.0/totp/complete-voluntary-enrollment` | E016 |
| POST | `/rest/tsv/1.0/totp/start-enrollment-update` | E016 |
| POST | `/rest/tsv/1.0/totp/complete-enrollment-update` | E016 |

Other source references include `/rest/basicauth/1.0`, `/logout.action`, `/ajaxlogincomplete.action`. The Basic base is not a verified login request. Shared configuration fields for SAML/OIDC do not establish which features are configured locally. Enrollment/elevation would change authentication state; none were executed.

## PAT lifecycle and verified read access

The user's PAT run returns 200 from `/rest/api/user/current`, `/rest/api/content/search`, `/rest/api/search`, `/rest/api/space`, `/rest/api/content/{id}`. Identity is present; both searches and space listing return one result with limit=1; the selected page returns JSON. This proves one token/account/query/process, not broad recall, all permissions, pagination, attachment APIs, writes, token lifetime, revocation or secure persistence.

Atlassian documents PATs as user-permission bearer credentials for Confluence 7.9+. Expiry can be set, with admin restrictions; an expired PAT is replaced rather than refreshed. `/rest/pat/latest/tokens` is a documented token-creation endpoint, not observed or used here. [Official PAT guide](https://confluence.atlassian.com/enterprise/using-personal-access-tokens-1026032365.html)

The probe's PAT and OAuth modes both send a bearer token. The successful PAT does not establish OAuth registration or OAuth API compatibility.

## Other documented/source-only possibilities

Confluence's provider guide supports authorization code and PKCE on 7.17+, using `/rest/oauth2/latest/authorize` and `/rest/oauth2/latest/token` after registration. It shows bearer access to content. Earlier versioned REST-reference OAuth2 notices appear inconsistent with that example; their applicability to the native DC provider is unresolved. A registered-client test is needed before claiming native OAuth works or is impossible. [OAuth provider guide](https://developer.atlassian.com/server/confluence/confluence-oauth2-provider-api/)

Legacy app-link fragments include `/rest/applinks/1.0`, `/rest/applinks-oauth/1.0`, `/authentication/consumer`, `/authentication/provider`, `/oauth/consumer-info`. Prefix/suffix composition and client configuration need targeted review; they are not a proven user-level login flow.

Current service-account docs describe client ID/secret grants, limited-lived tokens, no refresh tokens, and requesting a new access token after expiry. This is documentation-only and does not prove this older Wiki has that feature. [Service-account overview](https://confluence.atlassian.com/enterprise/service-accounts-overview-1627095923.html)

The hosted Rovo option discussed uses an administrator-managed DC bridge into Teamwork Graph. It requires cloud/admin setup and content selection; the user's organizational constraint excludes it. [Data Center bridge](https://support.atlassian.com/organization-administration/docs/connect-confluence-data-center-to-teamwork-graph/)

## Persistence discussed, not implemented

Storage is separate from login. We discussed a central token provider with OS-vault adapters. PAT storage would retain the PAT; web-session storage would retain a complete cookie jar. Encrypted username/password storage was the last fallback considered; working PAT access makes it unnecessary for these reads. The current probe saves nothing, and no persistence/ownership/concurrency implementation is claimed here.

Open questions: PAT expiry/revocation; Basic policy; native OAuth rights; SSO-only cookie reuse; MFA/CAPTCHA enforcement; future encrypted-store persistence across processes and account ownership.
