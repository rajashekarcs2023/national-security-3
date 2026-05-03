"""Discover the actual schema of each Foundry stream so we can shape
records to match.
"""
import json
import os

import httpx
from dotenv import load_dotenv

load_dotenv()

stack = (os.environ.get("FOUNDRY_STACK_URL") or "https://nshackathon.palantirfoundry.com").rstrip("/")
token = os.environ.get("FOUNDRY_API") or os.environ.get("FOUNDRY_TOKEN") or ""
rid = "ri.foundry.main.dataset.0476ff6d-56ab-4e4d-9c55-611103f6cc0e"

candidates = [
    # v2 streams API (matches the publishRecord URL family that worked):
    f"{stack}/api/v2/streams/datasets/{rid}",
    f"{stack}/api/v2/streams/datasets/{rid}/streams/master",
    f"{stack}/api/v2/streams/datasets/{rid}?preview=true",
    f"{stack}/api/v2/streams/datasets/{rid}/streams/master?preview=true",
    # v1 dataset schema:
    f"{stack}/api/v1/datasets/{rid}/schema",
    f"{stack}/api/v1/datasets/{rid}/branches/master/schema",
    # highScale variant:
    f"{stack}/api/v2/highScale/streams/datasets/{rid}/streams/master",
    f"{stack}/api/v2/highScale/streams/datasets/{rid}/streams/master?preview=true",
    # Schema-metadata service:
    f"{stack}/foundry-schema-inference/api/datasets/{rid}/branches/master/schema",
    f"{stack}/foundry-metadata/api/schemas/datasets/{rid}/branches/master/schema",
]

with httpx.Client(timeout=httpx.Timeout(10.0)) as c:
    for url in candidates:
        try:
            r = c.get(url, headers={"Authorization": f"Bearer {token}"})
            tag = "OK " if r.status_code // 100 == 2 else "   "
            print(f"{tag}{r.status_code}  {url[len(stack):]}")
            if r.status_code // 100 == 2 and r.text:
                # Pretty-print the schema if we got one.
                try:
                    j = r.json()
                    print(json.dumps(j, indent=2)[:1500])
                    print("...")
                except Exception:
                    print(f"      body: {r.text[:500]}")
            elif r.text and "envoy-error-page-assets" not in r.text:
                print(f"      body: {r.text[:200]}")
            print()
        except httpx.RequestError as e:
            print(f"ERR  {url[len(stack):]}: {type(e).__name__}: {e}\n")
