"""Ivy on a speaker: Whisper listens for her name, the agent answers, and Kokoro speaks.

Audio goes through pacat (PulseAudio's client; PipeWire serves the same protocol). Everything
runs locally: room speech is transcribed on the server, and only a request that starts with
the wake word reaches the LLM.

There is no separate wake-word model: off-the-shelf ones barely recognised the owner's voice,
while Whisper heard "Ivy" fine. The cost is that every sentence said in the room is transcribed.
"""

import asyncio
import logging
import re
import unicodedata
from pathlib import Path

import httpx
import numpy as np
from faster_whisper import WhisperModel
from faster_whisper.vad import VadOptions, get_speech_timestamps
from kokoro_onnx import Kokoro

from .agent import Agent
from .config import Config

log = logging.getLogger(__name__)

CHAT_KEY = "voice:speaker"
RATE = 16000
CHUNK = 1280  # 80 ms of audio per read
VAD_EVERY = 5  # chunks between speech checks (400 ms)
LEAD_CHUNKS = 5  # kept while nobody speaks, so the first word isn't cut off
MAX_CHUNKS = 125  # 10 s cap on one sentence
END_SILENCE = int(0.8 * RATE)  # quiet after speech that ends a sentence
NO_REPLY_CHUNKS = 32  # after the chime, give up this soon (2.5 s) if nobody speaks

# Kokoro sounds far more natural than Piper, at a few times the CPU. "Heart" is an American voice.
KOKORO_URL = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/"
KOKORO_MODEL, KOKORO_VOICES = "kokoro-v1.0.onnx", "voices-v1.0.bin"
KOKORO_VOICE = "af_heart"
WHISPER_MODEL = "base.en"

_t = np.arange(int(0.15 * RATE)) / RATE
CHIME = (np.sin(2 * np.pi * 880 * _t) * np.hanning(_t.size) * 8000).astype(np.int16).tobytes()


def speech_end(audio: np.ndarray) -> int | None:
    """Where the last speech in `audio` ends, or None if there is none."""
    # The default min_silence_duration_ms (2 s) would keep the last segment open past END_SILENCE;
    # min_speech_duration_ms skips clicks and coughs.
    options = VadOptions(min_silence_duration_ms=300, min_speech_duration_ms=250, speech_pad_ms=0)
    speech = get_speech_timestamps(audio.astype(np.float32) / 32768, options)
    return speech[-1]["end"] if speech else None


def addressed(text: str, wake_word: str) -> str | None:
    """The request if `text` starts with the wake word ("Ivy, ..." or "Hey Ivy, ..."), else None."""
    match = re.match(rf"\W*(?:(?:hey|hi|ok|okay)\W+)?{re.escape(wake_word)}\b\W*", text, re.IGNORECASE)
    return text[match.end() :].strip() if match else None


def question(actions: list[str]) -> str:
    """A short spoken yes/no question for actions waiting to be confirmed."""
    return "Do you want to " + " and ".join(a[:1].lower() + a[1:] for a in actions) + "?"


def _fetch(url: str, path: Path) -> None:
    if path.exists():
        return
    log.info("downloading %s", url)
    part = path.with_name(path.name + ".part")
    with httpx.stream("GET", url, follow_redirects=True, timeout=60) as r:
        r.raise_for_status()
        with part.open("wb") as f:
            for block in r.iter_bytes():
                f.write(block)
    part.rename(path)


