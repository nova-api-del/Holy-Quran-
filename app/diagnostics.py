#!/usr/bin/env python3
"""Safe YouTube OAuth secret fingerprint diagnostics; never prints credential values."""
import os, json, hashlib
from pathlib import Path
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / "work"
WORK.mkdir(exist_ok=True)
NAMES = ("YOUTUBE_CLIENT_ID", "YOUTUBE_CLIENT_SECRET", "YOUTUBE_REFRESH_TOKEN")

def metadata(value):
    return {
        "present": bool(value),
        "has_outer_whitespace": bool(value) and value != value.strip(),
        "fingerprint_sha256_12": hashlib.sha256(value.encode("utf-8")).hexdigest()[:12] if value else None,
        "length": len(value),
    }

values = {name: os.environ.get(name, "") for name in NAMES}
report = {
    "checked_at_utc": datetime.now(timezone.utc).isoformat(),
    "check": "youtube_oauth_secret_fingerprint_diagnostics",
    "secrets": {name: metadata(value) for name, value in values.items()},
    "oauth": {
        "tested": False,
        "ok": False,
        "note": "OAuth refresh is tested once inside app/main.py immediately before the YouTube upload."
    },
    "diagnosis": []
}
missing = [name for name, value in values.items() if not value]
if missing:
    report["diagnosis"].append("Missing GitHub Actions secrets: " + ", ".join(missing))
if any(value != value.strip() for value in values.values() if value):
    report["diagnosis"].append("One or more OAuth secrets contain leading/trailing whitespace.")
if not missing and not report["diagnosis"]:
    report["diagnosis"].append("Fingerprint baseline recorded. The upload process will compare these fingerprints and perform the actual OAuth refresh using the same environment values.")

(WORK / "diagnostics.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
lines = [
    "# Latest workflow diagnostics", "",
    f"- Checked (UTC): {report['checked_at_utc']}",
    "- Check: YouTube OAuth secret fingerprint baseline",
    "- OAuth refresh: deferred to app/main.py so the exact upload-process credentials are tested once",
    "", "## Secret presence and fingerprints", "",
    "| Secret | Present | Length | Outer whitespace | SHA-256 fingerprint (12 chars) |",
    "|---|---:|---:|---:|---|"
]
for name, item in report["secrets"].items():
    lines.append(f"| {name} | {item['present']} | {item['length']} | {item['has_outer_whitespace']} | {item['fingerprint_sha256_12'] or 'missing'} |")
lines += ["", "## Diagnosis", ""]
lines += [f"- {item}" for item in report["diagnosis"]]
lines += ["", "> Security: raw Client ID, Client Secret, Refresh Token, and Access Token are never written to this report."]
(WORK / "diagnostics.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
print("OAuth secret fingerprint baseline written (credential values redacted).")
