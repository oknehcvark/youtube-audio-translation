#!/usr/bin/env python3
"""youtube-audio-translation - dub a video's speech into another language, keeping the music and the timing.

    dub.py separate   VIDEO_OR_AUDIO -o DIR                       -> DIR/vocals.wav, DIR/music.wav
    dub.py transcribe DIR/vocals.wav --lang ru -o source.srt      -> phrase-level subtitles with timings
    dub.py budget     source.srt --lang fr                        -> characters that fit into each phrase
    dub.py srt        source.srt lines.json -o target.srt         -> translated text on the original timings
    dub.py build      target.srt --ref VIDEO --voice fr-FR-HenriNeural -o OUT   -> OUT/dub_only.wav
    dub.py mix        OUT/dub_only.wav --music DIR/music.wav --ref VIDEO -o OUT/final
    dub.py check      OUT/dub_only.wav target.srt --lang fr --ref VIDEO

The idea: every translated phrase is spoken by a TTS voice and put at the exact second where the
original phrase starts (no drift), sped up a little only if it does not fit before the next phrase.
The result is mixed with the music and cut to the exact length of the video.
"""
import argparse
import asyncio
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np

SR = 48000
CJK = {"zh", "ja", "ko", "th"}   # languages scored per character instead of per word


# ----------------------------------------------------------------------------- small helpers
def run(cmd, **kw):
    return subprocess.run([str(c) for c in cmd], check=True, **kw)


def need(binary, hint):
    if not shutil.which(binary):
        sys.exit(f"`{binary}` not found. {hint}")


def t2s(t):
    h, m, rest = t.strip().split(":")
    s, ms = rest.replace(".", ",").split(",")
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000


def s2t(x):
    x = max(x, 0.0)
    ms = int(round(x * 1000))
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


def read_srt(path):
    """-> [(start, end, text)]"""
    text = Path(path).read_text(encoding="utf-8-sig").replace("\r\n", "\n").strip()
    out = []
    for block in re.split(r"\n\s*\n", text):
        lines = [ln for ln in block.strip().split("\n")]
        k = next((i for i, ln in enumerate(lines) if "-->" in ln), None)
        if k is None:
            continue
        a, b = lines[k].split("-->")
        out.append((t2s(a), t2s(b), " ".join(x.strip() for x in lines[k + 1:] if x.strip())))
    return out


def write_srt(path, segs):
    blocks = [f"{i}\n{s2t(a)} --> {s2t(b)}\n{t}\n" for i, (a, b, t) in enumerate(segs, 1)]
    Path(path).write_text("\n".join(blocks), encoding="utf-8")


def duration(path):
    """Real duration of a media file in seconds.

    A raw .aac (ADTS) file has no duration in its header: ffprobe only ESTIMATES it from the bitrate
    and can be off by many seconds, so such files are decoded and counted instead. Pass the VIDEO as
    --ref whenever you can: platforms compare the audio track with the video's duration.
    """
    path = Path(path)
    if path.suffix.lower() in {".aac", ".adts"}:
        data = run(["ffmpeg", "-v", "error", "-i", path, "-f", "s16le", "-ac", "1", "-ar", "8000", "-"],
                   stdout=subprocess.PIPE).stdout
        return len(data) / 2 / 8000
    out = run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
              stdout=subprocess.PIPE).stdout.decode().strip()
    return float(out)


def read_wav(path, mono=True):
    import soundfile as sf
    y, sr = sf.read(str(path), dtype="float32", always_2d=True)
    y = y.mean(axis=1) if mono else y
    return y, sr


# ----------------------------------------------------------------------------- timing logic (pure)
def trim_silence(y, thr=0.02, pad=0.015, sr=SR):
    if len(y) == 0:
        return y
    a = np.abs(y)
    idx = np.where(a > thr * a.max())[0]
    if len(idx) == 0:
        return y[:0]
    p = int(pad * sr)
    return y[max(idx[0] - p, 0): min(idx[-1] + p, len(y))]


def available(segs, i, tail=3.0):
    """Seconds a phrase may take: from its start up to the start of the next phrase.
    The last phrase may run a little past its end."""
    start, end, _ = segs[i]
    nxt = segs[i + 1][0] if i + 1 < len(segs) else end + tail
    return max(nxt - start - 0.06, 0.3)


