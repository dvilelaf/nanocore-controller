# USB audio: Nanocore through the PC speakers

The NANOCORE is a driver-free (class-compliant) USB audio interface, so Linux exposes it as a normal sound card (`hw:6,0` on the development PC) next to its USB MIDI port. This is separate from the controller: `nanocore` configures the device, while the setup below only moves audio.

To hear the guitar through the PC speakers, the capture side of the Nanocore is routed to the speaker output in software. The device's own headphone output has zero latency, but it does not play through the PC.

## Requirements

- PipeWire with the PulseAudio compatibility layer (`pipewire-pulse`) and WirePlumber. On elementary OS 7.1 / Ubuntu 22.04:

  ```bash
  sudo apt install pipewire-audio-client-libraries pipewire-pulse wireplumber libspa-0.2-bluetooth
  systemctl --user --now disable pulseaudio.service pulseaudio.socket pipewire-media-session.service
  systemctl --user mask pulseaudio.service pulseaudio.socket
  systemctl --user --now enable pipewire pipewire-pulse wireplumber
  pactl info | grep -i "nombre del servidor\|server name"   # PulseAudio (on PipeWire ...)
  ```

  This replaces PulseAudio for the whole session. To roll back, disable those three services, unmask `pulseaudio.service` and `pulseaudio.socket`, and enable them again.
- The default PulseAudio setup was tried first (`module-loopback`, 4 ms target) and gave about 17 ms: roughly 4.6 ms in the loopback buffer plus 12.4 ms in the USB speakers' sink. PipeWire at 64 or 32 samples feels noticeably better.

## Install the service

The script `contrib/nanocore-loopback/nanocore-loopback` watches for the Nanocore input and the speaker sink every 2 seconds. While both exist it runs `pw-loopback` with the chosen buffer and sets the levels. It stops the loopback when either disappears, so plugging and unplugging the device needs no manual step.

```bash
mkdir -p ~/.local/bin ~/.config/systemd/user
install -m 755 contrib/nanocore-loopback/nanocore-loopback ~/.local/bin/
install -m 644 contrib/nanocore-loopback/nanocore-loopback.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now nanocore-loopback.service
```

It starts with the login session. It does not run at the login screen.

The device names are matched by pattern (`alsa_input.*Nanocore.*analog-stereo` and `Q_Acoustics.*analog-stereo`). If your speakers are not the Q Acoustics M20, change the `sink=` pattern in the script. List candidates with `pactl list short sinks`.

## The web editor starts with the pedal

The same service also runs the editor's server while the Nanocore is plugged in over USB, and opens it in your browser (`xdg-open`) unless a page already has it open. It asks the server (`GET /api/health` reports `clients`, the pages connected) and gives an open page 8 seconds to reconnect after the server starts, so a tab you left open is reused. Tabs are never opened more often than every 30 seconds. Unplugging the pedal stops the server.

The server and the command line cannot share the USB port. To run a `nanocore` command or a script:

```bash
touch ~/.cache/nanocore-loopback.pause   # the server stops; the audio keeps running
rm ~/.cache/nanocore-loopback.pause      # the server comes back
```

Settings go in `~/.config/nanocore-loopback.env` (a shell file read by the script), for example:

```bash
NANOCORE_BIN=/path/to/nanocore-controller/.venv/bin/nanocore   # default: nanocore, from PATH
export NANOCORE_EAD_DECRYPTOR="..."   # optional, see docs/ead-decryptor.md
SERVE_ARGS=(--read-only)              # extra arguments for `nanocore serve`
OPEN_BROWSER=no                       # start the server but never open a tab
```

The server runs with `--no-token`: it listens on 127.0.0.1 only, but any program on the computer can change the presets. The automatic opening depends on it: with a token the page address changes on every start, and the script does not read it.

## Tuning

Edit the variables at the top of the script (or set them in the settings file), then `systemctl --user restart nanocore-loopback`:

| Variable | Default | Meaning |
|---|---|---|
| `QUANTUM` | `64` | Buffer size in samples at 48 kHz |
| `SINK_VOL` | `70%` | Speaker output volume, set every time the loopback starts |
| `STREAM_VOL` | `160%` | Software gain on the loopback stream |
| `SPEAKERS` | `Q_Acoustics.*-stereo$` | Regular expression for the output to play through |
| `SERVE_PORT` | `8765` | Port of the editor's server |

Buffer size changes latency and stability, not sound quality. Smaller buffers cost less delay but leave less margin for the CPU:

| Samples | Buffer delay |
|---|---|
| 32 | 0.7 ms |
| 64 | 1.3 ms |
| 128 | 2.7 ms |
| 256 | 5.3 ms |
| 1024 (PipeWire default) | 21 ms |

Use the smallest value that gives no clicks. The `ERR` column of `pw-top` counts xruns for each node. If it keeps rising while you play, raise `QUANTUM` to 128.

The Nanocore is a USB full-speed device that only runs at 44.1 kHz and delivers audio in 1 ms USB frames (45 samples). Going below about 45 samples in the graph therefore gains nothing, and 32 samples was no better than 64 in practice. The playback side (M20) already uses 16-sample periods.

