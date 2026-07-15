# Jukebox

Jukebox is a SYM music app with a shared VPS-hosted song library and independent playback in each browser.

## Install in SYM

Install **Jukebox** from the SYM-OS App Store. SYM-Node owns installation, port allocation, start/restart, and the public app URL. Do not manually start a second server or configure a fixed port.

After installation:

1. Open Jukebox in SYM-OS.
2. The main `/` page is the full library manager.
3. Use **Upload**, **Folder**, or drag and drop to add music.
4. Use `/mini-sym` for the compact device-style player controls.

Jukebox accepts MP3, M4A, AAC, OGG, WAV, and FLAC audio. Album artwork can be uploaded as JPG, PNG, or WebP.

## App-owned storage

Jukebox keeps uploaded music and runtime state inside its own app folder:

```text
$HOME/project_files/Apps/jukebox/.sym-data/
```

The managed data layout is:

```text
.sym-data/
├── library/       # Uploaded music and album artwork
├── playlists/     # Saved playlists
└── assets/        # Generated/runtime artwork
```

The app creates these directories automatically. Upload music through the main page (`/` or the backwards-compatible `/manage` route); do not copy files into another profile, `/Users`, `/home/samos`, or a shared top-level library.

The manifest declares `.sym-data` as persistent app data so it remains app-scoped across managed restarts and updates.

## Independent browser playback

Songs, album art, and playlists are hosted by the Jukebox app on the VPS. Playback state is not shared through the server.

Each browser stores its own current track, queue, paused state, volume, and playback position in same-origin browser storage under `jukebox.playback.v1`. Pausing or changing songs in one browser does not control another computer. The server only streams the requested song file to each browser's local audio element.

The main page and `/mini-sym` use the same browser-local state on a given browser profile, so switching between those views keeps that browser's queue and position.

## Managed runtime contract

Jukebox:

- reads its assigned port from `PORT`;
- reads `HOST` when supplied by SYM-Node;
- defaults storage to `<app-root>/.sym-data`;
- exposes `GET /_sym/health` for managed health checks;
- serves the full library manager at `/` and `/manage`;
- serves the device-style player controls at `/mini-sym` for the Sym Browser viewer;
- starts through the committed `package.json` and `package-lock.json` contract.

The npm start command launches the Python standard-library server. The core web app has no third-party Python package dependency.

## Development checks

Run finite checks only; do not leave a manual server running beside the SYM-managed instance.

```bash
npm ci
python3 -m compileall -q jukebox
```

For managed start or restart, use SYM-Node's profile-scoped app lifecycle action.
