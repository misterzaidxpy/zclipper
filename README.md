# zclipper

A Claude Code plugin that turns YouTube, Twitch and Kick streams into vertical clips with burned-in captions.

zclipper does the measuring and the video work on your own computer. Claude does the judging: it picks the moments, tightens the cuts and writes the titles, captions and hashtags. No API keys and no per-clip credits.

## What it does

- **Finds moments in a finished stream or video** from YouTube's "most replayed" graph, or from bursts of fast speech and reactions in the captions.
- **Watches live streams** on Twitch, Kick and YouTube and cuts a clip when chat spikes.
- **Cuts only the seconds it needs**, so a 6 hour stream is not downloaded in full.
- **Renders 9:16 clips** at 1080x1920 by default: the facecam on top and gameplay below, or the gameplay cropped to fill the frame when there is no facecam. 4:5, 1:1 and 16:9 on request. Word-by-word captions and a thumbnail with every clip.
- **Falls back to local Whisper** when a video has no captions.

## Requirements

- Python 3.10 or newer
- ffmpeg on PATH (`winget install Gyan.FFmpeg` on Windows, `brew install ffmpeg` on macOS)
- `pip install -U yt-dlp pydantic curl_cffi`
- Optional, for videos without captions: `pip install faster-whisper numpy`

## Install

First install the requirements above. Then pick one of these.

**With the skills CLI** (installs the skill into Claude Code):

```
npx skills add misterzaidxpy/zclipper -a claude-code
```

**As a Claude Code plugin** (gets updates when a new version is released):

```
/plugin marketplace add misterzaidxpy/zclipper
/plugin install zclipper@zclipper
```

**Ask Claude to install it.** Paste this into Claude Code:

> Install the zclipper skill from https://github.com/misterzaidxpy/zclipper: clone the repository and copy its `skills/zclipper` folder to `~/.claude/skills/zclipper`.

**From a local clone, for development:**

```
claude --plugin-dir /path/to/zclipper
```

Restart Claude Code after installing. Then check the setup by asking "run the zclipper doctor".

## Use

Just ask:

- "Clip me 5 GTA 6 best moments"
- "Give me the best moments from IShowSpeed's livestream"
- "3 clips from https://www.youtube.com/watch?v=..., square format"
- "Watch kaicenat on Twitch for an hour and clip the chat spikes"

Claude finds the videos or the live stream, picks the moments, renders the clips and replies with each clip's file, title, caption, hashtags and thumbnail. Clips are 9:16 with the streamer's facecam on top and the gameplay below; ask for 4:5, 1:1 or 16:9 if you need another shape.

Set `ZCLIPPER_HOME` to choose where Whisper models and temporary files are kept.

## Status

Early (0.1.0). Run the tests with `python -m pytest` from the repository root.

## License

MIT
