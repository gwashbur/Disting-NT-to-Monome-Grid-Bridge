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

Raspberry Pi documentation
Official setup and reference: [Raspberry Pi Documentation](https://www.raspberrypi.com/documentation/).

- **First-time setup:** Use [Raspberry Pi Imager](https://www.raspberrypi.com/documentation/computers/getting-started.html#install-using-imager) to install Raspberry Pi OS (32 GB+ SD card recommended). You can enable SSH in Imager’s customisation for headless use.
- **Raspberry Pi 5 power:** Use a 5 V/5 A USB‑C supply (e.g. [official 27 W](https://www.raspberrypi.com/documentation/computers/raspberry-pi.html#power-supply)); underpowered USB can cause issues with the grid and Disting NT.
- **USB:** The Monome Grid and Disting NT connect over USB; no extra drivers are needed. If serialosc reports “Permission denied”, add your user to `dialout`/`uucp` (see step 3 below).

Installation

**Note:** On Raspberry Pi OS (Debian), `serialosc` is not in the default apt repositories. Install it by building from source as below. On Ubuntu you can use `sudo add-apt-repository ppa:artfwo/monome` then `apt install serialosc` instead.

1. Install system packages and build dependencies
```bash
sudo apt update
sudo apt install -y python3-pip git \
  liblo-dev libudev-dev \
  libavahi-compat-libdnssd-dev libuv1-dev
```

2. Build and install serialosc (required for Monome Grid)
```bash
# libmonome (dependency)
git clone https://github.com/monome/libmonome.git
cd libmonome
./waf configure
./waf
sudo ./waf install
cd ..

# serialosc
git clone https://github.com/monome/serialosc.git --recursive
cd serialosc
./waf configure
./waf
sudo ./waf install
cd ..

sudo ldconfig
```

3. (Optional) USB access for the grid — if you get "Permission denied" when running serialosc:
```bash
sudo gpasswd -a $USER uucp
sudo gpasswd -a $USER dialout
```
Then log out and back in.

4. Install Python dependencies
```bash
cd /path/to/Disting-NT-to-Monome-Grid-Bridge
python3 -m pip install --upgrade pip
python3 -m pip install -r requirements.txt
```

Running

Connect:

- Disting NT via USB
- Monome Grid via USB

Start the serialosc daemon (in a terminal or before the bridge):
```bash
serialoscd
```

Then run the bridge:
```bash
python3 "Monome Grid Bridge.py"
```

Start at boot (systemd)
To have serialosc and the bridge start automatically on boot (recommended on Raspberry Pi):

1. Copy the service files into systemd (run from the project directory):
```bash
cd /home/admin/Disting-NT-to-Monome-Grid-Bridge
sudo cp systemd/serialoscd.service systemd/monome-grid-bridge.service /etc/systemd/system/
```

2. Reload systemd, enable both services, and start them:
```bash
sudo systemctl daemon-reload
sudo systemctl enable serialoscd.service monome-grid-bridge.service
sudo systemctl start serialoscd.service monome-grid-bridge.service
```

3. Check status:
```bash
sudo systemctl status serialoscd.service
sudo systemctl status monome-grid-bridge.service
```

- **serialosc** starts first; the **bridge** starts after a short delay so the grid can be discovered.
- Logs: `journalctl -u serialoscd.service -f` and `journalctl -u monome-grid-bridge.service -f`.
- To stop auto-start: `sudo systemctl disable serialoscd.service monome-grid-bridge.service`.
- **Port 12002:** serialosc uses UDP port 12002. If the service fails with exit code 255, the port is likely in use (e.g. another serialoscd). Run only the systemd service, or only a manual `serialoscd`—not both. To free the port: `sudo systemctl stop serialoscd.service` and `pkill -x serialoscd`, then start the service again.

If your username is not `admin`, edit both `.service` files and change `User=admin` to your user, and update the paths in `monome-grid-bridge.service` to match your home directory before copying to `/etc/systemd/system/`.

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

