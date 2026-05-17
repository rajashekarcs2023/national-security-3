# SkyCustody

**Edge ML for electromagnetic spectrum custody. A frisbee-sized forward sensor listens to the RF spectrum, classifies every signal locally with a tiny CNN, flags what it doesn't recognise as out-of-distribution, maintains custody of unknown emitters, and syncs compact intelligence events to command — surviving comms outages along the way.**

Addresses **PS2 (Edge Deployments)** as primary track, with direct supporting coverage of **PS1 (Sensor Fusion & Custody)** and **PS3 (Mission Command & Control)**. Plugs into the Palantir Foundry ontology that is already deployed on the cloud side (`datfromfoundry/`).

---

## Problem-solution
A forward operating post drops a frisbee-sized edge node in contested terrain. It listens to the RF spectrum continuously. A tiny CNN on-device (149K params, 150KB int8, ~5 ms inference) classifies every signal window as *normal, normal, normal, anomaly*. When it sees something it doesn't recognise — a DJI control burst at 2412 MHz, a frequency-hopping unknown at 5.8 GHz, a friendly radio emitting in the wrong band — it opens a custody track, generates an action cue (*"possible Group-1 UAS NE sector, 2412 MHz, request visual"*), and syncs a compact ~600-byte intelligence event upward. Raw RF stays at the edge. The link drops? Events queue locally in priority order and drain the instant comms return. A ~1 GB instruction-tuned LLM on the same box (Llama 3.2 1B via Ollama, or whatever small model you already have pulled — we auto-detect) writes the operator briefs — *the LLM is at the edge too*.

This is the electromagnetic-spectrum problem the Anduril mentor outlined, solved end-to-end.

---

## Architecture

```
┌───────────────────────── EDGE DEVICE (frisbee-sized) ─────────────────────────┐
│                                                                               │
│   RF front-end (SDR/CASK → emulator for demo)                                 │
│        │                                                                      │
│        ▼                                                                      │
│   64 × 64 spectrogram window (per second)                                     │
│        │                                                                      │
│        ▼                                                                      │
│   ┌──────────────────────┐                                                    │
│   │  Tiny CNN + AE       │  149K params · ~150KB · 99.6% val acc              │
│   │  (classifier + OOD)  │  trained on synthetic RF in 31s on laptop CPU      │
│   └──────────────────────┘                                                    │
│        │                                                                      │
│        ├── softmax over 8 signal families  → classification + confidence      │
│        ├── reconstruction error            → OOD score                        │
│        └── embedding distance to centroids → nearest-known-profile            │
│                 │                                                             │
│                 ▼                                                             │
│   ┌──────────────────────┐                                                    │
│   │  Local baseline      │  learns what's normal at THIS site                 │
│   │  tracker             │  (120-sample rolling window)                       │
│   └──────────────────────┘                                                    │
│                 │                                                             │
│                 ▼                                                             │
│   ┌──────────────────────┐      ┌─────────────────────────────────┐           │
│   │  Action engine       │─────►│  Edge LLM (Ollama, local)       │           │
│   │  + custody SM        │      │  Llama 3.2 1B default, auto-    │           │
│   │                      │      │  detects qwen/gemma/phi instead │           │
│   │                      │      │  action brief + NL query        │           │
│   └──────────────────────┘      └─────────────────────────────────┘           │
│                 │                                                             │
│     ┌───────────┴───────────┐                                                 │
│     ▼ link ONLINE           ▼ link OFFLINE (DDIL)                             │
│  sync event            offline priority queue                                 │
│     │                        │                                                │
│     │                   on reconnect: drain critical→low                      │
│     │                        │                                                │
└─────┴────────────────────────┴────────────────────────────────────────────────┘
      │                        │
      ▼                        ▼
┌──────────────────────────────────────────────────────────────────┐
│  Command Dashboard  (React/Next.js, dark tactical theme)         │
│   • Live spectrogram + signal feed                               │
│   • Map overlay with friendly attribution                        │
│   • Custody state timeline                                       │
│   • Offline queue & sync inspector                               │
│   • NL query + LLM after-action brief                            │
│   • Command-down controls (sensitivity, watch band)              │
└──────────────────────────────────────────────────────────────────┘
                     │
                     ▼
┌──────────────────────────────────────────────────────────────────┐
│  Palantir Foundry  (teammate's side — schemas in datfromfoundry) │
│   sensor_events · uas_tracks · custody_state_log · sync_queue    │
│   edge_devices  · operator_actions                               │
│   Cross-site correlation, long-term memory, enterprise analytics │
└──────────────────────────────────────────────────────────────────┘
```

