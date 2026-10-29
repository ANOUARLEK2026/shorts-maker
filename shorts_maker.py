# pyright: basic, reportMissingImports=false, reportOptionalMemberAccess=false, reportArgumentType=false, reportCallIssue=false, reportOperatorIssue=false, reportGeneralTypeIssues=false, reportIndexIssue=false
# type: ignore
"""
Shorts Maker 2.0 - long-form video -> polished vertical shorts.

Pipeline : yt-dlp (download, cached) -> faster-whisper (word-level transcript, cached)
           -> clip planner (fixed / smart sentences / manual) -> live preview
           -> ffmpeg (reframe, burn animated subtitles, title, watermark, progress bar,
              jump-cuts, loudness, hardware encoding).

Run:  python shorts_maker.py
Needs: pip install customtkinter yt-dlp faster-whisper imageio-ffmpeg deno
"""
import os, re, sys, json, math, time, queue, bisect, hashlib, shutil, tempfile, threading, subprocess

try:
    import customtkinter as ctk
    import tkinter as tk
    import tkinter.font as tkfont
    from tkinter import colorchooser, filedialog
except Exception:  # lets the engine be imported / tested without a GUI
    ctk = tk = tkfont = colorchooser = filedialog = None

APP_DIR = os.path.join(os.path.expanduser("~"), "ShortsMaker")
CACHE_DIR = os.path.join(APP_DIR, "cache")
CONFIG_PATH = os.path.join(APP_DIR, "settings.json")

FORMATS = {"9:16": (1080, 1920), "1:1": (1080, 1080), "4:5": (1080, 1350), "16:9": (1920, 1080)}
RES = {"720p": 2 / 3, "1080p": 1.0, "1440p": 4 / 3}
FRAMING = ["Blur fit", "Crop fill", "Fit + bars"]
CUT_MODES = ["Fixed length", "Smart sentences", "Manual ranges"]
HL_STYLES = ["Color", "Color + pop", "Block", "Karaoke sweep", "None"]
ANIMS = ["None", "Pop", "Fade"]
MARK_POS = {"Top left": 7, "Top right": 9, "Bottom left": 1, "Bottom right": 3}
MODELS = ["tiny", "base", "small", "medium", "large-v3", "turbo"]
STEP_NAMES = ["Source", "Review", "Export"]
TL_H = 50  # height of the speech timeline under the preview

SENT_END = re.compile(r"[.!?\u061F\u2026]+[\"')\]]*$")
PUNCT_STRIP = re.compile(r"[.,!?;:\u2026\u060C\u061B\u061F\"\u201C\u201D\u00AB\u00BB()\[\]]+")


class Cancelled(Exception):
    pass


# ----------------------------------------------------------------- helpers
def _kw():
    return {"creationflags": 0x08000000} if sys.platform.startswith("win") else {}


def _run(cmd, **k):
    return subprocess.run(cmd, stdin=subprocess.DEVNULL, **_kw(), **k)


def ass_c(hex_color, alpha=0):
    """ASS style colour &HAABBGGRR."""
    h = hex_color.lstrip("#")
    return f"&H{int(alpha) & 255:02X}{h[4:6]}{h[2:4]}{h[0:2]}".upper()


def ass_tag_c(hex_color):
    """ASS override-tag colour &HBBGGRR&."""
    h = hex_color.lstrip("#")
    return f"&H{h[4:6]}{h[2:4]}{h[0:2]}&".upper()


def T(*tags):
    return "{" + "".join(tags) + "}"


def ass_time(t):
    cs = max(0, int(round(t * 100)))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def srt_time(t):
    ms = max(0, int(round(t * 1000)))
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


def parse_ts(s):
    sec = 0.0
    for p in s.strip().split(":"):
        sec = sec * 60 + float(p)
    return sec


