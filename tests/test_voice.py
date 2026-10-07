"""Voice messages -> calendar: choosing audio, cleaning transcripts, the mlx-whisper / PyAV adapter (with fakes).

No mlx, no av, no network: the real libraries only exist on an Apple Silicon Mac.
"""

from __future__ import annotations

import asyncio
import io
import subprocess
import sys
import types
import wave

import pytest

from mungchi import config, voice
from mungchi.voice import (
    AudioSelection,
    DecodedAudio,
    MlxWhisperBackend,
    Transcriber,
    VoiceError,
    VoiceUnavailable,
    clean_transcript,
    heard_text,
    is_audio_file,
    select_audio,
    voice_prompt,
)

MB = 1024 * 1024


def _file(name, mimetype=None, size=1000, **extra):
    file = {"id": f"F{name}", "name": name, "size": size}
    if mimetype is not None:
        file["mimetype"] = mimetype
    return {**file, **extra}


@pytest.fixture
def apple_silicon(monkeypatch):
    monkeypatch.setattr(config, "current_platform", lambda: "darwin")
    monkeypatch.setattr(voice, "current_machine", lambda: "arm64")


# ---------------------------------------------------------------- choosing the audio of a message


@pytest.mark.parametrize(
    "file",
    [
        _file("voice.m4a", "audio/mp4"),
        _file("memo.mp3", "audio/mpeg"),
        _file("rec.wav", "audio/wav"),
        _file("clip.webm", "audio/webm"),
        _file("x.m4a", "audio/x-m4a"),
        _file("memo.m4a"),  # no mimetype: from the name
        _file("memo.m4a", "application/octet-stream"),
        # Slack's own audio clips, whatever container they come in.
        _file("audio_message.mp4", "video/mp4", subtype="slack_audio"),
        _file("audio_message.webm", "video/webm", media_display_type="audio"),
        _file("Recording.mov", "video/quicktime", filetype="m4a"),
        _file("audio_message.mp4", "video/mp4", filetype="aac"),
    ],
)
def test_audio_files_are_recognised(file):
    assert is_audio_file(file)


@pytest.mark.parametrize(
    "file",
    [
        _file("trip.mp4", "video/mp4"),  # a video, not a voice message
        _file("clip.mp4", "video/mp4", subtype="slack_video"),
        _file("clip.webm", "video/webm", media_display_type="video"),
        _file("poster.jpg", "image/jpeg"),
        _file("notice.pdf", "application/pdf"),
        _file("data.bin", "application/octet-stream"),
    ],
)
def test_other_files_are_not_audio(file):
    assert not is_audio_file(file)


def test_selecting_the_one_audio_file_of_a_message():
    files = [
        _file("huge.m4a", "audio/mp4", size=voice.MAX_AUDIO_BYTES + 1),
        _file("long.webm", "audio/webm", duration_ms=301_500 + 1_000),
        _file("first.m4a", "audio/mp4"),
        _file("second.mp3", "audio/mpeg"),
        _file("poster.jpg", "image/jpeg"),
        _file("notice.pdf", "application/pdf"),
        "not a file",
    ]
    selection = select_audio(files, max_seconds=300)
    assert selection.any_audio and selection.audio["name"] == "first.m4a"
    assert selection.notes == [
        "음성은 한 번에 하나만 들어요. 첫 번째 음성만 들을게요.",
        "25MB보다 큰 음성 파일은 듣지 않았어요: huge.m4a",
        "5분보다 긴 음성은 듣지 않았어요: long.webm",
        "음성과 함께 온 다른 파일은 읽지 않았어요 (읽지 않은 파일: notice.pdf).",
    ]
    assert voice.MAX_AUDIO_BYTES == 25 * MB
    # Exactly at the limits is fine; Slack's length counts with a second of slack.
    edge = select_audio([_file("ok.m4a", "audio/mp4", size=25 * MB, duration_ms=300_900)], max_seconds=300)
    assert edge.audio is not None and edge.notes == []
    # Nothing but too-big audio: still a voice message (the caller notes it), but nothing to hear.
    only_big = select_audio([_file("huge.m4a", "audio/mp4", size=26 * MB)], max_seconds=300)
    assert only_big.any_audio and only_big.audio is None
    # No audio at all: an empty selection, the photo path decides.
    assert select_audio([_file("poster.jpg", "image/jpeg"), _file("a.pdf", "application/pdf")]) == AudioSelection()
    # The default limit is VOICE_MAX_SECONDS.
    assert select_audio([_file("c.m4a", "audio/mp4", duration_ms=200_000)], max_seconds=None).audio is not None


