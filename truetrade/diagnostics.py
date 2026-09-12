"""Visible single-line diagnostics for both structured and plain-text log views.

Call only with sanitized status data, never raw exchange payloads or credentials.
"""
import json


def emit(event, details):
    summary = json.dumps(details, ensure_ascii=False, sort_keys=True, allow_nan=False)
    print(json.dumps({"message": f"{event}: {summary}", "event": event,
                      "details": details}, ensure_ascii=False, allow_nan=False), flush=True)
