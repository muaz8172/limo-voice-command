# LIMO Voice Command

![Python](https://img.shields.io/badge/python-3.9%2B-blue)
![Platform](https://img.shields.io/badge/platform-windows-lightgrey)
![Speech](https://img.shields.io/badge/speech%20recognition-offline%20(vosk)-brightgreen)

Voice-controlled command dispatcher for a [LIMO](https://www.agilex.ai/chassis/13) robot competition run. It listens on a Windows PC's microphone, transcribes speech **live and fully offline** using [Vosk](https://alphacephei.com/vosk/), matches the transcript against a small set of known commands, and sends the result to [Node-RED](https://nodered.org/) over a websocket so it can drive the robot.

```
mic -> Vosk (live transcription) -> command matcher -> websocket -> Node-RED
```

## Contents

- [Requirements](#requirements)
- [Setup](#setup)
- [Usage](#usage)
- [Commands](#commands)
- [Files](#files)
- [Node-RED side](#node-red-side)
- [Troubleshooting](#troubleshooting)
- [Acknowledgments](#acknowledgments)
- [License](#license)

## Requirements

- Windows, Python 3.9+
- A working microphone
- A Node-RED instance reachable on the network, with a websocket-in node
  (see [Node-RED side](#node-red-side))

## Setup

1. Install dependencies (Windows cmd/PowerShell, **not** WSL — audio capture
   needs a real Windows mic device):
   ```
   pip install vosk websockets sounddevice
   pip install PyQt6          # only needed for the dashboard
   ```
2. The Vosk speech model (`vosk-model-small-en-us-0.15/`) is already
   included in this repo, so no separate download is needed. (If you swap
   in a different model, update `MODEL_PATH`.)
3. Find your microphone's device index:
   ```
   python windows_voice_client.py --list-devices
   ```
4. Edit the `CONFIG` section at the top of `windows_voice_client.py` for
   your setup:

   | Setting | Purpose | Default |
   |---|---|---|
   | `NODE_RED_IP` | IP address of the Node-RED host | `10.21.215.131` |
   | `PORT` | Node-RED websocket port | `1880` |
   | `PATH` | Websocket path Node-RED listens on | `/voice/muaz` |
   | `MIC_DEVICE_INDEX` | Index from `--list-devices` | `1` |

## Usage

**Console client:**
```
python windows_voice_client.py
```

**PyQt6 dashboard** — connection status, live captions, and a history table:
```
python voice_dashboard.py
```

Both share the same capture/matching/websocket logic in
`windows_voice_client.py`; the dashboard just wraps it in a GUI.

## Commands

| You say            | Sent as             |
|---------------------|----------------------|
| "hello"             | `hello`              |
| "do mission 1"      | `Do Mission One`     |
| "do mission 2"      | `Do Mission Two`     |
| "do mission 3"      | `Do Mission Three`   |

Recognition is never perfect, so matching doesn't rely on an exact string —
it normalizes the text, checks a table of known mis-hearings (e.g. "wine",
"won", "juan" all mean "one"), and falls back to fuzzy matching. See
`NOTES.txt` for the full rationale and tuning history.

Each recognized phrase is sent to Node-RED as JSON:
```json
{
  "command": "Do Mission One",
  "raw_text": "do mishun one",
  "match_reason": "keyword match (number word 'one')",
  "source": "muaz-pc"
}
```
If nothing matches, `command` is sent as `null` (unless `SEND_UNMATCHED` is
set to `False` in the config, in which case unmatched phrases aren't sent
at all).

## Files

| File | Purpose |
|---|---|
| `windows_voice_client.py` | Core logic: audio capture, live transcription, command matching, websocket send |
| `voice_dashboard.py` | PyQt6 GUI wrapper around the same logic |
| `list_mics.py` | Standalone microphone listing helper |
| `speech_log.csv` | Auto-logged history of every recognized phrase, for accuracy analysis |
| `vosk-model-small-en-us-0.15/` | Offline speech recognition model |
| `NOTES.txt` | Setup details, design rationale, and calibration history |

## Node-RED side

Your Node-RED flow should have a websocket-in node listening on the path
configured in `PATH` (default `/voice/muaz`), feeding a switch node that
checks `msg.payload.command` against the exact strings: `hello`,
`Do Mission One`, `Do Mission Two`, `Do Mission Three`.

## Troubleshooting

- **Wrong/no audio captured** — re-run `--list-devices` and double-check
  `MIC_DEVICE_INDEX`; indices can shift after Windows updates or when
  devices are plugged/unplugged. Avoid picking "Stereo Mix" (speaker
  output, not a mic).
- **Client can't connect** — it retries every `RECONNECT_DELAY_SEC`
  automatically; verify `NODE_RED_IP`/`PORT`/`PATH` match your Node-RED
  websocket-in node and that both machines are on the same network.
- **Commands mis-recognized** — check `speech_log.csv` for the actual
  transcribed text and adjust `NUMBER_SYNONYMS`/`CANONICAL_PHRASES` in
  `windows_voice_client.py`; see `NOTES.txt` for the calibration history
  and known limitations.

## Acknowledgments

- [Vosk](https://alphacephei.com/vosk/) for the offline speech recognition
  engine and pretrained models.
- [sounddevice](https://python-sounddevice.readthedocs.io/) for
  cross-platform audio capture with no compiler tooling required.

## License

No license is currently specified, so default copyright applies — others
may view this code but are not granted rights to use, modify, or
redistribute it. If you'd like to allow reuse, consider adding an
[open-source license](https://choosealicense.com/) (e.g. MIT).
