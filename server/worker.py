"""Short-lived SDK process; no credentials travel in tool arguments or output."""
import contextlib
import importlib.metadata
import json
import logging
import os
import sys

from data import OptionsService, provider_error


def main():
    logging.disable(logging.CRITICAL)
    try:
        request = json.load(sys.stdin)
        if not os.environ.get("THETADATA_API_KEY"):
            return {"error": {"code": "MISSING_API_KEY", "message": "Set THETADATA_API_KEY in the environment of the process launching this plugin."}}
        with contextlib.redirect_stdout(sys.stderr):
            from thetadata.client import ThetaClient
            # Prevent implicit discovery of unrelated .env files.
            client = ThetaClient(api_key=os.environ["THETADATA_API_KEY"],
                                 dotenv_path=os.devnull, dataframe_type="polars")
            service = OptionsService(client)
            if request["operation"] == "expirations":
                return service.expirations(**request["arguments"])
            if request["operation"] == "chain":
                return service.chain(**request["arguments"])
            return {"error": {"code": "INVALID_OPERATION", "message": "Unsupported operation."}}
    except (ImportError, importlib.metadata.PackageNotFoundError):
        return {"error": {"code": "MISSING_DEPENDENCY", "message": "Run scripts/setup.sh with Python 3.12 or newer to install the plugin dependencies."}}
    except Exception as error:
        return {"error": provider_error(error)}


if __name__ == "__main__":
    print(json.dumps(main(), allow_nan=False))
