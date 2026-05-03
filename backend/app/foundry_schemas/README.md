# SpectrumCustody — Foundry Object Type Schemas

These YAML files declare the Palantir-Foundry-compatible **Object Types**
(AKA "ontology entries") that the edge node emits.

In a real deployment each YAML maps onto a Foundry Dataset that stores the
column-shaped JSONL the edge ships with. The edge node's outbound payloads
in `app/pipeline/foundry_push.py` are *intentionally* the same shape as
these schemas so an analyst running Foundry can ingest them with a 5-line
transform — no schema gymnastics, no Python in the middle.

| Object Type             | YAML                              | Source module                           |
| ----------------------- | --------------------------------- | --------------------------------------- |
| `IntelligenceEvent`     | `intelligence_event.yml`          | `app.schemas.IntelligenceEvent`         |
| `Attribution`           | `attribution.yml`                 | `app.schemas.AttributionResult`         |
| `TDOAFix`               | `tdoa_fix.yml`                    | `app.schemas.TdoaSolution`              |
| `PersistentEmitter`     | `persistent_emitter.yml`          | `app.schemas.PersistentEmitter`         |
| `BlueForceUnit`         | `blue_force_unit.yml`             | `app.schemas.BlueForceUnit`             |
| `SensorNode`            | `sensor_node.yml`                 | `app.schemas.SensorNode`                |
| `EmitterProfile`        | `emitter_profile.yml`             | `app.schemas.EmitterProfile`            |

The pseudo-Foundry sink at `/foundry/*` accepts these objects via HTTP POST
and writes them to JSONL files in `foundry_data/`. The dashboard polls the
sink's `/foundry/stats` endpoint for a sync indicator.