# ---------------------------------------------------------------- transcripts and prompts


def test_cleaning_transcripts():
    assert clean_transcript({"text": "  내일   오후 3시  회의 ", "segments": [{"no_speech_prob": 0.01}]}) == "내일 오후 3시 회의"
    assert clean_transcript("다음 주 화요일") == "다음 주 화요일"
    for empty in (
        None,
        "",
        {"text": "   "},
        {"text": "... !?"},
        {"text": "♪♪"},
        {"text": "감사합니다."},
        {"text": " 시청해 주셔서 감사합니다! "},
        {"text": "구독과 좋아요 부탁드립니다"},
        {"text": "MBC 뉴스 김철수입니다."},
        {"text": "Thank you."},
        # Every segment is probably not speech: noise.
        {"text": "음 그러니까", "segments": [{"no_speech_prob": 0.9}, {"no_speech_prob": 0.7}]},
    ):
        assert clean_transcript(empty) == "", empty
    # One real segment is enough.
    assert clean_transcript({"text": "회의 잡아줘", "segments": [{"no_speech_prob": 0.9}, {"no_speech_prob": 0.1}]}) == "회의 잡아줘"
    # A real sentence that contains a filler phrase is kept.
    assert clean_transcript("회의 끝나고 감사합니다 인사하기") == "회의 끝나고 감사합니다 인사하기"


def test_heard_text_is_cut_to_about_500_characters():
    assert heard_text("내일  3시\n회의") == '🎙️ 들은 내용: "내일 3시 회의"'
    long = heard_text("가" * 800)
    assert long.startswith('🎙️ 들은 내용: "가') and long.endswith('…"') and len(long) < 520
    assert heard_text("가" * 800, limit=None).count("가") == 800


def test_the_agent_prompt_marks_the_transcript_and_asks_for_care():
    assert voice_prompt("", " 내일 3시 \n 회의 ") == (
        "[음성 메시지 받아쓰기] 내일 3시 회의\n"
        "(음성 인식으로 받아 적은 글이라 잘못 들은 글자가 있을 수 있어요. 특히 이름·숫자·날짜·시각이 애매하면 짐작하지 말고 물어봐 주세요.)"
    )
    assert voice_prompt(" Research로 ", "회의").startswith("Research로\n[음성 메시지 받아쓰기] 회의\n")


def test_describe_seconds():
    assert [voice.describe_seconds(s) for s in (300, 90, 45, 3600, 0)] == ["5분", "1분 30초", "45초", "60분", "0초"]


def test_voice_settings():
    assert config.get_whisper_model({}) == "mlx-community/whisper-large-v3-turbo"
    assert config.get_whisper_model({"WHISPER_MODEL": " mlx-community/whisper-small-mlx "}) == "mlx-community/whisper-small-mlx"
    assert config.get_whisper_language({}) == "ko"
    assert config.get_whisper_language({"WHISPER_LANGUAGE": "EN"}) == "en"
    assert config.get_whisper_language({"WHISPER_LANGUAGE": ""}) is None  # empty: detect the language
    assert config.get_whisper_language({"WHISPER_LANGUAGE": "auto"}) is None
    assert config.get_voice_max_seconds({}) == 300
    assert config.get_voice_max_seconds({"VOICE_MAX_SECONDS": "120"}) == 120
    for bad in ("0", "-5", "abc", "99999"):
        assert config.get_voice_max_seconds({"VOICE_MAX_SECONDS": bad}) == 300
    transcriber = Transcriber(FakeBackend(), env={"WHISPER_MODEL": "m", "WHISPER_LANGUAGE": "", "VOICE_MAX_SECONDS": "60"})
    assert (transcriber.model, transcriber.language, transcriber.max_seconds) == ("m", None, 60.0)


