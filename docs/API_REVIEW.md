# Review of the supplied official reference — 2026-09-12

The user supplied the exchange's API reference text and a Railway log export.
This review replaces uncertainty about the signing rules with the supplied
official contract; it does not imply a successful authenticated request.

Confirmed by the supplied reference:
- Timestamp is Unix milliseconds; method uppercase; signature lowercase HMAC-SHA256.
- Signed URI includes the transmitted query string; body is never signed.
- `trade-futures` covers both reads and writes across futures endpoints.
- `walletType` accepts `debit` (default) or `credit`.
- `size` is contract quantity; `cost` is quote collateral; `notionalSize` is position value.
- IP rejection, invalid signature and expired timestamp can share 401/403 errors;
  the HTTP status alone does not identify the cause.

Still absent from the supplied text:
- A demo API selector and server-side confirmation that the account is demo.
  Neither debit nor credit is described as demo in this reference.
- Expanded query/body details for the listed endpoint accordion panels.
- Market precision/contract multiplier and full balance/protection/funding schemas.

The supplied log export contains Starting Container and two empty info entries.
It provides no readable preflight result and cannot establish authentication success
or failure. The previous code emitted structured JSON without a message field.
Railway documents message as required log content:
https://docs.railway.com/observability/logs#structured-logs

The logging fix adds a message containing the sanitized status summary, retains
structured details, and explicitly logs missing credentials. It changes no signing
rules or exchange write permissions. Check the new deployment's connection_preflight
line before diagnosing the key, signature, allowlist or scope.

The subsequent JSON export (`logs.1789205950975.json`) shows a running research
worker and an HTTP 403 response that could not be parsed as JSON. This does not
identify the rejecting layer or establish whether credentials are valid.
Preflight now retains profile and markets failures separately, with safe response
classifications (content type, HTML/empty body, size and recognized server/header
indicators). No response body, arbitrary header values or credentials are logged.
These indicators are diagnostic hints, not proof of the rejection's cause.
After deploying this change, inspect the new `connection_preflight` entry.
