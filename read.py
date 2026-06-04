#!/usr/bin/env python3
"""
read — a local, offline text-to-speech reader for the Mac.

Reads web articles, PDF sections, clipboard text, or literal strings aloud
using Kokoro TTS (mlx-community/Kokoro-82M-bf16) via MLX-Audio on Apple Silicon.

Everything runs locally; after the first model download it works fully offline.
"""

import argparse
import contextlib
import logging
import os
import queue
import re
import signal
import subprocess
import sys
import tempfile
import threading
import wave

# Quiet the noisy third-party startup chatter before anything imports them.
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")

# espeak's phonemizer emits frequent harmless "words count mismatch" warnings
# on the espeak-ng fallback path; keep the console clean.
logging.getLogger("phonemizer").setLevel(logging.ERROR)

MODEL_ID = "mlx-community/Kokoro-82M-bf16"
DEFAULT_VOICE = "af_bella"
MAX_CHARS = 500          # target characters per synthesis chunk
WORDS_PER_SEC = 2.5      # rough Kokoro speaking rate at speed 1.0, for estimates

# Language code is derived from the first letter of the voice name.
#   a=American English  b=British English  e=Spanish  f=French  h=Hindi
#   i=Italian  j=Japanese  p=Portuguese  z=Mandarin Chinese
# This reader is tuned for English (a/b); other languages need extra G2P deps.


def err(msg):
    """Print a clean one-line error and exit (no stack trace)."""
    print(f"reader: {msg}", file=sys.stderr)
    sys.exit(1)


# --------------------------------------------------------------------------- #
# Text sources
# --------------------------------------------------------------------------- #
def text_from_url(url):
    try:
        import trafilatura
    except ImportError:
        err("trafilatura is not installed (run: uv sync)")
    downloaded = trafilatura.fetch_url(url)
    if not downloaded:
        err(f"could not fetch URL: {url}")
    text = trafilatura.extract(
        downloaded, include_comments=False, include_tables=False
    )
    if not text or not text.strip():
        err(f"no readable article text found at: {url}")
    return text


def parse_pages(spec):
    """'3-7' -> (3, 7); '5' -> (5, 5). 1-indexed, inclusive."""
    spec = spec.strip()
    m = re.fullmatch(r"(\d+)(?:-(\d+))?", spec)
    if not m:
        err(f"invalid --pages value: {spec!r} (use e.g. 3-7 or 5)")
    start = int(m.group(1))
    end = int(m.group(2)) if m.group(2) else start
    if start < 1 or end < start:
        err(f"invalid page range: {spec!r}")
    return start, end


def text_from_pdf(path, pages=None):
    try:
        from pypdf import PdfReader
    except ImportError:
        err("pypdf is not installed (run: uv sync)")
    try:
        reader = PdfReader(path)
    except Exception as e:
        err(f"could not open PDF {path}: {e}")
    total = len(reader.pages)
    if pages:
        start, end = pages
        if start > total:
            err(f"--pages {start}-{end} but PDF has only {total} pages")
        end = min(end, total)
        selected = range(start - 1, end)
    else:
        selected = range(total)
    parts = []
    for i in selected:
        try:
            parts.append(reader.pages[i].extract_text() or "")
        except Exception:
            parts.append("")
    text = "\n\n".join(p.strip() for p in parts if p.strip())
    if not text.strip():
        err(f"no extractable text in PDF (it may be scanned images): {path}")
    return text


def text_from_clipboard():
    try:
        out = subprocess.run(["pbpaste"], capture_output=True, text=True, check=True)
    except Exception as e:
        err(f"could not read clipboard via pbpaste: {e}")
    if not out.stdout.strip():
        err("clipboard is empty")
    return out.stdout


# --------------------------------------------------------------------------- #
# Chunking
# --------------------------------------------------------------------------- #
_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[\"'(\[]?[A-Z0-9])")


def split_sentences(paragraph):
    return [s for s in _SENT_SPLIT.split(paragraph) if s.strip()]


def hard_wrap(sentence, limit):
    """Break an over-long sentence on commas/clauses, then on whitespace."""
    if len(sentence) <= limit:
        return [sentence]
    pieces, buf = [], ""
    for part in re.split(r"(?<=[,;:])\s+", sentence):
        if len(buf) + len(part) + 1 <= limit:
            buf = f"{buf} {part}".strip()
        else:
            if buf:
                pieces.append(buf)
            if len(part) <= limit:
                buf = part
            else:  # still too long: split on whitespace
                words, line = part.split(), ""
                for w in words:
                    if len(line) + len(w) + 1 <= limit:
                        line = f"{line} {w}".strip()
                    else:
                        pieces.append(line)
                        line = w
                buf = line
    if buf:
        pieces.append(buf)
    return pieces


