# Complete SkyCustody Implementation Guide

## Answer First: Where Does the ML Model Run?

**The model stays on YOUR system (edge device / laptop). NOT on Foundry.**

This is actually the **correct architecture** and a key selling point:

```
YOUR SYSTEM (Edge)                         FOUNDRY (Cloud/Command)
┌─────────────────────────┐               ┌──────────────────────────┐
│ RF Sensor Data (raw)    │               │                          │
│         ↓               │               │  Streams (receive events)│
│ Your CNN (149KB model)  │               │         ↓                │
│         ↓               │               │  Ontology (objects)      │
│ Autoencoder (anomaly)   │               │         ↓                │
│         ↓               │  HTTP POST    │  Workshop (dashboard)    │
│ Classified Events ──────┼──────────────→│         ↓                │
│ (~512 bytes each)       │  Bearer Token │  Operator Actions        │
│                         │               │                          │
│ If offline → JSONL buf  │               │  Correlation with known  │
│ On reconnect → replay   │               │  data at enterprise level│
└─────────────────────────┘               └──────────────────────────┘
```

**Why this is RIGHT:**
- Edge ML = no cloud dependency = works in DDIL
- Only intelligence (not raw data) touches the network
- Foundry does what it's best at: correlate, visualize, enable operator decisions
- The model can be swapped/upgraded without touching Foundry

---

## Step-by-Step Implementation Plan

### Phase 1: Create Streams in Foundry (20 min)

Create one stream per object type. Do this in the Foundry UI:

**For each of your 7 object types:**

1. Go to `/NatSec Hackathon-32b6df/skycustody Project/`
2. Click **+ New → Stream**
3. Define schema (see below)
4. Throughput: **Normal**
5. Click **Create stream**
6. On the Connect page → Choose **cURL** → **Push with personal token**
7. **Copy the stream RID** (looks like `ri.foundry.main.dataset.xxxx`)

**Stream Schemas to Create:**

#### Stream 1: `intelligence_events_stream` (PRIMARY — create this first)
```json
{
  "event_id": "String",
  "timestamp": "Timestamp",
  "sensor_id": "String",
  "center_frequency_mhz": "Double",
  "bandwidth_khz": "Double",
  "power_dbm": "Double",
  "duration_ms": "Integer",
  "modulation_hint": "String",
  "classification": "String",
  "priority": "String",
  "confidence": "Double",
  "latitude": "Double",
  "longitude": "Double",
  "source_label": "String",
  "anomaly_score": "Double",
  "action_cue": "String"
}
```

#### Stream 2: `attribution_results_stream`
```json
{
  "attribution_id": "String",
  "event_id": "String",
  "timestamp": "Timestamp",
  "verdict": "String",
  "attributed_unit_id": "String",
  "feature_scores": "String",
  "confidence": "Double"
}
```

#### Stream 3: `tdoa_fixes_stream`
```json
{
  "fix_id": "String",
  "event_id": "String",
  "timestamp": "Timestamp",
  "latitude": "Double",
  "longitude": "Double",
  "cep_meters": "Double",
  "method": "String"
}
```

#### Stream 4: `persistent_emitters_stream`
```json
{
  "emitter_id": "String",
  "first_seen": "Timestamp",
  "last_seen": "Timestamp",
  "center_frequency_mhz": "Double",
  "latitude": "Double",
  "longitude": "Double",
  "event_count": "Integer",
  "classification": "String",
  "threat_level": "String"
}
```

#### Stream 5: `blue_force_units_stream`
```json
{
  "unit_id": "String",
  "timestamp": "Timestamp",
  "callsign": "String",
  "latitude": "Double",
  "longitude": "Double",
  "unit_type": "String",
  "status": "String"
}
```

#### Stream 6: `sensor_nodes_stream` (pushed once on startup)
```json
{
  "sensor_id": "String",
  "sensor_name": "String",
  "latitude": "Double",
  "longitude": "Double",
  "sensor_types": "String",
  "status": "String",
  "firmware_version": "String"
}
```

#### Stream 7: `emitter_profiles_stream` (pushed once on startup)
```json
{
  "profile_id": "String",
  "emitter_name": "String",
  "frequency_min_mhz": "Double",
  "frequency_max_mhz": "Double",
  "modulation": "String",
  "affiliation": "String",
  "threat_category": "String"
}
```

