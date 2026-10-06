---
name: youtube-audio-translation
description: Dub a video's speech into another language and keep the original music, with every phrase landing on the same second as in the original. Use when the user wants to translate / dub / voice-over a video or audio into other languages, make an extra audio track for YouTube (multi-language audio), or "translate my video into French / Spanish / Hindi / Chinese ...". The agent translates the text itself, the tool speaks it, places it on the original timings, mixes it with the music and cuts it to the exact length of the video.
---

# YouTube audio translation

Turns one video into extra audio tracks in other languages. Everything is free and local except the
voices (`edge-tts` uses Microsoft's online voices). The engine is `scripts/dub.py` in this skill's folder.

The pipeline: **separate** voice and music -> **transcribe** the voice into timed phrases -> **you translate**
each phrase to fit its time -> **build** (speak every phrase and put it on the original second) ->
**mix** with the music at the exact length of the video -> **check**.

## Rules (each one cost a failed attempt)

1. **The length of the result must equal the length of the VIDEO file** (`--ref video.mp4`). Never take it
   from a raw `.aac`: `ffprobe` only estimates its duration from the bitrate and can be 15 s wrong. YouTube
   rejects a track that does not match the video ("the audio track and the video must be the same length").
2. **You translate; do not use an automatic translator.** A translator ignores how many seconds a phrase has.
   Run `budget` first and write each line within its character budget. Over budget the voice is sped up and
   sounds rushed; under 80% of the slot the tool slows the voice a little instead.
3. **Fix recognition mistakes before translating.** Names and terms get misheard (a service called "Klap" was
   heard as "Clip" and "Клэп"; "экспорт" as "эксперт"). Read the whole `source.srt`, correct it, and tell the user
   which guesses you made.
4. **Phrases are placed on their original start second, never glued one after another.** The tool already does
   this; do not post-process the audio in a way that shifts phrases.
5. **You cannot hear the audio.** Verify with `check` and the numbers, and say plainly that nobody has listened.
   Ask the user (ideally a native speaker) to listen. Never write that it "sounds natural".
6. Ask before installing anything. Never overwrite or delete the user's files; work in a separate folder
   (for example `work/` next to the video).

## 1. Check the setup (once)

```bash
python3 --version                       # 3.10 or newer
ffmpeg -version | head -1               # 4.4 or newer
python3 -c "import numpy, soundfile, edge_tts, faster_whisper, pyrubberband; print('ok')"
audio-separator --version               # only for the separation step
rubberband --version                    # optional: nicer speed-ups (otherwise ffmpeg atempo is used)
```

Missing pieces:

- Python packages: `pip3 install -r requirements.txt`
- `ffmpeg`: `brew install ffmpeg` (macOS) / `winget install ffmpeg` (Windows) / `apt install ffmpeg` (Linux)
- `rubberband` (optional): `brew install rubberband` / `apt install rubberband-cli`
- the separator (large, install on its own): `pip3 install "audio-separator[cpu]"`. If `import scipy` crashes on
  macOS (a `dlopen` error), `pip3 install scipy==1.14.1` fixes it.

The first `transcribe` downloads a ~1.5 GB speech model; the first `separate` downloads a ~70 MB model.
Tell the user, and make sure there are several GB of free disk space.

## 2. Separate the voice from the music

```bash
python3 scripts/dub.py separate "/path/video.mp4" -o work
```

Gives `work/vocals.wav` and `work/music.wav`. If the video has no music, skip this step and mix with silence
(use `--music` with any silent file) - or simply build and deliver `dub_only.wav`.

## 3. Transcribe

```bash
python3 scripts/dub.py transcribe work/vocals.wav --lang ru -o work/source.srt
```

`--lang` is the language of the ORIGINAL speech. Read `work/source.srt` completely and correct mistakes in
place (keep the timings). Phrases come out about 2-7 seconds long.

## 4. Translate

```bash
python3 scripts/dub.py budget work/source.srt --lang fr
```

It prints, for every phrase, the seconds it has and the maximum number of characters (about 15 chars/s for
French and English, 14 Spanish, 13 Hindi and German, 5 characters/s for Chinese). Write the translation
yourself as a JSON list of strings in the same order, one per phrase, e.g. `work/lines.fr.json`:

```json
["First phrase translated.", "Second phrase translated.", "..."]
```

Translate the meaning, not word by word; shorten freely (drop fillers, repeated words); keep brand names as
they are written. For Hindi, write foreign brand names in Devanagari so the voice pronounces them. Digits
stay digits. Keep a consistent form of address ("vous", "ustedes", ...). Phrases may be re-split across
neighbouring lines when a sentence crosses a boundary.

```bash
python3 scripts/dub.py srt work/source.srt work/lines.fr.json -o work/fr.srt
```

`fr.srt` is also the subtitle file for the same language (original timings) - give it to the user.

## 5. Build, mix, check

Pick a voice: `edge-tts --list-voices | grep fr-` (examples: `fr-FR-HenriNeural`, `es-MX-JorgeNeural`,
`hi-IN-MadhurNeural`, `zh-CN-YunxiNeural`, `en-US-GuyNeural`, `de-DE-ConradNeural`).

```bash
python3 scripts/dub.py build work/fr.srt --ref "/path/video.mp4" --voice fr-FR-HenriNeural -o work/fr --loudness-ref work/vocals.wav
```

Read the summary. If it lists phrases that need more than 1.25x or that spill into the next one, shorten those
lines in `lines.fr.json`, rebuild `fr.srt` and run `build` again: only the changed phrases are spoken again.

```bash
python3 scripts/dub.py mix work/fr/dub_only.wav --music work/music.wav --ref "/path/video.mp4" -o work/fr/final
python3 scripts/dub.py check work/fr/dub_only.wav work/fr.srt --lang fr --ref "/path/video.mp4"
```

`mix` writes `final.wav` and `final.m4a` and prints their length next to the video's; they must agree to
within a few hundredths of a second. `check` re-reads the dub with a speech recognizer (error rate: a few % to
~15% is normal; digits, brand names and spelling variants count as errors; Chinese is scored per character).

Repeat steps 4-5 for every language; steps 2-3 are done once.

## 6. Report to the user

Tell the user, per language: the files (`final.m4a`, `<lang>.srt`), the voice used, the length against the
video, the error rate, how many phrases were sped up and by how much at most. Say what you could not
check: the sound itself, and the translation if you cannot judge the language well. List the guesses you made
when fixing recognition mistakes.

Uploading to YouTube is up to the user: YouTube Studio -> Subtitles -> add the language -> add the audio
track (and the `.srt` as that language's subtitles). Do not upload anything on the user's behalf.

## If something goes wrong

- `TTS failed for phrase N` / `retry`: the network dropped; run `build` again, finished phrases are kept.
- A phrase is silent: its text was empty; check the line in the JSON.
- The voice sounds rushed: the line is over its budget; shorten it.
- YouTube says the lengths differ: run `ffprobe` on the video and on `final.m4a` and compare; rebuild with
  the VIDEO as `--ref`. The `.m4a` length is rounded to the AAC frame (about 0.02 s).
- Windows/Linux: not tested by the author; the code is cross-platform, but report problems honestly.
