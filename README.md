# ThetaData Options

A read-only ThetaData MCP service with **hosted Streamable HTTP** and local stdio
operation. Deploy it on Render or another Python/container host. The Python client
connects directly to ThetaData; Theta Terminal is not required. It does not place trades.

The hosted service exposes authenticated `/mcp` and public `/health` endpoints.
`THETADATA_API_KEY` stays on Render for provider access. HTTP authentication has
two explicit modes: `bearer` (the existing default, using `MCP_API_KEY`) and
`oauth` (using access tokens from your identity provider). Your Mac's environment
variables do not automatically carry over to Render.

**For ChatGPT's custom MCP setup, use OAuth and follow [Auth0 setup](docs/auth0-setup.md).**
The web setup flow has no documented static bearer-token input. An API-key
declaration in a plugin ZIP does not resolve that limitation. OAuth mode requires
the `options:read` scope and a valid token for this specific MCP service from the
configured issuer; it never falls back to the static service token. Access is
controlled through Auth0 permissions, with no server-side account allowlist.

## Tools

| Tool | Purpose |
| --- | --- |
| `get_status` | Check dependency versions and API-key presence without a provider request; HTTP authentication is still required. |
| `list_expirations` | Discover available expirations for one underlying, with optional date bounds. |
| `get_option_chain` | Join quotes, IV, first-order Greeks and daily open interest by symbol, expiration, exact strike and right. |
| `get_option_contract` | Refresh one exact contract using its underlying, expiration, dollar strike and call/put right. |

Chain output includes NBBO bid/ask, bid/ask size, midpoint, dollar spread, spread
as a percentage of midpoint, IV, delta, theta, vega, rho, epsilon and lambda.
Quote, Greek, underlying and open-interest
timestamps are kept separately. Missing values stay null. Each requested
endpoint has its own status, so missing entitlement does not discard quotes or OI.

## Calculated horizon gamma

Both options tools add a separate `horizon_gamma` object to every returned row.
It estimates Black–Scholes–Merton gamma at a future time from that row's
**Greeks snapshot timestamp**, holding spot, implied volatility and carry fixed.
The default advances 25% of the remaining lifetime: four hours remaining means
a one-hour horizon and three hours remaining in the gamma formula. This is a
European-model scenario, not a prediction of future spot, IV or executable prices.
Provider fields, including `greeks.gamma`, are preserved.

For `T` seconds between the Greeks timestamp and the modeled expiration cutoff,
`tau = (1 - fraction) * T / (365 * 86400)` and
`d1 = (ln(S/K) + (r - q + sigma^2/2) * tau) / (sigma * sqrt(tau))`.
The estimate is `exp(-q*tau) * normal_pdf(d1) / (S*sigma*sqrt(tau))`, expressed
as delta change per **$1 of underlying**, with no contract-multiplier scaling.
IV is used as an annualized decimal (`0.25` means 25%), without magnitude-based
conversion. Rates are annual continuously compounded decimals.

| Setting | Default | Behavior |
| --- | --- | --- |
| `HORIZON_GAMMA_FRACTION` | `0.25` | Finite fraction in `[0, 1)`; zero evaluates gamma at the snapshot. |
| `HORIZON_GAMMA_RISK_FREE_RATE` | `0.0` | Finite annual rate; negative rates are supported. |
| `HORIZON_GAMMA_DIVIDEND_YIELD` | `0.0` | Finite, nonnegative annual continuous dividend yield. |
| `HORIZON_GAMMA_SYMBOL_PROFILES` | QQQ/SPY `late_close` | JSON object merged with these defaults; keys are normalized symbols. |

These are independent modeling assumptions, not the undisclosed rates used for
the provider's IV. They do not model discrete dividends or early exercise.
Set environment values on Render and restart/redeploy to apply them; local `.env`
values do not automatically reach a hosted process. Invalid values fail startup
in both HTTP and stdio mode. The API arguments stay unchanged.

