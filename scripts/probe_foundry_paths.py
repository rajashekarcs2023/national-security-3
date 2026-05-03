"""One-shot probe: which Foundry streams push URL works on this tenant?

Tries the 3 FDE-suggested variants with the value-as-OBJECT payload
(not stringified). Prints status + body for each. Run from repo root:

    backend/.venv/bin/python scripts/probe_foundry_paths.py
"""
import os
import sys

import httpx
from dotenv import load_dotenv

load_dotenv()

DEFAULT_STACK = "https://nshackathon.palantirfoundry.com"
stack = (os.environ.get("FOUNDRY_STACK_URL") or DEFAULT_STACK).rstrip("/")
token = os.environ.get("FOUNDRY_API") or os.environ.get("FOUNDRY_TOKEN") or ""
if not token:
    print("Missing FOUNDRY_API in .env", file=sys.stderr)
    sys.exit(1)
print(f"stack: {stack}")
print(f"token: {'*' * 8}{token[-6:] if len(token) > 6 else ''}")
print()

rid = "ri.foundry.main.dataset.0476ff6d-56ab-4e4d-9c55-611103f6cc0e"
row = {
    "event_id": "probe_001",
    "timestamp": "2026-05-03T18:24:00.000Z",
    "sensor_id": "EDGE-VERIFY",
    "center_frequency_mhz": 2412.0,
    "classification": "verify",
}
payload = [{"value": row}]  # value is an OBJECT per the FDE fix

# 1) AUTH CHECK — does the token work for the dataset GET?
auth_check_url = f"{stack}/api/v1/datasets/{rid}"
print(f"AUTH CHECK: GET {auth_check_url}")
with httpx.Client(timeout=httpx.Timeout(10.0)) as c:
    r = c.get(auth_check_url, headers={"Authorization": f"Bearer {token}"})
    print(f"  -> {r.status_code} {r.text[:300] if r.text else ''}")
print()

# 2) PUSH PATH PROBE — try every variant we can find documented anywhere.
view = "ri.foundry-streaming.main.view.b264182e-5b4b-4d01-a245-a01ba16769fd"
DP = f"{stack}/foundry-data-proxy"  # this base routes to a real service

# PRIORITY PROBE: these two URLs returned 405 (= "wrong method"), so the
# routes exist. Try every plausible verb to discover the right one.
VERB_PROBE = [
    f"{DP}/streams/v1/{rid}/branches/master/records",
    f"{DP}/streams/api/v2/{rid}/branches/master/records",
    f"{DP}/streams/v1/{rid}/jsonRecords",
    f"{DP}/streams/api/v2/{rid}/jsonRecords",
]
print("=" * 60)
print("VERB PROBE — try every HTTP method on the 405 URLs:")
print("=" * 60)
with httpx.Client(timeout=httpx.Timeout(10.0)) as c:
    for url in VERB_PROBE:
        print(f"\n  URL: {url[len(stack):]}")
        for verb in ("GET", "POST", "PUT", "PATCH", "OPTIONS"):
            try:
                r = c.request(
                    verb, url,
                    json=payload if verb in ("POST", "PUT", "PATCH") else None,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                    },
                )
                allow = r.headers.get("allow") or r.headers.get("Allow") or ""
                body = (r.text or "").strip()[:120].replace("\n", " ")
                print(f"    {verb:7s} -> {r.status_code}  allow={allow!r}  body={body!r}")
            except httpx.RequestError as e:
                print(f"    {verb:7s} -> ERR {type(e).__name__}: {e}")
print()

candidates = [
    # /foundry-data-proxy variants — this one returned a structured Foundry
    # 404 above, so the path family is right.
    ("data-proxy + dataset rid + jsonRecords (original)",
     "POST", f"{DP}/api/streams/{rid}/branches/master/jsonRecords"),
    ("data-proxy + view rid + jsonRecords",
     "POST", f"{DP}/api/streams/{view}/branches/master/jsonRecords"),
    ("data-proxy + dataset rid (no branch)",
     "POST", f"{DP}/api/streams/{rid}/jsonRecords"),
    ("data-proxy + dataset rid + records (no branch)",
     "POST", f"{DP}/api/streams/{rid}/records"),
    ("data-proxy + dataset rid + records (with branch)",
     "POST", f"{DP}/api/streams/{rid}/branches/master/records"),
    ("data-proxy + datasets/{rid}/streams/master",
     "POST", f"{DP}/api/datasets/{rid}/streams/master/jsonRecords"),
    ("data-proxy + datasets/{rid}/branches/master/streams",
     "POST", f"{DP}/api/datasets/{rid}/branches/master/streams/jsonRecords"),
    ("data-proxy + dataset rid (record singular)",
     "POST", f"{DP}/api/streams/{rid}/branches/master/jsonRecord"),
    # Some Foundry stacks expose the streaming-write endpoint under
    # /foundry-data-proxy/streams/v1/...
    ("data-proxy + streams/v1 + dataset rid",
     "POST", f"{DP}/streams/v1/{rid}/branches/master/records"),
    ("data-proxy + streams/api/v2 + dataset rid",
     "POST", f"{DP}/streams/api/v2/{rid}/branches/master/records"),
    # The "highScaleCompute" service is sometimes the entry point for
    # streaming writes on hackathon stacks.
    ("/foundry-streaming/api/streams (alt service)",
     "POST", f"{stack}/foundry-streaming/api/streams/{rid}/branches/master/jsonRecords"),
    ("/foundry-streaming/api/streams (view rid)",
     "POST", f"{stack}/foundry-streaming/api/streams/{view}/branches/master/jsonRecords"),
]

with httpx.Client(timeout=httpx.Timeout(10.0), follow_redirects=False) as c:
    for label, method, url in candidates:
        try:
            r = c.request(method, url, json=payload, headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            })
            tag = "OK " if r.status_code // 100 == 2 else "   "
            print(f"{tag}{r.status_code}  {label}")
            print(f"      {method} {url[len(stack):]}")
            loc = r.headers.get("Location") or r.headers.get("location")
            if loc:
                print(f"      -> redirect: {loc}")
            body = (r.text or "").strip()
            # Skip the giant Envoy HTML 404 — it's not informative.
            if body and "envoy-error-page-assets" not in body:
                print(f"      -> body: {body[:300]}")
            print()
        except httpx.RequestError as e:
            print(f"ERR  {label}: {type(e).__name__}: {e}\n")
