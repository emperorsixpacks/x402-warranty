"""Response validation logic (v3: objective failures only)."""

import json

from models import ExpectConfig, RefundReason


def validate_response(response: dict, expect: ExpectConfig | None) -> tuple[bool, RefundReason | str | None]:
    """Return (is_good, refund_reason).

    Refunds trigger only on objective failures. The buyer's ``expect``
    config is accepted on the request for compatibility but never fails
    the verdict — a missing string or short body is not a refund event.
    """
    del expect  # informational only; never a refund trigger
    status = response.get("status")
    if status is None:
        return False, RefundReason.NETWORK_ERROR
    if status < 200 or status >= 300:
        return False, RefundReason.NON_2XX

    body = response.get("body") or ""
    if not body:
        return False, RefundReason.EMPTY_BODY

    headers = response.get("headers", {})
    content_type = headers.get("content-type", "")

    # If upstream claims JSON but body is invalid JSON, refund
    if "json" in content_type:
        try:
            json.loads(body)
        except json.JSONDecodeError:
            return False, RefundReason.INVALID_JSON

    return True, None
