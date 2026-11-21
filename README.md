# zclipper

<p align="center">
  <b>Ask Claude for the best moments of a stream. Get finished vertical clips back in chat.</b>
</p>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/github/license/misterzaidxpy/zclipper?style=flat-square&color=green" alt="License: MIT"></a>
  <img src="https://img.shields.io/badge/python-3.10%2B-yellow?style=flat-square&logo=python&logoColor=white" alt="Python 3.10+">
  <img src="https://img.shields.io/badge/Claude_Code-skill_%2B_plugin-D97757?style=flat-square" alt="Claude Code skill and plugin">
  <img src="https://img.shields.io/badge/sources-YouTube%20%7C%20Twitch%20%7C%20Kick-blue?style=flat-square" alt="YouTube, Twitch and Kick">
  <img src="https://img.shields.io/badge/API_keys-none-lightgrey?style=flat-square" alt="No API keys">
</p>

zclipper is a Claude skill that turns YouTube, Twitch and Kick streams into short vertical clips with word-by-word captions: the streamer's facecam on top, the gameplay below, ready for Shorts, TikTok and Reels. It finds the videos, measures where the best moments are, cuts only those seconds and renders the clips on your own computer. Claude does the judging: which moments are worth posting, where to cut, and the title, caption and hashtags for each clip.

No paid APIs, no credits, no upload to a clipping service.

## Table of Contents