> **Tip:** Use **"Generate from JSON sample"** button when creating each stream — paste a sample JSON object and it auto-generates the schema.

---

### Phase 2: Test Push (5 min)

After creating the first stream (`intelligence_events_stream`), test it:

```bash
# Replace with YOUR values
STACK="https://YOUR-STACK.palantirfoundry.com"
TOKEN="YOUR_BEARER_TOKEN"
STREAM_RID="ri.foundry.main.dataset.XXXXX"  # from stream creation

# Push a test record
curl -X POST "$STACK/stream/api/streams/$STREAM_RID/branches/master/jsonRecords" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '[
    {
      "value": {
        "event_id": "EVT-TEST-001",
        "timestamp": "2026-05-03T14:30:00Z",
        "sensor_id": "EDGE-RF-01",
        "center_frequency_mhz": 5805.0,
        "bandwidth_khz": 1200,
        "power_dbm": -44,
        "duration_ms": 300,
        "classification": "DRONE_CONTROL",
        "priority": "HIGH",
        "confidence": 0.87,
        "latitude": 34.0522,
        "longitude": -118.2437,
        "source_label": "drone_control_like",
        "anomaly_score": 0.91,
        "action_cue": "Possible drone control link. Recommend visual confirmation NE sector."
      }
    }
  ]'
```

If you get a `200 OK` → you're in business. Check the stream in Foundry — you should see the record.

---

### Phase 3: Integrate Into Your FastAPI Edge App (30 min)

Add this to your edge application:

```python
"""
foundry_stream_pusher.py
Drop into your FastAPI app. Handles push + DDIL buffering.
"""
import requests
import json
import time
import os
from collections import deque
from threading import Thread, Lock
from datetime import datetime, timezone

class FoundryStreamPusher:
    def __init__(self, stack_url: str, token: str, stream_rids: dict):
        """
        stack_url: "https://yourstack.palantirfoundry.com"
        token: Bearer token
        stream_rids: {"intelligence_event": "ri.foundry...", "attribution": "ri.foundry...", ...}
        """
        self.stack = stack_url.rstrip('/')
        self.token = token
        self.stream_rids = stream_rids
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }
        self.buffers = {k: deque() for k in stream_rids}
        self.lock = Lock()
        self.online = True
        self.local_buffer_path = "ddil_buffer.jsonl"
        self.stats = {
            "pushed": 0, "queued": 0, "failed": 0,
            "bytes_sent": 0, "raw_bytes_processed": 0
        }

    def push_event(self, stream_key: str, event: dict):
        """Buffer an event for the named stream."""
        with self.lock:
            self.buffers[stream_key].append(event)

    def flush_stream(self, stream_key: str) -> bool:
        """Flush one stream's buffer to Foundry."""
        with self.lock:
            if not self.buffers[stream_key]:
                return True
            events = list(self.buffers[stream_key])
            self.buffers[stream_key].clear()

        stream_rid = self.stream_rids[stream_key]
        payload = [{"value": e} for e in events]

        try:
            resp = requests.post(
                f"{self.stack}/stream/api/streams/{stream_rid}/branches/master/jsonRecords",
                headers=self.headers,
                json=payload,
                timeout=5
            )
            resp.raise_for_status()
            self.stats["pushed"] += len(events)
            self.stats["bytes_sent"] += len(json.dumps(payload))
            self.online = True
            return True

        except requests.exceptions.RequestException as e:
            # Push failed → save to local DDIL buffer
            self.online = False
            with self.lock:
                for evt in reversed(events):
                    self.buffers[stream_key].appendleft(evt)

            # Also persist to disk (survive crashes)
            with open(self.local_buffer_path, "a") as f:
                for evt in events:
                    f.write(json.dumps({"stream": stream_key, "event": evt}) + "\n")

            self.stats["queued"] += len(events)
            print(f"[DDIL] Offline. Queued {len(events)} events for {stream_key}")
            return False

    def flush_all(self):
        """Flush all stream buffers."""
        for key in self.stream_rids:
            self.flush_stream(key)

    def replay_local_buffer(self):
        """Replay DDIL buffer after reconnect."""
        if not os.path.exists(self.local_buffer_path):
            return
        with open(self.local_buffer_path) as f:
            lines = f.readlines()
        if not lines:
            return

        print(f"[DDIL] Replaying {len(lines)} buffered events...")
        for line in lines:
            record = json.loads(line.strip())
            self.push_event(record["stream"], record["event"])

        # Clear the buffer file
        open(self.local_buffer_path, "w").close()
        self.flush_all()

    def start_background_flush(self, interval=2):
        """Background thread: flush every N seconds."""
        def _loop():
            while True:
                time.sleep(interval)
                if any(self.buffers[k] for k in self.buffers):
                    self.flush_all()
                    if self.online:
                        self.replay_local_buffer()
        t = Thread(target=_loop, daemon=True)
        t.start()
        print(f"[Foundry] Background flush started (every {interval}s)")

    def print_stats(self):
        total = self.stats['pushed'] + self.stats['queued']
        print(f"\n[Stats] Pushed: {self.stats['pushed']} | "
              f"Queued: {self.stats['queued']} | "
              f"Sent: {self.stats['bytes_sent']/1024:.1f} KB | "
              f"Online: {'✓' if self.online else '✗'}")


# ============================================================
# USAGE IN YOUR FASTAPI APP
# ============================================================
# 
# pusher = FoundryStreamPusher(
#     stack_url="https://yourstack.palantirfoundry.com",
#     token="eyJ...",
#     stream_rids={
#         "intelligence_event": "ri.foundry.main.dataset.XXXX",
#         "attribution": "ri.foundry.main.dataset.YYYY",
#         "tdoa_fix": "ri.foundry.main.dataset.ZZZZ",
#         "persistent_emitter": "ri.foundry.main.dataset.AAAA",
#         "blue_force": "ri.foundry.main.dataset.BBBB",
#         "sensor_node": "ri.foundry.main.dataset.CCCC",
#         "emitter_profile": "ri.foundry.main.dataset.DDDD",
#     }
# )
# pusher.start_background_flush(interval=2)
#
# # In your classifier callback:
# async def on_classification(result):
#     pusher.push_event("intelligence_event", {
#         "event_id": str(uuid4()),
#         "timestamp": datetime.now(timezone.utc).isoformat(),
#         "classification": result.label,
#         "confidence": result.confidence,
#         ...
#     })
```

