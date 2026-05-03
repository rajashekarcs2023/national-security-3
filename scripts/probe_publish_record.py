"""Confirm the highScale publishRecord endpoint works on this tenant."""
import os
import time

import httpx
from dotenv import load_dotenv

load_dotenv()

stack = (os.environ.get("FOUNDRY_STACK_URL") or "https://nshackathon.palantirfoundry.com").rstrip("/")
token = os.environ.get("FOUNDRY_API") or os.environ.get("FOUNDRY_TOKEN") or ""
rid = "ri.foundry.main.dataset.0476ff6d-56ab-4e4d-9c55-611103f6cc0e"
view = "ri.foundry-streaming.main.view.b264182e-5b4b-4d01-a245-a01ba16769fd"

url = (
    f"{stack}/api/v2/highScale/streams/datasets/{rid}"
    f"/streams/master/publishRecord"
)
record = {
    "timestamp": int(time.time() * 1000),
    "value": (
        '{"event_id":"probe_001","sensor_id":"EDGE-VERIFY",'
        '"classification":"verify"}'
    ),
}
body = {"record": record, "viewRid": view}

print(f"POST {url}")
print(f"body: {body}")
print()
with httpx.Client(timeout=httpx.Timeout(10.0)) as c:
    r = c.post(url, json=body, headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    })
    print(f"status: {r.status_code}")
    if r.text:
        print(f"body  : {r.text}")

# Try without viewRid (per docs, it's optional and writes to "latest stream").
print("\n--- without viewRid ---")
body2 = {"record": record}
with httpx.Client(timeout=httpx.Timeout(10.0)) as c:
    r = c.post(url, json=body2, headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    })
    print(f"status: {r.status_code}")
    if r.text:
        print(f"body  : {r.text}")
