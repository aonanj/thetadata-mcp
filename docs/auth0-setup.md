# Connect ThetaData Options to ChatGPT with Auth0

The server supports OAuth as a protected resource. Auth0 owns the browser login,
consent, authorization-code + PKCE flow and token issuance. `THETADATA_API_KEY`
stays on Render. Neither it nor `MCP_API_KEY` belongs in the OAuth client-secret
field or the plugin ZIP.

## New tenant and the MCP API audience

The supplied tenant's public discovery document was checked on November 8, 2026:

| Setting | Value |
| --- | --- |
| Issuer | `https://thetadata-options.us.auth0.com/` |
| JWKS | `https://thetadata-options.us.auth0.com/.well-known/jwks.json` |
| Authorization endpoint | `https://thetadata-options.us.auth0.com/authorize` |
| Token endpoint | `https://thetadata-options.us.auth0.com/oauth/token` |
| DCR endpoint | `https://thetadata-options.us.auth0.com/oidc/register` |
| CIMD | `client_id_metadata_document_supported: true` |
| Issuer in authorization responses | `authorization_response_iss_parameter_supported: true` |

Both OAuth authorization-server and OpenID discovery advertise `S256` PKCE,
CIMD, DCR, and `none` / `private_key_jwt` token authentication. **Use CIMD by
default**, or explicitly choose DCR. Auth0 handles client identification and
registration; this MCP resource server validates the resulting access tokens.
The discovery documents do not prove API permissions, login connections, or
successful browser authorization.

The supplied `https://thetadata-options.us.auth0.com/api/v2/` audience is the
**Auth0 Management API**, and the supplied `grant_type=client_credentials` is
a machine-to-machine grant. Neither configures ChatGPT's user sign-in. The
supplied application client ID is not needed for CIMD or DCR and is not packaged.
Do not use a Management API token to call this server.

In this new tenant, create or confirm a custom API named **ThetaData Options**
whose identifier is the actual public MCP URL, for example
`https://YOUR-SERVICE.onrender.com/mcp`, with **RS256** signing. The identifier must
exactly equal `MCP_RESOURCE_URL`, including the `/mcp` path and no trailing slash.
Add the permission **`options:read`**. If RBAC is enabled, assign that permission
to the account that will connect. The server checks the token's `scope` claim.

In Auth0 **Settings → Advanced**, enable **Resource Parameter Compatibility
Profile**. ChatGPT sends `resource=<MCP_RESOURCE_URL>`; this profile maps it to
the API audience. This is a tenant-level setting: review its effect on your
existing applications before changing it. Do not replace the tenant's default
audience with the MCP URL to work around a missing resource parameter.

## Configure CIMD or DCR for user sign-in

1. Keep CIMD enabled and select **OAuth → CIMD** in the ChatGPT connection or use
   the package below. Leave static OAuth client credentials unset. ChatGPT uses
   its HTTPS client metadata document URL as its client ID; Auth0 validates it.
   If configuring a tenant-side CIMD trust policy, use the **exact metadata URL
   and redirect URI shown in the ChatGPT connection setup**. This tenant
   advertises issuer-bound authorization responses, for which OpenAI documents
   `https://chatgpt.com/oauth/client.json` and
   `https://chatgpt.com/connector_platform_oauth_redirect`. Confirm the values
   shown by the connection rather than guessing or wildcarding callbacks.
2. For **DCR**, choose DCR in ChatGPT or export with `--client-mode dcr`.
   ChatGPT registers at the advertised `/oidc/register` endpoint and manages the
   returned credentials. Do not insert the supplied static client ID. Preserve
   the dynamically registered client while that connection remains in use.
3. Configure Auth0's third-party application access to the custom MCP API:
   permit user-delegated access and grant only the required `options:read`
   permission to the intended client, or use appropriately scoped default
   third-party permissions for dynamic clients. Enable a login connection that
   these clients can use; Auth0's strict third-party flow requires a domain-level
   connection. Merely advertising CIMD/DCR does not configure these permissions.
