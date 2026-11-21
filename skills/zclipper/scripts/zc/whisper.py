"""Word-timed speech from a local file with faster-whisper, for videos without captions and for live clips.

Optional: pip install faster-whisper numpy. Models are downloaded once into
$ZCLIPPER_HOME/models (default ~/.zclipper/models). Point ZCLIPPER_HOME at a
roomy drive when the home drive is short on space.
"""

import os
import subprocess
from pathlib import Path

from .errors import ZClipperError
from .moments import Word

_MODELS: dict = {}


def home() -> Path:
    """zclipper's folder for models and temporary files."""
    return Path(os.environ.get("ZCLIPPER_HOME") or Path.home() / ".zclipper")


def decode_audio(path: str | Path):
    """16 kHz mono samples via ffmpeg, so faster-whisper never needs PyAV to decode."""
    import numpy

    command = ["ffmpeg", "-v", "error", "-i", str(path), "-vn", "-ac", "1", "-ar", "16000", "-f", "f32le", "-"]
    done = subprocess.run(command, capture_output=True)
    if done.returncode != 0:
        raise ZClipperError(f"ffmpeg could not read the audio: {done.stderr.decode(errors='replace').strip()[-300:]}")
    return numpy.frombuffer(done.stdout, dtype=numpy.float32)


def whisper_words(path: str | Path, model_size: str = "base", language: str | None = None, model=None) -> list[Word]:
    """Words spoken in an audio or video file, timed from its start, transcribed on this computer.

    base is about 145 MB and takes roughly 10 to 30 seconds per 40 seconds of
    audio on a CPU; small is about 460 MB and more accurate. model is
    replaceable for tests.
    """
    if model is None:
        if model_size not in _MODELS:
            try:
                from faster_whisper import WhisperModel
            except ImportError as exc:
                raise ImportError("Local transcription needs faster-whisper: pip install faster-whisper numpy") from exc
            folder = home() / "models"
            folder.mkdir(parents=True, exist_ok=True)
            _MODELS[model_size] = WhisperModel(model_size, device="cpu", compute_type="int8", download_root=str(folder))
        model = _MODELS[model_size]
        audio = decode_audio(path)
    else:
        audio = str(path)
    segments, _info = model.transcribe(audio, language=language, word_timestamps=True, vad_filter=True)
    words = []
    for segment in segments:
        for word in segment.words or []:
            text = word.word.strip()
            if text:
                words.append(Word(start=round(word.start, 2), end=round(max(word.end, word.start + 0.05), 2), text=text))
    return words
