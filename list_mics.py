import sounddevice as sd

print()
print("=" * 60)
print("AVAILABLE AUDIO DEVICES")
print("=" * 60)
print()

devices = sd.query_devices()

for index, device in enumerate(devices):

    if device["max_input_channels"] > 0:

        default_marker = ""

        try:
            if index == sd.default.device[0]:
                default_marker = "  <-- current default input"
        except Exception:
            pass

        print(
            f"[{index}] {device['name']}"
            f"  (inputs: {device['max_input_channels']}, "
            f"rate: {device['default_samplerate']:.0f}Hz)"
            f"{default_marker}"
        )

print()
print("=" * 60)
print("Copy the number in [brackets] for your mic")
print("and set it as MICROPHONE_DEVICE in the voice control script.")
print("=" * 60)
print()
