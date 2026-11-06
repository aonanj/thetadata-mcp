"""Create a small remote plugin package after verifying your deployed endpoint."""
import argparse
import json
from pathlib import Path
from urllib.parse import urlparse
import zipfile


def package(url: str, output: Path):
    parsed = urlparse(url)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.path.rstrip("/") != "/mcp"):
        raise ValueError("Supply the verified HTTPS /mcp URL without credentials, query or fragment")
    root = Path(__file__).resolve().parents[1]
    manifest = json.loads((root / "plugin.json").read_text())
    metadata = manifest["extensions"]["com.openai"]
    metadata["requires_local_executor"] = False
    manifest["description"] = "Read-only ThetaData options data from your authenticated hosted MCP service."
    metadata["interface"]["longDescription"] = (
        "Fetch options quotes, bid/ask spreads, IV, Greeks and daily open interest from your hosted ThetaData service. "
        "Preserves source timestamps and data-quality flags. Connect with the service's MCP_API_KEY; "
        "the ThetaData API key stays on the server. Provider subscription limits apply.")
    config = {"$schema": "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json",
              "mcpServers": {"thetadata-options": {
                  "type": "streamable-http", "url": url.rstrip("/"),
                  "extensions": {"com.openai": {"auth": {"type": "api_key", "headerScheme": "bearer"}}}}}}
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
    args = parser.parse_args()
    print(package(args.url, args.output))
