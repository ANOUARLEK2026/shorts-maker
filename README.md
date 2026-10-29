Shorts Maker

Turn long-form videos into ready-to-post vertical shorts, with a desktop GUI, in a few clicks.

Shorts Maker downloads a video (or loads a local file), transcribes it with word-level timestamps, helps you pick the best clips, shows a live preview, and renders the final shorts with captions, titles and branding already applied.

Features
Flexible sources: paste a YouTube URL (via yt-dlp, with cookie support) or open a local video file. Downloads and transcripts are cached, so reloading the same video is fast.
Word-level transcription: powered by faster-whisper, with model choices from tiny to large-v3 and turbo.
Three clip-planning modes:
Fixed length: evenly sized clips
Smart sentences: cuts that respect sentence boundaries
Manual ranges: your own timestamps
Live preview and speech timeline: render a quick preview of any clip and see where speech, silences and the title fall on a timeline.
Animated subtitles: burned-in captions with several highlight styles (Color, Color + pop, Block, Karaoke sweep) and Pop or Fade animations. Right-to-left scripts such as Arabic are handled too. You can also export .srt files.
Multiple formats and resolutions: 9:16, 1:1, 4:5 and 16:9, at 720p, 1080p or 1440p.
Reframing options: blur-fit, crop-fill, or fit with bars.
Branding and polish: title overlay, watermark with corner placement, and a progress bar.
Silence jump-cuts and loudness normalization: tighter pacing and consistent audio.
Hardware-accelerated encoding: auto-detects NVIDIA NVENC, Intel QuickSync and AMD AMF, with CPU (x264) as fallback.
Presets and saved settings: save your own style presets, customize output file names with templates, and batch-render all selected clips with ETA tracking and cancel support.
How it works
yt-dlp (download, cached) → faster-whisper (word-level transcript, cached)
  → clip planner (fixed / smart sentences / manual) → live preview
  → ffmpeg (reframe, subtitles, title, watermark, progress bar, jump-cuts, loudness, HW encode)
Installation and usage
bash
pip install customtkinter yt-dlp faster-whisper imageio-ffmpeg pillow numpy deno
python shorts_maker.py

If any dependency is missing, the app offers to install it for you on first run. It uses a system FFmpeg if it includes libass, otherwise it falls back to the bundled imageio-ffmpeg build.

A few suggestions for the repo: add a requirements.txt, a screenshot or GIF of the UI at the top of the README, and a license file (MIT is a common choice). I can generate any of those, or a version of this README in another language, if you’d like.