def slow_pct(dur, avail, floor=-20):
    """Phrase much shorter than its slot -> how many % to slow the TTS voice (None = leave it)."""
    if dur >= 0.8 * avail:
        return None
    pct = max(int(round((dur / (0.9 * avail) - 1) * 100)), floor)
    return pct if pct <= -3 else None


def place_clips(clips, segs, total, sr=SR, max_rate=1.5, stretch=None, fade=0.006):
    """Put every clip at its phrase start. A clip that does not fit before the next phrase is sped up
    (at most `max_rate`). Returns (mono float32 array of `total` seconds, report)."""
    dub = np.zeros(int(round(total * sr)) + sr, dtype=np.float32)
    report = []
    for i, ((start, _end, _t), y) in enumerate(zip(segs, clips)):
        if len(y) == 0:
            continue
        dur = len(y) / sr
        avail = available(segs, i)
        rate = max(dur / avail, 1.0)
        if rate > 1.0:
            y = (stretch or stretch_audio)(y, min(rate, max_rate), sr).astype(np.float32)
        y = y.copy()
        f = max(int(fade * sr), 1)
        y[:f] *= np.linspace(0, 1, f)
        y[-f:] *= np.linspace(1, 0, f)
        s0 = int(round(start * sr))
        dub[s0:s0 + len(y)] += y[: max(len(dub) - s0, 0)]
        final = len(y) / sr
        report.append(dict(n=i + 1, start=round(start, 2), slot=round(avail, 2), tts=round(dur, 2),
                           rate=round(rate, 3), final=round(final, 2),
                           overflow=round(max(final - avail, 0), 2)))
    return dub[: int(round(total * sr))], report


def stretch_audio(y, rate, sr=SR):
    """Speed up by `rate` keeping the pitch. rubberband when available, else ffmpeg atempo."""
    try:
        import pyrubberband as pyrb
        if shutil.which("rubberband"):
            return pyrb.time_stretch(y, sr, rate)
    except Exception:
        pass
    import soundfile as sf
    with tempfile.TemporaryDirectory() as d:
        a, b = Path(d, "a.wav"), Path(d, "b.wav")
        sf.write(str(a), y, sr)
        run(["ffmpeg", "-y", "-v", "error", "-i", a, "-af", f"atempo={rate:.4f}", b])
        return sf.read(str(b), dtype="float32")[0]


def active_rms(x, sr=SR):
    blk = int(0.05 * sr)
    n = len(x) // blk
    if n == 0:
        return 1e-9
    r = np.sqrt((x[: n * blk].reshape(n, blk) ** 2).mean(axis=1))
    r = r[r > r.max() * 0.1]
    return float(np.sqrt((r ** 2).mean())) if len(r) else 1e-9


# Comfortable speaking speed of the stock voices, in characters per second (tuned on real runs: with these
# budgets phrases needed at most ~1.1-1.25x speed-up). Chinese/Japanese/Korean are counted in characters.
CPS = {"en": 15, "fr": 15, "es": 14, "pt": 14, "it": 14, "de": 13, "ru": 14, "hi": 13, "zh": 5, "ja": 7, "ko": 8}


def max_chars(slot, lang):
    return int(slot * CPS.get(lang, 14))


def group_words(words, gap=0.5, max_len=7.0, min_sentence=2.0):
    """words: [(start, end, text)] -> phrases [(start, end, text)] of roughly 2-7 seconds."""
    phrases, cur = [], []
    for w in words:
        if cur:
            prev, first = cur[-1], cur[0]
            ends_sentence = re.search(r"[.!?…。！？]$", prev[2].strip()) is not None
            if (w[0] - prev[1] >= gap) or (ends_sentence and prev[1] - first[0] >= min_sentence) \
                    or (w[1] - first[0] > max_len):
                phrases.append(cur)
                cur = []
        cur.append(w)
    if cur:
        phrases.append(cur)
    joiner = "" if False else " "
    return [(p[0][0], p[-1][1], joiner.join(x[2].strip() for x in p).strip()) for p in phrases]


