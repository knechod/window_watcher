# WindowWatch  (W.01.00)

A small Windows tray-panel utility that flags activity in **background / hidden**
windows and plays an audio cue — so you stop missing the chat reply or message
that arrived in a window you weren't looking at.

## What it does

- Keeps a small **always-on-top status panel** listing the windows you've chosen
  to watch.
- A watched window's dot turns **orange ●** when activity happens *while the
  window is hidden or in the background*. Click the row to clear the flag.
- An **audio cue** plays **only** when the activity happened while that window
  was hidden (not when you're already looking at it).

## Two detection methods (both run together)

| Badge | Method | Good for | Cost |
|-------|--------|----------|------|
| **F** | **Flash** — listens for the OS "request attention" / taskbar-flash signal | Slack, Teams, Discord, most desktop apps | almost none |
| **P** | **Pixel** — screenshots the hidden window (via `PrintWindow`) every ~1.5s and diffs it | Browser chat tabs (ChatGPT/Claude) that don't flash | light CPU, only runs while the window is hidden |

When you add a window you can tick "Also pixel-watch" — leave it on for browser
chat tabs, turn it off for apps that already flash.

## Build the .exe

1. Install Python 3.10+ on Windows (you already have 3.12).
2. Open a command prompt **in this folder** and run:

   ```
   build.bat
   ```

3. The standalone app appears at **`dist\WindowWatch.exe`** — double-click to run,
   no installation required. Copy that .exe anywhere you like.

## Run from source instead (for tweaking)

```
pip install -r requirements.txt
python windowwatch.py
```

## Tuning

Open `windowwatch.py` and adjust the constants near the top:

- `PIXEL_POLL_SECONDS` — how often pixel-watched windows are captured (default 1.5)
- `PIXEL_DIFF_THRESHOLD` — how much must change to count as activity (default 0.004).
  Raise it if you get false positives from a blinking cursor; lower it if it misses
  small text changes.
- `AUDIO_FILE` — set to a path like `r"C:\Sounds\ding.wav"` for a custom sound,
  or leave `None` for the default system beep.
- `AUDIO_COOLDOWN_SECONDS` — minimum gap between repeat alerts for one window.

## Known limitations

- **Pixel capture can return blank** for some heavily GPU-accelerated windows
  (certain Chrome/Edge configurations). If a browser tab never flags, that's why —
  the flash method still covers apps, but a fully GPU-composited tab that also
  doesn't flash is the hard case. Workaround: keep that browser window partly
  visible, or watch the whole browser window rather than relying on the tab.
- Window handles (HWND) change when you close and reopen an app, so re-add a
  window after relaunching it. (A future version could match by title instead.)
- Watching many windows with pixel mode raises CPU use; prefer Flash where it works.

## Version

W.01.00 — initial build. Versioning: `W.MAJOR.MINOR`.
