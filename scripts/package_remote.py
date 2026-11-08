"""Create a small remote plugin package after verifying your deployed endpoint."""
import argparse
import json
from pathlib import Path
from urllib.parse import urlparse
import zipfile


def package(url: str, output: Path, *, auth: str = "oauth", client_mode: str | None = None,
            client_id: str | None = None, token_endpoint_auth_method: str | None = None):
    parsed = urlparse(url)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.path.rstrip("/") != "/mcp"):
        raise ValueError("Supply the verified HTTPS /mcp URL without credentials, query or fragment")
    if auth not in {"oauth", "bearer"}:
        raise ValueError("Choose oauth or bearer authentication")
    if auth == "bearer" and any((client_mode, client_id, token_endpoint_auth_method)):
        raise ValueError("OAuth client settings cannot be used with bearer authentication")
    authentication = {"type": "api_key", "headerScheme": "bearer"}
    if auth == "oauth":
        authentication = {"type": "oauth", "baseScopes": ["options:read"]}
        if client_mode is None and (client_id or token_endpoint_auth_method):
            raise ValueError("Set --client-mode provided when supplying a client ID or token auth method")
        client_mode = client_mode or "cimd"
        if client_mode not in {"provided", "dcr", "cimd"}:
            raise ValueError("Choose provided, dcr or cimd OAuth client registration")
        client = {"mode": client_mode}
        if client_mode == "provided":
            if not client_id or not client_id.strip():
                raise ValueError("Provided registration requires --client-id (never a client secret)")
            if token_endpoint_auth_method not in {"none", "client_secret_post", "client_secret_basic", "private_key_jwt"}:
                raise ValueError("Provided registration requires an explicit --token-endpoint-auth-method")
            client.update(clientId=client_id, tokenEndpointAuthMethod=token_endpoint_auth_method)
        elif client_id or token_endpoint_auth_method:
            raise ValueError("Client credentials/method apply only to provided registration")
        authentication["client"] = client
    root = Path(__file__).resolve().parents[1]
    manifest = json.loads((root / "plugin.json").read_text())
    metadata = manifest["extensions"]["com.openai"]
    metadata["requires_local_executor"] = False
    manifest["description"] = "Read-only ThetaData options data from your authenticated hosted MCP service."
    metadata["interface"]["longDescription"] = (
        "Fetch options quotes, bid/ask spreads, IV, Greeks and daily open interest from your hosted ThetaData service. "
        "Preserves source timestamps and data-quality flags. "
        + ("Sign in with the service's OAuth identity provider; " if auth == "oauth" else
           "Use a client that supports the service's static bearer token; ChatGPT web setup requires OAuth. ") +
        "the ThetaData API key stays on the server. Provider subscription limits apply.")
    config = {"$schema": "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json",
              "mcpServers": {"thetadata-options": {
                  "type": "streamable-http", "url": url.rstrip("/"),
                  "extensions": {"com.openai": {"auth": authentication}}}}}
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("plugin.json", json.dumps(manifest, indent=2) + "\n")
        archive.writestr("mcp.json", json.dumps(config, indent=2) + "\n")
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--output", type=Path, default=Path("../thetadata-options-remote.zip"))
    parser.add_argument("--auth", choices=("oauth", "bearer"), default="oauth",
                        help="OAuth for ChatGPT; bearer only for clients with a static-token input")
    parser.add_argument("--client-mode", choices=("provided", "dcr", "cimd"),
                        help="OAuth defaults to cimd; use dcr for dynamic registration or provided for an existing OAuth client")
    parser.add_argument("--client-id", help="Public OAuth client ID; never supply a secret")
    parser.add_argument("--token-endpoint-auth-method",
                        choices=("none", "client_secret_post", "client_secret_basic", "private_key_jwt"))
    args = parser.parse_args()
    print(package(args.url, args.output, auth=args.auth, client_mode=args.client_mode, client_id=args.client_id,
                  token_endpoint_auth_method=args.token_endpoint_auth_method))