- [How it works](#how-it-works)
- [What you get](#what-you-get)
- [Installation](#installation)
- [Usage](#usage)
- [What's inside](#whats-inside)
- [Commands](#commands)
- [Limits](#limits)
- [Updating](#updating)
- [Contributing](#contributing)
- [License](#license)

## How it works

You ask in plain words. The skill triggers on its own and Claude takes it from there:

1. **Find the source.** For a topic ("GTA 6") it searches YouTube for the most viewed long videos and past streams of the week. For a streamer it checks YouTube, Twitch and Kick at once to see where they are live, and lists their latest finished streams.
2. **Find the moments.** On YouTube, the "most replayed" graph and bursts of fast speech and reactions in the captions give a ranked list of candidates with what was said. On a live stream, zclipper reads the chat and marks the moments when it explodes; on Twitch and Kick it can also use the clips viewers already made.
3. **Pick and write.** Claude reads the candidates, keeps the ones a viewer with no context would still enjoy, tightens the cuts, and writes the post copy.
4. **Cut and render.** Only the chosen seconds are downloaded, so a 6 hour stream never lands on your disk. Each clip is rendered with captions and a thumbnail.
5. **Reply.** Claude answers in chat with every clip's file, title, caption, hashtags and thumbnail.

## What you get

Every clip is 1080x1920 by default:

```
+--------------------+
|                    |
|      facecam       |   the streamer's webcam, cropped and sharpened
|                    |
|== BIG CAPTIONS ====|   word-by-word, the spoken word in yellow
|                    |
|                    |
|     gameplay       |   cropped to fill, slid away from the webcam
|                    |   so the streamer never shows up twice
|                    |
+--------------------+
```

- **No facecam?** The gameplay fills the whole 9:16 frame, with a title bar on top.
- **Another shape?** Ask for 4:5, 1:1 or 16:9 and captions and thumbnails scale to fit.
- **With every clip:** a `.jpg` thumbnail with the punchline in big type, and `clips.json` with the title, caption, hashtags and transcript.

## Installation

### Requirements

- Python 3.10 or newer
- [ffmpeg](https://ffmpeg.org) on PATH: `winget install Gyan.FFmpeg` (Windows), `brew install ffmpeg` (macOS), `sudo apt install ffmpeg` (Linux)
- The Python packages:

  ```bash
  pip install -U yt-dlp pydantic curl_cffi
  ```

- Optional, for videos and live clips without captions (local speech-to-text):

  ```bash
  pip install faster-whisper numpy
  ```

### Claude Code

Pick one.

**Skills CLI**

```bash
npx skills add misterzaidxpy/zclipper -a claude-code
```

**Plugin marketplace** (updates arrive with new versions)

```
/plugin marketplace add misterzaidxpy/zclipper
/plugin install zclipper@zclipper
```

**Ask Claude to install it**

> Install the zclipper skill from https://github.com/misterzaidxpy/zclipper: clone the repository and copy its `skills/zclipper` folder to `~/.claude/skills/zclipper`.

Restart Claude Code, then ask **"run the zclipper doctor"** to check that everything is in place.

## Usage

Just ask. Some examples:

```
Clip me 5 GTA 6 best moments
```

```
Give me the best moments from IShowSpeed's livestream
```

```
3 clips from https://www.youtube.com/watch?v=..., square format
```

```
Watch kaicenat on Twitch for an hour and clip the chat spikes
```

```
Make clips from this VOD for my Vyro campaign, facecam on top
```

What to expect:

| You ask for | zclipper does |
|---|---|
| A topic or game | Clips the most viewed long videos about it from this week, at most two clips per video |
| A streamer who is live on Twitch or Kick | Renders the most viewed clips viewers made of the current stream |
| A streamer who is live on YouTube | Watches chat for 30 minutes (or as long as you say) and clips every burst |
| A streamer who is offline | Clips their latest finished stream |
| A video URL | Clips that video |

Defaults: 3 clips, 9:16, facecam on top. Say otherwise and it follows.

## What's inside

```
zclipper/
├── .claude-plugin/
│   ├── plugin.json          # plugin manifest
│   └── marketplace.json     # lets /plugin marketplace add find it
├── skills/zclipper/
│   ├── SKILL.md             # what Claude reads: the recipes, how to pick moments, the reply
│   └── scripts/
│       ├── zclipper.py      # command line, JSON out
│       └── zc/
│           ├── find.py      # topic search, where a streamer is live
│           ├── moments.py   # replay graph, caption bursts, cutting at pauses
│           ├── live.py      # YouTube, Twitch and Kick live checks, chat, replay cuts
│           ├── render.py    # ffmpeg layouts, captions, thumbnails
│           ├── clips.py     # the pipelines that tie it together
│           └── whisper.py   # local speech-to-text
└── tests/
```

The `skills/zclipper` folder is the whole skill. Copy it into `~/.claude/skills/` and it works without the plugin wrapper.

## Commands

Claude runs these for you; they are listed for debugging and for using zclipper without Claude. Every command prints JSON.

| Command | What it does |
|---|---|
| `zclipper.py doctor` | Checks Python, ffmpeg and the packages |
| `zclipper.py find "GTA 6" --count 5` | Most viewed recent videos on a topic |
| `zclipper.py find --streamer ishowspeed` | Where a streamer is live, and their latest streams |
| `zclipper.py moments <url>` | Candidate moments with what was said |
| `zclipper.py frame <url> --at 120` | One frame, to find the facecam |
| `zclipper.py make picks.json` | Renders the picked moments |
| `zclipper.py live <platform> <channel>` | Clips chat bursts on a live stream |
| `zclipper.py viewer-clips <platform> <channel> --make 3` | Renders the top viewer clips (Twitch, Kick) |
| `zclipper.py transcript <file>` | Word-timed transcript with local Whisper |

Useful options: `--aspect 9:16|4:5|1:1|16:9`, `--layout split|crop`, `--facecam x,y,w,h`, `--out <folder>`. Set `ZCLIPPER_HOME` to keep Whisper models and temporary files on a roomier drive.

## Limits

- **YouTube live:** YouTube gives nothing to rank the part of a stream that already happened, so live YouTube clipping starts from the moment you ask.
- **Replays off:** Twitch and Kick streamers who turn replays off cannot be cut after the fact.
- **Chat bursts** find loud moments, not always good ones. A raid or a giveaway can set one off, which is why Claude checks the clips.
- **Platform terms:** clip content you have the right to use, such as a campaign that pays for clips of that creator.

## Updating

- Plugin: `/plugin marketplace update zclipper`
- Skills CLI: run `npx skills add misterzaidxpy/zclipper -a claude-code` again

## Contributing

Issues and pull requests are welcome. Run the tests before opening a PR:

```bash
pip install pytest
python -m pytest
```

To try your changes in Claude Code without installing:

```bash
claude --plugin-dir /path/to/zclipper
```

## License

[MIT](LICENSE)
