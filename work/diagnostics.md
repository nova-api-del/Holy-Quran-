# Latest workflow diagnostics

- Checked (UTC): 2026-11-24T21:41:53.756741+00:00
- Check: YouTube OAuth refresh token preflight

## Secret presence and fingerprints

| Secret | Present | Length | Outer whitespace | SHA-256 fingerprint (12 chars) |
|---|---:|---:|---:|---|
| YOUTUBE_CLIENT_ID | True | 72 | False | 1710daecf89a |
| YOUTUBE_CLIENT_SECRET | True | 35 | False | cbee94d164d2 |
| YOUTUBE_REFRESH_TOKEN | True | 127 | False | cba661ebefa4 |

## Google OAuth refresh test

- Tested: True
- Success: False
- http_status: 400
- error: invalid_grant
- error_description: Bad Request

## Diagnosis

- Google rejected this refresh_token/client combination (invalid_grant). Likely causes: token issued to a different OAuth client, token revoked/expired, or token pasted incorrectly. Compare the fingerprints below with the values used during token generation.

> Security: raw Client Secret, Refresh Token, and Access Token are never written to this report.

## Workflow result

- OAuth diagnostic step: success
- Build and publish step: failure