---

### Phase 4: Create Object Types for New Streams (I Do This)

Once you create the streams and give me their RIDs, I'll create:
- 7 new object types backed by the streams
- Link types connecting them (IntelligenceEvent → AttributionResult, → TDOAFix, etc.)
- Conditional formatting

---

### Phase 5: Workshop Dashboard (1-2 hrs)

Use AIP Assist in Workshop to build the command picture. All object types (existing 6 + new 7) will be available to wire into widgets.

---

## Summary: What Runs Where

| Component | Where It Runs | Why |
|---|---|---|
| **RF sensor capture** | Your laptop/Pi | Edge-first, no cloud dependency |
| **CNN classifier (149KB)** | Your laptop/Pi | Edge ML, runs offline |
| **Autoencoder anomaly scorer** | Your laptop/Pi | Edge ML, runs offline |
| **DDIL buffer (JSONL)** | Your laptop/Pi | Survives network loss |
| **Push to Foundry** | HTTP from your system → Foundry | Only classified events, ~512B each |
| **Ontology (objects, links)** | Foundry | Enterprise correlation layer |
| **Workshop dashboard** | Foundry | Command picture for operators |
| **Action types (Respond)** | Foundry | Human-in-the-loop decisions |

---

## 🎯 Priority Order (3 Hours Left)

| # | Task | Time | Who |
|---|---|---|---|
| 1 | Create `intelligence_events_stream` in Foundry UI | 5 min | You |
| 2 | Test curl push → confirm record appears | 5 min | You |
| 3 | Create remaining 6 streams | 15 min | You |
| 4 | Integrate `FoundryStreamPusher` into your FastAPI app | 20 min | You |
| 5 | Give me stream RIDs → I create object types | 10 min | Me |
| 6 | Build Workshop dashboard (AIP Assist) | 60 min | You |
| 7 | End-to-end test + record demo video | 30 min | You |

---

**Go create the first stream now and test the curl push. Once it works, paste the stream RID here and I'll start wiring up object types!** ⚡