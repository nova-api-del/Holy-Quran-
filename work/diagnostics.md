# Latest workflow diagnostics

- Checked (UTC): 2026-11-24T22:20:15.780029+00:00
- Check: YouTube OAuth secret fingerprint baseline
- OAuth refresh: deferred to app/main.py so the exact upload-process credentials are tested once

## Secret presence and fingerprints

| Secret | Present | Length | Outer whitespace | SHA-256 fingerprint (12 chars) |
|---|---:|---:|---:|---|
| YOUTUBE_CLIENT_ID | True | 72 | False | d6ba81fd8a0a |
| YOUTUBE_CLIENT_SECRET | True | 35 | False | 36479ed630de |
| YOUTUBE_REFRESH_TOKEN | True | 125 | False | e57c8275e996 |

## Diagnosis

- Fingerprint baseline recorded. The upload process will compare these fingerprints and perform the actual OAuth refresh using the same environment values.

> Security: raw Client ID, Client Secret, Refresh Token, and Access Token are never written to this report.

## Workflow result

- OAuth diagnostic step: success
- Build and publish step: failure