The rest of the delay comes from outside the graph: the Nanocore's USB capture frames, the USB speakers, and the clock matching between the two devices (the Nanocore capture endpoint is asynchronous and the graph runs at 48 kHz). A rough total is 5 to 15 ms; it is an estimate, not a loopback measurement. For latencies a player perceives as zero (under about 5 to 10 ms), the Nanocore's own headphone output is the only zero-latency path.

With the Nanocore on a root port, `ERR` stays at a few counts or rises slowly (about 4 in 20 s at 64 samples). On a shared hub it rose continuously (about 7 in 20 s) and the audio was noisy.

## Level troubleshooting

The Nanocore line input is quiet at the default levels. With the stream at 100% and the speakers at 35% the guitar was barely audible, so the defaults above add gain on the stream. If a strong preset distorts, lower `STREAM_VOL` to about 120% and raise the speakers instead. The Nanocore's own output volume and the active preset's volume also count.

Per-stream volumes on PulseAudio were remembered by `module-stream-restore`. PipeWire does not carry that over, which is why the 160% has to be set explicitly.

## USB connection

- Plug it into a motherboard port, ideally USB 2.0. Front-panel ports work but are more prone to noise and voltage drop.
- A hub is fine if it has its own power supply. Unpowered hubs can cause clicks, dropouts, or low current, and the Nanocore also charges its battery over the same USB-C port.
- Do not share the hub with the speakers' own USB audio, a microphone, or a webcam. All of them use isochronous USB audio or video bandwidth.
- A good shielded USB 2.0 extension cable up to about 3 m is a safe alternative. The USB 2.0 limit is 5 m per segment.
- Check where a port really goes with `lsusb -t` and `lsusb | grep -i nanocore`. A rear-panel port is not necessarily a root port: on the development PC the first rear port tested was behind a 4-port internal hub (Bus 9) shared with the speakers, keyboard and a receiver. A root port shows the Nanocore alone on its bus (it was on Bus 001 here).
- Plugging the Nanocore into a neighbouring port of the speakers' hub reset the speakers (`device not accepting address ... error -71` in `journalctl -k`). That error is an electrical or signal problem during USB enumeration. Keep the audio devices on separate buses.

## Operations

```bash
systemctl --user status nanocore-loopback          # state
journalctl --user -u nanocore-loopback -n 30       # logs
systemctl --user disable --now nanocore-loopback   # remove
pw-link -l | grep nanocore_                        # see the links
```

Run the loopback by hand without the service:

```bash
PIPEWIRE_LATENCY="32/48000" pw-loopback \
  -C alsa_input.usb-Ember_Nanocore_XXXXXXXXXX-00.analog-stereo \
  -P alsa_output.usb-C-Media_Electronics_Inc._Q_Acoustics_M20-00.analog-stereo \
  --capture-props='node.name=nanocore_in node.passive=false' \
  --playback-props='node.name=nanocore_out'
```

The `XXXXXXXXXX` in the input name is the device serial.

## Troubleshooting

**No sound although the service is running.** Work from the source to the speakers:

1. Is the service doing its job? `systemctl --user status nanocore-loopback` and `pw-link -l | grep nanocore_` (four links, two from the Nanocore capture and two to the speaker playback).
2. Is signal leaving the Nanocore? Record a few seconds while playing. Use the PipeWire **node id**, not the index that `pactl` prints; they differ. Get it with `pw-dump` or `wpctl status`, then:

   ```bash
   pw-record --target <node-id> --rate 44100 --channels 2 --format s16 /tmp/in.wav
   ```

   All zeros means the device is silent. The capture can also be tested directly from ALSA with the service stopped: `arecord -D hw:<card>,0 -f S16_LE -r 44100 -c 2 -d 5 /tmp/in.wav`.
3. Is signal reaching the speakers? `parec -d <speaker-sink>.monitor --rate=48000 --format=s16le --channels=2 --raw` shows the level in the sink.
4. If the signal reaches the sink but nothing is heard, the speakers are not listening to USB. See below.

**The speakers do not play although the PC sends audio (Q Acoustics M20 HD).** The speakers can be connected over USB and Bluetooth at the same time and they follow the last active input. Disconnect the Bluetooth link and they return to USB:

```bash
bluetoothctl disconnect <speaker-address>
```

Reconnect later with `bluetoothctl connect <speaker-address>`. While the Bluetooth link is up, the PC's default sink can also move to the Bluetooth speaker if the USB card disappears.

**The speakers vanish from the sound list after a USB reset.** The kernel still has the card but PipeWire did not recreate its sink. Restarting WirePlumber rescans the cards and the loopback service reconnects by itself a couple of seconds later:

```bash
systemctl --user restart wireplumber
```

**Noise or a brief high whistle when muting the strings.** Rule out USB first (root port, no shared hub, `ERR` stable). Then lower `STREAM_VOL`, because 160% also amplifies the noise floor of a strong preset. A short whistle on string damping is typical of acoustic feedback when the guitar is near the speakers, or of a high-gain preset with a gate. Listening through the Nanocore's headphone output tells them apart: if the whistle is still there, it is in the preset.

**The device is not available to the controller over Bluetooth.** The `nanocore` command defaults to Bluetooth, and the saved profile pins an adapter (`hci1` here) that may not exist after a reboot or replug. Check `bluetoothctl list` and reconfigure with `nanocore configure <address> --adapter hci0`. `status` is not supported over USB MIDI.
