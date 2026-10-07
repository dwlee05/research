"""Voice messages -> calendar: pick the audio of a Slack message, decode it in memory, transcribe it locally.

A voice message (Slack's mic button, or a voice-memo file) sent to 일정, 고뭉치
or 업뎃 (Slack, or ``--audio``) is turned into text on this Mac with
mlx-whisper (Apple Silicon only), so the audio never leaves the computer and
no credits are spent. Only the transcript goes to the agent, which proposes
its events like a pasted note (the user still picks the category).

* Decoding: PyAV (``av``) reads the bytes from memory (m4a, mp3, wav, webm,
  ogg, mp4 clips ...) and resamples them to 16 kHz mono float32. No ffmpeg
  binary, no files on disk.
* Transcribing: ``mlx_whisper.transcribe(samples, path_or_hf_repo=<local
  model folder>, language=...)``. The model must already be on this Mac
  (``python -m mungchi --voice-setup`` downloads it once); the bot never
  starts the 1.6 GB download itself. mlx-whisper keeps the loaded model
  (``ModelHolder``), so only the first message after a start loads it.
* All MLX work runs on one dedicated worker thread (MLX streams are per
  thread), one transcription at a time, with a timeout.

Both libraries are imported only when audio arrives (or by ``--voice-setup``),
and only on an Apple Silicon Mac; elsewhere, or without them, the user gets a
Korean note with what to install, or the tip to use the phone keyboard's
dictation instead. The transcript and the audio are never logged.
"""

from __future__ import annotations

import asyncio
import io
import platform
import re
import sys
import threading
import time
import unicodedata
import wave
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Protocol, TextIO

from . import config
from .images import ACCEPTED_MIMETYPES as IMAGE_MIMETYPES, file_mimetype, short_name

SAMPLE_RATE = 16_000
MAX_AUDIO_BYTES = 25 * 1024 * 1024
MAX_AUDIO_MB = MAX_AUDIO_BYTES // (1024 * 1024)
# A message may decode to a little more than its stated length.
DURATION_TOLERANCE_SECONDS = 1.0
TRANSCRIBE_TIMEOUT_SECONDS = 180.0
# Importing mlx-whisper (with numba and scipy) the first time is part of this check.
READY_TIMEOUT_SECONDS = 120.0
MAX_HEARD_CHARS = 500
# A segment Whisper itself thinks is probably not speech.
NO_SPEECH_THRESHOLD = 0.6
DEFAULT_MODEL_SIZE_TEXT = "약 1.6GB"
SETUP_COMMAND = "python -m mungchi --voice-setup"
SETUP_SIGNAL_SECONDS = 1.0

# Slack audio clips (the mic button) and voice-memo files.
CLIP_VIDEO_MIMETYPES = frozenset({"video/mp4", "video/webm", "video/quicktime"})
AUDIO_SUFFIXES = frozenset(
    {".m4a", ".mp3", ".wav", ".aac", ".ogg", ".oga", ".opus", ".flac", ".weba", ".amr", ".caf", ".aif", ".aiff"}
)
AUDIO_FILETYPES = frozenset(
    {"m4a", "mp3", "wav", "aac", "ogg", "oga", "opus", "flac", "weba", "amr", "caf", "aif", "aiff"}
)
# What a binary download without a real type may be called.
_UNTYPED_MIMETYPES = frozenset({"", "application/octet-stream", "binary/octet-stream"})

# ---------------------------------------------------------------- Korean texts