Timing uses the pinned offline NYSE calendar from `pandas_market_calendars`,
including early closes and holidays. `regular_close` uses its session close;
`late_close` adds 15 minutes. QQQ/SPY normally use 4:15 p.m. ET and 1:15 p.m. on
early-close days. This is a model trading-session cutoff, **not an exercise
deadline**. Elapsed UTC seconds include nights/weekends and account for DST;
integer DTE is never used in the formula. Calendar corrections require a package
update; there is no calendar network request at runtime.

For another verified PM-settled equity/ETF timing convention, configure e.g.
`HORIZON_GAMMA_SYMBOL_PROFILES='{"AAPL":"regular_close","IWM":"late_close"}'`.
Choose the profile appropriate to the specific option class. Unconfigured
symbols, AM-settled products and dates with no matching session are not guessed.
An explicit profile override replaces that symbol's default.

Each `horizon_gamma` contains:

- `value`, `status` (`ok`, `warning`, `unavailable`), `reason`, and `warnings`.
- `as_of`, `horizon_at`, and `model_expiry_at` as timezone-aware ISO timestamps.
- `seconds_to_expiry`, `horizon_seconds`, and `remaining_seconds` without a time floor.

Response-level `horizon_gamma_model` reports the model/version, fraction, rates,
IV convention, ACT/365F basis, units, profiles, calendar version and limitations.
Spot, strike, IV and their original timestamps remain available in the row.
With `include_greeks=false`, the new result is unavailable with
`greeks_not_requested`; no additional provider fetch is performed.

Stale inputs still produce a historical snapshot-anchored scenario with warnings.
Warnings cover stale/future/missing source times, pairwise source gaps exceeding
`stale_after_seconds`, horizons already past, invalid/crossed/zero-bid quotes,
spreads above 5% of midpoint, and maturities outside 0–5 calendar DTE **as of the
Greeks snapshot**. The existing `calendar_dte` remains retrieval-date based.
Warnings neither filter rows nor certify liquidity. Daily OI is not a gamma input.

ThetaData documents a one-hour minimum time in its latest IV methodology.
During the final hour, `provider_iv_minimum_tte_may_apply` highlights that inherited
limitation; the calculation reuses IV and uses exact remaining seconds. Binomial
IV is accepted with `binomial_iv_in_european_model`. Unknown snapshot time,
invalid/nonpositive spot, strike or IV, unsupported timing, expiration reached
at the snapshot, or numerical failure yield null with a reason. A valid
deep-tail gamma can underflow to zero; unavailable results are never replaced by
zero. Calculation statuses do not change provider `partial` or `source_status`.

