# kokoro-reader

> Listen to web articles and PDFs read aloud by a local neural voice — no cloud, no subscriptions, no data leaving your Mac.

![Platform](https://img.shields.io/badge/platform-macOS%20(Apple%20Silicon)-black)
![Python](https://img.shields.io/badge/python-3.13-blue)
![License](https://img.shields.io/badge/license-MIT-green)
![Offline](https://img.shields.io/badge/runs-100%25%20offline-success)

A local, **offline** text-to-speech reader for the Mac (Apple Silicon). It reads
web articles, PDF sections, clipboard text, or literal strings aloud using
**Kokoro TTS** (`mlx-community/Kokoro-82M-bf16`, Kokoro v1.0) via
**MLX-Audio**, Metal-accelerated on the M-series GPU.

No subscriptions, no cloud. After the first model download (~330 MB) it runs
fully offline.

```sh
reader https://example.com/some-long-article      # fetch, de-clutter, and read it aloud
reader paper.pdf --pages 12-18                     # read just a section
reader --clipboard --speed 1.2                     # read whatever you copied
```

The command is **`reader`** (not `read` — `read` is a zsh built-in and can't be
shadowed by a PATH executable).

The command is **`reader`** (not `read` — `read` is a zsh built-in and can't be
shadowed by a PATH executable).

---

## What it does

- `reader <url>` — fetch a page, strip nav/ads/boilerplate to the article text, and read it
- `reader <file.pdf>` — extract and read a PDF; `--pages 3-7` for a page range
- `reader --text "…"` — read a literal string
- `reader --clipboard` — read whatever is on the clipboard (`pbpaste`)
- `--voice <name>` — pick a voice (default `af_bella`); see `reader --list-voices`
- `--speed <n>` — speech speed (default `1.0`)
- `--save out.wav` — save audio to a file instead of playing

It **streams**: text is split into sentence/paragraph chunks and synthesized +
played chunk by chunk, so audio starts within a second or two of the model
loading instead of after the whole article. Progress shows as `▶ chunk 3/40`,
and the estimated length/chunk count is printed up front.

---

## Install (from scratch)

Requires [`uv`](https://docs.astral.sh/uv/). Everything else is handled by `uv`.

```sh
# 1. Get the code into ~/kokoro-reader (already there if Claude set it up).
cd ~/kokoro-reader

# 2. Create the environment and install dependencies (reproducible).
uv python install 3.13
uv venv --python 3.13
uv sync

# 3. Put the `reader` command on your PATH.
mkdir -p ~/.local/bin
cat > ~/.local/bin/reader <<'EOF'
#!/bin/sh
exec "$HOME/kokoro-reader/.venv/bin/python" "$HOME/kokoro-reader/read.py" "$@"
EOF
chmod +x ~/.local/bin/reader
```

Make sure `~/.local/bin` is on your `PATH` (it already is on this machine). If not,
add to `~/.zshrc`:

```sh
export PATH="$HOME/.local/bin:$PATH"
```

The whole environment is reproducible from `pyproject.toml` + `uv.lock` with a
single `uv sync`.

> **Why Python 3.13 and not 3.14?** Kokoro's English G2P (`misaki` → `spacy`)
> has no 3.14 wheels yet (`spacy` ships `cp313` only). 3.13 is the newest
> version where the whole stack installs from prebuilt wheels. `requires-python`
> is pinned to `>=3.13,<3.14` so `uv sync` won't drift onto 3.14 and break.

> **Kept deliberately lean.** The obvious install, `misaki[en]`, pulls in
> **PyTorch (~2 GB)** and a transformer stack that Kokoro's default G2P never
> touches. Instead this project installs only the pieces it actually uses
> (`misaki`, `spacy`, `num2words`, plus `espeakng-loader` + `phonemizer-fork`
> for the espeak-ng fallback that pronounces out-of-dictionary proper nouns),
> and pins the small spaCy English model directly. No Torch, much smaller env.

### First run

The first time you read anything, it downloads:
- the Kokoro model (~330 MB) from Hugging Face, and
- the small spaCy English model (already pinned as a dependency).

Both are cached. After that, it works **offline**.

---

## Examples

```sh
reader https://claude.com/blog/how-anthropic-enables-self-service-data-analytics-with-claude
reader paper.pdf --pages 3-7
reader --text "The quick brown fox." --voice af_heart
reader --clipboard --speed 1.2
reader longread.pdf --save longread.wav     # save instead of play
reader --list-voices
```

---

## Model & voices

**Model:** `mlx-community/Kokoro-82M-bf16` — Kokoro v1.0, a top-ranked
open-weight TTS model with excellent quality-per-compute and reliable
pronunciation on clean English text.

**Default voice:** `af_bella` — chosen for long-form listening comfort and
stable pronunciation over hour-plus articles without artifacts.

**Recommended alternative:** `af_heart` — extremely clean; the model's default
voice. Worth A/B testing on your own text:

```sh
reader --text "your sample paragraph" --voice af_bella
reader --text "your sample paragraph" --voice af_heart
```

`reader --list-voices` shows everything available (US/UK male & female, plus
other-language voices). Voice prefixes: `af`/`am` = US female/male,
`bf`/`bm` = UK female/male. The first letter sets the language (a=US English,
b=UK English), so picking a `bf_`/`bm_` voice reads in British English
automatically.

### Pronunciation note

Kokoro is tuned for clean, well-punctuated English (American/British) in a
neutral/informational tone — ideal for blog posts, docs, and PDFs. Pronunciation
of unusual proper nouns or acronyms can occasionally slip (it falls back to
espeak-ng phonemes for out-of-dictionary words). That's a known limitation of
the model, not a bug in this tool.

---

## Stopping it / checking nothing is left running

A **single Ctrl-C** immediately stops synthesis *and* playback, kills the child
audio process, and exits cleanly — no orphaned `afplay` or `python` processes.

To double-check nothing is left running:

```sh
pgrep -fl "mlx_audio|afplay|read.py"     # list anything still alive (should be empty)
```

If you ever need to force-kill (e.g. after a terminal crash):

```sh
pkill -f afplay
pkill -f read.py
```

---

## How it stays kill-safe

- Chunks are synthesized in a background worker thread and played in order via
  `afplay` subprocesses.
- The currently playing `afplay` PID is tracked; on Ctrl-C it is `terminate()`d
  (then `kill()`ed if needed).
- The worker thread is a daemon and watches a stop flag, so it can't outlive the
  main process or keep the GPU busy after you quit.
- Temporary WAV files are written under a per-run temp dir that is removed on
  exit.

---

## Files

- `read.py` — the whole tool (one readable script)
- `pyproject.toml` / `uv.lock` — reproducible environment
- `~/.local/bin/reader` — the command wrapper on your PATH

---

## License

[MIT](LICENSE).

This project uses but does not bundle: the Kokoro v1.0 model (Apache-2.0),
MLX-Audio, misaki, spaCy, trafilatura, and pypdf — each under its own license.