class VoiceChannel:
    def __init__(self, cfg: Config, agent: Agent) -> None:
        models = Path(cfg.voice_dir)
        models.mkdir(parents=True, exist_ok=True)
        for name in (KOKORO_VOICES, KOKORO_MODEL):
            _fetch(KOKORO_URL + name, models / name)

        self.whisper = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8", download_root=str(models / "whisper"))
        self.kokoro = Kokoro(str(models / KOKORO_MODEL), str(models / KOKORO_VOICES))
        self.agent = agent
        self.wake_word = cfg.voice_wake_word
        # Without this hint Whisper often hears "Ivy, turn" as "I return". It sometimes echoes the
        # hint into other speech, but an echo only counts if it starts with the wake word, and the
        # worst it can do then is switch a light on.
        self.hint = f"{cfg.voice_wake_word.capitalize()}, turn on the light."
        self.source = ["--device", cfg.voice_source] if cfg.voice_source else []
        self.sink = ["--device", cfg.voice_sink] if cfg.voice_sink else []
        self._chunks: asyncio.Queue[np.ndarray] = asyncio.Queue()
        self._speaker = asyncio.Lock()

    async def run(self) -> None:
        await asyncio.gather(self._record(), self._converse())

    async def send(self, chat_id: str, text: str) -> None:
        await self.say(text)

    async def say(self, text: str) -> None:
        # Kokoro would read markdown out ("asterisk asterisk"), and emoji like ⏰ have no sound.
        spoken = "".join(c for c in text if c not in "*#_`" and unicodedata.category(c) != "So")

        def synthesize() -> tuple[bytes, int]:
            audio, rate = self.kokoro.create(spoken, voice=KOKORO_VOICE, lang="en-us")
            return (np.clip(audio, -1, 1) * 32767).astype(np.int16).tobytes(), rate

        await self._play(*await asyncio.to_thread(synthesize))

    async def _record(self) -> None:
        cmd = ["pacat", "--record", "--raw", "--format=s16le", f"--rate={RATE}", "--channels=1", "--latency-msec=80", *self.source]
        while True:
            proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE)
            assert proc.stdout is not None
            try:
                while True:
                    self._chunks.put_nowait(np.frombuffer(await proc.stdout.readexactly(CHUNK * 2), np.int16))
            except asyncio.IncompleteReadError:
                # Usually the mic was unplugged, or a Bluetooth one went out of range.
                log.warning("microphone stopped (pacat exit code %s); retrying in 10 s", await proc.wait())
            await asyncio.sleep(10)

    async def _converse(self) -> None:
        while True:
            audio = await self._hear()
            assert audio is not None  # _hear only gives up when given a patience
            try:
                command = addressed(await self._transcribe(audio), self.wake_word)
                if command is None:  # not talking to Ivy; the transcript isn't logged, it's room talk
                    continue
                if not command:  # just "Ivy", then a pause
                    command = await self._listen_again()
                while command:
                    log.info("heard request: %s", command)
                    reply = await self.agent.handle(CHAT_KEY, command)
                    # Out loud, the chat's "Should I do this? • ... Reply yes" becomes one short question.
                    asks = self.agent.pending_descriptions(CHAT_KEY)
                    await self.say(question(asks) if asks else reply)
                    command = await self._listen_again() if asks else ""
            except Exception:
                # One failed request must not stop the speaker from listening.
                log.exception("voice request failed")
            self._flush()  # drop what the mic heard meanwhile, including Ivy's own voice

    async def _hear(self, patience: int | None = None) -> np.ndarray | None:
        """Record the next sentence, until END_SILENCE of quiet. With a patience (in chunks),
        give up and return None if nobody starts speaking by then."""
        chunks: list[np.ndarray] = []
        waited = 0
        while True:
            chunks.append(await self._chunks.get())
            if len(chunks) % VAD_EVERY:
                continue
            audio = np.concatenate(chunks)
            end = speech_end(audio)
            if end is None:
                waited += VAD_EVERY
                if patience is not None and waited >= patience:
                    return None
                chunks = chunks[-LEAD_CHUNKS:]
            elif len(audio) - end > END_SILENCE or len(chunks) >= MAX_CHUNKS:
                return audio

    async def _listen_again(self) -> str:
        """Chime, then hear the next thing said, with no wake word needed."""
        await self._play(CHIME, RATE)
        self._flush()
        audio = await self._hear(patience=NO_REPLY_CHUNKS)
        return await self._transcribe(audio) if audio is not None else ""

    async def _transcribe(self, audio: np.ndarray) -> str:
        def transcribe() -> str:
            segments, _ = self.whisper.transcribe(
                audio.astype(np.float32) / 32768, language="en", vad_filter=True, initial_prompt=self.hint
            )
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