References: [Black–Scholes gamma](https://www.mathworks.com/help/finance/blsgamma.html),
[ThetaData IV/time convention](https://thetadata.net/docs/operations_python/option_snapshot_greeks_first_order.html),
[Nasdaq options hours](https://www.nasdaqtrader.com/Trader.aspx?id=optionshours),
[ThetaData early closes](https://thetadata.net/docs/operations/calendar_open_today.html),
[offline calendar](https://pandas-market-calendars.readthedocs.io/en/latest/usage.html).

After deploying, verify narrow QQQ/SPY chains and exact contracts through the
installed plugin. Confirm `horizon_gamma_model.version`, effective settings and
calendar version; reproduce each value from its returned inputs and remaining
seconds. Current-session acceptance additionally needs fresh, aligned snapshots;
after-hours checks verify calculation/provenance but not executable quotes.

## Deploy on Render

For ChatGPT, start with the [OAuth migration and connection instructions](docs/auth0-setup.md).
The Blueprint uses OAuth with the `thetadata-options.us.auth0.com` tenant.
Push this directory to Git, create or update the Render Blueprint, set
`THETADATA_API_KEY`, configure Auth0 permissions, verify discovery, and connect
through ChatGPT with CIMD or DCR. The detailed
steps below cover each action. You do not need Theta Terminal or your Mac to
remain running after deployment.

### 1. Prepare the repository

Extract the package and put **this directory's contents at the root of a Git
repository**, then push it to GitHub or another Render-supported Git provider.
The root must contain `render.yaml`, `requirements.txt`, `.python-version`,
`Dockerfile`, `server/` and `scripts/`. Do not commit `.env`, credentials or `.venv`.

If you keep this directory inside a larger repository instead, add
`rootDir: path/to/thetadata-options` under the service in `render.yaml`, using its
actual repository-relative path. Select that directory's `render.yaml` as the
Blueprint path. For manual Web Service setup, use the same path in **Root Directory**.
The steps below assume the package contents are at the repository root.

### 2. Create the service from the Blueprint

1. In Render, select **New → Blueprint** and connect the repository.
2. Select the repository branch and its `render.yaml` file.
3. Enter your existing ThetaData key for `THETADATA_API_KEY`.
4. Review the service and deploy. The Blueprint starts with the **Free** plan
   for testing; choose a paid instance for a service you expect to remain available.
5. Follow [Auth0 setup](docs/auth0-setup.md) to configure the MCP API audience and
   `options:read` permission, then connect through CIMD or DCR. The Blueprint
   does not generate a static client token; existing bearer clients must migrate
   when the service switches to OAuth.

The Blueprint defines a Python web service in Virginia, installs dependencies,
starts Uvicorn, and checks `/health`. `.python-version` selects Python 3.14 and
lets Render choose its available patch release. Runtime dependencies are
installed at build time; the hosted service does not use the local bootstrapper.

Render supplies `RENDER_EXTERNAL_HOSTNAME`. The app automatically allows that
specific hostname while keeping MCP host/origin validation enabled. No host
allowlist changes are needed for the default `onrender.com` URL.

### Alternative: create a Web Service manually

Select **New → Web Service**, connect the repository, and use:

| Render setting | Value |
| --- | --- |
| Language / runtime | Python 3 |
| Root Directory | Blank when these files are at the repository root |
| Build Command | `pip install -r requirements.txt` |
| Start Command | `bash scripts/start_http.sh` |
| Health Check Path | `/health` |
| Instance type | Free for testing, or a paid instance to avoid idle spin-down |

For OAuth, set the same environment values as the Blueprint and the
[Auth0 setup guide](docs/auth0-setup.md). For a legacy static-bearer deployment,
set `MCP_AUTH_MODE=bearer`, `THETADATA_API_KEY` and `MCP_API_KEY` before deploying.
Generate the separate service token locally:

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
```

Store that generated value as `MCP_API_KEY` on Render and in your client's secure
configuration. Never put either secret into Git, a plugin ZIP or a URL.

### 3. Verify the deployment

Use the actual service URL shown in Render, which might differ from its requested name:

```bash
curl --fail https://thetadata-options.onrender.com/health
```

Expected response: `{"status":"ok"}`. This is a liveness check; it does **not**
authenticate with ThetaData or confirm subscription access.

An unauthenticated request to `/mcp` must return `401 Unauthorized`:

```bash
curl -i -X POST https://thetadata-options.onrender.com/mcp
```

For the OAuth deployment, verify protected-resource discovery and ChatGPT sign-in
as described in [Auth0 setup](docs/auth0-setup.md). The CLI can consume an existing
OAuth access token from `MCP_OAUTH_ACCESS_TOKEN` with `--auth oauth`.

For bearer mode, install the requirements locally if needed, then set your local `MCP_API_KEY`
to the same generated service token, using your normal secret-management method.
Run the supplied client check:

```bash
python scripts/check_remote.py --url https://thetadata-options.onrender.com/mcp
python scripts/check_remote.py --url https://thetadata-options.onrender.com/mcp --live --symbol QQQ
python scripts/check_remote.py --url https://thetadata-options.onrender.com/mcp --live --symbol QQQ --greeks-model binomial
```

The first command verifies MCP initialization, tool discovery and remote status.
The `--live` command also fetches expirations and a small chain from ThetaData.
On an existing Render service, deploy the updated source before running these
checks. In `source_status.greeks`, confirm the selected `endpoint` and look for
`status: ok` and a positive `row_count`.
The live check exits successfully only when expirations and all three requested
chain sources return records. Provider errors or empty sources exit with status 1
and a concise JSON diagnostic on stderr, including the failing tool/source.
The MCP tools still preserve partial chain results for clients. Secrets and raw
provider exception details are not printed.
Opening `/mcp` in a browser is not an MCP connection test.

### 4. Connect a client or create the remote plugin

| Connection field | Value |
| --- | --- |
| Transport | Streamable HTTP |
| Endpoint | `https://thetadata-options.onrender.com/mcp` |
| Authentication | Static bearer token / API key |
| Header | `Authorization: Bearer <your MCP_API_KEY>` |
| Suggested tool timeout | At least 60 seconds |

This table applies to `MCP_AUTH_MODE=bearer` and clients that can supply a static
token. For ChatGPT, set `MCP_AUTH_MODE=oauth` and configure the external identity
provider as described in [Auth0 setup](docs/auth0-setup.md). The server implements
the OAuth resource-server side; Auth0 handles sign-in, consent, authorization
codes, PKCE and token issuance. HTTPS termination is supplied by Render.

For Codex, an example MCP entry in its configuration is:

```toml
[mcp_servers.thetadata_options]
url = "https://thetadata-options.onrender.com/mcp"
bearer_token_env_var = "MCP_API_KEY"
tool_timeout_sec = 60
```

Make `MCP_API_KEY` available to the process launching Codex. The remote client
does not need `THETADATA_API_KEY`; that key stays on Render. Avoid enabling the
local and remote versions together unless you intentionally want both.

The root `mcp.json` still describes local stdio operation. **After verifying your
deployed endpoint**, generate a separate remote plugin package pointing to it:

```bash
python scripts/package_remote.py \
  --url https://thetadata-options.onrender.com/mcp \
  --auth bearer \
  --output ../thetadata-options-remote.zip
```

This explicit `--auth bearer` export is only for clients with a static-token
credential input. It cannot complete ChatGPT's web setup. The generator now
defaults to OAuth with CIMD; use `--client-mode dcr` for dynamic registration.
See [Auth0 setup](docs/auth0-setup.md). ZIPs contain connection metadata only, with no server code
or secrets. Generating a package does not deploy, register or install anything.

## Environment variables

| Variable | Required | Purpose |
| --- | --- | --- |
| `THETADATA_API_KEY` | Yes | ThetaData credential; set it on the server. |
| `MCP_AUTH_MODE` | No | `bearer` (Python default) or `oauth` (Render Blueprint); neither mode allows anonymous MCP requests. |
| `MCP_API_KEY` | Bearer mode | Separate random token, at least 32 characters without whitespace. Ignored in OAuth mode. |
| `MCP_OAUTH_ISSUER` | OAuth mode | Exact HTTPS issuer including its trailing slash, when present. |
| `MCP_OAUTH_JWKS_URL` | OAuth mode | Trusted issuer's HTTPS public signing-key endpoint; never taken from a token header. |
| `MCP_RESOURCE_URL` | OAuth mode outside Render / custom domain | Canonical public HTTPS `/mcp` URL and required audience. Defaults to `https://<RENDER_EXTERNAL_HOSTNAME>/mcp` on Render. |
| `MCP_OAUTH_ALGORITHM` | No | Pinned JWT signing algorithm: `RS256` (default) or `ES256`. |
| `MCP_OAUTH_ACCESS_TOKEN` | OAuth checker only | Short-lived access token used by `check_remote.py --auth oauth`; never a server setting. |
| `PORT` | No | HTTP listener port; uses the host's value or defaults to `10000`. |
| `MCP_ALLOWED_HOSTS` | Other hosts / custom domains | Comma-separated trusted hostnames, optionally with ports; no scheme, paths or wildcards. Example: `options.example.com`. |
| `RENDER_EXTERNAL_HOSTNAME` | Automatic on Render | Adds the service's default Render hostname to the allowlist. |
| `THETADATA_MAX_CONCURRENT_REQUESTS` | No | Concurrent provider subprocess limit, 1–8; defaults to 1 per web process. |
| `THETADATA_PYTHON` | No | Python executable for local launcher/start scripts. |

Local loopback hosts are allowed for testing. For a Render custom domain, add
that domain to `MCP_ALLOWED_HOSTS` and redeploy. On other hosts, add their assigned
hostname explicitly. Missing required auth settings or the provider key cause
HTTP startup to fail. In OAuth mode the resource hostname must also be allowed.

## Docker and other hosting providers

The included Dockerfile runs the same HTTP service as a non-root user. It needs
no database, Redis or persistent disk. From the repository root:

```bash
docker build -t thetadata-options .
cp .env.example .env
# Edit .env locally with both actual secrets; .env is excluded from Git and the image.
docker run --rm --env-file .env -p 10000:10000 thetadata-options
```

Then test `http://127.0.0.1:10000/health` and use `check_remote.py` with the local
`http://127.0.0.1:10000/mcp` URL. The checker permits plain HTTP only on loopback.

On another container host, deploy this Dockerfile, set `THETADATA_API_KEY`, the
selected mode's authentication settings, and
`MCP_ALLOWED_HOSTS` to its actual public hostname, expose the port in `PORT`,
enable HTTPS, and configure `/health` as the health check. A native Python host
can instead use the same build/start commands as Render. Its reverse proxy must
forward the `Authorization` header, preserve the public Host header, and allow
requests to run longer than the 45-second provider timeout. Run one web process
initially; concurrency limits are per process, not shared across replicas.

## Operation and troubleshooting

- **Free Render service sleeps / connection times out:** Free services spin down
  after 15 minutes without inbound traffic; a cold start can take about a minute.
  Allow the service to wake before connecting, or use a paid instance. The
  Blueprint's free plan is a testing default.
- **OAuth `401` / `403` / `503`:** See [Auth0 setup](docs/auth0-setup.md).
  Tokens need the correct signature, issuer, audience, lifetime and scope, and a
  valid subject claim. A signing-key outage fails closed; retry after 30 seconds.
- **Bearer-mode `401 Unauthorized`:** The client must send the exact server-side `MCP_API_KEY`
  as a bearer token. Sending `THETADATA_API_KEY` instead is incorrect. Rotate the
  service token by changing it on the server, redeploying and updating clients.
- **`421 Misdirected Request` or invalid Origin:** Add the real hostname to
  `MCP_ALLOWED_HOSTS`. Do not disable host validation or add broad wildcard domains.
- **`AUTHENTICATION_FAILED` / `PERMISSION_DENIED`:** Check the server-side
  ThetaData key and endpoint entitlement. The original local environment is not
  inherited by Render. Check `source_status.greeks.endpoint` to confirm which
  first-order route the deployed version used.
- **`INTERNAL`:** ThetaData returned an internal server error. The server retries
  each transiently failing SDK request up to twice, with 0.25- and 0.5-second
  delays, using the same endpoint and filters. Retries remain within the overall
  45-second worker limit. Deploy the updated server code to enable these retries
  on Render; updating the local checker alone changes only its diagnostics.
  If failures persist, retry later or contact ThetaData support with the endpoint
  and time. An `INTERNAL` response alone does not establish an authentication or
  entitlement problem. Authentication, permission and no-data errors are not retried.
- **`BUSY`:** Another provider query holds the default single worker slot. Retry
  when it completes. Increase `THETADATA_MAX_CONCURRENT_REQUESTS` only after
  checking service memory and your provider concurrency allowance.
- **`TIMEOUT`, memory pressure or missing contracts:** Narrow `max_dte` and
  `strike_range`. Each tool query starts a bounded SDK subprocess; chain sources
  are requested sequentially within its 45-second limit.
- **`/health` works but no live data:** Health checks do not call ThetaData. Use
  `check_remote.py --live`, then inspect `source_status` and timestamps. No data
  before market open or outside a contract's trading life is not proof of a
  deployment failure.

## Local stdio operation

1. Install/select the private **ThetaData Options** plugin in a host supporting
   local stdio MCP tools. The package uses `mcp.json` and `${PLUGIN_ROOT}`.
2. Ensure the host process can read `THETADATA_API_KEY`. A variable exported in
   one Terminal session does not necessarily reach a GUI application. Do not
   paste the key into chat or put its value into the plugin package.
3. Launch the plugin. The first launch prepares an isolated environment under
   `~/.cache/thetadata-options` (or `XDG_CACHE_HOME`) and installs the dependencies.
   Existing system packages may be reused; global packages are not modified.
   First setup needs PyPI network access and can take longer than normal startup.
4. Call `get_status`, then `list_expirations` for `QQQ` to check authenticated access.

You can prepare dependencies before connecting the plugin:

```bash
bash scripts/setup.sh
```

To choose the Python interpreter, set `THETADATA_PYTHON` to its executable path.
To run the server manually, from the plugin directory:

```bash
bash scripts/run.sh
```

The process speaks MCP over stdio. Setup and logging go to stderr. The API key
is inherited locally and used only by the ThetaData SDK; it is not a tool argument,
not included in responses, and not packaged. Provider errors use generic categories
instead of returning raw messages that might contain credentials or account details.

For hosts without plugin-package installation, configure a local MCP server with
command `bash` and an argument containing the absolute path to `scripts/run.sh`.
Use that host's supported environment-variable forwarding mechanism for
`THETADATA_API_KEY`. Local stdio does not require HTTP authentication settings.

## Example requests

- “List QQQ option expirations during the next week.”
- “Show QQQ calls and puts with at most 5 calendar DTE and 3 strikes on each
  side of ATM. Include spreads, Greeks, IV, open interest and timestamps.”
- “Refresh the QQQ put expiring [date] at strike [dollar strike].”
- “Fetch SPY quotes and open interest only; skip Greeks.”

Example tool arguments:

```json
{
  "symbol": "QQQ",
  "expiration": "*",
  "max_dte": 5,
  "strike_range": 3,
  "right": "both",
  "include_greeks": true,
  "greeks_model": "standard",
  "include_open_interest": true
}
```

## Filters and interpretation

- `expiration` accepts `YYYY-MM-DD` or `*`. With `*`, `max_dte` defaults to 7
  calendar days and is capped at 60. With a specific expiration, `max_dte` is
  ignored. `max_dte=0` uses today's New York date explicitly to avoid the SDK's
  zero-value filter handling.
- `strike_range=5` requests up to five strikes above and below spot plus ATM
  per expiration/right. Valid range is 1–50. Exact contracts use a dollar strike,
  such as `500.5`; do not multiply by 1,000.
- `max_rows` defaults to 200, at most 500. Rows are sorted by expiration, strike
  and right. `truncated` and `total_matching_rows` describe a capped response;
  returned rows are not ranked candidates.
- `spread = ask - bid`; `spread_pct_of_mid = 100 × spread / midpoint`.
  Invalid, incomplete or crossed quotes do not produce a derived spread.
  Prices are dollars per share; contract multipliers are not assumed.
- ThetaData IV and Greek values are returned in their native units. No automatic
  percent, per-day, or per-contract conversion is applied. `iv_error` is retained.
- Naive provider timestamps are interpreted as `America/New_York`, including
  daylight-saving offsets. Already aware timestamps keep their supplied offset.
  This timezone interpretation is an explicit adapter convention.
- Quote and Greek age flags default to a 120-second threshold. Their timestamps
  and the gap between them are exposed because the three endpoint requests are
  separate, not an atomic market snapshot.
- Snapshots can return the last message from the previous seven calendar days.
  Outside trading hours, expect previous-session values. Use
  `current_day_only=true` to exclude prior dates; this can legitimately return
  no option quotes before options trading opens.
- Open interest is a daily OPRA report, generally published around 06:30 ET,
  reflecting the previous trading day's positions. Its timestamp is separate;
  it must not be described as current intraday positioning or flow.
- Both tools default to `greeks_model="standard"`, which calls
  `option_snapshot_greeks_first_order`. Set `greeks_model="binomial"` to use
  `option_snapshot_binomial_greeks_first_order`. ThetaData lists both Python
  endpoints for Standard and Pro. Each request uses one model, identified in
  `source_status.greeks`. Set `include_greeks=false` to skip the Greeks request.
  Existing response fields for higher-order Greeks, including gamma, are
  retained for compatibility and are null when the first-order route omits them.
- A complete tool request is bounded to 45 seconds. Network failures, missing keys,
  unavailable data and subscription errors return structured error categories.

## Development and verification

Tested against `thetadata 1.0.17`, `mcp 1.30.0`, and Python 3.14.5 on macOS.
The explicit `python-dotenv` dependency repairs an import dependency omitted by
the installed ThetaData distribution.

Run with the environment prepared by setup, or any Python containing the requirements:

```bash
python -m unittest discover -s tests -v
python tests/smoke_mcp.py
python tests/smoke_http.py
```

Validation includes:

- Automated tests covering exact contract joins, fractional strikes,
  spread calculations, stale/crossed quotes, duplicate records, source timestamps,
  missing data, partial access, zero-DTE filtering, bounded requests, input schemas
  and secret-safe error messages, bounded transient retries, and remote-check
  exit statuses through the MCP HTTP transport.
- Official MCP client initialization, discovery of all four tools, status call,
  missing-key response and invalid-input rejection.
- Local launcher and dependency setup.
- HTTP authorization, duplicate-header rejection, public health, MCP initialization,
  discovery and calls, schema validation, Host/Origin rejection and startup checks.
- Provider-worker capacity limits, cancellation cleanup and separation of client
  tokens from the provider subprocess environment.
- Remote plugin export validation.
- OAuth discovery, JWT signatures/claims, scope enforcement across subjects,
  signing-key rotation/outages, OAuth tool metadata, and an official MCP client
  check using locally signed test tokens. These do not prove a live Auth0 login.

The hosted refactor includes automated tests plus official MCP-client checks for
both stdio and HTTP. The HTTP smoke test uses an in-process ASGI transport rather
than a deployed URL. Docker image build/run was not tested because the local
Docker daemon was unavailable.

On November 7, 2026, the hosted check returned QQQ expirations and quotes, with
`INTERNAL` errors for Greeks and open interest. A separate direct SDK probe
returned `INTERNAL` for expirations while Greeks and open interest succeeded.
This reproduced intermittent provider failures; it does not establish that all
endpoints are consistently healthy. The updated checker correctly exited with
status 1 when the live expiration retries were exhausted. A subsequent live
check through the updated provider adapter returned expirations and 10 chain rows,
with quotes, Greeks and open interest all successful. All 40 automated tests and
both MCP smoke tests passed. These changes have not been deployed to Render;
after deploying, run `check_remote.py --live` to verify current hosted access.

## Official references

- [Python getting started and API-key authentication](https://thetadata.net/docs/Python-Library/Getting-Started.html)
- [Expiration discovery](https://thetadata.net/docs/operations_python/option_list_expirations.html)
- [Snapshot NBBO quotes and available filters](https://thetadata.net/docs/operations_python/option_snapshot_quote.html)
- [First-order snapshot Greeks](https://thetadata.net/docs/operations_python/option_snapshot_greeks_first_order.html)
- [First-order binomial snapshot Greeks](https://thetadata.net/docs/operations_python/option_snapshot_binomial_greeks_first_order.html)
- [Daily open-interest semantics](https://thetadata.net/docs/operations_python/option_snapshot_open_interest.html)
- [Render Python web-service deployment](https://render.com/docs/web-services)
- [Render Blueprint configuration and secrets](https://render.com/docs/blueprint-spec)
- [Render Python version selection](https://render.com/docs/python-version)
- [Render default environment variables](https://render.com/docs/environment-variables)
- [Render Free service behavior](https://render.com/docs/free)
- [Codex MCP configuration](https://developers.openai.com/codex/mcp)

This project is an independent integration and is not an official ThetaData product.