TRANSCRIBING_TEXT = "🎙️ 음성을 글로 옮기는 중…"
HEARD_TEXT = '🎙️ 들은 내용: "{transcript}"'
EMPTY_TRANSCRIPT_TEXT = "음성에서 내용을 알아듣지 못했어요. 다시 녹음해 주세요."
DICTATION_TIP = "그동안은 휴대폰 키보드의 🎤 받아쓰기로 말한 내용을 글로 보내 주셔도 돼요."
UNSUPPORTED_PLATFORM_TEXT = (
    "음성 받아쓰기는 Apple Silicon Mac(M1 이후)에서 봇을 돌릴 때만 돼요(mlx-whisper). "
    "대신 휴대폰 키보드의 🎤 받아쓰기로 말한 내용을 글로 보내 주세요."
)
DEPS_MISSING_TEXT = (
    "음성 받아쓰기에 필요한 mlx-whisper와 av가 설치되어 있지 않아요. 저장소 폴더에서 가상환경을 켜고 "
    f"pip install -e . 를 실행한 뒤 {SETUP_COMMAND} 으로 모델을 받고, "
    "python -m mungchi service restart 로 봇을 다시 시작하세요. " + DICTATION_TIP
)
DEPS_BROKEN_TEXT = (
    "음성 받아쓰기 도구({kind})를 불러오지 못했어요. 저장소 폴더에서 가상환경을 켜고 pip install -e . 를 다시 실행한 뒤 "
    f"{SETUP_COMMAND} 으로 확인해 주세요. " + DICTATION_TIP
)
MODEL_MISSING_TEXT = (
    "음성 인식 모델({model})이 아직 이 Mac에 없어요. 터미널에서 "
    f"{SETUP_COMMAND} 을 한 번 실행해 모델(기본 모델은 {DEFAULT_MODEL_SIZE_TEXT})을 받은 뒤 다시 보내 주세요. "
    + DICTATION_TIP
)
UNREADABLE_AUDIO_TEXT = "음성 파일을 읽지 못했어요. m4a·mp3·wav 파일로 다시 보내 주세요."
TOO_LONG_TEXT = "음성이 너무 길어요({limit}까지 들어요). 짧게 나눠서 다시 보내 주세요."
TOO_BIG_TEXT = f"{MAX_AUDIO_MB}MB보다 큰 음성 파일은 듣지 않았어요"
TOO_LONG_FILES_TEXT = "{limit}보다 긴 음성은 듣지 않았어요: {names}"
TIMEOUT_TEXT = "음성을 글로 옮기는 데 너무 오래 걸려서 멈췄어요({limit}). 더 짧게 녹음해서 다시 보내 주세요."
FAILED_TEXT = "음성을 글로 옮기지 못했어요 ({kind}). 다시 보내 주시거나, 휴대폰 키보드의 🎤 받아쓰기로 글을 보내 주세요."
DOWNLOAD_FAILED_TEXT = "음성 파일을 받지 못했어요 ({reason})."
ONE_AUDIO_TEXT = "음성은 한 번에 하나만 들어요. 첫 번째 음성만 들을게요."
OTHER_FILES_TEXT = "음성과 함께 온 다른 파일은 읽지 않았어요 (읽지 않은 파일: {names})."

# Sent to the agent with the transcript (the system prompts explain it too).
VOICE_TAG = "[음성 메시지 받아쓰기]"
VOICE_NOTE = (
    "(음성 인식으로 받아 적은 글이라 잘못 들은 글자가 있을 수 있어요. "
    "특히 이름·숫자·날짜·시각이 애매하면 짐작하지 말고 물어봐 주세요.)"
)


class VoiceError(RuntimeError):
    """A voice message that cannot be transcribed; ``str()`` is a Korean line for the user.

    Never carries the transcript, the audio or a file address.
    """


class VoiceUnavailable(VoiceError):
    """Transcription is not possible here: not an Apple Silicon Mac, libraries or model missing."""


# ---------------------------------------------------------------- small helpers


def describe_seconds(seconds: float) -> str:
    """``300`` -> ``5분``, ``90`` -> ``1분 30초``, ``45`` -> ``45초``."""
    total = max(0, int(round(seconds)))
    minutes, rest = divmod(total, 60)
    if minutes and rest:
        return f"{minutes}분 {rest}초"
    return f"{minutes}분" if minutes else f"{rest}초"


