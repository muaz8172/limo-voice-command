# LIMO Voice Command

Voice-controlled command dispatcher for a LIMO robot competition run. Listens
on a Windows PC's microphone, transcribes speech **live and fully offline**
(Vosk), matches it against a small set of known commands, and sends the
result to Node-RED over a websocket so it can drive the robot.

```
mic -> Vosk (live transcription) -> command matcher -> websocket -> Node-RED
```

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

## Two ways to run it

**Console client:**
```
python windows_voice_client.py
```

**PyQt6 dashboard** — connection status, live captions, and a history table:
```
python voice_dashboard.py
```

## Setup

```
pip install vosk websockets sounddevice
pip install PyQt6          # only needed for the dashboard
```

This repo already includes the Vosk speech model
(`vosk-model-small-en-us-0.15/`), so no separate download is needed.

Edit `NODE_RED_IP`, `PORT`, and `MIC_DEVICE_INDEX` at the top of
`windows_voice_client.py` for your setup. To find your microphone's index:
```
python windows_voice_client.py --list-devices
```

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
