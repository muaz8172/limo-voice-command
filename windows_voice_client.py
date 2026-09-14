#!/usr/bin/env python3
"""
windows_voice_client.py
------------------------
Runs ONLY on your Windows PC.

Listens to your mic, transcribes speech LIVE using an offline Vosk
model (no network round-trip, so partial words show up on screen
while you're still talking), then matches the finished phrase against
4 known commands:

    1. "hello"          -> command code: hello
    2. "do mission 1"   -> command code: Do Mission One
    3. "do mission 2"   -> command code: Do Mission Two
    4. "do mission 3"   -> command code: Do Mission Three

AUDIO BACKEND NOTE:
    This uses `sounddevice` instead of PyAudio. PyAudio has no
    prebuilt wheel for brand-new Python versions (e.g. 3.14) and
    building it from source on Windows needs extra compiler tooling
    most people don't have installed. `sounddevice` ships ready-made
    wheels for current Python versions, so there's nothing to compile.

SPEECH RECOGNITION NOTE:
    This uses `vosk` (offline, streaming) instead of Google's Web
    Speech API. Vosk decodes audio locally as it arrives, so it can
    report PARTIAL results (what it thinks you're saying so far)
    continuously, not just a single final answer after you stop
    talking. That's what powers the live "you said: ..." line below.
    It also removes the network round-trip that used to be the
    biggest source of delay.

    Vosk needs a model folder next to this script. Download one from
    https://alphacephei.com/vosk/models (the small English model,
    "vosk-model-small-en-us-0.15", is ~40MB and plenty accurate for
    a handful of short commands) and unzip it here so you end up with:

        LIMO Voice Command/vosk-model-small-en-us-0.15/...

    Update MODEL_PATH below if you name the folder differently or use
    a different language's model.

Speech recognition is never perfect ("mission 1" can come back as
"mission juan", "mission won", "position one", etc). To handle that,
this script does NOT rely on an exact string match. Instead it:

    1. Normalizes the text (lowercase, strips punctuation/filler words)
    2. Checks a synonym/misheard-word table (e.g. "won"/"juan"/"1" all
       mean "one")
    3. Falls back to fuzzy string matching (difflib) against a list of
       known phrasings, so near-misses like "hallo" or "mishun one"
       still match

Only a clean command code is sent to Node-RED as JSON, e.g.:
    {"command": "Do Mission One", "raw_text": "do mishun one", "source": "muaz-pc"}

Your Node-RED "switch" node should check msg.payload.command against
the strings: hello / Do Mission One / Do Mission Two / Do Mission Three

Install deps (Windows cmd/PowerShell, NOT WSL):
    pip install vosk websockets sounddevice

Edit NODE_RED_IP / PORT / MIC_DEVICE_INDEX below, then run:
    python windows_voice_client.py

To see the exact device index/name list your machine assigns (indices
can shift after Windows updates or plugging things in/out), run:
    python windows_voice_client.py --list-devices
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
NODE_RED_IP = "10.21.215.131"   # <-- IP of the machine running Node-RED
PORT = 1880                    # <-- Node-RED's default port (change if yours differs)
PATH = "/voice/muaz"
SOURCE_NAME = "muaz-pc"
RECONNECT_DELAY_SEC = 3

# Folder containing the Vosk model (downloaded/unzipped next to this
# script -- see the SPEECH RECOGNITION NOTE above). The language you
# get is whichever model you download here, not a config flag.
MODEL_PATH = "vosk-model-small-en-us-0.15"

# Every finished phrase (recognized text + what it matched to) gets
# appended here as a CSV row, so you can review recognition accuracy
# later -- e.g. spot which words keep getting misheard and add them to
# NUMBER_SYNONYMS / CANONICAL_PHRASES below.
LOG_FILE = "speech_log.csv"

# ---- MICROPHONE SELECTION --------------------------------------------
# From your device list, good candidates for voice commands are:
#   [3]  Headset (Haylou X1 2023)                     <- best if you're
#        wearing it: close to your mouth, rejects room noise/echo
#   [1]/[10] Microphone Array (Intel Smart Sound)     <- built-in laptop
#        mic array, fine hands-free at a desk but picks up more room noise
# Avoid "Stereo Mix" (that's your speaker output, not a mic) and the
# duplicate "Steam Streaming Microphone" entries unless you're actually
# using Steam's virtual mic.
#
# Set the index that matches YOUR list (run --list-devices to confirm it
# hasn't shifted). None = let Windows use its current default input.
MIC_DEVICE_INDEX = 1          # <-- e.g. 3 for the Haylou headset

# Recording sample rate. 16000 Hz is what the Vosk model expects
# internally and works reliably across virtually every Windows mic,
# so you normally do NOT need to match the device's "native" rate here
# -- sounddevice/PortAudio resamples for you. Only change this if you
# get an error opening the device.
MIC_SAMPLE_RATE = 16000

# How often we pull a chunk of audio out of the mic and feed it to the
# recognizer. Smaller = live captions update more often (more
# responsive) but slightly more CPU/overhead; larger = choppier partial
# updates.
BLOCK_DURATION_SEC = 0.2

# How lenient the fuzzy fallback match is (0.0-1.0). Lower = more
# forgiving of misheard words, but more risk of false positives.
FUZZY_MATCH_CUTOFF = 0.6

# Send unmatched/unclear phrases to Node-RED too (useful for debugging
# your switch node / seeing what the mic is picking up). Set False once
# things are working so junk audio doesn't trigger anything downstream.
SEND_UNMATCHED = True
# ----------------------------------------------------------------------

WS_URL = f"ws://{NODE_RED_IP}:{PORT}{PATH}"


# ----------------------------------------------------------------------
# COMMAND CALIBRATION TABLE
# ----------------------------------------------------------------------
# Add more variants here any time you notice the recognizer consistently
# mishearing one of your commands a certain way -- just print raw_text
# (it's logged to the console every time) and add the new variant.

# Words/fillers to strip before matching, so "uh do mission one please"
# behaves the same as "do mission one".
# NOTE: "to" is deliberately NOT here even though it sounds like a filler --
# it's also the digit-2 synonym below, and speech_log.csv showed that
# stripping it here made "do mission to" lose its number entirely before
# the number-check ever ran, causing a wrong fuzzy-matched result.
FILLER_WORDS = {"uh", "um", "please", "robot", "hey", "the", "a"}

# Number-word calibration: maps common mis-hearings to the canonical digit.
# "wine" for "1" comes straight from speech_log.csv -- the small Vosk model
# transcribed "one" as "wine" in the large majority of mission-1 attempts.
NUMBER_SYNONYMS = {
    "1": {"1", "one", "won", "juan", "wun", "first", "wine"},
    "2": {"2", "two", "too", "to", "tu", "second"},
    "3": {"3", "three", "tree", "free", "thr", "third"},
}

# Digit -> the word used in the competition's required command strings
DIGIT_TO_WORD = {"1": "One", "2": "Two", "3": "Three"}

# Canonical phrasings used for fuzzy matching fallback. Dict keys are the
# EXACT command strings the competition rubric expects in msg.payload.command
# -- keep these matching your Node-RED switch node's rule values.
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

# Flatten for difflib: phrase -> command code
_PHRASE_TO_COMMAND = {
    phrase: cmd for cmd, phrases in CANONICAL_PHRASES.items() for phrase in phrases
}


def strip_punctuation(text: str) -> str:
    text = text.lower().strip()
    return re.sub(r"[^a-z0-9\s]", "", text)


def normalize(text: str) -> str:
    """Punctuation-stripped AND filler-word-stripped (for fuzzy/number matching)."""
    words = [w for w in strip_punctuation(text).split() if w not in FILLER_WORDS]
    return " ".join(words)


def match_command(raw_text: str):
    """
    Returns (command_code, confidence_note) or (None, reason) if nothing
    matched confidently.
    """
    # Greeting check runs on punctuation-only-stripped text, BEFORE filler
    # words are removed -- otherwise short greetings like "hey robot" would
    # get fully stripped away (both "hey" and "robot" are filler words used
    # elsewhere to clean up longer commands).
    greeting_tokens = strip_punctuation(raw_text).split()
    if any(w in greeting_tokens for w in ("hello", "hi", "hallo", "hey", "yo")):
        return "hello", "keyword match"

    text = normalize(raw_text)
    if not text:
        return None, "empty after normalization"

    # --- Step 2: mission + calibrated number check --------------------
    # speech_log.csv shows "mission" almost never survives transcription
    # intact -- it comes back as "me", "mention", "doom", etc. ("do me
    # said wine", "doom is and three", "do mention wine" were all real
    # mission attempts). Requiring the literal word "mission" here missed
    # most of them, so we also accept anything that at least starts with
    # "do" (every real command in CANONICAL_PHRASES does).
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

    # --- Step 3: fuzzy fallback against canonical phrases -------------
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
    """Appends one recognized phrase to LOG_FILE for later review."""
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
    """
    Feeds raw mic audio into a Vosk streaming recognizer.

    Unlike a one-shot API call, Vosk decodes continuously: while you're
    still talking you get PARTIAL results (its current best guess, which
    can still change), and once it decides you've finished a phrase
    (its own internal silence/endpoint detection) you get a FINAL
    result. That's what makes live captions possible.
    """

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
        """
        Blocks (up to `timeout` seconds if given) for one audio chunk and
        feeds it to the recognizer. Returns ("final", text) once a phrase
        is complete, ("partial", text) with the in-progress guess
        otherwise, or None if `timeout` elapsed with no audio -- callers
        that need to periodically check a stop condition pass a timeout.
        """
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

            # kind == "final" -- clear the live partial line, if any
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
                "command": command,          # e.g. "mission_1", or null if unmatched
                "raw_text": text,            # exactly what the recognizer heard
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

    vosk.SetLogLevel(-1)  # silence Kaldi's verbose C++ logging
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