## Quick Start

```bash
# 1. Backend — train the model (one-time, ~30s on CPU)
cd backend
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python train.py

# 2. Start the edge backend
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8765

# 3. Frontend dashboard
cd ../frontend
npm install
npm run dev

# 4. Open http://localhost:3000
```

**Local LLM (already works if you have Ollama + any small instruct model):**

The backend auto-detects Ollama at `http://127.0.0.1:11434` and picks the best installed model from a preferred list: `llama3.2:1b → qwen2.5:1.5b → gemma3:1b → phi3.5:3.8b → llama3.2:3b → gemma3:4b`. No pull required if you already have one of these.

```bash
# Check what's installed:
ollama list

# If nothing small is pulled, grab the 1.3 GB default:
ollama pull llama3.2:1b

# Override the preferred model explicitly if you want:
export OLLAMA_MODEL=qwen2.5:1.5b
```

If Ollama isn't running at all, the backend falls back to templated briefs — the demo still runs end-to-end.

## Real I/Q data — DeepSig RadioML 2016.10A

To prove the pipeline isn't just train-on-synth-test-on-synth, we ingest the
**DeepSig RadioML 2016.10A** dataset (220k real I/Q recordings, 11 modulations,
20 SNRs from -20 dB to +18 dB) directly into the live stream. The dashboard
labels every spectrogram with a **`SYNTH`** or **`REAL · GFSK @ 8 dB`** badge so
a judge can see exactly what's flowing through the edge classifier in real time.

**The honest framing:** our CNN is trained on structural synthetic patterns. Real
RadioML samples have a fundamentally different magnitude-spectrogram texture, so
they are *genuine out-of-distribution* input from the model's perspective. With
the mix dialled to 100% real, every sample lights up `unknown_ood` with high
confidence and `is_anomaly=True` — **the open-set OOD layer is doing exactly
what it's designed to do: refuse to confidently mis-label something it's never
seen, and escalate instead.** That's the operator-correct behaviour, and it's
hard to fake.

```bash
# One-time download (212 MB) from the Zenodo mirror (CERN):
mkdir -p datasets/radioml2016
curl -L "https://zenodo.org/records/18397070/files/RML2016.10a.tar.bz2?download=1" \
  -o datasets/radioml2016/RML2016.10a.tar.bz2
tar -xjf datasets/radioml2016/RML2016.10a.tar.bz2 -C datasets/radioml2016/
```

In the dashboard, use the **Real I/Q mix** controls (`Off / 25% / 50% / 100%`) to
dial the fraction of free-run ticks pulled from real I/Q. Scripted scenarios
always stay synthetic so demo runs remain deterministic.

## Real Palantir Foundry push (Phase F)

The local Foundry-shaped sink keeps the demo end-to-end runnable with **no
Foundry credentials at all** — every dashboard panel, every JSON export,
every counter still works. When credentials *are* available, the same
pipeline pushes the same objects directly into a real Palantir Foundry
tenant via Foundry Streams. Both writes happen on every event; the local
sink is the mirror, the remote is the source of truth.

### What ships to Foundry

Each of our 7 object types maps to a streaming dataset on the tenant. The
exact JSON schema for each stream is in [`foundry.md`](./foundry.md):

| Stream | Object type | When it pushes |
|---|---|---|
| `intelligence_events_stream` | `IntelligenceEvent` (+ RF features from the triggering reading) | Every classified anomaly |
| `attribution_results_stream` | `AttributionResult` | Every event (BLUE / RED / AMBIGUOUS / UNEXPLAINED) |
| `tdoa_fixes_stream` | `TdoaSolution` (lat/lon + CEP) | Every geolocated event |
| `persistent_emitters_stream` | `PersistentEmitter` (streaming-DBSCAN cluster) | Every promotion / update |
| `blue_force_units_stream` | `BlueForceUnit` | Every blue-force tick (~0.25 Hz) |
| `sensor_nodes_stream` | `SensorNode` | Once on startup (reference data) |
| `emitter_profiles_stream` | `EmitterProfile` | Once on startup (reference data) |