# ---------------------------------------------------------------- the transcriber (fake backend)


class FakeBackend:
    def __init__(self, result="내일 3시 회의", seconds=3.0, error=None):
        self.result, self.seconds, self.error = result, seconds, error
        self.calls: list[tuple] = []

    def check(self, model):
        self.calls.append(("check", model))

    def decode(self, data, *, max_seconds):
        self.calls.append(("decode", len(data), max_seconds))
        return DecodedAudio(samples=[0.0] * 4, seconds=self.seconds)

    def recognize(self, samples, *, model, language):
        self.calls.append(("recognize", model, language))
        if self.error:
            raise self.error
        return {"text": self.result, "segments": [{"no_speech_prob": 0.02}]}

    def download(self, model):
        self.calls.append(("download", model))
        return f"/cache/{model}"


def test_transcriber_decodes_then_recognises_with_the_settings():
    backend = FakeBackend()
    transcriber = Transcriber(backend, model="m", language="ko", max_seconds=300)
    assert asyncio.run(transcriber.transcribe(b"audio")) == "내일 3시 회의"
    asyncio.run(transcriber.check())
    assert backend.calls == [("decode", 5, 300.0), ("recognize", "m", "ko"), ("check", "m")]


def test_audio_longer_than_the_limit_is_rejected_before_transcribing():
    backend = FakeBackend(seconds=301.5)
    with pytest.raises(VoiceError, match=r"음성이 너무 길어요\(5분까지 들어요\)"):
        asyncio.run(Transcriber(backend, model="m", max_seconds=300).transcribe(b"audio"))
    assert [c[0] for c in backend.calls] == ["decode"]
    assert asyncio.run(Transcriber(FakeBackend(seconds=300.9), model="m", max_seconds=300).transcribe(b"a"))


def test_a_slow_transcription_times_out_with_a_korean_note():
    class Slow(FakeBackend):
        def recognize(self, samples, *, model, language):
            import time

            time.sleep(0.3)
            return super().recognize(samples, model=model, language=language)

    with pytest.raises(VoiceError) as caught:
        asyncio.run(Transcriber(Slow(), model="m", timeout=0.05).transcribe(b"audio"))
    assert str(caught.value).startswith("음성을 글로 옮기는 데 너무 오래 걸려서 멈췄어요")
    assert Transcriber(FakeBackend(), model="m").timeout == voice.TRANSCRIBE_TIMEOUT_SECONDS == 180.0


def test_transcriptions_run_on_one_worker_thread():
    names = []

    class Recording(FakeBackend):
        def recognize(self, samples, *, model, language):
            import threading

            names.append(threading.current_thread().name)
            return super().recognize(samples, model=model, language=language)

    async def many():
        transcriber = Transcriber(Recording(), model="m")
        return await asyncio.gather(*(transcriber.transcribe(b"a") for _ in range(4)))

    assert asyncio.run(many()) == ["내일 3시 회의"] * 4
    assert len(set(names)) == 1 and names[0].startswith("mungchi-voice")


# ---------------------------------------------------------------- the real adapter, with fake modules


def test_the_real_backend_needs_an_apple_silicon_mac():
    backend = MlxWhisperBackend()
    for call in (lambda: backend.check("m"), lambda: backend.decode(b"a", max_seconds=300), lambda: backend.download("m")):
        with pytest.raises(VoiceUnavailable) as caught:
            call()
        assert str(caught.value) == voice.UNSUPPORTED_PLATFORM_TEXT
    assert "Apple Silicon Mac" in voice.UNSUPPORTED_PLATFORM_TEXT and "🎤 받아쓰기" in voice.UNSUPPORTED_PLATFORM_TEXT