def heard_text(transcript: str, limit: int | None = MAX_HEARD_CHARS) -> str:
    """``🎙️ 들은 내용: "…"`` with the transcript cut to about ``limit`` characters (None: whole)."""
    text = " ".join((transcript or "").split())
    if limit is not None and len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return HEARD_TEXT.format(transcript=text)


def voice_prompt(request: str, transcript: str) -> str:
    """What the agent gets: the user's own text (if any), ``[음성 메시지 받아쓰기] …`` and the caution."""
    parts = [request.strip()] if (request or "").strip() else []
    parts.append(f"{VOICE_TAG} {' '.join(transcript.split())}")
    parts.append(VOICE_NOTE)
    return "\n".join(parts)


# Whisper's well-known output for silence or noise (normalized: no spaces or punctuation, lower case).
_HALLUCINATIONS = frozenset(
    {
        "감사합니다",
        "시청해주셔서감사합니다",
        "구독과좋아요부탁드립니다",
        "구독과좋아요알림설정부탁드립니다",
        "thankyou",
        "thanksforwatching",
        "thankyouforwatching",
    }
)
_HALLUCINATION_RE = re.compile(r"^mbc뉴스\w{0,8}입니다$")


def _normalized(text: str) -> str:
    return "".join(ch for ch in text.lower() if unicodedata.category(ch)[0] in ("L", "N"))


def clean_transcript(result: Mapping[str, Any] | str | None) -> str:
    """The transcript to use, or ``""`` when Whisper heard nothing (silence, noise, its usual filler).

    ``result`` is what ``mlx_whisper.transcribe`` returns (``text`` and
    ``segments`` with ``no_speech_prob``), or plain text. Empty when every
    segment is probably not speech, when no letter or digit is left, or when
    the whole text is a phrase Whisper is known to invent for silence
    ("시청해 주셔서 감사합니다", "감사합니다"). Pure.
    """
    if isinstance(result, str):
        text, segments = result, None
    elif isinstance(result, Mapping):
        text, segments = str(result.get("text") or ""), result.get("segments")
    else:
        return ""
    text = " ".join(text.split())
    if isinstance(segments, list) and segments:
        probabilities = [s.get("no_speech_prob") for s in segments if isinstance(s, Mapping)]
        if probabilities and all(isinstance(p, (int, float)) and p > NO_SPEECH_THRESHOLD for p in probabilities):
            return ""
    normalized = _normalized(text)
    if not normalized or normalized in _HALLUCINATIONS or _HALLUCINATION_RE.match(normalized):
        return ""
    return text


# ---------------------------------------------------------------- choosing the audio of a Slack message


def _suffix(file: Mapping[str, Any]) -> str:
    return Path(str(file.get("name") or file.get("title") or "")).suffix.lower()


def is_audio_file(file: Mapping[str, Any]) -> bool:
    """A Slack file that is a voice message or an audio file. Pure.

    ``audio/*``; Slack's own audio clips (``subtype: slack_audio`` or
    ``media_display_type: audio``, whatever their mimetype); ``video/mp4``,
    ``video/webm`` and ``video/quicktime`` only when the file says it is
    audio (an audio file type or name such as ``.m4a``). Slack video clips
    (``slack_video``) are not audio.
    """
    subtype = str(file.get("subtype") or "").lower()
    display = str(file.get("media_display_type") or "").lower()
    if subtype == "slack_audio" or display == "audio":
        return True
    if subtype == "slack_video" or display == "video":
        return False
    mimetype = file_mimetype(file)
    if mimetype.startswith("audio/"):
        return True
    looks_audio = str(file.get("filetype") or "").lower() in AUDIO_FILETYPES or _suffix(file) in AUDIO_SUFFIXES
    if mimetype in CLIP_VIDEO_MIMETYPES or mimetype in _UNTYPED_MIMETYPES:
        return looks_audio
    return False


def _size(file: Mapping[str, Any]) -> int:
    try:
        return int(file.get("size") or 0)
    except (TypeError, ValueError):
        return 0


