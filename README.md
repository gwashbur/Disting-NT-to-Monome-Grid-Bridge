Disting NT ↔ Monome Grid Bridge (Raspberry Pi 5)

A high-performance Python bridge that connects an Expert Sleepers Disting NT to a Monome Grid 128 using a Raspberry Pi 5.
This project implements a bidirectional MIDI protocol and a fast, buffered LED rendering pipeline using:

pymonome (serialosc)
mido
python-rtmidi
asyncio-based event architecture

The result is a tightly synchronized 1:1 mapping between the Grid (16×8) and NT UI state, with semantic mirroring and flicker-free full-frame updates.

Overview
The bridge provides:
1:1 coordinate mapping (Grid 16×8 → NT 128-step model)
Semantic MIDI protocol for state mirroring
Buffered LED rendering via GridBuffer
Overlay composition (cursor + playhead)
Clean separation between logic and rendering
Auto-detection of Disting NT MIDI ports
Serialosc device discovery for monome Grid

Designed for:
Sequencer control
Morphagene splice navigation
L-system step walkers
Generative pattern systems
Custom NT Lua UI integration
Architecture

Signal flow:

Monome Grid
     │
     │ (OSC via serialosc)
     ▼
Raspberry Pi 5 (Python bridge)
     │
     │ (USB MIDI)
     ▼
Disting NT (Lua script)

Layer Breakdown

On Raspberry Pi
MIDI interface (NT USB)
Framebuffer state model (16×8)
Overlay manager
Grid renderer (buffered full-frame updates)

On Disting NT
Lua algorithm
Semantic state emission (Note/CC messages)
Semantic MIDI Protocol

Grid → NT

Note On: note = y*16 + x, velocity 127
Note Off: velocity 0

NT → Grid (Semantic Mirroring)
Message	Meaning
CC10 value=0–127	Cursor index
NoteOn vel=20	Enabled step
NoteOn vel=0	Disabled step
NoteOn vel=100	Playhead

This avoids SysEx and keeps bandwidth low while preserving state meaning.

Features
Fast full-frame LED updates using GridBuffer
Dirty-flag rendering (no LED spam)
Playhead flashing overlay
Clean semantic state separation
Designed for extensibility (paging, modes, probability layers)

Requirements
Raspberry Pi 5 (Raspberry Pi OS recommended)
Expert Sleepers Disting NT
Monome Grid 128
serialosc installed and running

Installation

1. Install system packages
sudo apt update
sudo apt install -y serialosc python3-pip

2. Install Python dependencies
python3 -m pip install --upgrade pip
python3 -m pip install pymonome mido python-rtmidi
Running

Connect:

Disting NT via USB
Monome Grid via USB
Ensure serialosc is running.

Then run:
python3 grid_nt_bridge.py

You should see:

[MIDI] Connected IN : ...
[MIDI] Connected OUT: ...
[GRID] Connected to device ...
Running. Grid press -> NT notes. NT semantic MIDI -> grid LEDs.
Mapping Model

Index mapping is 1:1 across the system:
index = y * 16 + x
Grid (x,y)
MIDI note number
NT internal step index
Framebuffer index



Brightness Semantics (Grid 0–15)
Level	Meaning
0	     Off
6	     Enabled
12	     Cursor
15	     Playhead

Overlay priority:

Playhead > Cursor > Enabled > Off
Extending the System

Planned / easy extensions:

Paging for >128 NT steps
Probability visualization via brightness scaling
Multiple UI modes (edit/play)
MIDI clock sync
Bidirectional state refresh
Robust hot-reconnect handling

