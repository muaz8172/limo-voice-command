#!/usr/bin/env python3
"""
windows_voice_client.py

Mic -> live Vosk transcription -> command matching -> Node-RED over
websocket. See NOTES.txt for setup, rationale, and calibration history.
"""

import asyncio
import csv
import difflib
import json
import os
import queue
import re
import sys
from datetime import datetime

import sounddevice as sd
import vosk
import websockets

# ---- CONFIG ---------------------------------------------------------
# Override any of these with environment variables instead of editing the
# file directly, e.g.:
#   set LIMO_NODE_RED_IP=192.168.1.50
NODE_RED_IP = os.environ.get("LIMO_NODE_RED_IP", "192.168.1.100")
PORT = int(os.environ.get("LIMO_NODE_RED_PORT", "1880"))
PATH = os.environ.get("LIMO_NODE_RED_PATH", "/voice/limo")
SOURCE_NAME = os.environ.get("LIMO_SOURCE_NAME", "voice-client")
RECONNECT_DELAY_SEC = 3

MODEL_PATH = "vosk-model-small-en-us-0.15"
LOG_FILE = "speech_log.csv"

MIC_DEVICE_INDEX = 1
MIC_SAMPLE_RATE = 16000
BLOCK_DURATION_SEC = 0.2

FUZZY_MATCH_CUTOFF = 0.6
SEND_UNMATCHED = True
# ----------------------------------------------------------------------

WS_URL = f"ws://{NODE_RED_IP}:{PORT}{PATH}"


# ----------------------------------------------------------------------
# COMMAND CALIBRATION TABLE (see NOTES.txt for tuning history)
# ----------------------------------------------------------------------

FILLER_WORDS = {"uh", "um", "please", "robot", "hey", "the", "a"}

NUMBER_SYNONYMS = {
    "1": {"1", "one", "won", "juan", "wun", "first", "wine"},
    "2": {"2", "two", "too", "to", "tu", "second"},
    "3": {"3", "three", "tree", "free", "thr", "third"},
}

DIGIT_TO_WORD = {"1": "One", "2": "Two", "3": "Three"}

CANONICAL_PHRASES = {
    "hello": [
        "hello", "hi", "hey there", "hi robot", "hello robot", "hallo",
    ],
    "Do Mission One": [
        "do mission one", "mission one", "start mission one",
        "do mission 1", "mission 1", "run mission one",
    ],
    "Do Mission Two": [
        "do mission two", "mission two", "start mission two",
        "do mission 2", "mission 2", "run mission two",
    ],
    "Do Mission Three": [
        "do mission three", "mission three", "start mission three",
        "do mission 3", "mission 3", "run mission three",
    ],
}

_PHRASE_TO_COMMAND = {
    phrase: cmd for cmd, phrases in CANONICAL_PHRASES.items() for phrase in phrases
}


def strip_punctuation(text: str) -> str:
    text = text.lower().strip()
    return re.sub(r"[^a-z0-9\s]", "", text)


def normalize(text: str) -> str:
    words = [w for w in strip_punctuation(text).split() if w not in FILLER_WORDS]
    return " ".join(words)


def match_command(raw_text: str):
    """Returns (command_code, reason) or (None, reason) if nothing matched."""
    greeting_tokens = strip_punctuation(raw_text).split()
    if any(w in greeting_tokens for w in ("hello", "hi", "hallo", "hey", "yo")):
        return "hello", "keyword match"

    text = normalize(raw_text)
    if not text:
        return None, "empty after normalization"

    tokens = text.split()
    looks_like_mission = (
        "mission" in text or "mishun" in text or "position" in text
        or "doom" in text or "mention" in text
        or (tokens and tokens[0] == "do")
    )
    if looks_like_mission:
        for token in tokens:
            for digit, synonyms in NUMBER_SYNONYMS.items():
                if token in synonyms:
                    return f"Do Mission {DIGIT_TO_WORD[digit]}", f"keyword match (number word '{token}')"

    best = difflib.get_close_matches(
        text, _PHRASE_TO_COMMAND.keys(), n=1, cutoff=FUZZY_MATCH_CUTOFF
    )
    if best:
        matched_phrase = best[0]
        return _PHRASE_TO_COMMAND[matched_phrase], f"fuzzy match -> '{matched_phrase}'"

    return None, "no match"


# ----------------------------------------------------------------------
# AUDIO CAPTURE (sounddevice) + LIVE STREAMING RECOGNITION (vosk)
# ----------------------------------------------------------------------

