"""Dump the actual schema for every stream we configured, so we can
shape outgoing records to match.
"""
import json
import os

import httpx
from dotenv import load_dotenv

load_dotenv()

stack = (os.environ.get("FOUNDRY_STACK_URL") or "https://nshackathon.palantirfoundry.com").rstrip("/")
token = os.environ.get("FOUNDRY_API") or os.environ.get("FOUNDRY_TOKEN") or ""

# Parse the rids map from env (same logic as foundry_remote.py).
raw = os.environ.get("FOUNDRY_STREAM_RIDS", "")
rids = json.loads(raw) if raw else {}

if not rids:
    print("FOUNDRY_STREAM_RIDS not set in .env")
    raise SystemExit(1)


def shape(field: dict) -> str:
    """Compact one-line representation of a field's type."""
    t = field["schema"]["dataType"]["type"]
    nullable = field["schema"].get("nullable", True)
    return f"{t}{'?' if nullable else ''}"


with httpx.Client(timeout=httpx.Timeout(10.0)) as c:
    for stream_key, rid in rids.items():
        url = f"{stack}/api/v2/streams/datasets/{rid}/streams/master"
        r = c.get(url, headers={"Authorization": f"Bearer {token}"})
        print("=" * 70)
        print(f"{stream_key}  →  {rid[:50]}...")
        print("=" * 70)
        if r.status_code != 200:
            print(f"  ERR {r.status_code}: {r.text[:300]}\n")
            continue
        data = r.json()
        schema = data.get("schema", {})
        fields = schema.get("fields", [])
        keys = schema.get("keyFieldNames", [])
        print(f"  branch:    {data.get('branchName')}")
        print(f"  view:      {data.get('viewRid')}")
        print(f"  type:      {data.get('streamType')}")
        print(f"  keys:      {keys}")
        print(f"  fields:    ({len(fields)} columns)")
        for f in fields:
            print(f"    - {f['name']:30s}  {shape(f)}")
        print()
