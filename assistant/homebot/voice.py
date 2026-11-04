"""Ivy on a speaker: a wake word, Whisper for speech, the agent, and Piper for her voice.

Audio goes through pacat (PulseAudio's client; PipeWire serves the same protocol), so a
Bluetooth speaker with a mic works once it is in headset mode. Everything runs locally:
only the text of a request, after the wake word, reaches the LLM.
"""

import asyncio
import logging
import re
import unicodedata
from collections import deque
from pathlib import Path

import httpx
import numpy as np
import onnxruntime as ort
from faster_whisper import WhisperModel
from faster_whisper.vad import VadOptions, get_speech_timestamps
from piper import PiperVoice

from .agent import Agent
from .config import Config

log = logging.getLogger(__name__)

CHAT_KEY = "voice:speaker"
RATE = 16000
CHUNK = 1280  # 80 ms, the frame openWakeWord models are trained on
MEL_CONTEXT = 480  # extra samples before each chunk, as openWakeWord feeds its mel model
WAKE_THRESHOLD = 0.5  # openWakeWord's default; raise it if Ivy wakes by mistake
PRE_ROLL_CHUNKS = 2  # 160 ms before the trigger: it fires a little late, and this keeps the request's first word
MAX_CHUNKS = 125  # 10 s cap on one request
END_SILENCE = int(0.8 * RATE)  # quiet after speech that ends a request
NO_SPEECH = int(2.5 * RATE)  # give up this soon if nobody speaks at all
# Speech must run this far past the trigger to count: the wake word's own tail ends near it,
# and Whisper turns that fragment into made-up words ("Thank you.").
MIN_SPEECH_PAST_START = int(0.25 * RATE)

OWW_URL = "https://github.com/dscripka/openWakeWord/releases/download/v0.5.1/"
PIPER_VOICE = "en_US-lessac-medium"
PIPER_URL = "https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/lessac/medium/"
WHISPER_MODEL = "base.en"

_t = np.arange(int(0.15 * RATE)) / RATE
CHIME = (np.sin(2 * np.pi * 880 * _t) * np.hanning(_t.size) * 8000).astype(np.int16).tobytes()


class WakeWord:
    """openWakeWord's streaming pipeline, run straight on its ONNX models: audio -> mel frames
    -> embeddings -> score. (The openwakeword package itself can't install on Python 3.13.)"""

    def __init__(self, mel: Path, embedding: Path, model: Path) -> None:
        options = ort.SessionOptions()
        options.intra_op_num_threads = 1  # tiny models; more threads only add overhead on the i3
        self.mel, self.embedding, self.model = (ort.InferenceSession(str(p), options) for p in (mel, embedding, model))
        self.window = self.model.get_inputs()[0].shape[1]  # embeddings per prediction
        self.reset()

    def reset(self) -> None:
        self.audio = np.zeros(MEL_CONTEXT, np.float32)
        self.mels = np.ones((76, 32), np.float32)
        self.features: deque[np.ndarray] = deque(maxlen=self.window)

    def score(self, chunk: np.ndarray) -> float:
        self.audio = np.concatenate([self.audio[-MEL_CONTEXT:], chunk.astype(np.float32)])
        mel = _run(self.mel, self.audio[None]).squeeze() / 10 + 2  # openWakeWord's scaling
        self.mels = np.vstack([self.mels, mel])[-76:]
        self.features.append(_run(self.embedding, self.mels[None, :, :, None]).squeeze())
        if len(self.features) < self.window:
            return 0.0
        return float(_run(self.model, np.array(self.features)[None]).squeeze())


def _run(session: ort.InferenceSession, x: np.ndarray) -> np.ndarray:
    return session.run(None, {session.get_inputs()[0].name: x})[0]


def last_speech_end(audio: np.ndarray, start: int) -> int | None:
    """Where the last speech in `audio` ends, if any speech runs past `start`."""
    # The default min_silence_duration_ms (2 s) would keep the last segment open past END_SILENCE.
    options = VadOptions(min_silence_duration_ms=300, speech_pad_ms=0)
    speech = get_speech_timestamps(audio.astype(np.float32) / 32768, options)
    end = speech[-1]["end"] if speech else 0
    return end if end > start + MIN_SPEECH_PAST_START else None


def strip_wake_word(text: str, wake_word: str) -> str:
    """The request without the wake word, when Whisper heard it. A Bluetooth headset mic
    often garbles the wake word, so its absence doesn't mean the detector was wrong."""
    match = re.search(r"\b" + r"\W+".join(wake_word.split("_")) + r"\b\W*", text, re.IGNORECASE)
    return text[match.end() :].strip() if match else text


def _fetch(url: str, path: Path) -> None:
    if path.exists():
        return
    log.info("downloading %s", url)
    part = path.with_name(path.name + ".part")
    with httpx.stream("GET", url, follow_redirects=True, timeout=60) as r:
        if r.status_code == 404:
            raise FileNotFoundError(f"{path} is missing, and there is no download at {url}")
        r.raise_for_status()
        with part.open("wb") as f:
            for block in r.iter_bytes():
                f.write(block)
    part.rename(path)


