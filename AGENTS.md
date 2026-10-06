# YouTube Audio Translation - notes for AI coding agents

This repository dubs a video's speech into other languages (keeping the music and the exact timing) so
the result can be uploaded to YouTube as an extra audio track. It is meant to be driven by an AI coding
agent (Claude Code, Codex) and also works as a plain command-line tool.

## If the user wants a video dubbed / translated into another language

Read `skills/youtube-audio-translation/SKILL.md` and follow it step by step. The engine is one file,
`skills/youtube-audio-translation/scripts/dub.py`. The short version:

```bash
python3 skills/youtube-audio-translation/scripts/dub.py separate video.mp4 -o work          # work/vocals.wav, work/music.wav
python3 skills/youtube-audio-translation/scripts/dub.py transcribe work/vocals.wav --lang ru -o work/source.srt
# read work/source.srt, fix recognition mistakes, then translate it yourself (see `budget`)
python3 skills/youtube-audio-translation/scripts/dub.py budget work/source.srt --lang fr
python3 skills/youtube-audio-translation/scripts/dub.py srt work/source.srt work/lines.fr.json -o work/fr.srt
python3 skills/youtube-audio-translation/scripts/dub.py build work/fr.srt --ref video.mp4 --voice fr-FR-HenriNeural -o work/fr --loudness-ref work/vocals.wav
python3 skills/youtube-audio-translation/scripts/dub.py mix work/fr/dub_only.wav --music work/music.wav --ref video.mp4 -o work/fr/final
python3 skills/youtube-audio-translation/scripts/dub.py check work/fr/dub_only.wav work/fr.srt --lang fr --ref video.mp4
```

Rules that matter (each one was learned the hard way - see the skill):

- The length of the result must equal the length of the **video file**, not of a raw `.aac`.
- You translate the text yourself, phrase by phrase, within the character budget. Do not use an
  automatic translator: it ignores the time each phrase has.
- You cannot hear the audio. Verify with numbers (`check`) and say so; do not claim it sounds good.
- Ask before installing anything. Never overwrite or delete the user's files; write to a separate folder.

## If you are changing the code

- Keep the engine a single file, `skills/youtube-audio-translation/scripts/dub.py`. Heavy packages
  (`edge_tts`, `faster_whisper`, `pyrubberband`, `soundfile` in some paths) are imported lazily so the
  tests need only `numpy`, `soundfile` and `pytest`.
- Tests: `pip install numpy soundfile pytest && pytest -q`. They cover the timing logic and need no
  network, models or media.
- Do not commit videos, audio or build output (`.gitignore` blocks them).