def test_missing_packages_say_what_to_install(apple_silicon, monkeypatch):
    monkeypatch.setitem(sys.modules, "mlx_whisper", None)
    monkeypatch.setitem(sys.modules, "av", None)
    with pytest.raises(VoiceUnavailable) as caught:
        MlxWhisperBackend().check("m")
    assert str(caught.value) == voice.DEPS_MISSING_TEXT
    assert "pip install -e ." in voice.DEPS_MISSING_TEXT and "python -m mungchi --voice-setup" in voice.DEPS_MISSING_TEXT


def _fake_modules(monkeypatch, *, cached=True):
    calls: dict[str, list] = {"transcribe": [], "snapshot": []}

    def transcribe(audio, **options):
        calls["transcribe"].append((audio, options))
        return {"text": " 회의 ", "segments": [], "language": options.get("language", "ko")}

    def snapshot_download(repo_id, **options):
        calls["snapshot"].append((repo_id, options))
        if options.get("local_files_only") and not cached:
            raise FileNotFoundError("not in the cache")
        return f"/hf/{repo_id}/snapshot"

    monkeypatch.setitem(sys.modules, "av", types.ModuleType("av"))
    monkeypatch.setitem(sys.modules, "numpy", types.ModuleType("numpy"))
    monkeypatch.setitem(sys.modules, "mlx_whisper", types.SimpleNamespace(transcribe=transcribe))
    monkeypatch.setitem(sys.modules, "huggingface_hub", types.SimpleNamespace(snapshot_download=snapshot_download))
    return calls


def test_the_adapter_calls_mlx_whisper_with_the_local_model_and_language(apple_silicon, monkeypatch):
    calls = _fake_modules(monkeypatch)
    backend = MlxWhisperBackend()
    backend.check("mlx-community/whisper-large-v3-turbo")
    result = backend.recognize("SAMPLES", model="mlx-community/whisper-large-v3-turbo", language="ko")
    assert result["text"] == " 회의 "
    assert calls["transcribe"] == [
        ("SAMPLES", {"path_or_hf_repo": "/hf/mlx-community/whisper-large-v3-turbo/snapshot", "verbose": None, "language": "ko"})
    ]
    # The model is looked up in the local cache only (no network), once.
    assert calls["snapshot"] == [("mlx-community/whisper-large-v3-turbo", {"local_files_only": True})]
    backend.recognize("S", model="mlx-community/whisper-large-v3-turbo", language=None)
    assert "language" not in calls["transcribe"][-1][1]  # auto-detect


def test_a_model_not_downloaded_yet_points_to_voice_setup(apple_silicon, monkeypatch):
    calls = _fake_modules(monkeypatch, cached=False)
    with pytest.raises(VoiceUnavailable) as caught:
        MlxWhisperBackend().check("mlx-community/whisper-large-v3-turbo")
    assert "음성 인식 모델(mlx-community/whisper-large-v3-turbo)이 아직 이 Mac에 없어요" in str(caught.value)
    assert "python -m mungchi --voice-setup" in str(caught.value) and "약 1.6GB" in str(caught.value)
    assert calls["transcribe"] == []
    # --voice-setup downloads it (with network), then it is found.
    assert MlxWhisperBackend().download("mlx-community/whisper-large-v3-turbo") == "/hf/mlx-community/whisper-large-v3-turbo/snapshot"
    assert calls["snapshot"][-1] == ("mlx-community/whisper-large-v3-turbo", {})


def test_a_local_model_folder_is_used_as_is(apple_silicon, monkeypatch, tmp_path):
    calls = _fake_modules(monkeypatch)
    backend = MlxWhisperBackend()
    backend.recognize("S", model=str(tmp_path), language="ko")
    assert calls["transcribe"][0][1]["path_or_hf_repo"] == str(tmp_path) and calls["snapshot"] == []


def test_real_pyav_decoding_when_available(apple_silicon):
    pytest.importorskip("numpy")
    pytest.importorskip("av")
    decoded = MlxWhisperBackend().decode(voice.silence_wav(1.0), max_seconds=300)
    assert decoded.samples.dtype.name == "float32" and decoded.samples.shape == (16000,) and decoded.seconds == 1.0
    with pytest.raises(VoiceError, match="음성 파일을 읽지 못했어요"):
        MlxWhisperBackend().decode(b"not audio" * 50, max_seconds=300)