def _duration_seconds(file: Mapping[str, Any]) -> float | None:
    """Slack's ``duration_ms`` for clips, when present."""
    try:
        value = file.get("duration_ms")
        return float(value) / 1000 if value is not None else None
    except (TypeError, ValueError):
        return None


@dataclass
class AudioSelection:
    """The one audio file of a message to transcribe, and Korean notes about what was left out."""

    audio: Mapping[str, Any] | None = None
    notes: list[str] = field(default_factory=list)
    # Some file was audio (heard or not): the message is a voice message.
    any_audio: bool = False


def select_audio(files: Iterable[Any], *, max_seconds: float | None = None) -> AudioSelection:
    """Which audio file of a Slack message to download: the first one within 25 MB (and ``max_seconds``). Pure.

    Sizes and clip lengths come from Slack's file objects; the download and
    the decoder check them again. Files that are neither audio nor images
    are named in a note (images are the caller's business).
    """
    max_seconds = config.get_voice_max_seconds() if max_seconds is None else max_seconds
    selection = AudioSelection()
    candidates: list[Mapping[str, Any]] = []
    too_big: list[str] = []
    too_long: list[str] = []
    other: list[str] = []
    for file in files:
        if not isinstance(file, Mapping):
            continue
        name = short_name(file.get("name") or file.get("title"))
        if not is_audio_file(file):
            if file_mimetype(file) not in IMAGE_MIMETYPES:
                other.append(name)
            continue
        selection.any_audio = True
        duration = _duration_seconds(file)
        if _size(file) > MAX_AUDIO_BYTES:
            too_big.append(name)
        elif duration is not None and duration > max_seconds + DURATION_TOLERANCE_SECONDS:
            too_long.append(name)
        else:
            candidates.append(file)
    if not selection.any_audio:
        return AudioSelection()
    if candidates:
        selection.audio = candidates[0]
    if len(candidates) > 1:
        selection.notes.append(ONE_AUDIO_TEXT)
    if too_big:
        selection.notes.append(f"{TOO_BIG_TEXT}: {', '.join(too_big)}")
    if too_long:
        selection.notes.append(TOO_LONG_FILES_TEXT.format(limit=describe_seconds(max_seconds), names=", ".join(too_long)))
    if other:
        selection.notes.append(OTHER_FILES_TEXT.format(names=", ".join(other)))
    return selection


# ---------------------------------------------------------------- the backend: PyAV + mlx-whisper (adapter)


@dataclass
class DecodedAudio:
    """16 kHz mono float32 samples (a numpy array for the real backend) and their length in seconds."""

    samples: Any
    seconds: float


class VoiceBackend(Protocol):
    """What ``Transcriber`` needs; the real one is ``MlxWhisperBackend``, tests inject fakes."""

    def check(self, model: str) -> None:
        """Raise ``VoiceUnavailable`` (Korean) when transcription cannot run here; may load the model."""

    def decode(self, data: bytes, *, max_seconds: float) -> DecodedAudio:
        """Decode audio bytes in memory; ``VoiceError`` for unreadable or too long audio."""

    def recognize(self, samples: Any, *, model: str, language: str | None) -> Mapping[str, Any]:
        """Transcribe decoded samples: ``{"text": ..., "segments": [...], "language": ...}``."""

    def download(self, model: str) -> str:
        """Download the model once (``--voice-setup``) and return its local folder."""


def current_machine() -> str:
    """``platform.machine()`` ("arm64" on Apple Silicon); tests replace this function."""
    return platform.machine()


def supported_platform() -> bool:
    """mlx-whisper runs only on Apple Silicon Macs."""
    return config.current_platform() == "darwin" and current_machine() == "arm64"


def check_platform() -> None:
    if not supported_platform():
        raise VoiceUnavailable(UNSUPPORTED_PLATFORM_TEXT)


