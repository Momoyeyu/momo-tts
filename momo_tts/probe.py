import argparse
import base64
import io
import json
import time
import urllib.request
import wave
from pathlib import Path

from .api import read_key


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("port", type=int)
    parser.add_argument("--wait", type=int, default=0)
    parser.add_argument("--reference", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--language", choices=("zh", "en", "ja"), default="zh")
    args = parser.parse_args()
    deadline = time.monotonic() + args.wait
    base = f"http://127.0.0.1:{args.port}"
    headers = {"Authorization": "Bearer " + read_key().decode("ascii")}
    while True:
        try:
            with urllib.request.urlopen(
                urllib.request.Request(base + "/readyz", headers=headers), timeout=4
            ) as response:
                if json.load(response)["ready"] is True:
                    break
        except OSError:
            if time.monotonic() >= deadline:
                raise SystemExit("Inference worker did not become ready") from None
        if time.monotonic() >= deadline:
            raise SystemExit("Inference worker did not become ready")
        time.sleep(1)
    if not args.reference:
        return
    samples = {
        "zh": "你好，欢迎回来。",
        "en": "Hello, it's good to see you again.",
        "ja": "こんにちは、おかえりなさい。",
    }
    payload = {
        "input": samples[args.language],
        "language": args.language,
        "reference_audio": base64.b64encode(args.reference.read_bytes()).decode("ascii"),
        "reference_name": args.reference.name,
    }
    request = urllib.request.Request(
        base + "/v1/audio/speech",
        json.dumps(payload).encode(),
        {**headers, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=115) as response:
        audio = response.read()
    with wave.open(io.BytesIO(audio)) as wav:
        duration = wav.getnframes() / wav.getframerate()
        if (wav.getnchannels(), wav.getsampwidth()) != (1, 2) or duration < 0.25:
            raise SystemExit("Invalid synthesis result")
        if not any(wav.readframes(wav.getnframes())):
            raise SystemExit("Silent synthesis result")
        print(f"{args.language}: {wav.getframerate()} Hz, {duration:.2f} s")
    if args.output:
        args.output.write_bytes(audio)


if __name__ == "__main__":
    main()
