# Lua Script Verification (Deterministic Splice Stepper)

Checked against Disting NT Lua API, User Manual, and Morphagene (ORGANIZE 0–5 V, PLAY gate).

## Algorithm shape

| Item | Script | Status |
|------|--------|--------|
| Name | `"MG Deterministic Splice Stepper"` | OK |
| `init` | Returns inputs, outputs, parameters, midi (note + cc) | OK |
| `step(self, dt, inputs)` | Returns OUTPUT_BUFFER (CV, gate) | OK |
| `draw(self)` | drawText, drawBox, drawRectangle | OK |
| `trigger(self, input)` | Step clock; deterministic 1→2→…→n→1 | OK |

## Parameters

| # | Name | Min | Max | Default | Notes |
|---|------|-----|-----|---------|------|
| 1 | Splices | 0 | 96 | 20 | Valid splice count; 0 = none |
| 2 | Pulse ms | 1 | 1000 | 20 | PLAY gate length |
| 3 | Page | 1 | pageCount(96) | 1 | UI paging |
| 4 | MIDI ch | 1 | 16 | 1 | USB MIDI channel |

## Outputs

- **OUT1 (kLinear):** ORGANIZE CV, 0–5 V, center-of-bin per splice.
- **OUT2 (kGate):** 5 V while pulse active, else 0.

## NT → Grid protocol (Lua sends)

| Message | Meaning |
|---------|---------|
| CC12 value 0 | Clear playhead |
| CC13 value 0–96 | Splice count (dim LEDs) |
| Note On vel 100 | Playhead at note index (page-local) |

## Grid → NT

- **CC127:** Bridge requests state; Lua replies with CC13 + playhead or CC12 clear (after `recomputeAndClampParams`).
- **Note On (grid key):** Currently ignored (deterministic mode).

## Callbacks

| Callback | Role |
|----------|------|
| encoder1Turn | Cursor within page (on-module UI only) |
| encoder2Turn | Page change |
| encoder3Turn | Splices 0–96; `resolve_current_splice_after_count_change`; sendSpliceCount |
| button1Push | Reserved (recomputeAndClampParams only) |
| button2Push | Stop; sendPlayheadClear |
| button3Push | Reset playhead to splice 1 (no start) |
| button4Push | Stop; sendPlayheadClear |

## Playback

- Start at splice 1 on first trigger after stop.
- Advance 1→2→…→n; wrap to 1.
- No L-system or generative logic; no per-step enable/disable.