def _import(name: str) -> Any:
    """Import one of the voice libraries; ``VoiceUnavailable`` with what to install."""
    import importlib

    try:
        return importlib.import_module(name)
    except ImportError:
        raise VoiceUnavailable(DEPS_MISSING_TEXT) from None
    except Exception as exc:  # noqa: BLE001 - a broken install (e.g. a missing Metal library)
        raise VoiceUnavailable(DEPS_BROKEN_TEXT.format(kind=f"{name}: {type(exc).__name__}")) from None


class MlxWhisperBackend:
    """PyAV for decoding, mlx-whisper for transcribing (Apple Silicon only, imported on first use).

    Confirmed against mlx-whisper 0.4.3 and PyAV 19 (see the README's
    "음성으로 일정 등록"): ``mlx_whisper.transcribe(audio: str | np.ndarray |
    mx.array, *, path_or_hf_repo=..., verbose=None, **decode_options)`` takes
    the 16 kHz waveform directly; ``language`` goes in ``decode_options``.
    With ``verbose=None`` it prints nothing (no progress bar, no text).
    """

    def __init__(self) -> None:
        self._model_paths: dict[str, str] = {}

    # -- libraries

    def _av(self) -> tuple[Any, Any]:
        check_platform()
        return _import("av"), _import("numpy")

    def _mlx_whisper(self) -> Any:
        check_platform()
        return _import("mlx_whisper")

    # -- the model (never downloaded here except by ``download``)

    def model_path(self, model: str) -> str:
        """The model's local folder: ``model`` itself, or its Hugging Face cache entry (no network)."""
        cached = self._model_paths.get(model)
        if cached:
            return cached
        local = Path(model).expanduser()
        if local.is_dir():
            path = str(local)
        else:
            hub = _import("huggingface_hub")
            try:
                path = str(hub.snapshot_download(repo_id=model, local_files_only=True))
            except Exception:  # noqa: BLE001 - LocalEntryNotFoundError and friends: not downloaded yet
                raise VoiceUnavailable(MODEL_MISSING_TEXT.format(model=model)) from None
        self._model_paths[model] = path
        return path

    def check(self, model: str) -> None:
        self._av()
        self._mlx_whisper()
        self.model_path(model)

    def download(self, model: str) -> str:
        """``huggingface_hub.snapshot_download`` (progress bars on stderr); a local folder is used as is."""
        self._mlx_whisper()
        local = Path(model).expanduser()
        if local.is_dir():
            return str(local)
        hub = _import("huggingface_hub")
        path = str(hub.snapshot_download(repo_id=model))
        self._model_paths[model] = path
        return path

    # -- decoding (PyAV, in memory)

    def decode(self, data: bytes, *, max_seconds: float) -> DecodedAudio:
        av, np = self._av()
        limit = int((max_seconds + DURATION_TOLERANCE_SECONDS) * SAMPLE_RATE)
        too_long = VoiceError(TOO_LONG_TEXT.format(limit=describe_seconds(max_seconds)))
        chunks: list[Any] = []
        total = 0
        try:
            with av.open(io.BytesIO(data), mode="r") as container:
                stream = next((s for s in container.streams if s.type == "audio"), None)
                if stream is None:
                    raise VoiceError(UNREADABLE_AUDIO_TEXT)
                if container.duration is not None and container.duration / av.time_base * SAMPLE_RATE > limit:
                    raise too_long
                resampler = av.AudioResampler(format="flt", layout="mono", rate=SAMPLE_RATE)

                def add(frames: Iterable[Any]) -> None:
                    nonlocal total
                    for frame in frames:
                        samples = frame.to_ndarray().reshape(-1)
                        total += samples.size
                        if total > limit:
                            raise too_long
                        chunks.append(samples)

                for frame in container.decode(stream):
                    add(resampler.resample(frame))
                add(resampler.resample(None))
        except VoiceError:
            raise
        except Exception:  # noqa: BLE001 - av.FFmpegError (InvalidDataError ...), broken containers
            raise VoiceError(UNREADABLE_AUDIO_TEXT) from None
        samples = np.concatenate(chunks).astype(np.float32, copy=False) if chunks else np.zeros(0, dtype=np.float32)
        return DecodedAudio(samples=samples, seconds=samples.size / SAMPLE_RATE)

    # -- transcribing (mlx-whisper)

    def recognize(self, samples: Any, *, model: str, language: str | None) -> Mapping[str, Any]:
        mlx_whisper = self._mlx_whisper()
        options: dict[str, Any] = {"path_or_hf_repo": self.model_path(model), "verbose": None}
        if language:
            options["language"] = language
        return mlx_whisper.transcribe(samples, **options)