class VoiceChannel:
    def __init__(self, cfg: Config, agent: Agent) -> None:
        models = Path(cfg.voice_dir)
        models.mkdir(parents=True, exist_ok=True)
        for name in ("melspectrogram.onnx", "embedding_model.onnx", f"{PIPER_VOICE}.onnx", f"{PIPER_VOICE}.onnx.json"):
            _fetch((PIPER_URL if name.startswith(PIPER_VOICE) else OWW_URL) + name, models / name)
        # A trained ivy.onnx is put here by hand; openWakeWord's pretrained ones are named <name>_v0.1.onnx.
        _fetch(f"{OWW_URL}{cfg.voice_wake_word}_v0.1.onnx", models / f"{cfg.voice_wake_word}.onnx")

        self.wake = WakeWord(models / "melspectrogram.onnx", models / "embedding_model.onnx", models / f"{cfg.voice_wake_word}.onnx")
        self.whisper = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8", download_root=str(models / "whisper"))
        self.piper = PiperVoice.load(models / f"{PIPER_VOICE}.onnx")
        self.agent = agent
        self.wake_word = cfg.voice_wake_word
        self.source = ["--device", cfg.voice_source] if cfg.voice_source else []
        self.sink = ["--device", cfg.voice_sink] if cfg.voice_sink else []
        self._chunks: asyncio.Queue[np.ndarray] = asyncio.Queue()
        self._speaker = asyncio.Lock()

    async def run(self) -> None:
        await asyncio.gather(self._record(), self._converse())

    async def send(self, chat_id: str, text: str) -> None:
        await self.say(text)

    async def say(self, text: str) -> None:
        # Piper would read markdown out ("asterisk asterisk"), and emoji like ⏰ have no sound.
        spoken = "".join(c for c in text if c not in "*#_`" and unicodedata.category(c) != "So")

        def synthesize() -> bytes:
            return b"".join(chunk.audio_int16_bytes for chunk in self.piper.synthesize(spoken))

        await self._play(await asyncio.to_thread(synthesize), self.piper.config.sample_rate)

    async def _record(self) -> None:
        cmd = ["pacat", "--record", "--raw", "--format=s16le", f"--rate={RATE}", "--channels=1", "--latency-msec=80", *self.source]
        while True:
            proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE)
            assert proc.stdout is not None
            try:
                while True:
                    self._chunks.put_nowait(np.frombuffer(await proc.stdout.readexactly(CHUNK * 2), np.int16))
            except asyncio.IncompleteReadError:
                # Usually the speaker went out of range or was switched off.
                log.warning("microphone stopped (pacat exit code %s); retrying in 10 s", await proc.wait())
            await asyncio.sleep(10)

    async def _converse(self) -> None:
        while True:
            audio = await self._capture(await self._wait_for_wake())
            try:
                command = strip_wake_word(await self._transcribe(audio), self.wake_word) if audio is not None else ""
                if not command:  # just the wake word, then a pause
                    command = await self._listen_again()
                while command:
                    await self.say(await self.agent.handle(CHAT_KEY, command))
                    command = await self._listen_again() if self.agent.awaiting_confirmation(CHAT_KEY) else ""
            except Exception:
                # One failed request must not stop the speaker from listening.
                log.exception("voice request failed")
            self._flush()  # drop what the mic heard meanwhile, including Ivy's own voice

    async def _wait_for_wake(self) -> list[np.ndarray]:
        recent: deque[np.ndarray] = deque(maxlen=PRE_ROLL_CHUNKS)
        while True:
            chunk = await self._chunks.get()
            recent.append(chunk)
            if self.wake.score(chunk) >= WAKE_THRESHOLD:
                return list(recent)

    async def _capture(self, chunks: list[np.ndarray]) -> np.ndarray | None:
        """Keep recording onto `chunks` until the speaker goes quiet; None if nobody speaks after them."""
        start = len(chunks) * CHUNK
        while len(chunks) < MAX_CHUNKS:
            chunks.append(await self._chunks.get())
            if len(chunks) % 5 == 0:
                audio = np.concatenate(chunks)
                end = last_speech_end(audio, start)
                if end is None and len(audio) - start > NO_SPEECH:
                    return None
                if end is not None and len(audio) - end > END_SILENCE:
                    return audio
        return np.concatenate(chunks)

    async def _listen_again(self) -> str:
        """Chime, then hear the next thing said, with no wake word needed."""
        await self._play(CHIME, RATE)
        self._flush()
        audio = await self._capture([])
        return await self._transcribe(audio) if audio is not None else ""

    async def _transcribe(self, audio: np.ndarray) -> str:
        def transcribe() -> str:
            segments, _ = self.whisper.transcribe(audio.astype(np.float32) / 32768, language="en", vad_filter=True)
            return " ".join(s.text.strip() for s in segments).strip()

        return await asyncio.to_thread(transcribe)

    async def _play(self, pcm: bytes, rate: int) -> None:
        cmd = ["pacat", "--playback", "--raw", "--format=s16le", f"--rate={rate}", "--channels=1", *self.sink]
        async with self._speaker:
            proc = await asyncio.create_subprocess_exec(*cmd, stdin=asyncio.subprocess.PIPE)
            await proc.communicate(pcm)

    def _flush(self) -> None:
        while not self._chunks.empty():
            self._chunks.get_nowait()
        self.wake.reset()
