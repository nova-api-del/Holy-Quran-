# Latest workflow diagnostics

- Checked (UTC): 2026-11-26T18:17:09.275054+00:00
- Check: YouTube OAuth secret fingerprint baseline
- OAuth refresh: deferred to app/main.py so the exact upload-process credentials are tested once

## Secret presence and fingerprints

| Secret | Present | Length | Outer whitespace | SHA-256 fingerprint (12 chars) |
|---|---:|---:|---:|---|
| YOUTUBE_CLIENT_ID | True | 72 | False | 65413ba891a0 |
| YOUTUBE_CLIENT_SECRET | True | 35 | False | 3bd8865c894b |
| YOUTUBE_REFRESH_TOKEN | True | 127 | False | de3739a0ecc0 |

## Diagnosis

- Fingerprint baseline recorded. The upload process will compare these fingerprints and perform the actual OAuth refresh using the same environment values.

> Security: raw Client ID, Client Secret, Refresh Token, and Access Token are never written to this report.

## Workflow result

- OAuth diagnostic step: success
- Build and publish step: success
