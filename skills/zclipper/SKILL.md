---
name: zclipper
description: Make short vertical clips with captions from streams and videos. Use when the user asks to clip best moments of a topic or game ("clip me 5 GTA 6 best moments"), of a streamer or their livestream on YouTube, Twitch or Kick ("best moments from IShowSpeed's stream"), or of a specific video URL; or to make Shorts, TikToks or Reels from a stream.
version: 0.1.0
---

# zclipper

The user asks in plain words; you find the source, pick the moments, render the clips and answer in chat with the finished clips. The scripts measure and cut; you judge and write. Nothing calls a paid API.

`ZC` below means `python "<this skill's base directory>/scripts/zclipper.py"`. Every command prints JSON on stdout and progress or errors on stderr.

Run each command as one plain call with the full path written out: `python "<base dir>/scripts/zclipper.py" moments <url> --count 8`. No shell variables, no `&&` chains, no `for` loops and no `>` redirects: the user's permission rules can approve a plain `python` call, but not those, and a refused command wastes a turn. Read the JSON from the output directly. Independent commands, such as `moments` on three videos, can run as separate calls at the same time. `make` creates its output folder itself.

## Read the request

- **What:** a topic or game ("GTA 6"), a streamer ("IShowSpeed", "kaicenat"), or a URL.
- **How many clips:** use the number they give; otherwise 3.
- **Format:** 9:16 unless they ask for another (`--aspect 4:5`, `1:1` or `16:9`).
- **Live minutes** (streamer live on YouTube only): use what they say; otherwise 30.

Don't ask questions you can answer with a sensible default. Run `ZC doctor` once per session first; if it reports something missing, show the user its `fix` commands and stop.

Work in `./zclipper-out/<short-name>/` unless the user names a folder.

## Recipe A: a topic ("clip me 5 GTA 6 best moments")

1. `ZC find "GTA 6" --count 3` gives the most viewed long videos and past streams about the topic from this week (`--period today|week|month|year`).
2. For each video, `ZC moments <url> --count 8` gives candidate moments with their spoken text.
3. Pick the best moments across all videos (see "Picking moments"), at most two per video so the set has variety.
4. Find each video's facecam (see "Facecam"), write `picks.json`, run `ZC make picks.json`.
5. Answer in chat (see "The reply").

## Recipe B: a streamer ("best moments from IShowSpeed's livestream")

1. `ZC find --streamer ishowspeed` checks YouTube, Twitch and Kick under that name and lists their latest finished YouTube streams. If the platforms use different names, ask the user for the right one.
2. Then:
   - **Live on Twitch or Kick:** `ZC viewer-clips <platform> <name> --make <count> --facecam x,y,w,h` renders the most viewed clips viewers made of this stream. Done, go to the reply.
   - **Live on YouTube:** tell the user what you are doing ("Watching chat for 30 minutes, I will clip every burst"), then run `ZC live youtube <name> --minutes 30 --facecam x,y,w,h` in the background. It prints one JSON line per clip as chat bursts happen. When it ends, write copy for the clips (`ZC transcript <clip.mp4>` gives their words) and reply. If it made fewer clips than asked, say so; quiet streams make few bursts.
   - **Not live:** clip the newest stream in `recent_streams` the way Recipe A does, from step 2.
   - **Live on several platforms:** prefer Twitch or Kick (instant viewer clips) unless the user named one.

## A specific URL

YouTube video or past stream: `moments`, pick, `make`. Twitch or Kick VOD (`twitch.tv/videos/<id>`, Kick video URL): there is no replay graph to rank, so use moments the user names or viewer clips; `make` works with any of these URLs and captions come from local Whisper.

## Picking moments

Read each candidate's `text`. Choose what a viewer with no context would still enjoy: a clear setup and payoff, a strong reaction, a funny or surprising line, a chase or crash that goes wrong. Skip sponsor reads, donations read out, menus and loading screens, dead air, long quiet driving, and moments that need earlier context. Tighten `start` and `end` inside the candidate's range, never outside it; clips are 8 to 180 seconds, 20 to 45 is the sweet spot.

For each pick write:
- `title`: under 60 characters, says what happens, invents nothing the transcript does not show.
- `caption`: one punchline line under 70 characters in the style of viral clip pages: the streamer's short name, what happened with one key word in CAPS, then one or two emoji. For GTA 6: "Speed Tried to Fly a JET in GTA 6 ✈️😭", "Kai Cenat Got WASTED on a Plane in GTA 6 💀 (Wait for the End)".
- `hashtags`: 8 to 15 lowercase tags without `#`. GTA 6 clips include `gta6`, `gtavi`, `grandtheftauto6`, `gta6clips`, the streamer, `gaming`, `shorts` and the moment (`gta6funny`, `gta6fails`).
- `thumbnail_text`: 2 to 5 words, the punchline at its strongest, one key word in CAPS, no emoji.
- `thumbnail_at`: seconds from the start of your cut where the reaction peaks.

`picks.json` (each pick may carry its own `url` and `facecam` when picks come from several videos):
```json
{"url": "<default video url>", "picks": [
  {"url": "<optional>", "facecam": [0.74, 0.02, 0.24, 0.27], "start": 812.4, "end": 851.0, "title": "...", "caption": "...", "hashtags": ["..."], "thumbnail_text": "...", "thumbnail_at": 14.5}
]}
```

## Facecam

The default 9:16 clip puts the streamer's webcam on top and the gameplay below. For each source, grab one frame inside a pick and look at it:
```
ZC frame <url> --at <seconds> --out zclipper-out/<name>/frame.jpg
```
Estimate the webcam box as fractions of the frame, `x,y,width,height`. Box the person and keep the game HUD (minimap, health bars) out. If there is no webcam, give no box: the gameplay is then cropped to fill 9:16. For a live stream, grab the frame from the replay URL (Twitch `twitch.tv/videos/<vod_id>` from `find`), or skip the facecam for YouTube live.

## The reply

When the clips are done, answer in chat. One numbered entry per clip, best first:

```
1. **Speed Tried to Fly a JET in GTA 6 ✈️😭**
   ![thumbnail](<absolute path to .jpg>)
   Clip: [<file name>.mp4](<absolute path to .mp4>) (32 s, from <source title>, 1:23:05)
   Hashtags: #gta6 #gtavi ...
```

Show the thumbnail image where the client can display images; otherwise the link is enough. If the client offers a tool to send files to the user, also send the clips with it. Then one line on anything that went wrong (a pick that failed, fewer bursts than asked) and where the folder is. Keep it to that; no recap of the steps.

## Notes

- Follow a clipping campaign's brief (sources, length, required tags) over these defaults when the user mentions one.
- Clip content the user has the right to use, such as a campaign that pays for clips of that creator.
- Keep downloads and models off a nearly full drive: set `ZCLIPPER_HOME` and `--out` to a roomier drive when the user mentions low space.
- If a command fails twice the same way, show the user the error instead of retrying.
