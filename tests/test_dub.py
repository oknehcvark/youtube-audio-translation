"""Tests for the timing logic. They need only numpy (and ffmpeg for the one duration test) - no TTS, no models."""
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "youtube-audio-translation" / "scripts"))
import dub  # noqa: E402

SR = dub.SR


def tone(seconds, amp=0.5):
    t = np.arange(int(seconds * SR)) / SR
    return (amp * np.sin(2 * np.pi * 220 * t)).astype(np.float32)


def identity_stretch(y, rate, sr=SR):          # stand-in for rubberband: shorten by `rate`
    return y[: int(len(y) / rate)]


def test_srt_roundtrip(tmp_path):
    segs = [(0.0, 4.26, "Hello"), (4.46, 8.18, "second line"), (3661.5, 3662.0, "late")]
    p = tmp_path / "a.srt"
    dub.write_srt(p, segs)
    back = dub.read_srt(p)
    assert [t for *_, t in back] == ["Hello", "second line", "late"]
    assert all(abs(a[0] - b[0]) < 1e-3 and abs(a[1] - b[1]) < 1e-3 for a, b in zip(segs, back))


def test_read_srt_tolerates_crlf_and_multiline(tmp_path):
    p = tmp_path / "a.srt"
    p.write_bytes(b"1\r\n00:00:01,000 --> 00:00:02,500\r\nline one\r\nline two\r\n\r\n2\r\n00:00:03,000 --> 00:00:04,000\r\nx\r\n")
    segs = dub.read_srt(p)
    assert segs[0][2] == "line one line two" and abs(segs[0][1] - 2.5) < 1e-6 and len(segs) == 2


def test_available_runs_to_next_phrase_and_last_may_overrun():
    segs = [(0.0, 3.0, "a"), (3.2, 6.0, "b"), (10.0, 12.0, "c")]
    assert dub.available(segs, 0) == pytest.approx(3.2 - 0.06)
    assert dub.available(segs, 1) == pytest.approx(10.0 - 3.2 - 0.06)      # the gap belongs to the phrase
    assert dub.available(segs, 2) == pytest.approx(12.0 + 3.0 - 10.0 - 0.06)


def test_slow_pct_only_for_clearly_short_phrases():
    assert dub.slow_pct(4.0, 4.0) is None                  # fills the slot
    assert dub.slow_pct(3.4, 4.0) is None                  # >= 80%
    assert dub.slow_pct(2.0, 4.0) == -20                   # capped at the floor
    assert dub.slow_pct(3.0, 4.0) == -17                   # 3.0 / 3.6 - 1
    assert dub.slow_pct(3.1, 4.0) == -14


def test_trim_silence_removes_edges():
    y = np.concatenate([np.zeros(SR), tone(0.5), np.zeros(SR)])
    out = dub.trim_silence(y)
    assert 0.5 <= len(out) / SR < 0.56


def test_every_phrase_starts_on_its_original_second():
    segs = [(1.0, 3.0, "a"), (3.2, 6.0, "b"), (10.0, 12.0, "c")]
    clips = [tone(1.5), tone(2.0), tone(1.0)]
    total = 15.0
    dubbed, report = dub.place_clips(clips, segs, total, stretch=identity_stretch)
    assert len(dubbed) == int(total * SR)
    for (start, _e, _t), clip in zip(segs, clips):
        s0 = int(start * SR)
        assert np.abs(dubbed[s0 + SR // 10: s0 + len(clip) - SR // 10]).max() > 0.3     # sound is there
        assert np.abs(dubbed[s0 - SR // 20: s0 - SR // 100]).max() < 1e-6 if start > 0.1 else True


def test_long_phrase_is_sped_up_and_not_beyond_the_cap():
    segs = [(0.0, 2.0, "a"), (2.0, 4.0, "b")]
    clips = [tone(5.0), tone(1.0)]                         # first phrase is 5 s in a ~1.94 s slot
    dubbed, report = dub.place_clips(clips, segs, 6.0, max_rate=1.5, stretch=identity_stretch)
    assert report[0]["rate"] == pytest.approx(5.0 / 1.94, rel=1e-3)         # the rate that would be needed
    assert report[0]["final"] == pytest.approx(5.0 / 1.5, abs=0.01)         # but it is capped at 1.5x
    assert report[0]["overflow"] > 1.0                                      # and reported as a spill
    assert report[1]["overflow"] == 0.0


def test_total_length_is_exact_even_if_the_last_phrase_overruns():
    segs = [(0.0, 2.0, "a")]
    dubbed, _ = dub.place_clips([tone(4.0)], segs, 3.0, max_rate=1.0, stretch=identity_stretch)
    assert len(dubbed) == 3 * SR


def test_group_words_splits_on_pauses_sentences_and_length():
    words = [(0.0, 0.4, "Hello"), (0.45, 0.9, "there."), (1.0, 1.4, "Next"), (1.45, 1.9, "one"),
             (3.0, 3.4, "after"), (3.45, 3.9, "pause")]
    ph = dub.group_words(words)
    assert [p[2] for p in ph] == ["Hello there. Next one", "after pause"]      # a short sentence is not split
    long_words = [(i * 0.5, i * 0.5 + 0.45, f"w{i}") for i in range(30)]
    assert all(p[1] - p[0] <= 7.5 for p in dub.group_words(long_words, max_len=7.0))


def test_error_rate():
    assert dub.error_rate("the cat sat", "the cat sat", "en") == 0
    assert dub.error_rate("the cat sat", "the dog sat", "en") == pytest.approx(100 / 3)
    assert dub.error_rate("我爱你", "我爱你。", "zh") == 0                       # CJK: per character, punctuation ignored


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
def test_raw_aac_duration_is_decoded_not_estimated(tmp_path):
    wav = tmp_path / "t.wav"
    import soundfile as sf
    sf.write(str(wav), tone(3.0), SR)
    aac = tmp_path / "t.aac"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(wav), "-c:a", "aac", "-f", "adts", str(aac)], check=True)
    assert dub.duration(aac) == pytest.approx(3.0, abs=0.1)


def test_max_chars_follows_speaking_speed():
    assert dub.max_chars(4.0, "fr") == 60
    assert dub.max_chars(4.0, "zh") == 20
    assert dub.max_chars(4.0, "xx") == 56            # unknown language: a safe default
