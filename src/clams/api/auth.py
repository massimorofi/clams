"""Bearer-token auth for the control plane."""
from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse


def check_auth(request: Request, token: str) -> JSONResponse | None:
    """Return an error response if the request is not authorized."""
    if not token:
        return JSONResponse(status_code=503,
                            content={"error": "api token not configured"})
    header = request.headers.get("authorization", "")
    if header.startswith("Bearer ") and header[7:] == token:
        return None
    return JSONResponse(status_code=401, content={"error": "unauthorized"})
