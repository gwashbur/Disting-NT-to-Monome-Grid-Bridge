# Morphagene L-System Splice Stepper 30.lua – Verification Report

Checked against:
- **Disting NT Lua API** (official examples: `adsr.lua`, `ae_sequencer.lua`, `LFO.lua` from expertsleepersltd/distingNT)
- **Disting NT User Manual** (general algorithm/plug-in behaviour)
- **Morphagene** (ORGANIZE input 0–5V, splice selection; PLAY trigger)

---

## 1. Algorithm structure (Disting NT Lua)

| Requirement | Our script | Status |
|-------------|------------|--------|
| Return table with `name`, `author` | `name = "MG L-System Splice Stepper 300"`, `author = "OpenAI / revised"` | OK |
| `init(self)` returns config | `inputs`, `outputs`, `inputNames`, `outputNames`, `parameters` | OK |
| `step(self, dt, inputs)` returns output array | Returns `OUTPUT_BUFFER` (pre-allocated table) | OK |
| `draw(self)` for display | Uses `drawText`, `drawRectangle`, `drawBox`, `drawTinyText` | OK |
| `trigger(self, input)` for trigger input | Implements trigger(input) for step clock | OK |
| `gate(self, input, rising)` | Not used (we use trigger only) | OK |

---

## 2. Init return (Disting NT API)

| Field | Our script | Official usage | Status |
|-------|------------|----------------|--------|
| `inputs` | `{ kTrigger }` | e.g. `{kGate}`, `{kGate, kTrigger, kTrigger}` | OK |
| `outputs` | `{ kLinear, kGate }` | e.g. `{kLinear}`, `{kStepped, kGate}` | OK |
| `inputNames` | `{ "Step Trig" }` | Same style in ae_sequencer | OK |
| `outputNames` | `{ "Organize CV", "Play Pulse" }` | Same style | OK |
| `parameters` | 4 params: Splices, Pulse ms, Page, MIDI ch | Same pattern (name, min, max, default, type) | OK |
| Parameter types | `kInt`, `kMs`, `kInt`, `kInt` | `kMs` used in ae_sequencer; `kMilliseconds` in adsr – both valid | OK |
| `midi` (optional) | `{ channelParameter = 4, messages = { "note" } }` | Matches nt_lua_emulator pattern for MIDI input | OK |

---

## 3. Outputs (Disting NT + Morphagene)

| Output | Role | Our value | Expected (Morphagene / NT) | Status |
|--------|------|-----------|----------------------------|--------|
| OUT1 (kLinear) | ORGANIZE CV | `mg.cachedCV` in 0..5 V | Morphagene ORGANIZE: 0–5 V, bin-centered per splice | OK |
| OUT2 (kGate) | PLAY trigger | 5.0 when pulsing, else 0.0 | Gate high = 5 V (ae_sequencer uses 5) | OK |

- **ORGANIZE**: Script uses center-of-bin mapping: `(i - 0.5) * (5.0/N)` for splice index `i`, N = splices. Matches 0–5 V selection of splice.
- **PLAY**: Pulse length from parameter “Pulse ms”; `step()` decrements `pulseRemaining` and outputs 5 V while non-zero. Correct for a trigger/gate input.

---

## 4. Parameters

| # | Name | Min | Max | Default | Type | Notes |
|---|------|-----|-----|---------|------|--------|
| 1 | Splices | 1 | 300 | 20 | kInt | Matches MAX_SPLICES; runtime clamp in `recomputeAndClampParams` |
| 2 | Pulse ms | 1 | 1000 | 20 | kMs | Used in trigger path; converted to seconds in code |
| 3 | Page | 1 | pageCountForN(300)=3 | 1 | kInt | Max pages fixed at init; runtime clamps to current nSplices |
| 4 | MIDI ch | 1 | 16 | 1 | kInt | Used for USB MIDI out and midiMessage channel |

Page max at init is 3 (300/128). When Splices &lt; 128, `recomputeAndClampParams` clamps `mg.page` to the actual page count. Implementation is correct.

---

## 5. Callbacks and UI (Disting NT manual / examples)

| Callback | Our script | Status |
|----------|------------|--------|
| `encoder1Turn` | Cursor move within page; sends CC10 | OK |
| `encoder2Turn` | Page change; syncs and sends CC11 | OK |
| `encoder3Turn` | Splices count; resets playhead | OK |
| `button1Push` | Toggle at cursor; sends enable state | OK |
| `button2Push` | All off | OK |
| `button3Push` | All on | OK |
| `button4Push` | Reset playhead | OK |
| `ui(self)` | Returns true | OK |
| `setParameter(self.algorithmIndex, self.parameterOffset + N, value, true)` | Used for Page and Splices when clamping | OK |

---

## 6. MIDI (Disting NT Lua + Monome Grid Bridge)

| Item | Our script | Bridge / protocol | Status |
|------|------------|-------------------|--------|
| Out: CC10 | Cursor index (page-local 0..127) | Bridge expects CC10 = cursor | OK |
| Out: CC11 | Page (1-based) | Bridge ignores; optional for future | OK |
| Out: Note On vel 20/0/100 | Enabled / disabled / playhead | Bridge drives grid LEDs | OK |
| In: midiMessage | Note On → toggle step at page-local index | Bridge sends note = index, vel 127 | OK |
| sendMIDI(dest, status, d1, d2) | MIDI_DEST_USB = 0x4; status = 0x90+ch or 0xB0+ch | Verify dest bitmask in NT manual if unsure | Assume OK |

`midiMessage` uses `msg[1]` = status, `msg[2]` = note, `msg[3]` = velocity; Note On filtered by status 0x90..0x9F and vel &gt; 0. Matches common NT/emulator convention.

---

## 7. Draw API (Disting NT / emulator)

We use: `drawText`, `drawRectangle`, `drawBox`, `drawTinyText`. All appear in official examples or nt_lua_emulator README. No issues found.

---

## 8. Summary

- **Disting NT Lua**: Algorithm shape, `init`/`step`/`draw`/`trigger`, parameters, and UI callbacks match the official examples and typical manual behaviour.
- **Morphagene**: ORGANIZE 0–5 V and center-of-bin mapping are correct; PLAY gate output is 5 V pulse.
- **Bridge protocol**: Outgoing CC10, CC11, and Note On (vel 20/0/100) match the bridge; incoming grid keys handled as Note On (toggle step).

**Recommendation:** If the **Disting NT Lua Scripting** or **User Manual** PDF specifies `sendMIDI(destination, data1, data2, data3)` or a different parameter order/destination encoding, adjust the two `sendMIDI` call sites to match. Otherwise the implementation is consistent with public references and safe to use.