_default_backend: MlxWhisperBackend | None = None


def default_backend() -> MlxWhisperBackend:
    """One backend per process (it remembers the model folder; mlx-whisper keeps the loaded model)."""
    global _default_backend
    if _default_backend is None:
        _default_backend = MlxWhisperBackend()
    return _default_backend


# ---------------------------------------------------------------- one transcription at a time, on one thread

# MLX keeps its streams per thread, so every call into the backend runs on this
# single worker thread; it also means one transcription at a time.
_executor: ThreadPoolExecutor | None = None
_executor_guard = threading.Lock()
# Belt and braces for direct (synchronous) callers such as ``--voice-setup``.
_transcribe_lock = threading.Lock()


def voice_executor() -> ThreadPoolExecutor:
    global _executor
    with _executor_guard:
        if _executor is None:
            _executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mungchi-voice")
        return _executor


# ``Transcriber(language=...)`` not given: ``WHISPER_LANGUAGE``.
_FROM_ENV: Any = object()


class Transcriber:
    """Audio bytes -> transcript (``""`` when nothing was heard), with the configured model and limits.

    ``backend`` defaults to ``MlxWhisperBackend``; ``WHISPER_MODEL``,
    ``WHISPER_LANGUAGE`` and ``VOICE_MAX_SECONDS`` are read when it is made.
    """

    def __init__(
        self,
        backend: VoiceBackend | None = None,
        *,
        env: Mapping[str, str] | None = None,
        model: str | None = None,
        language: str | None = _FROM_ENV,
        max_seconds: float | None = None,
        timeout: float = TRANSCRIBE_TIMEOUT_SECONDS,
        ready_timeout: float = READY_TIMEOUT_SECONDS,
    ):
        self.backend: VoiceBackend = backend or default_backend()
        self.model = model or config.get_whisper_model(env)
        self.language = config.get_whisper_language(env) if language is _FROM_ENV else language
        self.max_seconds = float(max_seconds if max_seconds is not None else config.get_voice_max_seconds(env))
        self.timeout = timeout
        self.ready_timeout = ready_timeout

    # -- synchronous (runs on the voice thread)

    def check_sync(self) -> None:
        with _transcribe_lock:
            self.backend.check(self.model)

    def transcribe_sync(self, data: bytes) -> str:
        """Decode and transcribe ``data``; ``VoiceError`` (Korean) for audio that cannot be used."""
        with _transcribe_lock:
            decoded = self.backend.decode(data, max_seconds=self.max_seconds)
            if decoded.seconds > self.max_seconds + DURATION_TOLERANCE_SECONDS:
                raise VoiceError(TOO_LONG_TEXT.format(limit=describe_seconds(self.max_seconds)))
            result = self.backend.recognize(decoded.samples, model=self.model, language=self.language)
        return clean_transcript(result)

    # -- asynchronous (the Slack bots, ``--audio``)

    async def _on_voice_thread(self, work: Any, timeout: float) -> Any:
        abandoned = threading.Event()

        def run() -> Any:
            if abandoned.is_set():  # gave up while waiting for the thread: do not start
                raise VoiceError(TIMEOUT_TEXT.format(limit=describe_seconds(timeout)))
            return work()

        future = asyncio.get_running_loop().run_in_executor(voice_executor(), run)
        try:
            return await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError:
            abandoned.set()
            raise VoiceError(TIMEOUT_TEXT.format(limit=describe_seconds(timeout))) from None

    async def check(self) -> None:
        """``VoiceUnavailable`` before anything is downloaded when transcription cannot run here."""
        await self._on_voice_thread(self.check_sync, self.ready_timeout)

    async def transcribe(self, data: bytes) -> str:
        """``transcribe_sync`` on the voice thread, at most ``timeout`` seconds."""
        return await self._on_voice_thread(lambda: self.transcribe_sync(data), self.timeout)