def chunk_text(text, limit=MAX_CHARS):
    """Split text into synthesis chunks, never breaking mid-sentence."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    paragraphs = re.split(r"\n\s*\n+", text)
    chunks, buf = [], ""
    for para in paragraphs:
        para = " ".join(para.split())
        if not para:
            continue
        for sent in split_sentences(para):
            for piece in hard_wrap(sent, limit):
                if not buf:
                    buf = piece
                elif len(buf) + len(piece) + 1 <= limit:
                    buf = f"{buf} {piece}"
                else:
                    chunks.append(buf)
                    buf = piece
        # paragraph boundary: flush so we don't run sentences together
        if buf:
            chunks.append(buf)
            buf = ""
    if buf:
        chunks.append(buf)
    return chunks


# --------------------------------------------------------------------------- #
# Synthesis & audio
# --------------------------------------------------------------------------- #
def load_model_quiet():
    print("Loading Kokoro model (first run downloads ~330 MB, then it's offline)…",
          flush=True)
    try:
        with open(os.devnull, "w") as devnull, contextlib.redirect_stdout(devnull):
            from mlx_audio.tts.utils import load_model
            model = load_model(MODEL_ID)
    except Exception as e:
        err(f"could not load model {MODEL_ID}: {e}")
    return model


def synth(model, text, voice, speed, lang_code):
    """Return (mono float32 numpy audio, sample_rate) for one chunk."""
    import numpy as np
    results = list(model.generate(text=text, voice=voice, speed=speed,
                                  lang_code=lang_code))
    if not results:
        return None, None
    audio = np.concatenate([np.asarray(r.audio).reshape(-1) for r in results])
    return audio, results[0].sample_rate


def write_wav(path, audio, sr):
    import numpy as np
    pcm = (np.clip(audio, -1.0, 1.0) * 32767.0).astype("<i2")
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())


# --------------------------------------------------------------------------- #
# Playback (streaming) with clean Ctrl-C handling
# --------------------------------------------------------------------------- #
class Player:
    """Synthesize chunks in a worker thread, play them in order via afplay.

    A single Ctrl-C sets a stop flag, kills the current afplay child, and
    tears everything down so no orphaned afplay/python processes survive.
    """

    def __init__(self, model, chunks, voice, speed, lang_code):
        self.model = model
        self.chunks = chunks
        self.voice = voice
        self.speed = speed
        self.lang_code = lang_code
        self.stop = threading.Event()
        self.q = queue.Queue(maxsize=2)
        self.proc = None
        self.proc_lock = threading.Lock()
        self.tmpdir = tempfile.mkdtemp(prefix="kokoro-reader-")

    def _producer(self):
        for i, chunk in enumerate(self.chunks):
            if self.stop.is_set():
                break
            audio, sr = synth(self.model, chunk, self.voice, self.speed,
                              self.lang_code)
            if self.stop.is_set() or audio is None:
                break
            wav = os.path.join(self.tmpdir, f"chunk_{i:05d}.wav")
            write_wav(wav, audio, sr)
            # put with timeout so we re-check stop instead of blocking forever
            while not self.stop.is_set():
                try:
                    self.q.put((i, wav), timeout=0.3)
                    break
                except queue.Full:
                    continue
        self.q.put(None)  # sentinel: no more chunks

    def _kill_current(self):
        with self.proc_lock:
            if self.proc and self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
            self.proc = None

    def _play(self, wav):
        with self.proc_lock:
            if self.stop.is_set():
                return
            self.proc = subprocess.Popen(["afplay", wav],
                                         stdout=subprocess.DEVNULL,
                                         stderr=subprocess.DEVNULL)
        self.proc.wait()

    def run(self):
        worker = threading.Thread(target=self._producer, daemon=True)
        worker.start()
        n = len(self.chunks)
        try:
            while True:
                item = self.q.get()
                if item is None:
                    break
                i, wav = item
                print(f"\r▶ chunk {i + 1}/{n}", end="", flush=True)
                self._play(wav)
                with contextlib.suppress(OSError):
                    os.remove(wav)
            print("\rDone." + " " * 20)
        except KeyboardInterrupt:
            print("\nStopping…")
        finally:
            self.stop.set()
            self._kill_current()
            self._drain_and_cleanup(worker)

    def _drain_and_cleanup(self, worker):
        # free queue slots so a blocked producer can notice stop and exit
        with contextlib.suppress(queue.Empty):
            while True:
                self.q.get_nowait()
        worker.join(timeout=2)
        import shutil
        with contextlib.suppress(Exception):
            shutil.rmtree(self.tmpdir)


def save_to_file(model, chunks, voice, speed, lang_code, out_path):
    import numpy as np
    n = len(chunks)
    pieces, sr = [], None
    try:
        for i, chunk in enumerate(chunks):
            print(f"\r♪ synthesizing chunk {i + 1}/{n}", end="", flush=True)
            audio, csr = synth(model, chunk, voice, speed, lang_code)
            if audio is not None:
                pieces.append(audio)
                sr = csr
    except KeyboardInterrupt:
        print("\nStopping…")
        sys.exit(130)
    print()
    if not pieces:
        err("nothing was synthesized")
    write_wav(out_path, np.concatenate(pieces), sr)
    print(f"Saved {out_path}")


# --------------------------------------------------------------------------- #
# Voices
# --------------------------------------------------------------------------- #
def list_voices():
    import glob
    base = os.path.expanduser(
        "~/.cache/huggingface/hub/"
        "models--mlx-community--Kokoro-82M-bf16/snapshots/*/voices/*"
    )
    found = sorted({os.path.splitext(os.path.basename(p))[0]
                    for p in glob.glob(base)})
    if not found:
        # Fallback to the known v1.0 voice list if the model isn't downloaded yet.
        found = [
            "af_alloy", "af_aoede", "af_bella", "af_heart", "af_jessica",
            "af_kore", "af_nicole", "af_nova", "af_river", "af_sarah", "af_sky",
            "am_adam", "am_echo", "am_eric", "am_fenrir", "am_liam",
            "am_michael", "am_onyx", "am_puck", "am_santa",
            "bf_alice", "bf_emma", "bf_isabella", "bf_lily",
            "bm_daniel", "bm_fable", "bm_george", "bm_lewis",
        ]
    print("Available voices (prefix: a=US, b=UK female=f male=m):\n")
    groups = {"af": "US female", "am": "US male",
              "bf": "UK female", "bm": "UK male"}
    for pfx, label in groups.items():
        names = [v for v in found if v.startswith(pfx)]
        if names:
            print(f"  {label:9} {' '.join(names)}")
    other = [v for v in found if v[:2] not in groups]
    if other:
        print(f"  other     {' '.join(other)}")
    print(f"\nDefault: {DEFAULT_VOICE}   Recommended alt: af_heart")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser():
    p = argparse.ArgumentParser(
        prog="reader",
        description="Local offline TTS reader (Kokoro via MLX-Audio).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  reader https://example.com/article\n"
            "  reader paper.pdf --pages 3-7\n"
            "  reader --text \"hello world\" --voice af_heart\n"
            "  reader --clipboard --speed 1.2\n"
            "  reader report.pdf --save out.wav\n"
            "  reader --list-voices\n"
        ),
    )
    p.add_argument("source", nargs="?", help="a URL or a .pdf file path")
    p.add_argument("--text", help="read this literal string")
    p.add_argument("--clipboard", action="store_true",
                   help="read clipboard contents (pbpaste)")
    p.add_argument("--voice", default=DEFAULT_VOICE,
                   help=f"voice name (default: {DEFAULT_VOICE})")
    p.add_argument("--speed", type=float, default=1.0,
                   help="speech speed (default: 1.0)")
    p.add_argument("--pages", help="PDF page range, e.g. 3-7 or 5")
    p.add_argument("--save", metavar="FILE.wav",
                   help="save audio to a WAV file instead of playing")
    p.add_argument("--list-voices", action="store_true",
                   help="list available voices and exit")
    return p


def resolve_text(args):
    sources = sum(bool(x) for x in (args.source, args.text, args.clipboard))
    if sources == 0:
        err("nothing to read — give a URL/PDF, --text, or --clipboard "
            "(see: reader --help)")
    if sources > 1:
        err("choose only one of: URL/PDF, --text, --clipboard")

    if args.text:
        if args.pages:
            err("--pages only applies to PDF files")
        return args.text
    if args.clipboard:
        if args.pages:
            err("--pages only applies to PDF files")
        return text_from_clipboard()

    src = args.source
    if src.startswith(("http://", "https://")):
        if args.pages:
            err("--pages only applies to PDF files")
        return text_from_url(src)
    if src.lower().endswith(".pdf") or os.path.isfile(src):
        if not os.path.isfile(src):
            err(f"file not found: {src}")
        if not src.lower().endswith(".pdf"):
            err(f"only .pdf files are supported (got: {src})")
        pages = parse_pages(args.pages) if args.pages else None
        return text_from_pdf(src, pages)
    err(f"don't know how to read {src!r} — expected a URL or a .pdf path")


def main():
    args = build_parser().parse_args()

    if args.list_voices:
        list_voices()
        return

    if args.speed <= 0:
        err("--speed must be greater than 0")

    lang_code = args.voice[0] if args.voice else "a"

    text = resolve_text(args)
    chunks = chunk_text(text)
    if not chunks:
        err("no readable text after extraction")

    words = len(text.split())
    est_sec = words / WORDS_PER_SEC / args.speed
    mins, secs = divmod(int(est_sec), 60)
    print(f"Text: {len(text):,} chars · {words:,} words · "
          f"{len(chunks)} chunks · ~{mins}m{secs:02d}s of audio "
          f"(voice {args.voice}, speed {args.speed})")

    model = load_model_quiet()

    # Warm up once: triggers (and silences) pipeline creation and compiles
    # Metal kernels so the first real chunk starts almost immediately.
    with open(os.devnull, "w") as devnull, contextlib.redirect_stdout(devnull):
        with contextlib.suppress(Exception):
            list(model.generate(text="ready", voice=args.voice, speed=args.speed,
                                lang_code=lang_code))

    if args.save:
        save_to_file(model, chunks, args.voice, args.speed, lang_code, args.save)
    else:
        Player(model, chunks, args.voice, args.speed, lang_code).run()


if __name__ == "__main__":
    # Default SIGINT -> KeyboardInterrupt is what we rely on; keep it explicit.
    signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopping…")
        sys.exit(130)
