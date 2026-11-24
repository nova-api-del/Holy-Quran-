#!/usr/bin/env python3
"""Safe YouTube OAuth preflight. Never prints or stores credentials/tokens."""
import os, json, hashlib, urllib.parse, urllib.request, urllib.error
from pathlib import Path
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / "work"
WORK.mkdir(exist_ok=True)
NAMES = ("YOUTUBE_CLIENT_ID", "YOUTUBE_CLIENT_SECRET", "YOUTUBE_REFRESH_TOKEN")

def fingerprint(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12] if value else None

values = {name: os.environ.get(name, "") for name in NAMES}
report = {
    "checked_at_utc": datetime.now(timezone.utc).isoformat(),
    "check": "youtube_oauth_refresh_preflight",
    "secrets": {name: {"present": bool(v), "has_outer_whitespace": bool(v) and v != v.strip(),
                       "fingerprint_sha256_12": fingerprint(v), "length": len(v)}
                for name, v in values.items()},
    "oauth": {"tested": False, "ok": False},
    "diagnosis": [],
}
missing = [name for name, value in values.items() if not value]
if missing:
    report["diagnosis"].append("Missing GitHub Actions secrets: " + ", ".join(missing))
elif any(value != value.strip() for value in values.values()):
    report["diagnosis"].append("One or more OAuth secrets contain leading/trailing whitespace. Re-save without spaces or newlines.")
else:
    report["oauth"]["tested"] = True
    payload = urllib.parse.urlencode({
        "client_id": values["YOUTUBE_CLIENT_ID"],
        "client_secret": values["YOUTUBE_CLIENT_SECRET"],
        "refresh_token": values["YOUTUBE_REFRESH_TOKEN"],
        "grant_type": "refresh_token",
    }).encode("utf-8")
    req = urllib.request.Request("https://oauth2.googleapis.com/token", data=payload,
                                 headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            data = json.loads(response.read().decode("utf-8"))
            report["oauth"].update({"ok": bool(data.get("access_token")), "http_status": response.status})
    except urllib.error.HTTPError as exc:
        try:
            data = json.loads(exc.read().decode("utf-8", errors="replace"))
        except Exception:
            data = {}
        error = data.get("error", "unknown_error")
        report["oauth"].update({"ok": False, "http_status": exc.code, "error": error,
                                "error_description": data.get("error_description", "")[:300]})
        if error == "invalid_grant":
            report["diagnosis"].append("Google rejected this refresh_token/client combination (invalid_grant). Likely causes: token issued to a different OAuth client, token revoked/expired, or token pasted incorrectly. Compare the fingerprints below with the values used during token generation.")
        elif error == "invalid_client":
            report["diagnosis"].append("Google rejected the OAuth client credentials (invalid_client). Verify Client ID and Client Secret belong to the same Web client.")
        else:
            report["diagnosis"].append("Google OAuth token endpoint rejected the request; see safe error fields in this report.")
    except Exception as exc:
        report["oauth"].update({"ok": False, "error": type(exc).__name__})
        report["diagnosis"].append("Could not reach Google OAuth token endpoint: " + type(exc).__name__)

if report["oauth"]["ok"]:
    report["diagnosis"].append("OAuth refresh succeeded. These three GitHub secrets work together; if upload fails, inspect the subsequent YouTube API error.")
elif not report["diagnosis"]:
    report["diagnosis"].append("OAuth preflight did not complete; inspect the check fields.")

(WORK / "diagnostics.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
lines = ["# Latest workflow diagnostics", "", f"- Checked (UTC): {report['checked_at_utc']}",
         "- Check: YouTube OAuth refresh token preflight", "", "## Secret presence and fingerprints", "",
         "| Secret | Present | Length | Outer whitespace | SHA-256 fingerprint (12 chars) |",
         "|---|---:|---:|---:|---|"]
for name, item in report["secrets"].items():
    lines.append(f"| {name} | {item['present']} | {item['length']} | {item['has_outer_whitespace']} | {item['fingerprint_sha256_12'] or 'missing'} |")
lines += ["", "## Google OAuth refresh test", "", f"- Tested: {report['oauth']['tested']}",
          f"- Success: {report['oauth']['ok']}"]
for key in ("http_status", "error", "error_description"):
    if key in report["oauth"]:
        lines.append(f"- {key}: {report['oauth'][key]}")
lines += ["", "## Diagnosis", ""]
lines += [f"- {item}" for item in report["diagnosis"]]
lines += ["", "> Security: raw Client Secret, Refresh Token, and Access Token are never written to this report."]
(WORK / "diagnostics.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
print("OAuth diagnostics written to work/diagnostics.md and work/diagnostics.json (credentials redacted).")