def test_the_setup_test_signal_is_a_short_silent_wav():
    with wave.open(io.BytesIO(voice.silence_wav()), "rb") as signal:
        assert (signal.getnchannels(), signal.getframerate(), signal.getnframes()) == (1, 16000, 16000)


def test_importing_the_package_never_imports_mlx_or_av():
    code = (
        "import sys, mungchi, mungchi.main, mungchi.slack_bot, mungchi.voice\n"
        "mungchi.main.build_parser().format_help()\n"
        "mungchi.voice.select_audio([{'name': 'a.m4a', 'mimetype': 'audio/mp4'}])\n"
        "print(sorted(m for m in ('mlx_whisper', 'mlx', 'av', 'huggingface_hub') if m in sys.modules))\n"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout
    assert out.strip() == "[]"


# ---------------------------------------------------------------- python -m mungchi --voice-setup


def test_voice_setup_downloads_tests_and_says_where_the_model_is(capsys):
    backend = FakeBackend(result="")
    assert voice.run_voice_setup({}, backend=backend) == 0
    out = capsys.readouterr().out
    assert "- 모델: mlx-community/whisper-large-v3-turbo" in out and "- 언어: ko" in out
    assert "약 1.6GB" in out and "몇 분 걸릴 수 있으니" in out
    assert "- 모델 위치: /cache/mlx-community/whisper-large-v3-turbo" in out
    assert "시험 신호(1초 무음) 받아쓰기: 빈 결과" in out and "✅ 음성 받아쓰기 준비가 끝났어요." in out
    assert "python -m mungchi service restart" in out
    assert [c[0] for c in backend.calls] == ["download", "check", "decode", "recognize"]
    assert backend.calls[1] == ("check", "/cache/mlx-community/whisper-large-v3-turbo")  # the downloaded folder


def test_voice_setup_failures_are_korean_and_exit_1(capsys):
    assert voice.run_voice_setup({}) == 1  # Linux: the real backend
    assert f"[오류] {voice.UNSUPPORTED_PLATFORM_TEXT}" in capsys.readouterr().out

    class Offline(FakeBackend):
        def download(self, model):
            raise ConnectionError("huggingface.co unreachable")

    assert voice.run_voice_setup({"WHISPER_MODEL": "mlx-community/whisper-small-mlx"}, backend=Offline()) == 1
    out = capsys.readouterr().out
    assert "- 모델: mlx-community/whisper-small-mlx" in out
    assert "[오류] 음성 받아쓰기를 준비하지 못했어요 (ConnectionError: huggingface.co unreachable)" in out
    assert "모델을 받지 못했다면 인터넷 연결을 확인하고 다시 실행해 보세요" in out


def test_command_line_audio_files_are_checked(tmp_path):
    memo = tmp_path / "memo.m4a"
    memo.write_bytes(b"audio")
    assert voice.read_audio_file(memo) == b"audio"
    with pytest.raises(VoiceError, match="음성 파일을 찾을 수 없어요"):
        voice.read_audio_file(tmp_path / "missing.m4a")
    big = tmp_path / "big.m4a"
    with big.open("wb") as handle:
        handle.truncate(voice.MAX_AUDIO_BYTES + 1)
    with pytest.raises(VoiceError, match="25MB보다 큰 음성 파일은 듣지 않았어요: big.m4a"):
        voice.read_audio_file(big)


def test_env_example_lists_the_voice_settings_with_their_defaults():
    from pathlib import Path

    from dotenv import dotenv_values

    values = dotenv_values(Path(__file__).resolve().parents[1] / ".env.example")
    assert values["WHISPER_MODEL"] == "mlx-community/whisper-large-v3-turbo"
    assert values["WHISPER_LANGUAGE"] == "ko" and values["VOICE_MAX_SECONDS"] == "300"
    assert config.get_whisper_model(values) == config.DEFAULT_WHISPER_MODEL
    assert config.get_whisper_language(values) == "ko" and config.get_voice_max_seconds(values) == 300