4. In the **new tenant's** Auth0 **User Management → Users**, copy the connecting account's exact
   `user_id` (the access token's `sub`, often `auth0|…` or `google-oauth2|…`). Set
   that value in Render's `MCP_OAUTH_ALLOWED_SUBJECTS`. Comma-separate additional
   approved subjects if needed. Do not substitute an application client ID or
   assume the old tenant's user ID is valid here. Being able to sign in to this tenant alone does
   not grant access to the ThetaData subscription.

For persistent connections, optionally enable refresh tokens/offline access in
the Auth0 application/API and request `offline_access` in the client's OAuth
configuration. Without it, expect to reconnect after access-token expiration.
Never enable password or client-credentials grants as a substitute for the
ChatGPT authorization-code flow.

## Configure and deploy on Render

Deploy the updated repository and set the following in the service's Environment
settings. Substitute the **actual** service URL; the allowed user ID below is
the account supplied for this tenant:

```dotenv
MCP_AUTH_MODE=oauth
MCP_OAUTH_ISSUER=https://thetadata-options.us.auth0.com/
MCP_OAUTH_JWKS_URL=https://thetadata-options.us.auth0.com/.well-known/jwks.json
MCP_RESOURCE_URL=https://YOUR-SERVICE.onrender.com/mcp
MCP_OAUTH_ALLOWED_SUBJECTS=auth0|6af0393ca991a4b5dcdc7b72
MCP_OAUTH_ALGORITHM=RS256
```

Keep the existing `THETADATA_API_KEY`. OAuth mode does not use `MCP_API_KEY` even
if it remains in the environment. For a custom domain, also set its hostname in
`MCP_ALLOWED_HOSTS`. The default Render hostname is allowed automatically.

`render.yaml` now selects **OAuth**, the new tenant's issuer/JWKS, and the single
allowed subject above. A new Blueprint prompts for the provider key. On an
existing service, sync the Blueprint or set the same allowed subject in Environment
before deploying; startup fails closed if it is missing. If `MCP_RESOURCE_URL` is unset, Render's trusted
`RENDER_EXTERNAL_HOSTNAME` supplies `https://<hostname>/mcp`. Set the explicit
resource URL above for a custom domain or to preserve an existing canonical URL.
The request's Host header is never used to construct OAuth discovery URLs.

The Python default remains bearer mode for existing deployments without
`MCP_AUTH_MODE`; the updated Blueprint explicitly selects OAuth. `.env.example`
retains bearer mode for local Docker testing and shows the new OAuth settings.
The server does not read `AUTH0_AUDIENCE`, `AUTH0_DOMAIN`, or `AUTH0_ISSUER`.

## Verify discovery and connect

```bash
curl --fail https://YOUR-SERVICE.onrender.com/health
curl --fail https://YOUR-SERVICE.onrender.com/.well-known/oauth-protected-resource/mcp
curl -i -X POST https://YOUR-SERVICE.onrender.com/mcp
```

Health should return `{"status":"ok"}`. Resource metadata should identify the
exact MCP URL, Auth0 issuer, and `options:read` scope. The unauthenticated MCP
request must return **401**, with `WWW-Authenticate` advertising `resource_metadata`
and the scope. Both root and path-specific protected-resource metadata URLs are
public; tools, initialization and discovery of tools require authentication.

In ChatGPT, add a custom MCP server with the HTTPS `/mcp` URL, select **OAuth**
and **CIMD** (or DCR), and complete Auth0 sign-in as the allowed account. Clear
any old pre-registered client credentials: supplied static credentials can take
precedence over dynamic registration. Reconnect to the new issuer and refresh
tool metadata. If the saved connection locks the old registration settings,
create a new connection instead of assuming a package upload replaces them.
Verify initialization, all four tools, and `get_status`; then try
`list_expirations` to test provider access separately.

For an optional packaged connection, generate this **after** endpoint verification:

```bash
python scripts/package_remote.py \
  --url https://YOUR-SERVICE.onrender.com/mcp \
  --client-mode cimd \
  --output ../thetadata-options-remote.zip
```

OAuth with CIMD is the export default, so `--client-mode cimd` is optional.
Use `--client-mode dcr` for the other advertised registration method. The ZIP
declares `options:read` and the registration mode, without a client ID or secret.
It does not enable OAuth on Render or configure Auth0 by itself. Exporting
`--auth bearer` is reserved for clients supporting static tokens and cannot fix
ChatGPT's web setup flow.

For CLI diagnostics, obtain an access token through your configured OAuth client
and make it available as `MCP_OAUTH_ACCESS_TOKEN` using your normal secure local
environment mechanism, then run:

```bash
python scripts/check_remote.py --url https://YOUR-SERVICE.onrender.com/mcp --auth oauth
python scripts/check_remote.py --url https://YOUR-SERVICE.onrender.com/mcp --auth oauth --live --symbol QQQ
```

The checker consumes a token; it does not perform browser login or refresh. It
does not fall back to `MCP_API_KEY` when `--auth oauth` is selected.

## Failure meanings and verification limits

- **Startup failure:** Required OAuth settings/provider key missing, unsafe URL,
  unsupported algorithm, empty/wildcard account list, or untrusted resource host.
- **401:** Missing/invalid token, wrong signing key/issuer/audience, expired or
  not-yet-valid token. Old-tenant tokens, Management API tokens, and ID tokens
  are not MCP access tokens.
- **403:** Account not allowlisted, `options:read` missing, or invalid request Origin.
- **503:** Signing keys unavailable or malformed. Authentication fails closed and
  requests receive `Retry-After: 30`; details and token contents are not returned.

Signing keys are cached for up to five minutes, with refreshes limited to once
per 30 seconds per process, including unknown-key requests. Publish new keys
before issuing tokens using them. Removing a subject and restarting the service
revokes that subject's access immediately; otherwise JWTs are validated locally
until expiration. This server does not query Auth0's revocation state per request.

Local tests exercise real signed JWTs, discovery/challenges, scopes, subjects,
key rotation, failure handling and the official MCP HTTP client without calling
ThetaData or logging into Auth0. They do not establish that the deployed service,
Auth0 API/client settings, callback, or ChatGPT browser flow is working. Complete
the live connection above before claiming the authentication blocker is resolved.

References: [OpenAI custom MCP setup](https://developers.openai.com/api/docs/guides/custom-mcp-server),
[OpenAI authentication](https://developers.openai.com/plugins/build/auth),
[MCP authorization specification](https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization),
[Auth0 resource-parameter compatibility](https://support.auth0.com/center/s/article/mcp-audience-error-with-auth0),
[Auth0 third-party access controls](https://auth0.com/docs/get-started/applications/third-party-applications/security-controls),
and [the new tenant's public discovery document](https://thetadata-options.us.auth0.com/.well-known/oauth-authorization-server).