# ---------------------------------------------------------------- files given on the command line (--audio)


def read_audio_file(path: str | Path) -> bytes:
    """The bytes of an ``--audio`` file (at most 25 MB); ``VoiceError`` naming the file otherwise."""
    file = Path(path).expanduser()
    if not file.is_file():
        raise VoiceError(f"음성 파일을 찾을 수 없어요: {path}")
    if file.stat().st_size > MAX_AUDIO_BYTES:
        raise VoiceError(f"{TOO_BIG_TEXT}: {short_name(file.name)}")
    return file.read_bytes()


# ---------------------------------------------------------------- python -m mungchi --voice-setup


def silence_wav(seconds: float = SETUP_SIGNAL_SECONDS, rate: int = SAMPLE_RATE) -> bytes:
    """A short silent WAV file, in memory: the test signal of ``--voice-setup``."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(b"\x00\x00" * int(seconds * rate))
    return buffer.getvalue()


def run_voice_setup(
    env: Mapping[str, str] | None = None,
    *,
    backend: VoiceBackend | None = None,
    out: TextIO | None = None,
    clock: Any = time.monotonic,
) -> int:
    """Download the Whisper model once, transcribe a test signal, and say where the model is. Returns 0 when it works.

    Runs in the terminal, never in the bot. Uses no Claude API.
    """
    out = out or sys.stdout
    backend = backend or default_backend()
    model = config.get_whisper_model(env)
    language = config.get_whisper_language(env)

    def say(line: str = "") -> None:
        print(line, file=out, flush=True)

    say("음성 받아쓰기 준비 (mlx-whisper, 이 Mac에서만 처리)")
    say(f"- 모델: {model}")
    say(f"- 언어: {language or '자동 감지'}")
    say(f"- 최대 길이: {describe_seconds(config.get_voice_max_seconds(env))}")
    try:
        say(
            f"모델을 준비합니다. 처음 한 번은 Hugging Face에서 내려받아요(기본 모델은 {DEFAULT_MODEL_SIZE_TEXT}). "
            "몇 분 걸릴 수 있으니 기다려 주세요…"
        )
        started = clock()
        path = backend.download(model)
        say(f"- 모델 위치: {path}")
        transcriber = Transcriber(backend, env=env, model=path, language=language)
        loaded = clock()
        transcriber.check_sync()
        heard = transcriber.transcribe_sync(silence_wav())
        finished = clock()
    except VoiceError as exc:
        say(f"[오류] {exc}")
        return 1
    except Exception as exc:  # noqa: BLE001 - a short Korean line, never a traceback
        from .tools.common import scrub

        say(f"[오류] 음성 받아쓰기를 준비하지 못했어요 ({scrub(f'{type(exc).__name__}: {exc}')[:200]})")
        say(f"모델을 받지 못했다면 인터넷 연결을 확인하고 다시 실행해 보세요: {SETUP_COMMAND}")
        return 1
    result = f'"{heard}"' if heard else "빈 결과"
    say(
        f"- 시험 신호({describe_seconds(SETUP_SIGNAL_SECONDS)} 무음) 받아쓰기: {result} "
        f"(모델 준비 {loaded - started:.0f}초, 불러오기와 받아쓰기 {finished - loaded:.0f}초)"
    )
    say("✅ 음성 받아쓰기 준비가 끝났어요.")
    say("봇에 반영하려면: python -m mungchi service restart (터미널에서 돌리면 Ctrl+C로 끄고 다시 실행)")
    return 0