# ----------------------------------------------------------------------------- scoring (pure)
def error_rate(ref_text, hyp_text, lang):
    """Word error rate (character error rate for CJK languages), in percent."""
    def norm(s):
        s = s.lower().replace("’", "'")
        return re.sub(r"[\s.,!?;:\-—\"«»¿¡।'()，。！？：；、“”…]+", "" if lang in CJK else " ", s).strip()
    r, h = norm(ref_text), norm(hyp_text)
    r = list(r) if lang in CJK else r.split()
    h = list(h) if lang in CJK else h.split()
    if not r:
        return 0.0
    prev = list(range(len(h) + 1))
    for i in range(1, len(r) + 1):
        cur = [i] + [0] * len(h)
        for j in range(1, len(h) + 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (r[i - 1] != h[j - 1]))
        prev = cur
    return 100.0 * prev[-1] / len(r)


# ----------------------------------------------------------------------------- commands
def cmd_separate(a):
    need("ffmpeg", "Install ffmpeg (brew install ffmpeg / winget install ffmpeg / apt install ffmpeg).")
    exe = shutil.which("audio-separator")
    if not exe:
        sys.exit("`audio-separator` not found. Install it:  pip install \"audio-separator[cpu]\"")
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    wav = out / "_input.wav"
    run(["ffmpeg", "-y", "-v", "error", "-i", a.input, "-vn", "-ac", "2", "-ar", "44100", wav])
    run([exe, wav, "--model_filename", a.model, "--output_dir", out, "--output_format", "WAV"])
    voc = next(out.glob("*(Vocals)*.wav"), None)
    ins = next(out.glob("*(Instrumental)*.wav"), None)
    if not voc or not ins:
        sys.exit("The separator did not produce both stems; see its output above.")
    voc.replace(out / "vocals.wav")
    ins.replace(out / "music.wav")
    wav.unlink()
    print(f"vocals: {out / 'vocals.wav'}\nmusic:  {out / 'music.wav'}")


def cmd_transcribe(a):
    from faster_whisper import WhisperModel
    model = WhisperModel(a.model, device="auto", compute_type="auto")
    segments, _info = model.transcribe(str(a.audio), language=a.lang, word_timestamps=True,
                                       vad_filter=True, condition_on_previous_text=False)
    words = [(w.start, w.end, w.word) for s in segments for w in (s.words or [])]
    phrases = group_words(words, max_len=a.max_phrase)
    write_srt(a.out, phrases)
    print(f"{len(phrases)} phrases -> {a.out}. READ THEM and fix recognition mistakes (names, terms) "
          f"before translating.")


def cmd_srt(a):
    segs = read_srt(a.source)
    lines = json.loads(Path(a.lines).read_text(encoding="utf-8"))
    if isinstance(lines, dict):
        lines = [lines[str(i)] for i in range(1, len(lines) + 1)]
    if len(lines) != len(segs):
        sys.exit(f"{len(segs)} phrases in {a.source}, but {len(lines)} lines in {a.lines}.")
    write_srt(a.out, [(s, e, t) for (s, e, _), t in zip(segs, lines)])
    print(f"{len(segs)} phrases -> {a.out}")


def cmd_budget(a):
    segs = read_srt(a.source)
    print(f"{'#':>4} {'start':>8} {'slot,s':>7} {'max chars':>9}  source text")
    for i, (start, _e, text) in enumerate(segs):
        slot = available(segs, i)
        print(f"{i + 1:>4} {start:>8.2f} {slot:>7.2f} {max_chars(slot, a.lang):>9}  {text[:60]}")
    print(f"\nWrite each {a.lang} line at or under its 'max chars' (about {CPS.get(a.lang, 14)} chars/s). "
          f"Over the budget the voice has to be sped up and sounds rushed.")


async def synth(texts, voice, segdir, rates=None, only=None):
    import edge_tts
    sem = asyncio.Semaphore(4)
    rates = rates or {}

    async def one(i, text):
        if only is not None and i not in only:
            return
        mp3, wav = segdir / f"{i + 1:03d}.mp3", segdir / f"{i + 1:03d}.wav"
        key = segdir / f"{i + 1:03d}.key"
        stamp = f"{voice}|{text}"
        if only is None and wav.exists() and key.exists() and key.read_text(encoding="utf-8") == stamp:
            return                      # same voice and text as last time: keep the existing audio
        if not text.strip():
            return
        async with sem:
            for attempt in range(5):
                try:
                    await edge_tts.Communicate(text, voice, rate=rates.get(i, "+0%")).save(str(mp3))
                    break
                except Exception as e:                       # network blip: wait and retry
                    print(f"retry {i + 1}: {e}", flush=True)
                    await asyncio.sleep(2 * (attempt + 1))
            else:
                raise RuntimeError(f"TTS failed for phrase {i + 1}")
        run(["ffmpeg", "-y", "-v", "error", "-i", mp3, "-ac", "1", "-ar", str(SR), wav])
        key.write_text(stamp, encoding="utf-8")

    await asyncio.gather(*[one(i, t) for i, t in enumerate(texts)])