def fmt_ts(t):
    t = max(0.0, t)
    h, m, s = int(t // 3600), int(t % 3600 // 60), t % 60
    return f"{h}:{m:02d}:{s:04.1f}" if h else f"{m}:{s:04.1f}"


def contrast(hex_color):
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    return "#000000" if (0.299 * r + 0.587 * g + 0.114 * b) > 150 else "#FFFFFF"


def clean_name(s, n=60):
    return re.sub(r'[\\/:*?"<>|\r\n\t]+', "", s).strip()[:n].strip(" .")


def out_size(fmt, res):
    W, H = FORMATS[fmt]
    k = RES[res]
    return int(round(W * k / 2)) * 2, int(round(H * k / 2)) * 2


def fit(W, H, mw, mh):
    k = min(mw / W, mh / H)
    return max(2, int(W * k) // 2 * 2), max(2, int(H * k) // 2 * 2)


def find_ffmpeg():
    """System ffmpeg if it has libass, else the copy bundled by imageio-ffmpeg."""
    cands = []
    if shutil.which("ffmpeg"):
        cands.append(shutil.which("ffmpeg"))
    try:
        import imageio_ffmpeg
        cands.append(imageio_ffmpeg.get_ffmpeg_exe())
    except Exception:
        pass
    for c in cands:
        try:
            out = _run([c, "-hide_banner", "-filters"], capture_output=True, text=True).stdout
            if re.search(r"\bass\b", out):
                return c
        except Exception:
            pass
    return None


FFMPEG = find_ffmpeg()


def detect_encoders():
    found = {"CPU (x264)": "libx264"}
    try:
        out = _run([FFMPEG, "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
        for label, name in (("NVIDIA NVENC", "h264_nvenc"), ("Intel QuickSync", "h264_qsv"),
                            ("AMD AMF", "h264_amf")):
            if name in out:
                found[label] = name
    except Exception:
        pass
    return found


def probe(path):
    err = _run([FFMPEG, "-hide_banner", "-i", path], capture_output=True, text=True,
               encoding="utf-8", errors="replace").stderr
    m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", err)
    dur = int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3]) if m else 0.0
    v = re.search(r"Video:.*?, (\d{2,5})x(\d{2,5})", err)
    return dict(duration=dur, has_audio="Audio:" in err,
                w=int(v[1]) if v else 1920, h=int(v[2]) if v else 1080)


def load_config():
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_config(d):
    try:
        os.makedirs(APP_DIR, exist_ok=True)
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(d, f, indent=1, ensure_ascii=False)
    except Exception:
        pass


def open_path(p):
    try:
        if sys.platform.startswith("win"):
            os.startfile(p)
        else:
            subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", p])
    except Exception:
        pass


# ------------------------------------------------------- download / source
def yt_id(url):
    m = re.search(r"(?:v=|youtu\.be/|/shorts/|/embed/|/live/)([A-Za-z0-9_-]{11})", url)
    return m.group(1) if m else None


def download_video(url, folder, log, prog=lambda x, t=None: None, cookies=None, cancel=None):
    import yt_dlp
    stage = {"n": 0}

    def _hook(d):
        if cancel is not None and cancel.is_set():
            raise Cancelled()
        if d["status"] == "downloading":
            tot = d.get("total_bytes") or d.get("total_bytes_estimate") or 1
            done = d["downloaded_bytes"]
            prog(min(0.2 * done / tot, 0.2),
                 f"Downloading stream {stage['n'] + 1}: {min(done / tot, 1) * 100:.0f}%  at {(d.get('speed') or 0) / 1e6:.1f} MB/s")
        elif d["status"] == "finished":
            stage["n"] += 1
            log(f"  Stream {stage['n']} downloaded ({os.path.basename(d.get('filename', ''))})")

    def _pp_hook(d):
        if d["status"] == "started":
            log(f"  Post-processing: {d['postprocessor']} (please wait)...")
            prog(0.2, "Merging video + audio...")
        elif d["status"] == "finished":
            log(f"  {d['postprocessor']} finished")

    _ansi = re.compile(r"\x1b\[[0-9;]*m")

    class _Log:
        def debug(self, m):
            m = _ansi.sub("", m)
            if m.startswith("[debug]"):
                return
            if any(k in m for k in ("Merger", "Destination", "Extracting", "format(s)")):
                log("  yt-dlp: " + m[:120])
        info = debug
        def warning(self, m): log("  yt-dlp warning: " + _ansi.sub("", m)[:150])
        def error(self, m): log("  yt-dlp error: " + _ansi.sub("", m)[:150])

    js = {}
    try:  # deno from the `deno` pip package
        import deno
        js["deno"] = {"path": deno.find_deno_bin()}
    except Exception:
        for rt in ("deno", "node", "bun"):
            if shutil.which(rt):
                js[rt] = {"path": shutil.which(rt)}
                break
    if not js:
        log("  Warning: no JavaScript runtime found. Run: pip install deno")
    opts = {
        "ffmpeg_location": FFMPEG,
        "sleep_interval_requests": 1,
        "retries": 5,
        "fragment_retries": 5,
        "socket_timeout": 30,
        "logger": _Log(),
        "postprocessor_hooks": [_pp_hook],
        **({"cookiesfrombrowser": (cookies,)} if cookies else {}),
        "js_runtimes": js or {"deno": {}},
        "remote_components": ["ejs:github"],
        "format": "bv*[height<=1080][vcodec^=avc1]+ba[ext=m4a]/bv*[height<=1080]+ba/b[height<=1080]/b",
        "concurrent_fragment_downloads": 4,
        "merge_output_format": "mp4",
        "outtmpl": os.path.join(folder, "source.%(ext)s"),
        "quiet": True, "noprogress": True, "noplaylist": True,
        "progress_hooks": [_hook],
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
    except Exception as e:
        if cookies and "cookie" in str(e).lower():
            log(f"  Could not read {cookies} cookies (close the browser completely, or pick 'No cookies'). Retrying without cookies...")
            opts.pop("cookiesfrombrowser", None)
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
        else:
            raise
    title = clean_name(info.get("title", "video"))
    return os.path.join(folder, "source.mp4"), title


def prepare_source(url, o, log, prog, cancel):
    """Returns dict(src, title, dir, local). Downloads are cached, so reloads are instant."""
    if os.path.isfile(url):
        st = os.stat(url)
        key = "local_" + hashlib.md5(f"{os.path.abspath(url)}|{st.st_size}|{int(st.st_mtime)}".encode()).hexdigest()[:12]
        d = os.path.join(CACHE_DIR, key)
        os.makedirs(d, exist_ok=True)
        title = clean_name(os.path.splitext(os.path.basename(url))[0]) or "video"
        log(f"Using local file: {os.path.basename(url)}")
        return dict(src=url, title=title, dir=d, local=True)
    vid = yt_id(url)
    key = "yt_" + vid if vid else "url_" + hashlib.md5(url.encode()).hexdigest()[:12]
    d = os.path.join(CACHE_DIR, key)
    os.makedirs(d, exist_ok=True)
    src, meta = os.path.join(d, "source.mp4"), os.path.join(d, "meta.json")
    if os.path.isfile(src) and os.path.isfile(meta):
        try:
            with open(meta, encoding="utf-8") as f:
                title = json.load(f)["title"]
            log("Using cached download (delete it in Output > Clear cache to re-download).")
            return dict(src=src, title=title, dir=d, local=False)
        except Exception:
            pass
    log("Downloading video...")
    cookies = None if o["cookies"] == "No cookies" else o["cookies"]
    src, title = download_video(url, d, log, prog, cookies, cancel)
    with open(meta, "w", encoding="utf-8") as f:
        json.dump({"title": title, "url": url}, f, ensure_ascii=False)
    return dict(src=src, title=title, dir=d, local=False)


# ------------------------------------------------------------- transcribe
def decode_audio(video):
    import numpy as np
    raw = _run([FFMPEG, "-nostdin", "-v", "error", "-i", video, "-vn", "-ac", "1", "-ar", "16000",
                "-f", "s16le", "-"], capture_output=True, check=True).stdout
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def _whisper_pass(model, audio, o, music, duration, prog, cancel, log, tag):
    lang = (o["lang"] or "").strip()
    kw = dict(language=lang or None, task="translate" if o["task"].startswith("Translate") else "transcribe",
              word_timestamps=True, beam_size=5,
              condition_on_previous_text=False)  # stops Whisper stalling after the first segments
    if music:  # singing / music: never discard segments as "non-speech"
        kw.update(vad_filter=False, no_speech_threshold=1.0, log_prob_threshold=-100.0,
                  compression_ratio_threshold=100.0)
    else:
        kw["vad_filter"] = bool(o["vad"])
        if o["vad"]:
            kw["vad_parameters"] = dict(min_silence_duration_ms=700, speech_pad_ms=300)
    segs, info = model.transcribe(audio, **kw)
    log(f"Transcribing{' (music mode)' if music else ''} - language: {info.language}...")
    words = []
    for s in segs:
        if cancel.is_set():
            raise Cancelled()
        f = min(s.end / max(duration, 1), 1)
        prog(0.2 + 0.8 * f, f"Transcribing{tag}... {f * 100:.0f}%")
        for w in s.words or []:
            words.append((round(w.start, 3), round(w.end, 3), w.word.strip()))
    return words


def coverage(words, duration):
    return (words[-1][1] / duration) if (words and duration) else 0.0


def transcribe(video, o, log, duration, prog, cancel, cache_file):
    if cache_file and os.path.isfile(cache_file):
        try:
            with open(cache_file, encoding="utf-8") as f:
                words = [tuple(w) for w in json.load(f)]
            log(f"Using cached transcript ({len(words)} words).")
            return words
        except Exception:
            pass
    from faster_whisper import WhisperModel
    audio = decode_audio(video)
    size = o["model"]
    attempts = {"auto": [("auto", "auto"), ("cpu", "int8")], "cpu": [("cpu", "int8")],
                "cuda": [("cuda", "float16"), ("cpu", "int8")]}.get(o["device"], [("auto", "auto"), ("cpu", "int8")])
    words = []
    for n, (device, ct) in enumerate(attempts):
        try:
            log(f"Loading Whisper model '{size}' on {device} (first run downloads it)...")
            model = WhisperModel(size, device=device, compute_type=ct)
            music = bool(o.get("music"))
            words = _whisper_pass(model, audio, o, music, duration, prog, cancel, log, "")
            # Whisper sometimes stops early on songs / noisy audio: retry once in music mode
            if not music and duration > 40 and coverage(words, duration) < 0.4:
                log(f"  Only found speech up to {fmt_ts(words[-1][1]) if words else '0:00'} of {fmt_ts(duration)} - "
                    "retrying in music mode (no non-speech filtering)...")
                alt = _whisper_pass(model, audio, o, True, duration, prog, cancel, log, " (retry)")
                if len(alt) > len(words):
                    words = alt
            break
        except Cancelled:
            raise
        except Exception as e:
            if n + 1 >= len(attempts):
                raise
            log(f"  {device} failed ({str(e)[:90]}); retrying on CPU...")
    log(f"  {len(words)} words transcribed, covering {coverage(words, duration) * 100:.0f}% of the video")
    if duration > 40 and coverage(words, duration) < 0.5:
        log("  Tip: few words found. Try Music mode, a bigger model (medium / large-v3) and set the Language (e.g. ar).")
    if cache_file and words:
        try:
            with open(cache_file, "w", encoding="utf-8") as f:
                json.dump(words, f, ensure_ascii=False)
        except Exception:
            pass
    return words


# ---------------------------------------------------------------- planning
def clip_label(words, a, b, n=8):
    ws = [w[2] for w in words if a - 0.05 <= w[0] < b][:n]
    return " ".join(ws) or "(no speech)"


def sentences(words, gap=0.7):
    out, cur = [], []
    for w in words:
        if cur and w[0] - cur[-1][1] > gap:
            out.append(cur)
            cur = []
        cur.append(w)
        if SENT_END.search(w[2]):
            out.append(cur)
            cur = []
    if cur:
        out.append(cur)
    return out


def _fixed_spans(words, L, t0, t1, snap):
    ends = [w[1] for w in words if SENT_END.search(w[2])]
    spans, t = [], t0
    while t < t1 - 5:
        end = min(t + L, t1)
        if snap and end < t1:
            near = [x for x in ends if end - 8 <= x <= end + 4 and x > t + 10]
            if near:
                end = min(near, key=lambda x: abs(x - end))
        spans.append((t, end))
        t = end
    if not spans and t1 - t0 >= 1:
        spans = [(t0, t1)]
    return spans


def _smart_spans(words, L, t0, t1):
    ws = [w for w in words if w[0] >= t0 and w[1] <= t1]
    if not ws:
        return _fixed_spans(words, L, t0, t1, True)
    sents = sentences(ws)
    hard = L * 1.3
    spans, i = [], 0
    while i < len(sents):
        s, e, j = sents[i][0][0], sents[i][-1][1], i + 1
        while j < len(sents) and e - s < L and sents[j][-1][1] - s <= hard:
            e = sents[j][-1][1]
            j += 1
        spans.append((max(t0, s - 0.15), min(t1, e + 0.35)))
        i = j
    out = []
    for a, b in spans:  # glue tiny leftovers onto the previous clip
        if out and b - a < 8 and b - out[-1][0] <= hard:
            out[-1] = (out[-1][0], b)
        else:
            out.append((a, b))
    final = []
    for a, b in out:  # split a runaway sentence-less stretch
        if b - a > hard * 1.5:
            final += _fixed_spans([], L, a, b, False)
        else:
            final.append((a, b))
    return [(a, b) for a, b in final if b - a >= 5] or final


def _raw_score(words, a, b):
    ws = [w for w in words if w[0] >= a and w[1] <= b + 0.3]
    if not ws:
        return 0.0
    q = sum(1 for w in ws if "?" in w[2] or "\u061F" in w[2])
    ex = sum(1 for w in ws if "!" in w[2])
    first = " ".join(w[2] for w in ws[:12])
    hook = 1.0 if ("?" in first or "\u061F" in first) else 0.0
    return len(ws) / max(b - a, 1) + 0.25 * q + 0.15 * ex + hook


def _opt_ts(s, default):
    try:
        return parse_ts(s) if s and s.strip() else default
    except ValueError:
        return default


def plan_clips(o, duration, words, log=lambda m: None):
    mode, L = o["mode"], float(o["clip_len"])
    mx = int(float(o["max_clips"]))
    t0 = max(0.0, _opt_ts(o.get("range_start"), 0.0))
    t1 = min(duration, _opt_ts(o.get("range_end"), duration)) if duration else _opt_ts(o.get("range_end"), 0.0)
    spans = []
    if mode == "Manual ranges":
        for line in (o.get("manual") or "").strip().splitlines():
            line = line.strip()
            if "-" not in line:
                continue
            a, b = line.split("-", 1)
            try:
                a, b = parse_ts(a), parse_ts(b)
            except ValueError:
                log(f"  Skipped bad range: {line}")
                continue
            if duration:
                b = min(b, duration)
            if b - a >= 1:
                spans.append((a, b))
    elif mode == "Smart sentences":
        spans = _smart_spans(words, L, t0, t1)
    else:
        spans = _fixed_spans(words, L, t0, t1, bool(o["snap"]))
    raws = [_raw_score(words, a, b) for a, b in spans]
    order = sorted(range(len(raws)), key=lambda i: raws[i])
    score = {i: (round(100 * r / (len(raws) - 1)) if len(raws) > 1 else 50) for r, i in enumerate(order)}
    clips = [dict(start=a, end=b, on=True, label=clip_label(words, a, b), score=score[i])
             for i, (a, b) in enumerate(spans)]
    if mx:
        if mode == "Smart sentences":
            clips = sorted(sorted(clips, key=lambda c: -c["score"])[:mx], key=lambda c: c["start"])
        else:
            clips = clips[:mx]
    return clips


def keep_segments(cw, dur, gap, pad=0.12, tail=0.3):
    """Jump-cut: keep only stretches with speech, drop silences longer than `gap`."""
    if len(cw) < 2:
        return None
    runs, s, e = [], cw[0][0], cw[0][1]
    for a, b, _ in cw[1:]:
        if a - e <= gap:
            e = max(e, b)
        else:
            runs.append((s, e))
            s, e = a, b
    runs.append((s, e))
    segs = []
    for s, e in runs:
        s, e = max(0.0, s - pad), min(dur, e + tail)
        if segs and s <= segs[-1][1]:
            segs[-1][1] = max(segs[-1][1], e)
        else:
            segs.append([s, e])
    if len(segs) == 1 and segs[0][0] <= 0.01 and segs[0][1] >= dur - 0.01:
        return None
    segs = [(a, b) for a, b in segs if b - a > 0.15]
    return segs or None


def remap_time(t, segs):
    acc = 0.0
    for a, b in segs:
        if t < a:
            return acc
        if t <= b:
            return acc + (t - a)
        acc += b - a
    return acc


# --------------------------------------------------------------- subtitles
def prep_words(cw, o):
    mask = {m.strip().lower() for m in (o.get("mask") or "").split(",") if m.strip()}
    out = []
    for a, b, w in cw:
        t, core = w, PUNCT_STRIP.sub("", w).lower()
        if core and core in mask:
            t = core[0] + "*" * (len(core) - 1)
        if o["strip_punct"]:
            t = PUNCT_STRIP.sub("", t)
        if o["upper"]:
            t = t.upper()
        t = t.replace("\\", "").replace("{", "(").replace("}", ")")
        if t.strip():
            out.append((a, b, t, w))
    return out


def layout(cw, o):
    n = max(1, int(float(o["wpl"])))
    groups, cur = [], []
    for w in prep_words(cw, o):
        if cur and (len(cur) >= n or w[0] - cur[-1][1] > 0.9 or SENT_END.search(cur[-1][3])):
            groups.append(cur)
            cur = []
        cur.append(w)
    if cur:
        groups.append(cur)
    res = []
    for gi, g in enumerate(groups):
        gend = g[-1][1]
        hold = gend + 0.45
        if gi + 1 < len(groups):
            hold = min(hold, max(groups[gi + 1][0][0], gend))
        res.append(dict(words=[(a, b, t) for a, b, t, _ in g], start=g[0][0], end=max(hold, g[0][0] + 0.1)))
    return res


def word_index_at(starts, t):
    """Index of the word being spoken at time t (the last word that has started), or -1 before the first."""
    return bisect.bisect_right(starts, t) - 1


SAMPLE_WORDS = [(i * 0.35, i * 0.35 + 0.3, w)
                for i, w in enumerate("THIS IS HOW YOUR SUBTITLES WILL LOOK".split())]


def sub_events(o, cw, tp=None):
    """Subtitle events (a, b, style, text, layer) on the clip's source timeline."""
    lay = layout(cw, o)
    hl = o["hl_style"]
    if o["box"] and hl == "Block":
        hl = "Color"
    sc = 1.0  # sizes are scaled in the header; \bord below is scaled by caller via o["_sc"]
    ow = float(o["outline"]) * o.get("_sc", 1.0)
    base, hlc, oc = ass_tag_c(o["color"]), ass_tag_c(o["hl_color"]), ass_tag_c(o["outline_color"])
    blk = ass_tag_c(contrast(o["hl_color"]))
    anim = o["anim"] if tp is None else "None"
    pre = {"Pop": T(r"\fscx82\fscy82", r"\t(0,110,\fscx100\fscy100)"),
           "Fade": T(r"\fad(110,0)")}.get(anim, "")

    def mark(w):
        if hl == "Color":
            return T(r"\c" + hlc) + w + T(r"\c" + base)
        if hl == "Color + pop":
            return T(r"\c" + hlc, r"\fscx112\fscy112") + w + T(r"\c" + base, r"\fscx100\fscy100")
        if hl == "Block":
            return (T(r"\c" + blk, r"\3c" + hlc, r"\bord%.1f" % (ow + 10 * o.get("_sc", 1.0)))
                    + w + T(r"\c" + base, r"\3c" + oc, r"\bord%.1f" % ow))
        return w

    if tp is not None:
        if not lay:
            return []
        g = next((g for g in lay if g["start"] <= tp < g["end"]), None) or min(lay, key=lambda g: abs(g["start"] - tp))
        words = g["words"]
        if hl == "Karaoke sweep":
            parts = [T(r"\k0" if (a + b) / 2 <= tp else r"\kf99999") + t for a, b, t in words]
            return [(0, 10, "Karaoke", " ".join(parts), 1)]
        j = max([k for k, (a, b, t) in enumerate(words) if a <= tp] or [0])
        text = " ".join(mark(t) if (k == j and hl != "None") else t for k, (a, b, t) in enumerate(words))
        return [(0, 10, "Main", text, 1)]

    ev = []
    for g in lay:
        words = g["words"]
        if hl == "Karaoke sweep":
            parts = []
            for k, (a, b, t) in enumerate(words):
                nxt = words[k + 1][0] if k + 1 < len(words) else b
                parts.append(T(r"\kf%d" % max(1, int(round((nxt - a) * 100)))) + t)
            ev.append((g["start"], g["end"], "Karaoke", pre + " ".join(parts), 1))
            continue
        for j, (a, b, t) in enumerate(words):
            stop = max(words[j + 1][0] if j + 1 < len(words) else g["end"], a + 0.05)
            text = " ".join(mark(w) if (k == j and hl != "None") else w for k, (_, _, w) in enumerate(words))
            ev.append((a, stop, "Main", (pre if j == 0 else "") + text, 1))
    return ev


def ass_header(o, W, H, font):
    sc = max(W, H) / 1920.0
    fs = max(8, int(round(float(o["fsize"]) * sc)))
    sp = round(float(o["spacing"]) * sc, 1)
    mv = int(H * (1 - float(o["pos_y"])))
    bold = -1 if o["bold"] else 0
    if o["box"]:
        bs, outc, outl = 3, ass_c(o["box_color"], (1 - float(o["box_op"])) * 255), round(max(6.0, 16 * sc), 1)
    else:
        bs, outc, outl = 1, ass_c(o["outline_color"]), round(float(o["outline"]) * sc, 1)
    shadow = round(float(o["shadow"]) * sc, 1)
    back = "&H64000000"
    txt, hlc = ass_c(o["color"]), ass_c(o["hl_color"])
    if o["title_box"]:
        tb = (3, ass_c(o["title_box_color"], 0.12 * 255), round(max(8.0, 20 * sc), 1))
    else:
        tb = (1, "&H00000000", round(max(2.0, 6 * sc), 1))
    ts = max(8, int(round(float(o["title_size"]) * sc)))
    ms = max(8, int(round(float(o["mark_size"]) * sc)))
    ma = (1 - float(o["mark_op"])) * 255
    fmt = ("Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,"
           "Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,"
           "MarginV,Encoding")

    def style(name, fn, size, p, s, oc, bk, b, bsty, ol, sh, al, ml, mr, mvv, spc=0):
        return f"Style: {name},{fn},{size},{p},{s},{oc},{bk},{b},0,0,0,100,100,{spc},0,{bsty},{ol},{sh},{al},{ml},{mr},{mvv},1"

    styles = [
        style("Main", font, fs, txt, txt, outc, back, bold, bs, outl, shadow, 2, int(70 * sc), int(70 * sc), mv, sp),
        style("Karaoke", font, fs, hlc, txt, outc, back, bold, bs, outl, shadow, 2, int(70 * sc), int(70 * sc), mv, sp),
        style("Title", font, ts, ass_c(o["title_color"]), ass_c(o["title_color"]), tb[1], back, -1, tb[0], tb[2], 0, 8,
              int(90 * sc), int(90 * sc), int(H * float(o["title_y"]))),
        style("Mark", font, ms, ass_c("#FFFFFF", ma), ass_c("#FFFFFF", ma), ass_c("#000000", ma), "&HFF000000", -1, 1,
              round(max(1.0, 3 * sc), 1), 0, MARK_POS.get(o["mark_pos"], 9), int(50 * sc), int(50 * sc), int(70 * sc)),
        style("Bar", font, 10, "&H00FFFFFF", "&H00FFFFFF", "&H00000000", "&H00000000", 0, 1, 0, 0, 7, 0, 0, 0),
    ]
    return ("[Script Info]\nScriptType: v4.00+\n" f"PlayResX: {W}\nPlayResY: {H}\nWrapStyle: 0\n"
            "ScaledBorderAndShadow: yes\n\n[V4+ Styles]\n" + fmt + "\n" + "\n".join(styles) +
            "\n\n[Events]\nFormat: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text\n")


def extra_events(o, W, H, D, label, tp=None):
    """Title, watermark and progress bar (output timeline)."""
    ev = []
    pv = tp is not None
    title = (o.get("title") or "").strip()
    if title:
        title = title.replace("{auto}", label).replace("\\n", "\\N").replace("{", "(").replace("}", ")")
        td = float(o["title_dur"])
        ev.append((0, 10 if pv else (D if td <= 0 else min(D, td)), "Title",
                   (T(r"\fad(180,250)") if not pv else "") + title, 3))
    mark = (o.get("mark") or "").strip()
    if mark:
        ev.append((0, 10 if pv else D, "Mark", mark.replace("{", "(").replace("}", ")"), 3))
    if o["bar_on"]:
        bh = max(2, int(round(float(o["bar_h"]) * W / 1080)))
        y = H - bh
        col = ass_tag_c(o["bar_color"])
        shape = f"m 0 0 l {W} 0 l {W} {bh} l 0 {bh}"
        if pv:
            x = max(2, int(W * min(max(tp / max(D, 0.1), 0.02), 1)))
            tag = T(r"\an7", r"\pos(0,%d)" % y, r"\bord0\shad0", r"\1c" + col, r"\clip(0,%d,%d,%d)" % (y, x, H), r"\p1")
            ev.append((0, 10, "Bar", tag + shape + T(r"\p0"), 0))
        else:
            tag = T(r"\an7", r"\pos(0,%d)" % y, r"\bord0\shad0", r"\1c" + col, r"\clip(0,%d,0,%d)" % (y, H),
                    r"\t(0,%d,\clip(0,%d,%d,%d))" % (int(D * 1000), y, W, H), r"\p1")
            ev.append((0, D, "Bar", tag + shape + T(r"\p0"), 0))
    return ev


def compose(o, words, clip, label, W, H, font, preview_tp=None):
    s, e = clip
    dur = e - s
    cw = [(max(0.0, a - s), min(b - s, dur), t) for a, b, t in words if b > s and a < e]
    pv = preview_tp is not None
    if pv and not cw:
        cw = list(SAMPLE_WORDS)
        preview_tp = 0.5
    segs = keep_segments(cw, dur, float(o["silence_gap"])) if (o["silence_cut"] and cw and not pv) else None
    D = sum(b - a for a, b in segs) if segs else dur
    o = dict(o, _sc=max(W, H) / 1920.0)
    shift = float(o["shift_ms"]) / 1000.0
    lines = []
    for a, b, st, text, layer in (sub_events(o, cw, preview_tp) if o["burn"] else []):
        if not pv:
            a, b = a + shift, b + shift
            if segs:
                a, b = remap_time(a, segs), remap_time(b, segs)
            a, b = max(0.0, a), min(b, D)
            if b - a < 0.03:
                continue
        lines.append(f"Dialogue: {layer},{ass_time(a)},{ass_time(b)},{st},,0,0,0,,{text}")
    for a, b, st, text, layer in extra_events(o, W, H, D, label, preview_tp if pv else None):
        lines.append(f"Dialogue: {layer},{ass_time(a)},{ass_time(b)},{st},,0,0,0,,{text}")
    ass = ass_header(o, W, H, font) + "\n".join(lines) + "\n"
    return dict(ass=ass, segs=segs, D=D, cw=cw)


def srt_text(o, cw, segs, D):
    shift = float(o["shift_ms"]) / 1000.0
    out = []
    for i, g in enumerate(layout(cw, o), 1):
        a, b = g["start"] + shift, g["end"] + shift
        if segs:
            a, b = remap_time(a, segs), remap_time(b, segs)
        out.append(f"{i}\n{srt_time(max(a, 0))} --> {srt_time(min(b, D))}\n{' '.join(t for _, _, t in g['words'])}\n")
    return "\n".join(out)


# ------------------------------------------------------------------ ffmpeg
def prepare_font(o, workdir):
    ff = (o.get("font_file") or "").strip()
    fam = (o.get("font") or "Arial").replace(",", " ").strip() or "Arial"
    if ff and os.path.isfile(ff):
        d = os.path.join(workdir, "fonts")
        os.makedirs(d, exist_ok=True)
        shutil.copy(ff, d)
        try:
            from PIL import ImageFont
            fam = ImageFont.truetype(ff, 20).getname()[0].replace(",", " ")
        except Exception:
            pass
        return fam, "fonts"
    return fam, None


def video_chain(o, W, H, cur, nxt):
    fr = o["framing"]
    if fr == "Crop fill":
        Z = max(1.0, float(o["zoom"]))
        return (f"{cur}scale={int(W * Z)}:{int(H * Z)}:force_original_aspect_ratio=increase,"
                f"crop={W}:{H}:x=(iw-ow)*{float(o['focus_x']):.3f}:y=(ih-oh)*{float(o['focus_y']):.3f}{nxt}")
    if fr == "Fit + bars":
        col = "0x" + o["bars_color"].lstrip("#")
        return (f"{cur}scale={W}:{H}:force_original_aspect_ratio=decrease,"
                f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color={col}{nxt}")
    k = 6  # blurred backdrop: blur a small copy, scale up (fast and smooth)
    w2, h2 = max(4, (W // k) // 2 * 2), max(4, (H // k) // 2 * 2)
    r = max(1, int(round(float(o["blur"]) * (W / 1080.0) / k)))
    r = max(1, min(r, min(w2, h2) // 4 - 1))
    dim = float(o["dim"])
    eq = f",eq=brightness={-dim:.2f}" if dim > 0.001 else ""
    return (f"{cur}split[s1][s2];"
            f"[s1]scale={w2}:{h2}:force_original_aspect_ratio=increase,crop={w2}:{h2},boxblur={r}:3{eq},"
            f"scale={W}:{H}:flags=bicubic[bg];"
            f"[s2]scale={W}:{H}:force_original_aspect_ratio=decrease[fg];"
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2{nxt}")


def build_graph(o, W, H, segs, has_audio, D, ass_name, fontsdir, preview=False):
    parts, cur, acur = [], "[0:v]", "[0:a]"
    if segs and not preview:
        for i, (a, b) in enumerate(segs):
            parts.append(f"[0:v]trim=start={a:.3f}:end={b:.3f},setpts=PTS-STARTPTS[v{i}]")
            if has_audio:
                parts.append(f"[0:a]atrim=start={a:.3f}:end={b:.3f},asetpts=PTS-STARTPTS[a{i}]")
        ins = "".join(f"[v{i}]" + (f"[a{i}]" if has_audio else "") for i in range(len(segs)))
        parts.append(f"{ins}concat=n={len(segs)}:v=1:a={1 if has_audio else 0}[vc]" + ("[ac]" if has_audio else ""))
        cur, acur = "[vc]", "[ac]"
    parts.append(video_chain(o, W, H, cur, "[fr]"))
    post = []
    fade = float(o["fade"])
    use_fade = (not preview) and fade > 0 and D > fade * 2.5
    if not preview and o["fps"] != "Source":
        post.append(f"fps={o['fps']}")
    if use_fade:
        post += [f"fade=t=in:st=0:d={fade:.2f}", f"fade=t=out:st={max(D - fade, 0):.2f}:d={fade:.2f}"]
    post.append(f"ass={ass_name}" + (f":fontsdir={fontsdir}" if fontsdir else ""))
    post.append("format=yuv420p")
    parts.append("[fr]" + ",".join(post) + "[v]")
    amap = None
    if has_audio and not preview:
        items = []
        if o["loudnorm"]:
            items += ["loudnorm=I=-16:TP=-1.5:LRA=11", "aresample=48000"]
        if use_fade:
            items += [f"afade=t=in:st=0:d={fade:.2f}", f"afade=t=out:st={max(D - fade, 0):.2f}:d={fade:.2f}"]
        if items:
            parts.append(acur + ",".join(items) + "[a]")
            amap = "[a]"
        else:
            amap = "[ac]" if segs else "0:a:0"
    return ";".join(parts), amap


def encoder_args(enc, crf, speed, fast=False):
    crf = str(int(float(crf)))
    if enc == "h264_nvenc":
        return ["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr", "-cq", crf, "-b:v", "0"]
    if enc == "h264_qsv":
        return ["-c:v", "h264_qsv", "-global_quality", crf, "-preset", "medium"]
    if enc == "h264_amf":
        return ["-c:v", "h264_amf", "-quality", "quality", "-rc", "cqp", "-qp_i", crf, "-qp_p", crf]
    preset = "ultrafast" if fast else {"Fast": "veryfast", "Balanced": "medium", "Best": "slow"}.get(speed, "medium")
    return ["-c:v", "libx264", "-preset", preset, "-crf", crf]


def run_ffmpeg(cmd, cwd, total, cancel, on_prog):
    p = subprocess.Popen(cmd, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True, encoding="utf-8", errors="replace", **_kw())
    tail = []
    for line in p.stdout:
        if cancel is not None and cancel.is_set():
            p.terminate()
            break
        line = line.strip()
        m = re.match(r"out_time=(\d+):(\d+):([\d.]+)", line)
        if m:
            sec = int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3])
            on_prog(min(sec / max(total, 0.1), 1.0))
        elif not re.match(r"^[a-z_0-9]+=", line) and line:
            tail = (tail + [line])[-25:]
    p.wait()
    if cancel is not None and cancel.is_set():
        raise Cancelled()
    if p.returncode:
        raise RuntimeError("ffmpeg failed:\n" + "\n".join(tail))


def render_clip(o, src, info, words, clip, label, out_path, workdir, W, H, log, on_prog, cancel, preview=False):
    s, e = clip
    if preview:
        e = min(e, s + 8.0)
        o = dict(o, loudnorm=False, fade=0.0)
    fam, fdir = prepare_font(o, workdir)
    comp = compose(o, words, (s, e), label, W, H, fam)
    ass_name = "sub_%d.ass" % (abs(hash(out_path)) % 100000)
    with open(os.path.join(workdir, ass_name), "w", encoding="utf-8-sig") as f:
        f.write(comp["ass"])
    graph, amap = build_graph(o, W, H, comp["segs"], info["has_audio"], comp["D"], ass_name, fdir)

    def cmd_for(enc):
        c = [FFMPEG, "-y", "-hide_banner", "-loglevel", "error", "-nostdin", "-progress", "pipe:1", "-nostats",
             "-ss", f"{s:.3f}", "-t", f"{e - s:.3f}", "-i", src, "-filter_complex", graph, "-map", "[v]"]
        if amap:
            c += ["-map", amap]
        c += encoder_args(enc, 30 if preview else o["crf"], o["speed"], fast=preview)
        if amap:
            c += ["-c:a", "aac", "-b:a", "128k" if preview else o["abitrate"], "-ar", "48000"]
        return c + ["-movflags", "+faststart", out_path]

    enc = "libx264" if preview else o.get("encoder_name", "libx264")
    try:
        run_ffmpeg(cmd_for(enc), workdir, comp["D"], cancel, on_prog)
    except RuntimeError as ex:
        if enc == "libx264":
            raise
        log(f"  {enc} failed, falling back to CPU x264 ({str(ex).splitlines()[-1][:80]})")
        run_ffmpeg(cmd_for("libx264"), workdir, comp["D"], cancel, on_prog)
    if not preview:
        base = os.path.splitext(out_path)[0]
        if o["save_srt"]:
            with open(base + ".srt", "w", encoding="utf-8-sig") as f:
                f.write(srt_text(o, comp["cw"], comp["segs"], comp["D"]))
        if o["save_thumb"]:
            try:
                _run([FFMPEG, "-y", "-loglevel", "error", "-ss", "0.8", "-i", out_path, "-frames:v", "1",
                      "-q:v", "2", base + ".jpg"], capture_output=True)
            except Exception:
                pass
    return comp["D"]


def extract_frame(src, t, out):
    _run([FFMPEG, "-y", "-hide_banner", "-loglevel", "error", "-ss", f"{max(t, 0):.2f}", "-i", src,
          "-frames:v", "1", "-q:v", "3", out], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, check=True)


def draw_safe(img, fmt):
    from PIL import Image, ImageDraw
    w, h = img.size
    ov = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(ov)
    if fmt == "9:16":
        red = (255, 60, 60, 70)
        d.rectangle([0, 0, w, int(h * 0.10)], fill=red)
        d.rectangle([0, int(h * 0.78), w, h], fill=red)
        d.rectangle([int(w * 0.84), int(h * 0.45), w, int(h * 0.78)], fill=red)
    else:
        m = int(min(w, h) * 0.05)
        d.rectangle([m, m, w - m, h - m], outline=(255, 255, 255, 120), width=1)
    return Image.alpha_composite(img.convert("RGBA"), ov).convert("RGB")


_FONTS = {}


def ui_font(size):
    if size not in _FONTS:
        from PIL import ImageFont
        f = None
        for name in ("segoeui.ttf", "arial.ttf", "DejaVuSans.ttf", "Helvetica.ttc"):
            try:
                f = ImageFont.truetype(name, size)
                break
            except Exception:
                pass
        if f is None:
            try:
                f = ImageFont.load_default(size=size)
            except Exception:
                f = ImageFont.load_default()
        _FONTS[size] = f
    return _FONTS[size]


def draw_guides(img, items, active=None):
    """Editing overlay (never exported): dashed guide lines with a tag, on a copy of img.
    items = [(key, y_fraction, label, (r, g, b))]; the active one is drawn stronger."""
    from PIL import Image, ImageDraw
    w, h = img.size
    ov = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(ov)
    font = ui_font(max(11, h // 46))
    th = getattr(font, "size", 12)
    for key, frac, label, rgb in items:
        y = int(min(1.0, max(0.0, frac)) * (h - 1))
        on = key == active
        wd = 2 if on else 1
        x = 0
        while x < w:
            d.line([(x, y), (min(x + 10, w), y)], fill=rgb + (240 if on else 150,), width=wd)
            x += 16
        txt = f"{label}  {frac * 100:.0f}%"
        tw = d.textlength(txt, font=font)
        by = (y + 5) if key == "sub" else (y - th - 12)   # captions sit above their line, the title below it
        if by < 2:
            by = y + 5
        if by + th + 8 > h:
            by = y - th - 12
        d.rounded_rectangle([8, by, 8 + tw + 14, by + th + 8], radius=6, fill=rgb + (235 if on else 190,))
        d.text((15, by + 3), txt, fill=(0, 0, 0, 255) if sum(rgb) > 560 else (255, 255, 255, 255), font=font)
    return Image.alpha_composite(img.convert("RGBA"), ov).convert("RGB")


def make_preview(cfg, workdir, cache, seq):
    """WYSIWYG still: real frame (or a placeholder) through the exact render pipeline."""
    o, (W, H) = cfg["o"], cfg["pv"]
    os.makedirs(workdir, exist_ok=True)
    clip, src = cfg["clip"], cfg["src"]
    if src and clip:
        s, e = clip
        dur = e - s
        tp = min(max(cfg["pos"], 0.0), 0.999) * dur
        key = (src, round(s + tp, 1))
        frame = cache.get(key)
        if not frame or not os.path.isfile(frame):
            frame = os.path.join(workdir, "frame_%d.jpg" % (abs(hash(key)) % 10 ** 8))
            extract_frame(src, s + tp, frame)
            cache[key] = frame
            while len(cache) > 12:
                old = cache.pop(next(iter(cache)))
                try:
                    os.remove(old)
                except OSError:
                    pass
        inp, words, label = ["-i", frame], cfg["words"], cfg["label"]
        if not words:  # no transcript (transcription skipped): don't fake captions over real footage
            o = dict(o, burn=False)
    else:
        clip, tp, words, label = (0.0, 10.0), 0.5, SAMPLE_WORDS, "Your hook title here"
        inp = ["-f", "lavfi", "-i", "color=c=0x2A3042:s=1280x720:r=25:d=1"]
    fam, fdir = prepare_font(o, workdir)
    comp = compose(o, words, clip, label, W, H, fam, preview_tp=tp)
    ass_name = "pv.ass"
    with open(os.path.join(workdir, ass_name), "w", encoding="utf-8-sig") as f:
        f.write(comp["ass"])
    graph, _ = build_graph(o, W, H, None, False, comp["D"], ass_name, fdir, preview=True)
    out = os.path.join(workdir, "pv_%d.png" % seq)
    r = subprocess.run([FFMPEG, "-y", "-hide_banner", "-loglevel", "error", "-nostdin", *inp, "-filter_complex",
                        graph, "-map", "[v]", "-frames:v", "1", out], cwd=workdir, stdin=subprocess.DEVNULL,
                       capture_output=True, text=True, encoding="utf-8", errors="replace", **_kw())
    if r.returncode:
        raise RuntimeError(r.stderr.strip()[-400:])
    return out


# ====================================================================== GUI
ACCENT, BG, CARD, FIELD, DIM = "#7C5CFF", "#0E1015", "#171A21", "#232733", "#8A93A6"
HOVER, SEL = "#2C3140", "#3A3170"
OK, WARN, BAD, BORDER = "#3DDC97", "#FFB84D", "#FF6B6B", "#232836"


class Banner:
    """Inline notice card that can be shown above the first card of a tab."""
    def __init__(self, parent, before):
        self.before, self.shown = before, False
        self.f = ctk.CTkFrame(parent, fg_color="#2B2438", corner_radius=12, border_width=1, border_color="#4B3C6E")
        self.lbl = ctk.CTkLabel(self.f, text="", justify="left", anchor="w", wraplength=360, text_color="#E4DDFB",
                                font=ctk.CTkFont(size=12))
        self.lbl.pack(fill="x", padx=12, pady=(10, 8))
        self.btn = ctk.CTkButton(self.f, text="", height=28, corner_radius=8, fg_color=ACCENT, hover_color="#6A4BE8")

    def show(self, text, action_text=None, action=None):
        self.lbl.configure(text=text)
        if action_text:
            self.btn.configure(text=action_text, command=action)
            self.btn.pack(anchor="w", padx=12, pady=(0, 10))
        else:
            self.btn.pack_forget()
        if not self.shown:
            self.f.pack(fill="x", pady=(6, 2), padx=2, before=self.before)
            self.shown = True

    def hide(self):
        if self.shown:
            self.f.pack_forget()
            self.shown = False


class Tooltip:
    """Small hover hint for any widget."""
    def __init__(self, widget, text, delay=450):
        self.w, self.text, self.delay, self.tw, self.job = widget, text, delay, None, None
        try:
            widget.bind("<Enter>", self._enter, add="+")
            widget.bind("<Leave>", self._leave, add="+")
            widget.bind("<ButtonPress>", self._leave, add="+")
        except Exception:
            pass

    def _enter(self, _e=None):
        self._leave()
        self.job = self.w.after(self.delay, self._show)

    def _show(self):
        try:
            x, y = self.w.winfo_rootx() + 12, self.w.winfo_rooty() + self.w.winfo_height() + 6
            self.tw = tk.Toplevel(self.w)
            self.tw.wm_overrideredirect(True)
            self.tw.wm_geometry(f"+{x}+{y}")
            tk.Label(self.tw, text=self.text, bg="#2A2F3E", fg="#E6E9F2", padx=8, pady=4, justify="left",
                     wraplength=280, relief="solid", borderwidth=1).pack()
        except Exception:
            self.tw = None

    def _leave(self, _e=None):
        if self.job:
            try:
                self.w.after_cancel(self.job)
            except Exception:
                pass
            self.job = None
        if self.tw:
            try:
                self.tw.destroy()
            except Exception:
                pass
            self.tw = None

STYLE_KEYS = ["color", "hl_color", "outline_color", "fsize", "outline", "shadow", "wpl", "upper", "bold",
              "hl_style", "anim", "box", "box_color", "box_op", "spacing", "pos_y", "strip_punct"]


def _P(color="#FFFFFF", hl="#FFD400", oc="#000000", fsize=90, outline=6, wpl=3, upper=True, hl_style="Color + pop",
       anim="Pop", box=False, shadow=2, bold=True, box_op=0.75, spacing=0, pos_y=0.72, strip=False):
    return dict(color=color, hl_color=hl, outline_color=oc, fsize=float(fsize), outline=float(outline),
                shadow=float(shadow), wpl=float(wpl), upper=upper, bold=bold, hl_style=hl_style, anim=anim, box=box,
                box_color="#000000", box_op=box_op, spacing=float(spacing), pos_y=pos_y, strip_punct=strip)


PRESETS = {
    "Classic Yellow": _P(),
    "Clean White": _P(hl="#FFFFFF", fsize=76, outline=4, wpl=4, upper=False, hl_style="None", anim="None", bold=False),
    "Neon Green": _P(hl="#39FF14", fsize=96, outline=7, wpl=2),
    "Hot Pink": _P(hl="#FF2D87", oc="#1A0010", fsize=92, outline=7),
    "Block Highlight": _P(hl="#FFD400", fsize=88, outline=5, hl_style="Block"),
    "Karaoke Blue": _P(hl="#29B6FF", fsize=86, outline=6, wpl=4, hl_style="Karaoke sweep", anim="Fade"),
    "Boxed Captions": _P(hl="#FFD400", fsize=70, outline=0, wpl=4, upper=False, hl_style="Color", box=True, shadow=0),
    "Minimal Lower": _P(hl="#FFFFFF", fsize=62, outline=3, wpl=5, upper=False, hl_style="None", anim="None",
                       bold=False, pos_y=0.88, strip=True),
}


def default_values(enc_labels):
    base = PRESETS["Classic Yellow"]
    d = dict(
        cookies="No cookies", model="small", lang="", task="Transcribe", vad=True, music=False, device="auto", keep_cache=True,
        transcribe=True, auto_clip=True,
        out_dir=os.path.join(os.path.expanduser("~"), "Shorts"),
        mode="Fixed length", clip_len=45.0, max_clips=0.0, snap=True, range_start="", range_end="",
        silence_cut=False, silence_gap=0.6,
        fmt="9:16", framing="Blur fit", blur=28.0, dim=0.10, zoom=1.0, focus_x=0.5, focus_y=0.5,
        bars_color="#000000", fade=0.0, bar_on=False, bar_color="#7C5CFF", bar_h=10.0, res="1080p",
        burn=True, font="Arial", font_file="", shift_ms=0.0, mask="",
        title="", title_size=70.0, title_y=0.10, title_dur=3.5, title_box=True, title_color="#FFFFFF",
        title_box_color="#000000",
        mark="", mark_pos="Top right", mark_size=36.0, mark_op=0.6,
        encoder=enc_labels[0], speed="Balanced", crf=20.0, fps="Source", loudnorm=False, abitrate="192k",
        name_tpl="{title}_{n:02d}", save_srt=False, save_thumb=False, open_folder=True, auto_render=False,
        safe=True)
    d.update(base)
    return d


class App(ctk.CTk if ctk else object):
    def __init__(self):
        super().__init__()
        self.title("Shorts Maker 2")
        self.geometry("1500x900")
        self.minsize(1280, 780)
        ctk.set_appearance_mode("dark")
        self.configure(fg_color=BG)
        self.q = queue.Queue()
        self.cancel = threading.Event()
        self.tmp = tempfile.mkdtemp(prefix="shorts_ui_")
        self.enc_map = detect_encoders() if FFMPEG else {"CPU (x264)": "libx264"}
        self.opt, self.user_presets = {}, {}
        self.src = self.src_dir = None
        self.local = False
        self.title_text, self.dur, self.info, self.words, self.clips = "", 0.0, {}, [], []
        self.words_cache = None
        self.sel = None
        self._rows = []
        self._groups, self._busy, self._rendering, self._log_open = {}, False, False, False
        self._tx_t, self._tx_w, self._tx_spans = [], [], []   # words of the selected clip: start times, text, char spans
        self._pv_base, self._pv_hover, self._pv_hot, self._pv_drag, self._pv_leave_job = None, False, None, None, None
        self.pv_box = (340, 560)
        self._thumbs = {}
        self._frame_cache, self._pv_seq = {}, 0
        self._pv_after, self._pv_busy, self._pv_pending = None, False, None
        self._pv_lock = threading.Lock()
        self._ready = False
        self._make_vars()
        self._build()
        self._load_config()
        self._ready = True
        self._on_workflow()
        self.after(100, self._poll)
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.after(60, self._maximize)
        self.bind("<Control-l>", lambda e: self.start_load())
        self.bind("<Control-r>", lambda e: self.start_render())
        self.bind("<Escape>", lambda e: self.do_cancel())
        self.bind("<Alt-Left>", lambda e: self.step_word(-1))
        self.bind("<Alt-Right>", lambda e: self.step_word(1))
        self._update_buttons()
        self._on_mode()
        self.schedule_preview()
        if not FFMPEG:
            self.status.configure(text="ffmpeg with libass not found - run: pip install imageio-ffmpeg",
                                  text_color="#FF6B6B")

    def _maximize(self):
        try:
            self.state("zoomed")
        except Exception:
            try:
                self.attributes("-zoomed", True)
            except Exception:
                pass

    def _scale(self):
        try:
            return float(self._get_window_scaling())
        except Exception:
            return 1.0

    # ------------------------------------------------------------ variables
    def _make_vars(self):
        vals = default_values(list(self.enc_map))
        no_preview = {"cookies", "model", "lang", "task", "vad", "device", "keep_cache", "transcribe", "auto_clip", "out_dir", "mode", "clip_len",
                      "max_clips", "snap", "range_start", "range_end", "silence_cut", "silence_gap", "fade", "encoder",
                      "speed", "crf", "fps", "loudnorm", "abitrate", "name_tpl", "save_srt", "save_thumb",
                      "open_folder", "auto_render", "music"}
        for k, d in vals.items():
            if isinstance(d, bool):
                v = tk.BooleanVar(value=d)
            elif isinstance(d, (int, float)):
                v = tk.DoubleVar(value=float(d))
            else:
                v = tk.StringVar(value=d)
            if k not in no_preview:
                v.trace_add("write", self._on_change)
            self.opt[k] = v
        self.pv_pos = tk.DoubleVar(value=0.3)
        self.pv_pos.trace_add("write", self._on_change)
        self.pv_pos.trace_add("write", lambda *a: self._on_scrub())
        self.opt["mode"].trace_add("write", lambda *a: self._on_mode())
        self.opt["out_dir"].trace_add("write", lambda *a: self._sync_dir())

    def _on_change(self, *_):
        if self._ready:
            self.schedule_preview()

    def collect(self):
        o = {}
        for k, v in self.opt.items():
            try:
                o[k] = v.get()
            except Exception:
                o[k] = default_values(list(self.enc_map))[k]
        o["manual"] = self.manual.get("1.0", "end")
        o["encoder_name"] = self.enc_map.get(o["encoder"], "libx264")
        return o

    # ------------------------------------------------------------ UI helpers
    def card(self, parent, title):
        f = ctk.CTkFrame(parent, fg_color=CARD, corner_radius=14, border_width=1, border_color=BORDER)
        f.pack(fill="x", pady=6, padx=2)
        h = ctk.CTkFrame(f, fg_color="transparent")
        h.pack(fill="x", padx=14, pady=(11, 7))
        ctk.CTkFrame(h, width=4, height=14, corner_radius=2, fg_color=ACCENT).pack(side="left", padx=(0, 8))
        ctk.CTkLabel(h, text=title.upper(), font=ctk.CTkFont(size=11, weight="bold"),
                     text_color="#C9D0E0").pack(side="left")
        return f

    def fold(self, parent, title, open_=False):
        """Collapsible section inside a card. Returns the body frame to fill."""
        f = ctk.CTkFrame(parent, fg_color="transparent")
        f.pack(fill="x")
        body = ctk.CTkFrame(f, fg_color="transparent")
        st = {"open": open_}
        btn = ctk.CTkButton(f, text="", anchor="w", height=30, corner_radius=8, fg_color="transparent",
                            hover_color=HOVER, text_color=DIM, font=ctk.CTkFont(size=12, weight="bold"))

        def paint():
            btn.configure(text=("\u25BE  " if st["open"] else "\u25B8  ") + title)
            if st["open"]:
                body.pack(fill="x", pady=(0, 6))
            else:
                body.pack_forget()

        def toggle():
            st["open"] = not st["open"]
            paint()
        btn.configure(command=toggle)
        btn.pack(fill="x", padx=8, pady=(0, 2))
        paint()
        return body

    def _grp(self, name, *frames):
        self._groups.setdefault(name, []).extend(frames)

    def _set_enabled(self, w, on):
        if isinstance(w, ctk.CTkFrame):
            for ch in w.winfo_children():
                self._set_enabled(ch, on)
        else:
            try:
                w.configure(state="normal" if on else "disabled")
            except Exception:
                pass

    def hint(self, parent, text):
        ctk.CTkLabel(parent, text=text, text_color=DIM, font=ctk.CTkFont(size=11), anchor="w",
                     justify="left", wraplength=360).pack(anchor="w", padx=14, pady=(0, 8))

    def seg(self, parent, key, values):
        s = ctk.CTkSegmentedButton(parent, values=values, variable=self.opt[key], selected_color=ACCENT,
                                   selected_hover_color=ACCENT, unselected_color=FIELD, unselected_hover_color=HOVER)
        s.pack(fill="x", padx=14, pady=(0, 8))

    def menu(self, parent, label, key, values):
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", padx=14, pady=3)
        ctk.CTkLabel(row, text=label, width=105, anchor="w").pack(side="left")
        ctk.CTkOptionMenu(row, values=values, variable=self.opt[key], fg_color=FIELD, button_color=ACCENT,
                          button_hover_color="#6A4BE8", dynamic_resizing=False).pack(side="left", fill="x", expand=True)

    def switch(self, parent, label, key):
        ctk.CTkSwitch(parent, text=label, variable=self.opt[key], onvalue=True, offvalue=False,
                      progress_color=ACCENT).pack(anchor="w", padx=14, pady=4)

    def entry(self, parent, label, key, width=None):
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", padx=14, pady=3)
        ctk.CTkLabel(row, text=label, width=105, anchor="w").pack(side="left")
        e = ctk.CTkEntry(row, textvariable=self.opt[key], height=30, **({"width": width} if width else {}))
        e.pack(side="left", fill="x", expand=not width)
        return e

    def slider(self, parent, label, key, lo, hi, steps, fmt="{:.0f}"):
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", padx=14, pady=2)
        ctk.CTkLabel(row, text=label, width=112, anchor="w").pack(side="left")
        val = ctk.CTkLabel(row, width=48, text_color=DIM, text="")
        val.pack(side="right")
        v = self.opt[key]

        def upd(*_):
            try:
                val.configure(text=fmt.format(v.get()))
            except Exception:
                pass
        v.trace_add("write", upd)
        upd()
        ctk.CTkSlider(row, from_=lo, to=hi, number_of_steps=steps, variable=v, progress_color=ACCENT,
                      button_color=ACCENT, button_hover_color="#9A82FF").pack(side="left", fill="x", expand=True, padx=6)

    def btn(self, parent, text, cmd, side="left", color=FIELD, **kw):
        b = ctk.CTkButton(parent, text=text, command=cmd, height=30, fg_color=color, hover_color=HOVER
                          if color == FIELD else "#6A4BE8", **kw)
        b.pack(side=side, fill="x", expand=True, padx=3)
        return b

    def color_btn(self, parent, key, label):
        b = ctk.CTkButton(parent, text=label, height=30, corner_radius=8, command=lambda: self._pick_color(key))
        b.pack(side="left", fill="x", expand=True, padx=3)

        def paint(*_):
            try:
                c = self.opt[key].get()
                b.configure(fg_color=c, hover_color=c, text_color=contrast(c))
            except Exception:
                pass
        self.opt[key].trace_add("write", paint)
        paint()

    def row(self, parent, pad=11):
        r = ctk.CTkFrame(parent, fg_color="transparent")
        r.pack(fill="x", padx=pad, pady=(4, 6))
        return r

    def _pick_color(self, key):
        c = colorchooser.askcolor(self.opt[key].get())[1]
        if c:
            self.opt[key].set(c.upper())

    # ---------------------------------------------------------------- layout
    def _chip(self, parent, text, color=DIM):
        c = ctk.CTkLabel(parent, text=text, text_color=color, fg_color=FIELD, corner_radius=12, height=26,
                         font=ctk.CTkFont(size=11, weight="bold"))
        c.pack(side="right", padx=4, pady=14, ipadx=8)
        return c

    def _build(self):
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(1, weight=1)

        # ---- header bar
        hd = ctk.CTkFrame(self, fg_color=CARD, corner_radius=0, height=58, border_width=0)
        hd.grid(row=0, column=0, columnspan=2, sticky="ew")
        hd.pack_propagate(False)
        ctk.CTkLabel(hd, text="\u2702", width=36, height=36, corner_radius=10, fg_color=ACCENT,
                     font=ctk.CTkFont(size=18, weight="bold")).pack(side="left", padx=(16, 10), pady=11)
        tf = ctk.CTkFrame(hd, fg_color="transparent")
        tf.pack(side="left")
        ctk.CTkLabel(tf, text="Shorts Maker", font=ctk.CTkFont(size=17, weight="bold")).pack(anchor="w")
        self.sub_lbl = ctk.CTkLabel(tf, text="No video loaded yet", text_color=DIM, font=ctk.CTkFont(size=11), anchor="w")
        self.sub_lbl.pack(anchor="w")
        sf = ctk.CTkFrame(hd, fg_color="transparent")
        sf.pack(side="left", padx=30)
        self.step_lbls = []
        for i, nm in enumerate(STEP_NAMES, 1):
            if i > 1:
                ctk.CTkLabel(sf, text="\u2014", width=18, text_color=BORDER).pack(side="left")
            sl = ctk.CTkLabel(sf, text=f"{i}  {nm}", height=26, corner_radius=12, fg_color=FIELD, text_color=DIM,
                              font=ctk.CTkFont(size=12, weight="bold"))
            sl.pack(side="left", ipadx=8)
            self.step_lbls.append(sl)
        self.chip_ff = self._chip(hd, "ffmpeg OK" if FFMPEG else "ffmpeg missing", OK if FFMPEG else BAD)
        self.chip_enc = self._chip(hd, self.opt["encoder"].get())
        self.chip_model = self._chip(hd, "Whisper " + self.opt["model"].get())
        self.chip_vid = self._chip(hd, "no video")
        self.opt["encoder"].trace_add("write", lambda *a: self.chip_enc.configure(text=self.opt["encoder"].get()))
        self.opt["model"].trace_add("write", lambda *a: self._sync_chips())

        # ---- left: settings tabs
        left = ctk.CTkFrame(self, fg_color="transparent", width=450)
        left.grid(row=1, column=0, sticky="ns", padx=(14, 6), pady=(12, 14))
        tabs = ctk.CTkTabview(left, width=440, fg_color=BG, segmented_button_selected_color=ACCENT,
                              segmented_button_selected_hover_color=ACCENT, segmented_button_unselected_color=FIELD,
                              segmented_button_unselected_hover_color=HOVER)
        tabs.pack(fill="both", expand=True)
        try:
            tabs._segmented_button.configure(font=ctk.CTkFont(size=13, weight="bold"), height=34)
        except Exception:
            pass
        self.tabs = tabs
        builders = [("\u2B07 Source", self._tab_source), ("\u2702 Cut", self._tab_cut), ("\u25A3 Frame", self._tab_frame),
                    ("Aa Subs", self._tab_subs), ("\u2726 Extras", self._tab_extras), ("\u2699 Output", self._tab_output)]
        for name, fn in builders:
            t = tabs.add(name)
            sf = ctk.CTkScrollableFrame(t, fg_color="transparent", scrollbar_button_color=FIELD,
                                        scrollbar_button_hover_color=HOVER)
            sf.pack(fill="both", expand=True)
            fn(sf)

        # ---- right: preview + clips + footer
        main = ctk.CTkFrame(self, fg_color=CARD, corner_radius=18, border_width=1, border_color=BORDER)
        main.grid(row=1, column=1, sticky="nsew", padx=(6, 14), pady=(12, 14))
        main.grid_columnconfigure(0, weight=5, minsize=380)
        main.grid_columnconfigure(1, weight=6)
        main.grid_rowconfigure(0, weight=1)

        pvc = ctk.CTkFrame(main, fg_color="transparent")
        pvc.grid(row=0, column=0, sticky="nsew", padx=(16, 8), pady=(16, 4))
        pvc.grid_rowconfigure(0, weight=1)
        pvc.grid_columnconfigure(0, weight=1)
        self.pv_host = ctk.CTkFrame(pvc, fg_color="#0B0D12", corner_radius=14)
        self.pv_host.grid(row=0, column=0, sticky="nsew")
        self.pv_host.pack_propagate(False)
        self.pv_label = ctk.CTkLabel(self.pv_host, text="Preview", text_color=DIM)
        self.pv_label.pack(expand=True)
        self.pv_host.bind("<Configure>", self._on_pv_resize)
        for seq, fn in (("<Button-1>", self._pv_press), ("<B1-Motion>", self._pv_drag_move),
                        ("<ButtonRelease-1>", self._pv_release), ("<Motion>", self._pv_motion),
                        ("<Enter>", self._pv_motion), ("<Leave>", self._pv_leave)):
            try:
                self.pv_label.bind(seq, fn)
            except Exception:
                pass
        try:
            self.pv_label.configure(cursor="sb_v_double_arrow")
        except Exception:
            pass
        ctrl = ctk.CTkFrame(pvc, fg_color="transparent")
        ctrl.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        self.pv_info = ctk.CTkLabel(ctrl, text="Sample preview - load a video to see your footage", text_color="#C9D0E0",
                                    anchor="w", font=ctk.CTkFont(size=12, weight="bold"))
        self.pv_info.pack(fill="x")
        r = ctk.CTkFrame(ctrl, fg_color="transparent")
        r.pack(fill="x", pady=(4, 0))
        b1 = ctk.CTkButton(r, text="\u25C0", width=34, height=28, fg_color=FIELD, hover_color=HOVER,
                           command=lambda: self.step_clip(-1))
        b1.pack(side="left")
        self.pv_time = ctk.CTkLabel(r, text="", width=64, text_color=DIM)
        b2 = ctk.CTkButton(r, text="\u25B6", width=34, height=28, fg_color=FIELD, hover_color=HOVER,
                           command=lambda: self.step_clip(1))
        b2.pack(side="right")
        self.pv_time.pack(side="right", padx=4)
        ctk.CTkSlider(r, from_=0, to=1, variable=self.pv_pos, progress_color=ACCENT, button_color=ACCENT,
                      button_hover_color="#9A82FF").pack(side="left", fill="x", expand=True, padx=8)
        Tooltip(b1, "Previous clip")
        Tooltip(b2, "Next clip")
        # speech timeline: spoken words, on-screen caption spans, removed silences, title span, playhead
        self.tl = tk.Canvas(ctrl, height=TL_H, bg="#10131A", highlightthickness=0, cursor="hand2")
        self.tl.pack(fill="x", pady=(8, 0))
        self.tl.bind("<Button-1>", self._tl_seek)
        self.tl.bind("<B1-Motion>", self._tl_seek)
        self.tl.bind("<Configure>", lambda e: (self._refresh_timeline(), self._draw_playhead()))
        Tooltip(self.tl, "Speech timeline. Bright bars = spoken words, dark blocks = when each caption is on screen, "
                         "red = silence removed by jump cuts, amber = hook title. Click or drag to scrub.")
        r = ctk.CTkFrame(ctrl, fg_color="transparent")
        r.pack(fill="x", pady=(6, 0))
        bw1 = ctk.CTkButton(r, text="\u2039 Word", width=70, height=26, fg_color=FIELD, hover_color=HOVER,
                            command=lambda: self.step_word(-1))
        bw1.pack(side="left")
        bw2 = ctk.CTkButton(r, text="Word \u203A", width=70, height=26, fg_color=FIELD, hover_color=HOVER,
                            command=lambda: self.step_word(1))
        bw2.pack(side="right")
        self.pv_word = ctk.CTkLabel(r, text="", height=26, corner_radius=8, fg_color="#10131A", text_color="#FFFFFF",
                                    font=ctk.CTkFont(size=12, weight="bold"))
        self.pv_word.pack(side="left", fill="x", expand=True, padx=6)
        Tooltip(bw1, "Previous word (Alt+Left)")
        Tooltip(bw2, "Next word (Alt+Right)")
        r = ctk.CTkFrame(ctrl, fg_color="transparent")
        r.pack(fill="x", pady=(6, 0))
        sw = ctk.CTkSwitch(r, text="Safe zones", variable=self.opt["safe"], onvalue=True, offvalue=False,
                           progress_color=ACCENT, width=90)
        sw.pack(side="left")
        Tooltip(sw, "Red areas are covered by platform buttons and captions on Shorts / Reels / TikTok.")
        self.pv_state = ctk.CTkLabel(r, text="\u25CF live", text_color=OK, font=ctk.CTkFont(size=11))
        self.pv_state.pack(side="left", padx=10)
        self.play_btn = ctk.CTkButton(r, text="\u25B6  Play 8s preview", height=30, fg_color=FIELD, hover_color=HOVER,
                                      command=self.play_preview)
        self.play_btn.pack(side="right")
        Tooltip(self.play_btn, "Renders the first 8 seconds of the selected clip with all your settings and opens it in your video player.")
        ctk.CTkLabel(ctrl, text="Click or drag on the preview to place the subtitles (or the title, whichever line is "
                                "closer). Alt+\u2190 / Alt+\u2192 jump word by word.", text_color=DIM,
                     font=ctk.CTkFont(size=11), anchor="w", justify="left", wraplength=340).pack(fill="x", pady=(6, 0))

        cl = ctk.CTkFrame(main, fg_color="transparent")
        cl.grid(row=0, column=1, sticky="nsew", padx=(8, 16), pady=(16, 4))
        top = ctk.CTkFrame(cl, fg_color="transparent")
        top.pack(fill="x")
        self.clips_title = ctk.CTkLabel(top, text="CLIPS", font=ctk.CTkFont(size=11, weight="bold"), text_color=DIM)
        self.clips_title.pack(side="left")
        self.summary = ctk.CTkLabel(top, text="", text_color=DIM)
        self.summary.pack(side="right")
        tb = ctk.CTkFrame(cl, fg_color="transparent")
        tb.pack(fill="x", pady=(6, 4))
        tips = {"All": "Tick every clip", "None": "Untick every clip", "+ Add": "Add a clip starting at the preview position",
                "Split": "Split the selected clip at the preview position", "Merge \u2193": "Merge the selected clip with the next one",
                "Delete": "Remove the selected clip", "Re-plan": "Re-create clips using the Cut tab settings"}
        for text, fn in (("All", lambda: self._set_all(True)), ("None", lambda: self._set_all(False)),
                         ("+ Add", self.add_clip), ("Split", self.split_clip), ("Merge \u2193", self.merge_clip),
                         ("Delete", self.del_clip), ("Re-plan", self.replan)):
            bt = ctk.CTkButton(tb, text=text, width=58, height=28, fg_color=FIELD, hover_color=HOVER, command=fn)
            bt.pack(side="left", padx=2)
            Tooltip(bt, tips[text])
        hdr = ctk.CTkFrame(cl, fg_color="transparent")
        hdr.pack(fill="x", padx=2)
        for text, w, anc in (("", 40, "w"), ("", 82, "w"), ("#", 24, "w"), ("Start", 78, "w"), ("End", 78, "w"),
                             ("Len", 40, "w"), ("Score", 48, "w"), ("Opening words", 0, "w")):
            ctk.CTkLabel(hdr, text=text, width=w, anchor=anc, text_color=DIM,
                         font=ctk.CTkFont(size=10, weight="bold")).pack(side="left", padx=(2, 0), fill="x" if not w else None,
                                                                         expand=not w)
        self.rows_frame = ctk.CTkScrollableFrame(cl, fg_color="#10131A", corner_radius=10, scrollbar_button_color=FIELD,
                                                 scrollbar_button_hover_color=HOVER)
        self.rows_frame.pack(fill="both", expand=True)
        ctk.CTkLabel(cl, text="TRANSCRIPT OF SELECTED CLIP", font=ctk.CTkFont(size=11, weight="bold"),
                     text_color=DIM).pack(anchor="w", pady=(8, 2))
        self.tx = ctk.CTkTextbox(cl, height=96, fg_color="#10131A", state="disabled", wrap="word")
        self.tx.pack(fill="x")
        try:
            tw = getattr(self.tx, "_textbox", self.tx)
            tw.tag_config("cur", background=ACCENT, foreground="#FFFFFF")
            tw.bind("<Button-1>", self._tx_click, add="+")
        except Exception:
            pass

        foot = ctk.CTkFrame(main, fg_color="transparent")
        foot.grid(row=1, column=0, columnspan=2, sticky="ew", padx=16, pady=(4, 16))
        sr = ctk.CTkFrame(foot, fg_color="transparent")
        sr.pack(fill="x")
        self.status = ctk.CTkLabel(sr, text="Ready - paste a link on the Source tab, then press Load & Transcribe",
                                   text_color=DIM, anchor="w")
        self.status.pack(side="left")
        self.pct = ctk.CTkLabel(sr, text="", text_color=DIM, font=ctk.CTkFont(size=12, weight="bold"))
        self.pct.pack(side="right")
        self.log_btn = ctk.CTkButton(sr, text="Show log", width=74, height=22, corner_radius=6, fg_color="transparent",
                                     hover_color=HOVER, text_color=DIM, font=ctk.CTkFont(size=11),
                                     command=lambda: self._show_log(not self._log_open))
        self.log_btn.pack(side="right", padx=(0, 6))
        self.bar = ctk.CTkProgressBar(foot, progress_color=ACCENT, height=12, corner_radius=6)
        self.bar.set(0)
        self.bar.pack(fill="x", pady=(4, 6))
        self.log_box = ctk.CTkTextbox(foot, height=88, fg_color="#10131A", state="disabled",
                                      font=ctk.CTkFont(family="Consolas", size=12))
        br = ctk.CTkFrame(foot, fg_color="transparent")
        br.pack(fill="x", pady=(8, 0))
        self._br = br
        self.load_btn = ctk.CTkButton(br, text="\u2460  Load & Transcribe   (Ctrl+L)", height=46, corner_radius=12,
                                      fg_color=FIELD, hover_color=HOVER, font=ctk.CTkFont(size=14, weight="bold"),
                                      command=self.start_load)
        self.load_btn.pack(side="left", fill="x", expand=True, padx=(0, 4))
        self.render_btn = ctk.CTkButton(br, text="\u2461  Render selected shorts   (Ctrl+R)", height=46, corner_radius=12,
                                        fg_color=ACCENT, hover_color="#6A4BE8",
                                        font=ctk.CTkFont(size=14, weight="bold"), command=self.start_render)
        self.render_btn.pack(side="left", fill="x", expand=True, padx=4)
        self.cancel_btn = ctk.CTkButton(br, text="Cancel  (Esc)", width=110, height=46, corner_radius=12,
                                        fg_color="#5A2A33", hover_color="#7A3541", state="disabled",
                                        command=self.do_cancel)
        self.cancel_btn.pack(side="left", padx=(4, 4))
        ctk.CTkButton(br, text="Open folder", width=110, height=46, corner_radius=12, fg_color=FIELD,
                      hover_color=HOVER, command=self.open_out).pack(side="left", padx=(4, 0))
        self.opt["transcribe"].trace_add("write", self._on_workflow)
        self.opt["auto_clip"].trace_add("write", self._on_workflow)
        self.opt["auto_clip"].trace_add("write", lambda *a: self._on_clip_toggle())
        self._on_workflow()

    def _show_log(self, on):
        if on == self._log_open:
            return
        self._log_open = on
        if on:
            self.log_box.pack(fill="x", before=self._br)
        else:
            self.log_box.pack_forget()
        self.log_btn.configure(text="Hide log" if on else "Show log")

    def _sync_chips(self):
        if not hasattr(self, "chip_model"):
            return
        on = self.opt["transcribe"].get()
        self.chip_model.configure(text=("Whisper " + self.opt["model"].get()) if on else "No transcription",
                                  text_color=DIM if on else WARN)

    def _on_clip_toggle(self):
        if self._ready and self.src and not self._busy:
            self.replan()

    def _on_workflow(self, *_):
        if not hasattr(self, "load_btn"):
            return
        self._sync_workflow_buttons()
        self._sync_pipeline()
        self._sync_chips()
        self._apply_enablement()

    def _sync_pipeline(self):
        if not hasattr(self, "_pills"):
            return
        tx, cut = bool(self.opt["transcribe"].get()), bool(self.opt["auto_clip"].get())
        for k, text, on in (("dl", "Download", True), ("tx", "Transcribe", tx), ("cut", "Split", cut),
                            ("out", "Render", True)):
            self._pills[k].configure(text=text if on else text + "  \u2715", fg_color=SEL if on else FIELD,
                                     text_color="#FFFFFF" if on else DIM)
        self.flow_lbl.configure(text={
            (True, True): "Full pipeline: cut by sentences, burn animated subtitles.",
            (True, False): "Whole video with subtitles. No splitting.",
            (False, True): "Fast mode: fixed or manual cuts, no subtitles.",
            (False, False): "Quick reformat: whole video, no subtitles, no cuts.",
        }[(tx, cut)])

    def _apply_enablement(self):
        tx, cut = bool(self.opt["transcribe"].get()), bool(self.opt["auto_clip"].get())
        for name, on in (("tx", tx), ("subs", tx), ("jump", tx), ("cut", cut)):
            for fr in self._groups.get(name, []):
                self._set_enabled(fr, on)
        if hasattr(self, "subs_banner"):
            if not tx:
                self.subs_banner.show("Transcription is off, so there is nothing to caption. Turn it on to style "
                                      "and burn subtitles.", "Turn on transcription",
                                      lambda: self.opt["transcribe"].set(True))
            elif self.src and not self.words:
                self.subs_banner.show("This video was loaded without a transcript.", "Transcribe now", self.start_load)
            else:
                self.subs_banner.hide()
        if hasattr(self, "cut_banner"):
            if not cut:
                self.cut_banner.show("Auto-split is off: the whole video is exported as one file. Trim it in the "
                                     "clip list, or use Split to cut it by hand.", "Turn on auto-split",
                                     lambda: self.opt["auto_clip"].set(True))
            elif not tx:
                self.cut_banner.show("No transcript: 'Smart sentences' falls back to fixed lengths and jump cuts "
                                     "are unavailable.")
            else:
                self.cut_banner.hide()
        if hasattr(self, "summary"):
            self._update_summary()
            self._update_tx()

    def _on_pv_resize(self, e):
        k = self._scale()
        w, h = max(160, int(e.width / k) - 16), max(160, int(e.height / k) - 16)
        w, h = min(w, 760), min(h, 1100)
        if abs(w - self.pv_box[0]) > 8 or abs(h - self.pv_box[1]) > 8:
            self.pv_box = (w, h)
            self.schedule_preview()

    def step_clip(self, d):
        if self.sel is None:
            return
        i = self.sel + d
        if 0 <= i < len(self.clips):
            self.select(i)

    def _sync_workflow_buttons(self):
        if not hasattr(self, "load_btn"):
            return
        load_text = "①  Load source (skip transcription)" if not self.opt["transcribe"].get() else "①  Load & Transcribe"
        render_text = "②  Render full video" if not self.opt["auto_clip"].get() else "②  Render selected shorts"
        self.load_btn.configure(text=load_text + "   (Ctrl+L)")
        self.render_btn.configure(text=render_text + "   (Ctrl+R)")

    def _update_buttons(self):
        busy = getattr(self, "_busy", False)
        self._sync_workflow_buttons()
        loaded = bool(self.src)
        self.load_btn.configure(state="disabled" if busy else "normal", fg_color=FIELD if loaded else ACCENT,
                                hover_color=HOVER if loaded else "#6A4BE8")
        self.render_btn.configure(state="normal" if (loaded and not busy) else "disabled",
                                  fg_color=ACCENT if loaded else FIELD, hover_color="#6A4BE8" if loaded else HOVER)
        self.cancel_btn.configure(state="normal" if busy else "disabled")
        step = 3 if (busy and self._rendering) else (2 if loaded else 1)
        for i, (lb, nm) in enumerate(zip(self.step_lbls, STEP_NAMES), 1):
            if i < step:
                lb.configure(text=f"\u2713  {nm}", fg_color=FIELD, text_color=OK)
            elif i == step:
                lb.configure(text=f"{i}  {nm}", fg_color=ACCENT, text_color="#FFFFFF")
            else:
                lb.configure(text=f"{i}  {nm}", fg_color=FIELD, text_color=DIM)

    # ------------------------------------------------------------------ tabs
    def _tab_source(self, p):
        c = self.card(p, "Source")
        ur = ctk.CTkFrame(c, fg_color="transparent")
        ur.pack(fill="x", padx=14, pady=(0, 6))
        self.url = ctk.CTkEntry(ur, placeholder_text="Paste a YouTube link and press Enter...", height=38, corner_radius=10)
        self.url.pack(side="left", fill="x", expand=True)
        self.url.bind("<Return>", lambda e: self.start_load())
        pb = ctk.CTkButton(ur, text="Paste", width=64, height=38, corner_radius=10, fg_color=FIELD, hover_color=HOVER,
                           command=self._paste_url)
        pb.pack(side="left", padx=(6, 0))
        Tooltip(pb, "Paste the link from your clipboard")
        r = ctk.CTkFrame(c, fg_color="transparent")
        r.pack(fill="x", padx=14, pady=(0, 6))
        ctk.CTkButton(r, text="Use local video file...", height=32, fg_color=FIELD, hover_color=HOVER,
                      command=self._pick_file).pack(side="left", fill="x", expand=True, padx=(0, 4))
        ctk.CTkOptionMenu(r, values=["No cookies", "firefox", "chrome", "edge", "brave"], variable=self.opt["cookies"],
                          width=120, fg_color=FIELD, button_color=ACCENT).pack(side="left")
        self.dir_btn = ctk.CTkButton(c, text="", anchor="w", height=34, fg_color=FIELD, hover_color=HOVER,
                                     command=self._pick_dir)
        self.dir_btn.pack(fill="x", padx=14, pady=(0, 12))
        self._sync_dir()
        c = self.card(p, "Workflow")
        self.hint(c, "Choose what to run. Transcription and splitting are both optional.")
        pipe = ctk.CTkFrame(c, fg_color="transparent")
        pipe.pack(fill="x", padx=14, pady=(0, 6))
        self._pills = {}
        for i, (k, t) in enumerate((("dl", "Download"), ("tx", "Transcribe"), ("cut", "Split"), ("out", "Render"))):
            if i:
                ctk.CTkLabel(pipe, text="\u203A", width=14, text_color=DIM).pack(side="left")
            pl = ctk.CTkLabel(pipe, text=t, height=26, corner_radius=12, fg_color=SEL,
                              font=ctk.CTkFont(size=11, weight="bold"))
            pl.pack(side="left", ipadx=8)
            self._pills[k] = pl
        self.flow_lbl = ctk.CTkLabel(c, text="", anchor="w", justify="left", wraplength=360, text_color="#C9D0E0",
                                     font=ctk.CTkFont(size=12))
        self.flow_lbl.pack(anchor="w", padx=14, pady=(0, 6))
        self.switch(c, "Transcribe speech", "transcribe")
        self.hint(c, "Adds subtitles, smart sentence cuts and silence removal. Turn off for the fastest load.")
        self.switch(c, "Split into multiple shorts", "auto_clip")
        self.hint(c, "Turn off to export the whole video as one file. You can still trim or split it by hand in the clip list.")
        c = self.card(p, "Transcription")
        self._grp("tx", c)
        self.menu(c, "Model", "model", MODELS)
        self.entry(c, "Language", "lang")
        self.hint(c, "Language code, blank = auto-detect (en, ar, fr, es ...). Bigger models = better accuracy, slower.")
        adv = self.fold(c, "Advanced")
        self.menu(adv, "Task", "task", ["Transcribe", "Translate to English"])
        self.menu(adv, "Device", "device", ["auto", "cpu", "cuda"])
        self.switch(adv, "Skip silence (VAD) - turn OFF for music", "vad")
        self.switch(adv, "Music / singing mode", "music")
        self.hint(adv, "For songs, rap and noisy audio: keeps every sung word instead of discarding it as non-speech. "
                       "Turned on automatically if the first pass only finds a little speech.")
        self.hint(c, "The downloaded video and transcript are cached, so changing style or cuts never re-downloads.")

    def _tab_cut(self, p):
        c = self.card(p, "Cutting")
        self.cut_banner = Banner(p, c)
        self._grp("cut", c)
        self.seg(c, "mode", CUT_MODES)
        self.slider(c, "Target length (s)", "clip_len", 10, 120, 110)
        self.slider(c, "Max shorts (0=all)", "max_clips", 0, 30, 30)
        self.switch(c, "Snap cuts to sentence ends (fixed)", "snap")
        self.hint(c, "Smart sentences cuts at natural sentence boundaries and ranks clips by speech energy and hooks; "
                     "with 'Max shorts' set it keeps the best-scoring ones.")
        self.entry(c, "Only from", "range_start", 80)
        self.entry(c, "Only until", "range_end", 80)
        self.hint(c, "Optional range (e.g. 1:30 and 12:00) to limit auto-cutting to part of the video.")
        self.manual = ctk.CTkTextbox(c, height=90, fg_color=FIELD)
        self.manual.insert("1.0", "00:01:10-00:02:05\n")
        self.manual_hint = ctk.CTkLabel(c, text="One range per line: start-end (mm:ss or hh:mm:ss)", text_color=DIM,
                                        font=ctk.CTkFont(size=11))
        c2 = self.card(p, "Jump cuts")
        self._grp("jump", c2)
        self.switch(c2, "Remove silent gaps", "silence_cut")
        self.slider(c2, "Gap threshold (s)", "silence_gap", 0.2, 2.0, 18, "{:.1f}")
        self.hint(c2, "Cuts out pauses longer than the threshold for a faster, punchier pace. Subtitles stay in sync.")
        ctk.CTkButton(p, text="Re-plan clips with these settings", height=36, fg_color=ACCENT, hover_color="#6A4BE8",
                      command=self.replan).pack(fill="x", padx=4, pady=8)
        self.manual_card = c

    def _tab_frame(self, p):
        c = self.card(p, "Format")
        self.seg(c, "fmt", list(FORMATS))
        self.seg(c, "res", list(RES))
        self.seg(c, "framing", FRAMING)
        self.slider(c, "Backdrop blur", "blur", 5, 80, 75)
        self.slider(c, "Backdrop dim", "dim", 0, 0.6, 30, "{:.2f}")
        r = self.row(c)
        self.color_btn(r, "bars_color", "Bar color")
        c = self.card(p, "Crop & zoom (Crop fill)")
        self.slider(c, "Zoom", "zoom", 1.0, 2.5, 30, "{:.2f}")
        self.slider(c, "Focus left-right", "focus_x", 0, 1, 40, "{:.2f}")
        self.slider(c, "Focus up-down", "focus_y", 0, 1, 40, "{:.2f}")
        self.hint(c, "Move the focus so the speaker stays in frame. Watch the preview on the left.")
        c = self.card(p, "Polish")
        self.slider(c, "Fade in/out (s)", "fade", 0, 1.5, 15, "{:.1f}")
        self.switch(c, "Progress bar", "bar_on")
        self.slider(c, "Bar height", "bar_h", 4, 30, 26)
        r = self.row(c)
        self.color_btn(r, "bar_color", "Bar color")

    def _tab_subs(self, p):
        c = self.card(p, "Subtitle style")
        self.subs_banner = Banner(p, c)
        self._grp("subs", c)
        self.switch(c, "Burn subtitles into video", "burn")
        r = ctk.CTkFrame(c, fg_color="transparent")
        r.pack(fill="x", padx=14, pady=(0, 6))
        self.preset_menu = ctk.CTkOptionMenu(r, values=list(PRESETS), command=self.apply_preset, fg_color=FIELD,
                                             button_color=ACCENT, button_hover_color="#6A4BE8")
        self.preset_menu.set("Classic Yellow")
        self.preset_menu.pack(side="left", fill="x", expand=True)
        ctk.CTkButton(r, text="Save", width=52, height=30, fg_color=FIELD, hover_color=HOVER,
                      command=self.save_preset).pack(side="left", padx=(4, 0))
        ctk.CTkButton(r, text="Del", width=44, height=30, fg_color=FIELD, hover_color=HOVER,
                      command=self.del_preset).pack(side="left", padx=(4, 0))
        try:
            fams = sorted({f for f in tkfont.families() if not f.startswith("@")}, key=str.lower)
        except Exception:
            fams = ["Arial"]
        r = ctk.CTkFrame(c, fg_color="transparent")
        r.pack(fill="x", padx=14, pady=(0, 6))
        ctk.CTkComboBox(r, values=fams, variable=self.opt["font"], fg_color=FIELD, button_color=ACCENT).pack(
            side="left", fill="x", expand=True)
        ctk.CTkButton(r, text="Font file...", width=84, height=28, fg_color=FIELD, hover_color=HOVER,
                      command=self._pick_font).pack(side="left", padx=(4, 0))
        self.font_lbl = ctk.CTkLabel(c, text="", text_color=DIM, font=ctk.CTkFont(size=11), anchor="w")
        self.font_lbl.pack(anchor="w", padx=14)
        self.opt["font_file"].trace_add("write", lambda *a: self.font_lbl.configure(
            text=("Using font file: " + os.path.basename(self.opt["font_file"].get())) if self.opt["font_file"].get() else ""))
        self.slider(c, "Font size", "fsize", 30, 200, 170)
        self.slider(c, "Outline", "outline", 0, 14, 28, "{:.1f}")
        self.slider(c, "Shadow", "shadow", 0, 10, 20, "{:.1f}")
        self.slider(c, "Vertical position", "pos_y", 0.3, 0.97, 67, "{:.2f}")
        self.slider(c, "Words per line", "wpl", 1, 8, 7)
        self.slider(c, "Letter spacing", "spacing", -2, 12, 28, "{:.1f}")
        self.slider(c, "Timing shift (ms)", "shift_ms", -400, 400, 80)
        r = self.row(c)
        for k, l in (("color", "Text"), ("hl_color", "Highlight"), ("outline_color", "Outline")):
            self.color_btn(r, k, l)
        self.menu(c, "Highlight", "hl_style", HL_STYLES)
        self.menu(c, "Animation", "anim", ANIMS)
        self.switch(c, "Bold", "bold")
        self.switch(c, "UPPERCASE", "upper")
        self.switch(c, "Remove punctuation", "strip_punct")
        self.switch(c, "Caption box", "box")
        r = self.row(c)
        self.color_btn(r, "box_color", "Box color")
        self.slider(c, "Box opacity", "box_op", 0.1, 1, 18, "{:.2f}")
        c = self.card(p, "Transcript tools")
        self._grp("subs", c)
        self.entry(c, "Mask words", "mask")
        self.hint(c, "Comma-separated words shown as f*** in subtitles (e.g. swear words).")
        r = ctk.CTkFrame(c, fg_color="transparent")
        r.pack(fill="x", padx=14, pady=(0, 4))
        self.fix_find = ctk.CTkEntry(r, placeholder_text="wrong word", height=30)
        self.fix_find.pack(side="left", fill="x", expand=True, padx=(0, 4))
        self.fix_repl = ctk.CTkEntry(r, placeholder_text="correct (blank = delete)", height=30)
        self.fix_repl.pack(side="left", fill="x", expand=True)
        ctk.CTkButton(c, text="Fix this word everywhere in the transcript", height=30, fg_color=FIELD,
                      hover_color=HOVER, command=self.apply_fix).pack(fill="x", padx=14, pady=(2, 12))

    def _tab_extras(self, p):
        c = self.card(p, "Hook title overlay")
        self.entry(c, "Title text", "title")
        self.hint(c, "Use {auto} for the clip's first words. Type \\n for a new line. Blank = no title.")
        self.slider(c, "Size", "title_size", 30, 160, 130)
        self.slider(c, "Position", "title_y", 0.03, 0.7, 67, "{:.2f}")
        self.slider(c, "Show for (s, 0=all)", "title_dur", 0, 15, 30, "{:.1f}")
        self.switch(c, "Background box", "title_box")
        r = self.row(c)
        self.color_btn(r, "title_color", "Text")
        self.color_btn(r, "title_box_color", "Box")
        c = self.card(p, "Watermark / handle")
        self.entry(c, "Text", "mark")
        self.menu(c, "Corner", "mark_pos", list(MARK_POS))
        self.slider(c, "Size", "mark_size", 20, 90, 70)
        self.slider(c, "Opacity", "mark_op", 0.1, 1, 18, "{:.2f}")

    def _tab_output(self, p):
        c = self.card(p, "Encoding")
        self.menu(c, "Encoder", "encoder", list(self.enc_map))
        self.hint(c, "GPU encoders are much faster. If one fails, the app falls back to CPU automatically.")
        self.menu(c, "Speed", "speed", ["Fast", "Balanced", "Best"])
        self.slider(c, "Quality (CRF)", "crf", 14, 30, 16)
        self.hint(c, "Lower CRF = better quality, bigger files.")
        self.menu(c, "Frame rate", "fps", ["Source", "24", "30", "60"])
        c = self.card(p, "Audio")
        self.switch(c, "Normalize loudness (-16 LUFS)", "loudnorm")
        self.menu(c, "Bitrate", "abitrate", ["128k", "192k", "256k", "320k"])
        c = self.card(p, "Files")
        self.entry(c, "File name", "name_tpl")
        self.hint(c, "Tokens: {title} {n} {n:02d} {start} {end} {label}")
        self.switch(c, "Save .srt subtitle file", "save_srt")
        self.switch(c, "Save thumbnail (.jpg)", "save_thumb")
        self.switch(c, "Open folder when done", "open_folder")
        self.switch(c, "Auto-render right after loading", "auto_render")
        self.switch(c, "Keep downloaded video cache", "keep_cache")
        ctk.CTkButton(c, text="Clear cache", height=30, fg_color=FIELD, hover_color=HOVER,
                      command=self.clear_cache).pack(fill="x", padx=14, pady=(2, 12))

    # --------------------------------------------------------------- presets
    def apply_preset(self, name):
        p = PRESETS.get(name) or self.user_presets.get(name)
        if not p:
            return
        self._ready = False
        for k, v in p.items():
            if k in self.opt:
                try:
                    self.opt[k].set(v)
                except Exception:
                    pass
        self._ready = True
        self.schedule_preview()

    def _refresh_preset_menu(self):
        self.preset_menu.configure(values=list(PRESETS) + list(self.user_presets))

    def save_preset(self):
        name = ctk.CTkInputDialog(text="Preset name:", title="Save subtitle preset").get_input()
        if not name or not name.strip():
            return
        name = name.strip()
        self.user_presets[name] = {k: self.opt[k].get() for k in STYLE_KEYS}
        self._refresh_preset_menu()
        self.preset_menu.set(name)
        self.log(f"Saved preset '{name}'.")

    def del_preset(self):
        n = self.preset_menu.get()
        if n in self.user_presets:
            del self.user_presets[n]
            self._refresh_preset_menu()
            self.preset_menu.set("Classic Yellow")
        else:
            self.log("Built-in presets can't be deleted.")

    # ------------------------------------------------------------- dialogs
    def _paste_url(self):
        try:
            t = self.clipboard_get().strip()
        except Exception:
            return
        if t:
            self.url.delete(0, "end")
            self.url.insert(0, t)

    def _pick_file(self):
        f = filedialog.askopenfilename(filetypes=[("Video", "*.mp4 *.mkv *.mov *.webm *.avi *.m4v"), ("All", "*.*")])
        if f:
            self.url.delete(0, "end")
            self.url.insert(0, f)

    def _pick_dir(self):
        d = filedialog.askdirectory()
        if d:
            self.opt["out_dir"].set(d)

    def _pick_font(self):
        f = filedialog.askopenfilename(filetypes=[("Fonts", "*.ttf *.otf"), ("All", "*.*")])
        if f:
            self.opt["font_file"].set(f)

    def _sync_dir(self):
        self.dir_btn.configure(text="Folder: " + self.opt["out_dir"].get())

    def _on_mode(self):
        if not hasattr(self, "manual_card"):
            return
        if self.opt["mode"].get() == "Manual ranges":
            self.manual_hint.pack(anchor="w", padx=14)
            self.manual.pack(fill="x", padx=14, pady=(0, 12))
        else:
            self.manual_hint.pack_forget()
            self.manual.pack_forget()

    def open_out(self):
        d = self.opt["out_dir"].get()
        os.makedirs(d, exist_ok=True)
        open_path(os.path.join(d, self.title_text) if self.title_text and os.path.isdir(os.path.join(d, self.title_text)) else d)

    def clear_cache(self):
        try:
            shutil.rmtree(CACHE_DIR, ignore_errors=True)
            self.log("Cache cleared (downloads and transcripts will be fetched again).")
        except Exception as e:
            self.log(f"Could not clear cache: {e}")

    # --------------------------------------------------------- messaging
    def log(self, msg):
        self.q.put(("log", msg))

    def prog(self, frac, text=None):
        self.q.put(("prog", (frac, text)))

    def busy(self, flag):
        self._busy = flag
        self._update_buttons()

    def do_cancel(self):
        self.cancel.set()
        self.log("Cancelling...")

    def _poll(self):
        try:
            while True:
                kind, v = self.q.get_nowait()
                if kind == "prog":
                    self.bar.set(min(max(v[0], 0), 1))
                    self.pct.configure(text=f"{min(max(v[0], 0), 1) * 100:.0f}%")
                    if v[1]:
                        self.status.configure(text=v[1], text_color=OK if v[1].startswith("Done") else DIM)
                elif kind == "done":
                    self._rendering = False
                    self.busy(False)
                elif kind == "loaded":
                    self._on_loaded(v)
                elif kind == "thumb":
                    self._apply_thumb(v)
                elif kind == "autorender":
                    self.start_render()
                elif kind == "preview":
                    self._show_preview(v)
                elif kind == "pverr":
                    self.status.configure(text="Preview: " + v[:100], text_color="#FF9B6B")
                    self.pv_state.configure(text="\u25CF error", text_color=BAD)
                elif kind == "play":
                    open_path(v)
                elif kind == "playdone":
                    self.play_btn.configure(state="normal", text="\u25B6 Play 8s preview")
                else:
                    msg = str(v).strip()
                    bad = msg.lower().startswith("error") or "yt-dlp error" in msg.lower() or "ffmpeg failed" in msg.lower()
                    self.status.configure(text=msg[:110], text_color=BAD if bad else DIM)
                    if bad:
                        self._show_log(True)
                    self.log_box.configure(state="normal")
                    self.log_box.insert("end", v + "\n")
                    self.log_box.see("end")
                    self.log_box.configure(state="disabled")
        except queue.Empty:
            pass
        self.after(100, self._poll)

    # ------------------------------------------------------------ loading
    def start_load(self):
        if self._busy:
            return
        url = self.url.get().strip()
        if not url:
            return self.log("Paste a YouTube link or choose a local file first.")
        if not FFMPEG:
            return self.log("ffmpeg not found. Run: pip install imageio-ffmpeg  (then restart)")
        o = self.collect()
        self.cancel.clear()
        self.bar.set(0)
        self._rendering = False
        self.busy(True)
        threading.Thread(target=self._load_worker, args=(o, url), daemon=True).start()

    def _load_worker(self, o, url):
        try:
            s = prepare_source(url, o, self.log, self.prog, self.cancel)
            info = probe(s["src"])
            self.log(f"Loaded: {s['title']} ({info['duration'] / 60:.1f} min, {info['w']}x{info['h']})")
            if o.get("transcribe", True):
                lang = (o["lang"] or "auto").strip() or "auto"
                task = "tr" if o["task"].startswith("Translate") else "tx"
                cache = os.path.join(s["dir"], f"words_v2_{o['model']}_{lang}_{task}_{int(o['vad'])}_{int(o['music'])}.json")
                words = transcribe(s["src"], o, self.log, info["duration"], self.prog, self.cancel, cache)
            else:
                cache, words = None, []
                self.log("Transcription skipped by choice. Subtitles and speech-based smart cuts are disabled for this load.")
            clips = self._build_clips(o, info["duration"], words, s["title"])
            self.log(f"Prepared {len(clips)} output segment(s).")
            self.prog(1, "Loaded - review the clips, tweak the style, then render.")
            self.q.put(("loaded", dict(s, info=info, words=words, clips=clips, cache=cache)))
            if o["auto_render"]:
                self.q.put(("autorender", None))
        except Cancelled:
            self.log("Cancelled.")
        except Exception as e:
            import traceback
            self.log(f"Error: {e}")
            self.log(traceback.format_exc())
        finally:
            self.q.put(("done", None))

    def _build_clips(self, o, duration, words, title=""):
        """Create clip ranges, or one full-length range when auto-clipping is disabled."""
        if not o.get("auto_clip", True):
            if duration <= 0:
                return []
            return [dict(start=0.0, end=float(duration), on=True,
                         label=clean_name(title, 70) or "Full video", score=100)]
        if not words and o.get("mode") == "Smart sentences":
            self.log("No transcript available: Smart sentences uses fixed-length ranges instead.")
        return plan_clips(o, duration, words, self.log)

    def _on_loaded(self, d):
        self.src, self.title_text, self.src_dir, self.local = d["src"], d["title"], d["dir"], d["local"]
        self.info, self.dur, self.words, self.clips = d["info"], d["info"]["duration"], d["words"], d["clips"]
        self.words_cache = d["cache"]
        self.sel = 0 if self.clips else None
        self.refresh_clips()
        self.schedule_preview()
        self.sub_lbl.configure(text=self.title_text[:90])
        self.chip_vid.configure(text=f"{self.dur / 60:.1f} min  |  {self.info['w']}x{self.info['h']}")
        self._update_buttons()
        self._start_thumbs()
        self.save_config()

    def replan(self):
        if not self.src:
            return self.log("Load a video first.")
        o = self.collect()
        self.clips = self._build_clips(o, self.dur, self.words, self.title_text)
        self.sel = 0 if self.clips else None
        self.refresh_clips()
        self.schedule_preview()
        self.log(f"Re-planned: {len(self.clips)} clips.")
        self._start_thumbs()

    # --------------------------------------------------------- clip list
    def refresh_clips(self):
        for w in self.rows_frame.winfo_children():
            w.destroy()
        self._rows = []
        if not self.clips:
            ctk.CTkLabel(self.rows_frame, justify="left", text_color=DIM, text=(
                "No output segments yet.\n\n1.  Source tab: paste a YouTube link (or pick a video file)\n"
                "2.  Choose whether to transcribe and/or auto-split under Workflow\n"
                "3.  Load the source, review the preview and style\n4.  Render selected shorts or the full video")).pack(padx=20, pady=40)
        for i, c in enumerate(self.clips):
            self._make_row(i, c)
        self._update_summary()
        self._update_tx()

    def _thumb_image(self, path):
        from PIL import Image
        im = Image.open(path).convert("RGB")
        im.load()
        return ctk.CTkImage(light_image=im, dark_image=im, size=(76, 43))

    def _make_row(self, i, c):
        row = ctk.CTkFrame(self.rows_frame, fg_color=SEL if i == self.sel else FIELD, corner_radius=8,
                           border_width=1, border_color=ACCENT if i == self.sel else FIELD)
        row.pack(fill="x", pady=2, padx=2)
        var = tk.BooleanVar(value=c["on"])
        ctk.CTkCheckBox(row, text="", width=24, checkbox_width=18, checkbox_height=18, variable=var, fg_color=ACCENT,
                        hover_color=ACCENT, command=lambda i=i, v=var: self._toggle(i, v)).pack(
            side="left", padx=(8, 4), pady=4)
        th = ctk.CTkLabel(row, text="", width=76, height=43, fg_color="#10131A", corner_radius=6)
        th.pack(side="left", padx=(0, 6), pady=4)
        tp = self._thumbs.get((self.src, round(c["start"], 1)))
        if tp:
            try:
                th.configure(image=self._thumb_image(tp))
            except Exception:
                pass
        num = ctk.CTkLabel(row, text=str(i + 1), width=24, text_color=DIM)
        num.pack(side="left")
        es = ctk.CTkEntry(row, width=76, height=26)
        es.insert(0, fmt_ts(c["start"]))
        es.pack(side="left", padx=2)
        ee = ctk.CTkEntry(row, width=76, height=26)
        ee.insert(0, fmt_ts(c["end"]))
        ee.pack(side="left", padx=2)
        dl = ctk.CTkLabel(row, text=f"{c['end'] - c['start']:.0f}s", width=40, text_color=DIM)
        dl.pack(side="left")
        sc = ctk.CTkLabel(row, text=f"\u2605{c['score']}", width=48, text_color="#FFD400")
        sc.pack(side="left")
        lb = ctk.CTkLabel(row, text=c["label"][:70], anchor="w", text_color="#C9D0E0")
        lb.pack(side="left", fill="x", expand=True, padx=6)
        for w in (row, th, num, dl, sc, lb):
            w.bind("<Button-1>", lambda e, i=i: self.select(i))
        for ent in (es, ee):
            ent.bind("<Return>", lambda e, i=i: self._commit(i))
            ent.bind("<FocusOut>", lambda e, i=i: self._commit(i))
            ent.bind("<FocusIn>", lambda e, i=i: self.select(i))
        self._rows.append(dict(frame=row, s=es, e=ee, d=dl, l=lb, sc=sc, th=th))

    def _start_thumbs(self):
        if not self.src or not FFMPEG:
            return
        threading.Thread(target=self._thumb_worker, args=(self.src, [c["start"] for c in self.clips]),
                         daemon=True).start()

    def _thumb_worker(self, src, starts):
        d = os.path.join(self.tmp, "th")
        os.makedirs(d, exist_ok=True)
        for t in starts:
            key = (src, round(t, 1))
            if key in self._thumbs:
                continue
            out = os.path.join(d, "t_%d.jpg" % (abs(hash(key)) % 10 ** 9))
            try:
                _run([FFMPEG, "-y", "-loglevel", "error", "-ss", f"{t + 1:.2f}", "-i", src, "-frames:v", "1",
                      "-vf", "scale=-2:86", "-q:v", "5", out], capture_output=True, check=True)
                self._thumbs[key] = out
                self.q.put(("thumb", key))
            except Exception:
                pass

    def _apply_thumb(self, key):
        path = self._thumbs.get(key)
        if not path:
            return
        for i, c in enumerate(self.clips):
            if i < len(self._rows) and (self.src, round(c["start"], 1)) == key:
                try:
                    self._rows[i]["th"].configure(image=self._thumb_image(path))
                except Exception:
                    pass

    def _toggle(self, i, var):
        self.clips[i]["on"] = bool(var.get())
        self._update_summary()

    def _set_all(self, on):
        for c in self.clips:
            c["on"] = on
        self.refresh_clips()

    def _update_summary(self):
        n = sum(1 for c in self.clips if c["on"])
        tot = sum(c["end"] - c["start"] for c in self.clips if c["on"])
        whole = not self.opt["auto_clip"].get()
        self.clips_title.configure(text="OUTPUT" if whole else "CLIPS")
        self.summary.configure(text=(f"Whole video  |  {tot / 60:.1f} min" if (whole and self.clips) else
                                     f"{n} of {len(self.clips)} selected  |  {tot / 60:.1f} min total"))

    def select(self, i):
        if i == self.sel or i >= len(self.clips):
            return
        self.sel = i
        for k, r in enumerate(self._rows):
            r["frame"].configure(fg_color=SEL if k == i else FIELD, border_color=ACCENT if k == i else FIELD)
        self._update_tx()
        self.schedule_preview()

    def _update_tx(self):
        self.tx.configure(state="normal")
        self.tx.delete("1.0", "end")
        self._tx_t, self._tx_w, self._tx_spans = [], [], []
        if self.sel is not None and self.sel < len(self.clips):
            c = self.clips[self.sel]
            pos, parts = 0, []
            for a, b, t in self.words:
                if b > c["start"] and a < c["end"]:
                    self._tx_t.append(a)
                    self._tx_w.append(t)
                    self._tx_spans.append((pos, pos + len(t)))
                    parts.append(t)
                    pos += len(t) + 1
            self.tx.insert("1.0", " ".join(parts) or ("No transcript for this video." if not self.words else ""))
        self.tx.configure(state="disabled")
        self._refresh_timeline()
        self._on_scrub()

    def _commit(self, i):
        if i >= len(self._rows) or i >= len(self.clips):
            return
        r, c = self._rows[i], self.clips[i]
        try:
            a, b = parse_ts(r["s"].get()), parse_ts(r["e"].get())
            if self.dur:
                b = min(b, self.dur)
            a = max(0.0, a)
            if b - a < 1:
                raise ValueError
        except ValueError:
            a, b = c["start"], c["end"]
        changed = abs(a - c["start"]) > 1e-3 or abs(b - c["end"]) > 1e-3
        c["start"], c["end"] = a, b
        for ent, v in ((r["s"], a), (r["e"], b)):
            ent.delete(0, "end")
            ent.insert(0, fmt_ts(v))
        r["d"].configure(text=f"{b - a:.0f}s")
        if changed:
            c["label"] = clip_label(self.words, a, b)
            r["l"].configure(text=c["label"][:60])
            self._update_summary()
            self._update_tx()
            if i == self.sel:
                self.schedule_preview()

    def _cur_time(self):
        c = self.clips[self.sel]
        return c["start"] + self.pv_pos.get() * (c["end"] - c["start"])

    def add_clip(self):
        if not self.src:
            return self.log("Load a video first.")
        t = self._cur_time() if self.sel is not None else 0.0
        L = float(self.opt["clip_len"].get())
        a, b = t, min(self.dur, t + L)
        self.clips.append(dict(start=a, end=b, on=True, label=clip_label(self.words, a, b), score=50))
        self.clips.sort(key=lambda c: c["start"])
        self.sel = next(i for i, c in enumerate(self.clips) if c["start"] == a)
        self.refresh_clips()
        self.schedule_preview()

    def split_clip(self):
        if self.sel is None:
            return
        c, t = self.clips[self.sel], self._cur_time()
        if t - c["start"] < 2 or c["end"] - t < 2:
            return self.log("Move the preview slider to where you want to split (at least 2s from the ends).")
        n = dict(c, start=t, label=clip_label(self.words, t, c["end"]))
        c["end"], c["label"] = t, clip_label(self.words, c["start"], t)
        self.clips.insert(self.sel + 1, n)
        self.refresh_clips()
        self.schedule_preview()

    def merge_clip(self):
        if self.sel is None or self.sel + 1 >= len(self.clips):
            return
        a, b = self.clips[self.sel], self.clips.pop(self.sel + 1)
        a["end"] = b["end"]
        a["label"] = clip_label(self.words, a["start"], a["end"])
        self.refresh_clips()
        self.schedule_preview()

    def del_clip(self):
        if self.sel is None:
            return
        self.clips.pop(self.sel)
        self.sel = min(self.sel, len(self.clips) - 1) if self.clips else None
        self.refresh_clips()
        self.schedule_preview()

    def apply_fix(self):
        f, r = self.fix_find.get().strip(), self.fix_repl.get()
        if not f or not self.words:
            return self.log("Load a video and type the word to fix first.")
        pat, n, new = re.compile(re.escape(f), re.I), 0, []
        for a, b, t in self.words:
            t2 = pat.sub(r, t)
            n += t2 != t
            new.append((a, b, t2))
        self.words = new
        if self.words_cache:
            try:
                with open(self.words_cache, "w", encoding="utf-8") as fh:
                    json.dump(self.words, fh, ensure_ascii=False)
            except Exception:
                pass
        self.log(f"Replaced '{f}' in {n} place(s).")
        self._update_tx()
        self.schedule_preview()

    # ------------------------------------------------------------- preview
    def schedule_preview(self, *_):
        if not self._ready or not FFMPEG:
            return
        if self._pv_after:
            try:
                self.after_cancel(self._pv_after)
            except Exception:
                pass
        self._pv_after = self.after(220, self._preview_now)

    def _pv_cfg(self):
        o = self.collect()
        W, H = FORMATS[o["fmt"]]
        c = self.clips[self.sel] if (self.sel is not None and self.sel < len(self.clips) and self.src) else None
        pos = self.pv_pos.get()
        if c:
            self.pv_info.configure(text=f"Clip {self.sel + 1} of {len(self.clips)}   |   {fmt_ts(c['start'])} - "
                                        f"{fmt_ts(c['end'])}   |   {c['end'] - c['start']:.0f}s   |   \u2605{c['score']}")
        else:
            self.pv_info.configure(text="Sample preview - load a video to see your footage")
        self._on_scrub()
        self._refresh_timeline()
        return dict(o=o, pv=fit(W, H, *self.pv_box), src=self.src if c else None,
                    clip=(c["start"], c["end"]) if c else None, label=c["label"] if c else "", words=self.words, pos=pos)

    def _preview_now(self):
        self._pv_after = None
        cfg = self._pv_cfg()
        self.pv_state.configure(text="\u25CF updating", text_color=WARN)
        with self._pv_lock:
            if self._pv_busy:
                self._pv_pending = cfg
                return
            self._pv_busy = True
        threading.Thread(target=self._pv_worker, args=(cfg,), daemon=True).start()

    def _pv_worker(self, cfg):
        while True:
            try:
                self._pv_seq += 1
                self.q.put(("preview", (make_preview(cfg, os.path.join(self.tmp, "pv"), self._frame_cache,
                                                      self._pv_seq), cfg["o"]["fmt"], cfg["o"]["safe"])))
            except Exception as e:
                self.q.put(("pverr", str(e)))
            with self._pv_lock:
                cfg, self._pv_pending = self._pv_pending, None
                if cfg is None:
                    self._pv_busy = False
                    return

    def _show_preview(self, v):
        path, fmt, safe = v
        try:
            from PIL import Image
            img = Image.open(path).convert("RGB")
            img.load()
            try:
                os.remove(path)
            except OSError:
                pass
            if safe:
                img = draw_safe(img, fmt)
            self._pv_base = img
            self._redraw_preview()
            self.pv_state.configure(text="\u25CF live", text_color=OK)
        except Exception as e:
            self.status.configure(text=f"Preview error: {e}", text_color="#FF9B6B")

    # ------------------------------------------------ preview: guides + dragging
    def _pv_handles(self):
        hs = []
        if self.opt["transcribe"].get():
            hs.append(("sub", float(self.opt["pos_y"].get()), "Subtitles", (124, 92, 255)))
        if (self.opt["title"].get() or "").strip():
            hs.append(("title", float(self.opt["title_y"].get()), "Title", (255, 184, 77)))
        return hs

    def _redraw_preview(self):
        base = self._pv_base
        if base is None:
            return
        img = base
        if self._pv_hover or self._pv_drag:
            hs = self._pv_handles()
            if hs:
                try:
                    img = draw_guides(base, hs, self._pv_drag or self._pv_hot)
                except Exception:
                    img = base
        self._pv_img = ctk.CTkImage(light_image=img, dark_image=img, size=img.size)
        self.pv_label.configure(image=self._pv_img, text="")

    def _pv_frac(self, e):
        """Mouse y -> fraction (0..1) of the displayed preview image height."""
        if self._pv_base is None:
            return None
        lbl = getattr(self.pv_label, "_label", self.pv_label)
        ih = max(1.0, self._pv_base.size[1] * self._scale())
        top = (lbl.winfo_height() - ih) / 2.0
        return min(1.0, max(0.0, (e.y_root - lbl.winfo_rooty() - top) / ih))

    def _pv_nearest(self, f):
        hs = self._pv_handles()
        if f is None or not hs:
            return None
        return min(hs, key=lambda h: abs(h[1] - f))[0]

    def _pv_motion(self, e):
        if self._pv_leave_job:
            try:
                self.after_cancel(self._pv_leave_job)
            except Exception:
                pass
            self._pv_leave_job = None
        hot = self._pv_nearest(self._pv_frac(e))
        if not self._pv_hover or hot != self._pv_hot:
            self._pv_hover, self._pv_hot = True, hot
            self._redraw_preview()

    def _pv_leave(self, e):
        if self._pv_leave_job:
            return
        self._pv_leave_job = self.after(90, self._pv_hide_guides)

    def _pv_hide_guides(self):
        self._pv_leave_job = None
        if not self._pv_drag:
            self._pv_hover, self._pv_hot = False, None
            self._redraw_preview()

    def _pv_press(self, e):
        f = self._pv_frac(e)
        self._pv_drag = self._pv_nearest(f)
        if self._pv_drag:
            self._pv_move(f)

    def _pv_drag_move(self, e):
        if self._pv_drag:
            self._pv_move(self._pv_frac(e))

    def _pv_move(self, f):
        if f is None:
            return
        if self._pv_drag == "sub":
            self.opt["pos_y"].set(round(min(0.97, max(0.30, f)), 2))
        else:
            self.opt["title_y"].set(round(min(0.70, max(0.03, f)), 2))
        self._redraw_preview()

    def _pv_release(self, e):
        if self._pv_drag:
            self._pv_drag = None
            self._redraw_preview()
            self.save_config()

    # ------------------------------------------- preview: transcript position
    def _clip_now(self):
        return self.clips[self.sel] if (self.sel is not None and self.sel < len(self.clips)) else None

    def _on_scrub(self):
        """Cheap, immediate feedback while the preview position moves (the still itself re-renders debounced)."""
        if not hasattr(self, "tl"):
            return
        self._draw_playhead()
        c = self._clip_now()
        if not c:
            self.pv_time.configure(text="")
            self.pv_word.configure(text="")
            return
        t = self._cur_time()
        self.pv_time.configure(text=fmt_ts(t))
        if self._tx_t:
            i = max(0, word_index_at(self._tx_t, t))
            self.pv_word.configure(text=f"\u201C{self._tx_w[i]}\u201D    {i + 1} / {len(self._tx_t)}")
        else:
            self.pv_word.configure(text="no transcript")
        self._hilite_tx()

    def _hilite_tx(self):
        try:
            tw = getattr(self.tx, "_textbox", self.tx)
            tw.tag_remove("cur", "1.0", "end")
            c = self._clip_now()
            if not c or not self._tx_t:
                return
            i = max(0, word_index_at(self._tx_t, self._cur_time()))
            a, b = self._tx_spans[i]
            tw.tag_add("cur", f"1.0+{a}c", f"1.0+{b}c")
            tw.see(f"1.0+{a}c")
        except Exception:
            pass

    def _tx_click(self, e):
        c = self._clip_now()
        if not c or not self._tx_t:
            return
        try:
            tw = getattr(self.tx, "_textbox", self.tx)
            col = int(tw.index(f"@{e.x},{e.y}").split(".")[1])
        except Exception:
            return
        i = max(0, bisect.bisect_right([s for s, _ in self._tx_spans], col) - 1)
        self._seek_abs(self._tx_t[i])

    def _seek_abs(self, t):
        c = self._clip_now()
        if c:
            self.pv_pos.set(min(0.999, max(0.0, (t - c["start"]) / max(c["end"] - c["start"], 0.1))))

    def step_word(self, d):
        c = self._clip_now()
        if not c:
            return
        t = self._cur_time()
        if self._tx_t:
            inside = [x for x in self._tx_t if x >= c["start"]]
            cand = [x for x in inside if x > t + 0.03] if d > 0 else [x for x in inside if x < t - 0.03]
            if not cand:
                return
            self._seek_abs(cand[0] if d > 0 else cand[-1])
        else:
            self._seek_abs(t + d * 1.0)

    def _tl_seek(self, e):
        self.pv_pos.set(min(0.999, max(0.0, e.x / max(self.tl.winfo_width(), 1))))

    def _draw_playhead(self):
        cv = self.tl
        cv.delete("ph")
        w = cv.winfo_width()
        if w < 20 or not self._clip_now():
            return
        x = max(1, min(w - 1, self.pv_pos.get() * w))
        cv.create_line(x, 3, x, TL_H - 3, fill="#FFFFFF", width=2, tags="ph")
        cv.create_polygon(x - 5, 0, x + 5, 0, x, 7, fill="#FFFFFF", outline="", tags="ph")

    def _refresh_timeline(self):
        cv = self.tl
        w = cv.winfo_width()
        if w < 20:
            return
        cv.delete("all")
        c = self._clip_now()
        if not c:
            cv.create_text(w / 2, TL_H / 2, text="Select a clip to see its speech timeline", fill=DIM, font=("Arial", 9))
            return
        o = self.collect()
        dur = max(c["end"] - c["start"], 0.1)

        def X(t):
            return max(0.0, min(float(w), t / dur * w))
        cv.create_rectangle(0, 12, w, 30, fill="#171B24", outline="")
        rel = [(max(0.0, a - c["start"]), min(b - c["start"], dur), t) for a, b, t in self.words
               if b > c["start"] and a < c["end"]]
        if rel:
            for g in layout(rel, o):
                cv.create_rectangle(X(g["start"]), 12, max(X(min(g["end"], dur)), X(g["start"]) + 2), 30,
                                    fill=SEL, outline="")
            for a, b, _ in rel:
                cv.create_rectangle(X(a), 15, max(X(b), X(a) + 1.5), 27, fill=ACCENT, outline="")
            if o["silence_cut"] and o["transcribe"]:
                last = 0.0
                for a, b in (keep_segments(rel, dur, float(o["silence_gap"])) or []) + [(dur, dur)]:
                    if a - last > 0.05:
                        cv.create_rectangle(X(last), 32, X(a), 38, fill=BAD, outline="")
                    last = max(last, b)
        else:
            cv.create_text(w / 2, 21, text="no transcript in this clip", fill=DIM, font=("Arial", 9))
        if (o["title"] or "").strip():
            td = float(o["title_dur"])
            cv.create_rectangle(0, 5, X(dur if td <= 0 else min(dur, td)), 9, fill=WARN, outline="")
        cv.create_text(3, 44, anchor="w", text="0:00", fill=DIM, font=("Arial", 8))
        cv.create_text(w - 3, 44, anchor="e", text=fmt_ts(dur), fill=DIM, font=("Arial", 8))

    def play_preview(self):
        if not self.src or self.sel is None:
            return self.log("Load a video and select a clip first.")
        o, c = self.collect(), dict(self.clips[self.sel])
        words, info, src = list(self.words), dict(self.info), self.src
        self.play_btn.configure(state="disabled", text="Rendering...")
        threading.Thread(target=self._play_worker, args=(o, c, words, info, src), daemon=True).start()

    def _play_worker(self, o, c, words, info, src):
        try:
            work = os.path.join(self.tmp, "play")
            os.makedirs(work, exist_ok=True)
            W, H = fit(*out_size(o["fmt"], "720p"), 540, 960)
            out = os.path.join(work, "preview_%d.mp4" % int(time.time()))
            render_clip(o, src, info, words, (c["start"], c["end"]), c["label"], out, work, W, H, self.log,
                        lambda f: None, threading.Event(), preview=True)
            self.q.put(("play", out))
        except Exception as e:
            self.log(f"Preview error: {str(e)[-300:]}")
        finally:
            self.q.put(("playdone", None))

    # -------------------------------------------------------------- render
    def start_render(self):
        if not self.src:
            return self.log("Load a video first (Source tab).")
        clips = [dict(c) for c in self.clips if c["on"]]
        if not clips:
            return self.log("No clips selected.")
        o = self.collect()
        # If this source was loaded without a transcript, don't generate empty captions/SRT files.
        if not self.words:
            o["burn"] = False
            o["save_srt"] = False
            o["silence_cut"] = False
        self.cancel.clear()
        self.bar.set(0)
        self._rendering = True
        self.busy(True)
        threading.Thread(target=self._render_worker, args=(o, clips, self.src, self.title_text, dict(self.info),
                                                           list(self.words)), daemon=True).start()
        self.save_config()

    def _render_worker(self, o, clips, src, title, info, words):
        work = tempfile.mkdtemp(prefix="shorts_")
        try:
            W, H = out_size(o["fmt"], o["res"])
            out = os.path.join(o["out_dir"], title or "video")
            os.makedirs(out, exist_ok=True)
            n, t0 = len(clips), time.time()
            self.log(f"Rendering {n} shorts at {W}x{H} ({o['encoder']})...")
            for i, c in enumerate(clips, 1):
                if self.cancel.is_set():
                    raise Cancelled()
                try:
                    name = o["name_tpl"].format(title=title, n=i, start=int(c["start"]), end=int(c["end"]),
                                                label=clean_name(c["label"], 30))
                except Exception:
                    name = f"short_{i:02d}"
                name = clean_name(name, 120) or f"short_{i:02d}"
                path = os.path.join(out, name + ".mp4")

                def on_prog(f, i=i):
                    frac = ((i - 1) + f) / n
                    el = time.time() - t0
                    eta = f" - ETA {int(el * (1 - frac) / frac) // 60}:{int(el * (1 - frac) / frac) % 60:02d}" if frac > 0.03 else ""
                    self.prog(frac, f"Clip {i}/{n}  {f * 100:.0f}%{eta}")
                D = render_clip(o, src, info, words, (c["start"], c["end"]), c["label"], path, work, W, H, self.log,
                                on_prog, self.cancel)
                self.log(f"  [{i}/{n}] {name}.mp4 ({D:.0f}s)")
            self.log(f"Done! {n} shorts saved to: {out}")
            self.prog(1, "Done!")
            if o["open_folder"]:
                open_path(out)
        except Cancelled:
            self.log("Render cancelled.")
        except Exception as e:
            import traceback
            self.log(f"Error: {str(e)[-600:]}")
            self.log(traceback.format_exc()[-800:])
        finally:
            shutil.rmtree(work, ignore_errors=True)
            self.q.put(("done", None))

    # --------------------------------------------------------------- config
    def save_config(self):
        d = {k: v.get() for k, v in self.opt.items()}
        d["manual"] = self.manual.get("1.0", "end")
        d["user_presets"] = self.user_presets
        save_config(d)

    def _load_config(self):
        d = load_config()
        self._ready = False
        for k, v in d.items():
            if k in self.opt:
                try:
                    self.opt[k].set(v)
                except Exception:
                    pass
        self._ready = True
        self.user_presets = d.get("user_presets", {}) or {}
        self._refresh_preset_menu()
        if d.get("manual"):
            self.manual.delete("1.0", "end")
            self.manual.insert("1.0", d["manual"])
        if self.opt["encoder"].get() not in self.enc_map:
            self.opt["encoder"].set(list(self.enc_map)[0])

    def _close(self):
        try:
            self.save_config()
            if self.src_dir and not self.local and not self.opt["keep_cache"].get():
                shutil.rmtree(self.src_dir, ignore_errors=True)
            shutil.rmtree(self.tmp, ignore_errors=True)
        finally:
            self.destroy()


REQUIRED = [("customtkinter", "customtkinter"), ("yt_dlp", "yt-dlp"), ("faster_whisper", "faster-whisper"),
            ("imageio_ffmpeg", "imageio-ffmpeg"), ("PIL", "pillow"), ("numpy", "numpy"), ("deno", "deno")]


def ensure_packages():
    """Offer to pip-install anything missing into the Python that is running this script."""
    import importlib.util
    miss = [pip for mod, pip in REQUIRED if importlib.util.find_spec(mod) is None]
    if not miss:
        return
    print("\nMissing packages for this Python (%s):\n  %s" % (sys.executable, ", ".join(miss)))
    try:
        ans = input("Install them now? [Y/n] ").strip().lower()
    except EOFError:
        ans = "n"
    if ans in ("", "y", "yes"):
        if subprocess.call([sys.executable, "-m", "pip", "install", *miss]) == 0:
            print("\nInstalled. Restarting...\n")
            sys.exit(subprocess.call([sys.executable] + sys.argv))
        print("\npip failed - see the messages above.")
    else:
        print('Install manually with:\n  "%s" -m pip install %s' % (sys.executable, " ".join(miss)))
    sys.exit(1)


if __name__ == "__main__":
    ensure_packages()
    App().mainloop()