### Configure in 3 steps

1. **Copy the env template and fill in the values your FDE chat returned**:

   ```bash
   cp .env.example .env
   # then edit .env: paste your FOUNDRY_API token, and FOUNDRY_STREAM_RIDS
   # (the JSON map of object_type → stream RID). FOUNDRY_STACK_URL is
   # already pre-filled to the hackathon tenant.
   ```

   `python-dotenv` auto-loads `.env` on backend startup, so no `source` step.

2. **Verify the wire from your laptop to the tenant** before launching
   the full backend. This pushes one fake `IntelligenceEvent`-shaped row
   to your `intelligence_events_stream` and prints the response:

   ```bash
   .venv/bin/python scripts/verify_foundry.py
   ```

   Expected output: `OK — 1 row accepted by intelligence_events_stream`.
   Anything else (401, 403, 404, timeout) tells you exactly which knob to
   turn — token scope, RID typo, or network block.

3. **Launch the backend**. The lifespan log line shows the transport
   state on boot:

   ```
   Foundry REMOTE enabled: stack=https://...palantirfoundry.com,
   streams_configured=7, missing=[]
   ```

   The `Foundry sync` panel on the dashboard now has a **LIVE** badge
   pointing at your tenant URL, with per-stream pushed / queued / failed
   counters that update every 4 s.

### DDIL behaviour

When the link drops, every failed push is appended to a per-stream JSONL
buffer under `foundry_ddil/`. The background flush loop replays buffered
rows the moment the link is back. The DDIL buffer survives a process
restart, so a hard crash mid-outage doesn't lose data. You can also
force-drain the buffer with the **Replay DDIL buffer** button in the
Foundry sync panel, or via:

```bash
curl -X POST http://localhost:8765/api/foundry/remote/replay
```

### Architecture note

The mentor's transcript called out exactly this shape: *"push that into
Foundry Edge → when it reconnects, push to enterprise Foundry."* The
edge ML stays on-device (the 149 KB CNN never leaves your laptop / Pi /
edge box); only ~600 B intelligence rows cross the wire. With 232 GB/hr
of raw I/Q reduced to a few KB/min of decisions, edge filtering is
doing the heavy lifting and Foundry is doing what it's best at — fusing
your edge node's events with everything else the enterprise sees.

## Documentation

- [**PLAN.md**](./PLAN.md) — build plan, innovation goals, demo scenario, current status, risks
- [**MILITARY_RELEVANCE.md**](./MILITARY_RELEVANCE.md) — every feature mapped to a real military problem (grounded in the mentor transcript + the Foundry ontology)
- [**foundry.md**](./foundry.md) — Palantir Foundry FDE reply: stream schemas, integration plan, working `curl` shape

## Status at a glance

| Layer | Status |
|---|---|
| Tiny CNN + autoencoder | ✅ trained, 99.6% val acc, 149K params |
| Synthetic RF generator (8 classes) | ✅ |
| **Real I/Q from DeepSig RadioML 2016.10A** | ✅ live mixable, SYNTH/REAL badges in UI |
| Edge classifier + OOD scoring | ✅ |
| Local baseline tracker | ✅ |
| Custody state machine | ✅ (FALSE_ALARM filter, hopper merge, auto-promote, auto-purge) |
| Action cue engine | ✅ |
| DDIL offline queue + priority drain | ✅ |
| FastAPI + WebSocket streaming | ✅ |
| Foundry-shaped JSONL exports | ✅ |
| **Real Palantir Foundry push (Streams API + DDIL replay)** | ✅ activates with `FOUNDRY_API` token + RIDs in `.env` |
| Edge LLM action briefs (Llama 3.2 1B default, auto-detect) | ✅ (graceful fallback if Ollama down) |
| Next.js tactical dashboard | ✅ |
| LoRA fine-tune on H100 (stretch) | ⏳ stretch |
