# TAL Archive

A personal phone app for browsing and playing the This American Life archive by year, with resume and play history.

- Live app: https://atrain86.github.io/TAL-archive-/
- Episode list (`episodes.json`) is rebuilt by a GitHub Action twice a day (also runs on every push, and can be run manually from the Actions tab with a "full" re-read).
- Songs are matched to Spotify tracks by a gentle hourly job (100 songs an hour; it waits whenever Spotify asks for a pause).
- Listening history stays on the phone (browser storage). Use Settings → Save a backup to keep a copy.
- Audio and episode info come from thisamericanlife.org.
