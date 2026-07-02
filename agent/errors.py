"""JSON-RPC 2.0 error helpers for the Mindgate A2A dispatcher.

Standard JSON-RPC error codes plus A2A-specific application error code
(-32000) used to carry the spec error code string in the response data.
"""

# Standard JSON-RPC 2.0 error codes
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

# Application-level error code for A2A spec errors (HMAC, state machine, etc.)
A2A_ERROR = -32000


def rpc_error(request_id, code: int, message: str, data: dict | None = None) -> dict:
    """Return a JSON-RPC 2.0 error envelope for a failed method call."""
    error_obj: dict = {
        "code": code,
        "message": message,
    }
    # JSON-RPC 2.0 §5.1: "data" is OPTIONAL — only include when present.
    if data:
        error_obj["data"] = data
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": error_obj,
    }