def log_speech(raw_text: str, command, reason: str) -> None:
    is_new_file = not os.path.exists(LOG_FILE)
    with open(LOG_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if is_new_file:
            writer.writerow(["timestamp", "raw_text", "command", "match_reason"])
        writer.writerow([datetime.now().isoformat(timespec="seconds"), raw_text, command, reason])


def get_input_devices():
    """Returns [(index, device_info_dict), ...] for mic-capable devices."""
    return [
        (index, info) for index, info in enumerate(sd.query_devices())
        if info["max_input_channels"] > 0
    ]


def list_devices():
    print("=" * 60)
    print("AVAILABLE AUDIO DEVICES (sounddevice index)")
    print("=" * 60)
    for index, info in get_input_devices():
        marker = "  <-- MIC_DEVICE_INDEX currently set to this" if index == MIC_DEVICE_INDEX else ""
        print(f"[{index}] {info['name']}  (inputs: {info['max_input_channels']}, "
              f"rate: {int(info['default_samplerate'])}Hz){marker}")
    print("\nSet MIC_DEVICE_INDEX in the CONFIG section to the index you want.")


class LiveTranscriber:
    """Feeds mic audio into a Vosk streaming recognizer for live partial/final results."""

    def __init__(self, model: "vosk.Model", device=None):
        self._rec = vosk.KaldiRecognizer(model, MIC_SAMPLE_RATE)
        self._audio_q: "queue.Queue[bytes]" = queue.Queue()
        self.block_size = int(MIC_SAMPLE_RATE * BLOCK_DURATION_SEC)
        self.device = MIC_DEVICE_INDEX if device is None else device

    def _callback(self, indata, frames, time_info, status):
        if status:
            print(f"[audio] status: {status}", file=sys.stderr)
        self._audio_q.put(bytes(indata))

    def stream(self):
        return sd.RawInputStream(
            samplerate=MIC_SAMPLE_RATE,
            channels=1,
            dtype="int16",
            blocksize=self.block_size,
            device=self.device,
            callback=self._callback,
        )

    def next_result(self, timeout=None):
        """Blocks up to `timeout`s for one audio chunk. Returns ("final"|"partial", text) or None on timeout."""
        try:
            data = self._audio_q.get(timeout=timeout)
        except queue.Empty:
            return None
        if self._rec.AcceptWaveform(data):
            result = json.loads(self._rec.Result())
            return "final", result.get("text", "")
        partial = json.loads(self._rec.PartialResult())
        return "partial", partial.get("partial", "")


# ----------------------------------------------------------------------
# MAIN LOOP: capture -> live transcribe -> match -> send over websocket
# ----------------------------------------------------------------------

async def send_loop(websocket, transcriber: LiveTranscriber):
    loop = asyncio.get_event_loop()
    print("Ready. Try saying: 'hello', 'do mission 1', 'do mission 2', 'do mission 3'\n")

    last_partial = ""
    with transcriber.stream():
        while True:
            kind, text = await loop.run_in_executor(None, transcriber.next_result)

            if kind == "partial":
                if text and text != last_partial:
                    last_partial = text
                    sys.stdout.write(f"\r\U0001f3a4 {text}" + " " * 10)
                    sys.stdout.flush()
                continue

            if last_partial:
                sys.stdout.write("\r" + " " * (len(last_partial) + 15) + "\r")
                sys.stdout.flush()
            last_partial = ""

            if not text:
                continue

            command, reason = match_command(text)
            print(f"Heard: '{text}'  ->  command: {command}  ({reason})")
            log_speech(text, command, reason)

            if command is None and not SEND_UNMATCHED:
                continue

            payload = json.dumps({
                "command": command,
                "raw_text": text,
                "match_reason": reason,
                "source": SOURCE_NAME,
            })
            await websocket.send(payload)


async def main():
    if not os.path.isdir(MODEL_PATH):
        print(f"Vosk model folder not found: '{MODEL_PATH}'")
        print("Download one from https://alphacephei.com/vosk/models")
        print(f"and unzip it next to this script so the folder is named '{MODEL_PATH}'.")
        sys.exit(1)

    vosk.SetLogLevel(-1)
    model = vosk.Model(MODEL_PATH)

    try:
        transcriber = LiveTranscriber(model)
    except Exception as e:
        print(f"Could not set up audio capture: {e}")
        print("Run 'python windows_voice_client.py --list-devices' to see valid indices.")
        sys.exit(1)

    while True:
        try:
            print(f"Connecting to {WS_URL} ...")
            async with websockets.connect(WS_URL) as websocket:
                print("Connected to Node-RED. Streaming voice commands.")
                await send_loop(websocket, transcriber)
        except (ConnectionRefusedError, websockets.ConnectionClosed, OSError) as e:
            print(f"Connection lost/failed ({e}). Retrying in {RECONNECT_DELAY_SEC}s...")
            await asyncio.sleep(RECONNECT_DELAY_SEC)
        except KeyboardInterrupt:
            print("Stopped by user.")
            sys.exit(0)


if __name__ == "__main__":
    if "--list-devices" in sys.argv:
        list_devices()
        sys.exit(0)
    asyncio.run(main())
