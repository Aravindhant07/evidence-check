"""Voice statements for benchmark cases: text from case facts, spoken by a local open TTS model.

The TTS model (facebook/mms-tts-eng, CC-BY-NC-4.0) is used only to generate benchmark audio on your machine;
no generated audio is committed. Without it ("silent" mode) a short silent WAV is written and the transcript
lives in truth.json, which is all oracle perception needs.
"""

from __future__ import annotations

import struct
import threading
import wave
from datetime import date
from pathlib import Path

TTS_ID, TTS_REVISION = "facebook/mms-tts-eng", "c71de0fe7204c83f1c10820a7d696d0b450048ba"

ONES = "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen".split()
TENS = "_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()
ORD = {"one": "first", "two": "second", "three": "third", "five": "fifth", "eight": "eighth", "nine": "ninth", "twelve": "twelfth"}
MONTHS = "january february march april may june july august september october november december".split()


def number_words(n: int) -> str:
    if n < 20:
        return ONES[n]
    if n < 100:
        return TENS[n // 10] + ("" if n % 10 == 0 else " " + ONES[n % 10])
    if n < 1000:
        return ONES[n // 100] + " hundred" + ("" if n % 100 == 0 else " " + number_words(n % 100))
    return number_words(n // 1000) + " thousand" + ("" if n % 1000 == 0 else " " + number_words(n % 1000))


def ordinal_words(n: int) -> str:
    words = number_words(n).split()
    last = words[-1]
    words[-1] = ORD.get(last) or (last[:-1] + "ieth" if last.endswith("y") else last + "th")
    return " ".join(words)


def spoken_date(d: date) -> str:
    return f"{MONTHS[d.month - 1]} {ordinal_words(d.day)}"


def spoken_amount(x: float) -> str:
    """Round the way people speak ("about eighteen hundred"): to 10 below 1000, to 50 above."""
    step = 10 if x < 1000 else 50
    return number_words(max(step, int(round(x / step) * step))) + " dollars"


LOSS_PHRASES = {
    "vehicle_collision": "another car hit my car in a parking lot and dented the bumper",
    "water_damage": "a pipe burst under the sink and the kitchen floor was soaked",
    "fire": "a fire started in the kitchen and there was smoke damage",
    "theft": "someone broke in and stole the television and a laptop",
}


def statement_text(loss: str, when: date, amount: float) -> str:
    return (f"hello, this is my statement about the claim. on {spoken_date(when)} {LOSS_PHRASES.get(loss, 'there was damage')}. "
            f"the repairs came to about {spoken_amount(amount)}.")


class Speaker:
    """Writes a statement as WAV: real speech with mode="tts", a silent placeholder with mode="silent"."""

    def __init__(self, mode: str = "silent"):
        self.mode = mode
        self._m = None
        self._lock = threading.Lock()

    def _load(self):
        with self._lock:
            if self._m is None:
                import transformers as T

                model = T.VitsModel.from_pretrained(TTS_ID, revision=TTS_REVISION, use_safetensors=True).eval()
                tok = T.AutoTokenizer.from_pretrained(TTS_ID, revision=TTS_REVISION)
                self._m = (model, tok)
        return self._m

    def write(self, text: str, path: Path, seed: int = 0) -> None:
        if self.mode != "tts":
            with wave.open(str(path), "wb") as w:
                w.setnchannels(1), w.setsampwidth(2), w.setframerate(16000)
                w.writeframes(struct.pack("<h", 0) * 8000)
            return
        import numpy as np
        import torch

        model, tok = self._load()
        torch.manual_seed(seed)
        with torch.inference_mode():
            wav = model(**tok(text, return_tensors="pt")).waveform[0].numpy()
        pcm = (np.clip(wav, -1, 1) * 32767).astype("<i2").tobytes()
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1), w.setsampwidth(2), w.setframerate(model.config.sampling_rate)
            w.writeframes(pcm)
