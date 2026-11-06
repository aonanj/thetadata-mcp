"""Bound SDK subprocesses so hosted requests cannot spawn unlimited workers."""
import asyncio
import json
import os
from pathlib import Path
import sys


def concurrency_limit() -> int:
    try:
        limit = int(os.getenv("THETADATA_MAX_CONCURRENT_REQUESTS", "1"))
    except ValueError:
        raise ValueError("THETADATA_MAX_CONCURRENT_REQUESTS must be an integer from 1 to 8") from None
    if not 1 <= limit <= 8:
        raise ValueError("THETADATA_MAX_CONCURRENT_REQUESTS must be from 1 to 8")
    return limit


slots = asyncio.Semaphore(concurrency_limit())


async def execute(operation: str, arguments: dict) -> dict:
    try:
        await asyncio.wait_for(slots.acquire(), timeout=1)
    except TimeoutError:
        return {"error": {"code": "BUSY", "message": "All ThetaData workers are occupied. Retry after the current request finishes."}}
    try:
        return await run_worker(operation, arguments)
    finally:
        slots.release()


async def run_worker(operation: str, arguments: dict) -> dict:
    worker_env = dict(os.environ)
    worker_env.pop("MCP_API_KEY", None)  # Provider worker does not need the service's client token.
    proc = await asyncio.create_subprocess_exec(
        sys.executable, str(Path(__file__).with_name("worker.py")),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        cwd=str(Path(__file__).parent), env=worker_env,
    )
    try:
        stdout, _ = await asyncio.wait_for(
            proc.communicate(json.dumps({"operation": operation, "arguments": arguments}).encode()), timeout=45)
    except (TimeoutError, asyncio.CancelledError) as error:
        if proc.returncode is None:
            proc.kill()
        await proc.wait()
        if isinstance(error, asyncio.CancelledError):
            raise
        return {"error": {"code": "TIMEOUT", "message": "ThetaData did not finish within 45 seconds. Narrow the expiration and strike range."}}
    if proc.returncode != 0:
        return {"error": {"code": "WORKER_FAILED", "message": "The data worker could not complete. Check dependencies with get_status."}}
    try:
        return json.loads(stdout)
    except (ValueError, UnicodeDecodeError):
        return {"error": {"code": "INVALID_PROVIDER_RESPONSE", "message": "The data worker returned an invalid response."}}