def cmd_build(a):
    need("ffmpeg", "Install ffmpeg (brew install ffmpeg / winget install ffmpeg / apt install ffmpeg).")
    import soundfile as sf
    segs = read_srt(a.srt)
    total = duration(a.ref)
    out = Path(a.out)
    segdir = out / "segs"
    segdir.mkdir(parents=True, exist_ok=True)
    texts = [t for _, _, t in segs]

    asyncio.run(synth(texts, a.voice, segdir))

    def load(i):
        p = segdir / f"{i + 1:03d}.wav"
        return trim_silence(read_wav(p)[0]) if p.exists() else np.zeros(0, dtype=np.float32)

    if not a.no_adapt:     # voice much faster than the speaker: slow the voice itself (no artifacts)
        adapt = {}
        for i in range(len(segs)):
            pct = slow_pct(len(load(i)) / SR, available(segs, i))
            if pct is not None:
                adapt[i] = f"{pct}%"
        if adapt:
            print(f"slowing the voice a little for {len(adapt)} short phrases", flush=True)
            asyncio.run(synth(texts, a.voice, segdir, rates=adapt, only=set(adapt)))

    clips = [load(i) for i in range(len(segs))]
    dub, report = place_clips(clips, segs, total, max_rate=a.max_rate)

    if a.loudness_ref:                                   # match the original voice's loudness
        ref, ref_sr = read_wav(a.loudness_ref)
        gain = active_rms(ref, ref_sr) / active_rms(dub)
    else:
        gain = 0.08 / active_rms(dub)
    dub = np.clip(dub * gain, -0.98, 0.98).astype(np.float32)
    sf.write(str(out / "dub_only.wav"), np.stack([dub, dub], axis=1), SR, subtype="PCM_16")
    (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")

    rates = [r["rate"] for r in report]
    fast = [(r["n"], r["rate"]) for r in report if r["rate"] > 1.25]
    over = [(r["n"], r["overflow"]) for r in report if r["overflow"] > 0.05]
    print(f"{len(report)} phrases placed on a {total:.3f} s timeline -> {out / 'dub_only.wav'}")
    print(f"speed-up: max {max(rates, default=1):.2f}x, mean {np.mean(rates) if rates else 1:.3f}x")
    if fast:
        print("phrases needing >1.25x (shorten these lines and rebuild):", fast)
    if over:
        print("phrases that spill into the next one (shorten them):", over)


def cmd_mix(a):
    need("ffmpeg", "Install ffmpeg >= 4.4 (brew install ffmpeg / winget install ffmpeg / apt install ffmpeg).")
    total = duration(a.ref)
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    g = a.music_gain
    graph = (f"[0:a]aresample={SR},apad=whole_dur={total:.6f},atrim=0:{total:.6f}[d];"
             f"[1:a]aresample={SR},volume={g},apad=whole_dur={total:.6f},atrim=0:{total:.6f}[m];"
             f"[d][m]amix=inputs=2:duration=longest:normalize=0,alimiter=limit=0.95:level=0,"
             f"atrim=0:{total:.6f},asetpts=PTS-STARTPTS")
    wav, m4a = out.with_suffix(".wav"), out.with_suffix(".m4a")
    run(["ffmpeg", "-y", "-v", "error", "-i", a.dub, "-i", a.music, "-filter_complex", graph,
         "-ar", SR, "-ac", 2, "-c:a", "pcm_s16le", wav])
    run(["ffmpeg", "-y", "-v", "error", "-i", wav, "-c:a", "aac", "-b:a", "192k", m4a])
    print(f"video : {total:.3f} s")
    for f in (wav, m4a):
        d = duration(f)
        flag = "" if abs(d - total) < 0.05 else "   <-- DIFFERS FROM THE VIDEO"
        print(f"{f.name}: {d:.3f} s{flag}")


def cmd_check(a):
    from faster_whisper import WhisperModel
    segs = read_srt(a.srt)
    model = WhisperModel(a.model, device="auto", compute_type="auto")
    got, _ = model.transcribe(str(a.audio), language=a.lang, beam_size=1, vad_filter=True,   # VAD: no made-up text in pauses
                              condition_on_previous_text=False)
    hyp = " ".join(s.text.strip() for s in got)
    ref = " ".join(t for _, _, t in segs)
    unit = "CER" if a.lang in CJK else "WER"
    print(f"{unit}: {error_rate(ref, hyp, a.lang):.1f}%  (the speech recognizer re-reads the dub; "
          f"numbers, names and spelling variants count as errors, so a few % to ~15% is normal)")
    if a.ref:
        total, d = duration(a.ref), duration(a.audio)
        print(f"length: dub {d:.3f} s vs video {total:.3f} s" +
              ("" if abs(d - total) < 0.05 else "   <-- DIFFERS"))
    rep = Path(a.audio).with_name("report.json")
    if rep.exists():
        r = json.loads(rep.read_text(encoding="utf-8"))
        print(f"timing: every phrase starts at its original second; max speed-up "
              f"{max(x['rate'] for x in r):.2f}x; spills: {sum(1 for x in r if x['overflow'] > 0.05)}")
    print("Note: this does not listen to the audio. Have a person (ideally a native speaker) listen.")


# ----------------------------------------------------------------------------- cli
def main(argv=None):
    p = argparse.ArgumentParser(prog="dub.py", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("separate", help="split a video/audio into vocals.wav and music.wav")
    s.add_argument("input"); s.add_argument("-o", "--out", required=True)
    s.add_argument("--model", default="UVR-MDX-NET-Voc_FT.onnx", help="audio-separator model")
    s.set_defaults(fn=cmd_separate)

    s = sub.add_parser("transcribe", help="speech -> phrase-level .srt (faster-whisper)")
    s.add_argument("audio"); s.add_argument("--lang", required=True); s.add_argument("-o", "--out", required=True)
    s.add_argument("--model", default="large-v3-turbo", help="model name or local folder")
    s.add_argument("--max-phrase", type=float, default=7.0, help="longest phrase in seconds")
    s.set_defaults(fn=cmd_transcribe)

    s = sub.add_parser("srt", help="source.srt + lines.json -> target.srt on the same timings")
    s.add_argument("source"); s.add_argument("lines"); s.add_argument("-o", "--out", required=True)
    s.set_defaults(fn=cmd_srt)

    s = sub.add_parser("budget", help="how many characters fit into each phrase (use it before translating)")
    s.add_argument("source"); s.add_argument("--lang", required=True, help="TARGET language code, e.g. fr")
    s.set_defaults(fn=cmd_budget)

    s = sub.add_parser("build", help="speak target.srt and place every phrase at its original time")
    s.add_argument("srt"); s.add_argument("--ref", required=True, help="the VIDEO (its length is the target)")
    s.add_argument("--voice", required=True, help="edge-tts voice, e.g. fr-FR-HenriNeural")
    s.add_argument("-o", "--out", required=True)
    s.add_argument("--loudness-ref", help="original vocals.wav: match its loudness")
    s.add_argument("--max-rate", type=float, default=1.5, help="never speed a phrase up more than this")
    s.add_argument("--no-adapt", action="store_true", help="do not slow the voice on short phrases")
    s.set_defaults(fn=cmd_build)

    s = sub.add_parser("mix", help="dub + music -> final .wav/.m4a with the exact length of the video")
    s.add_argument("dub"); s.add_argument("--music", required=True)
    s.add_argument("--ref", required=True, help="the VIDEO"); s.add_argument("-o", "--out", required=True,
                                                                           help="output path without extension")
    s.add_argument("--music-gain", type=float, default=1.0)
    s.set_defaults(fn=cmd_mix)

    s = sub.add_parser("check", help="re-read the dub with a speech recognizer and compare lengths")
    s.add_argument("audio"); s.add_argument("srt"); s.add_argument("--lang", required=True)
    s.add_argument("--ref"); s.add_argument("--model", default="large-v3-turbo")
    s.set_defaults(fn=cmd_check)

    a = p.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
