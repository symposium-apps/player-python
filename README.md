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

SYM-Node provides a profile/app-scoped `UserData` directory. Jukebox consumes the platform paths supplied through `SYM_APP_USER_DATA_DIR` and `SYM_APP_STATE_DIR`; it does not require the immutable App Store release to be writable.

The visible data layout is:

```text
UserData/
├── Music/          # Uploaded audio and album-folder artwork
├── Playlists/      # M3U8 playlists
├── Artwork/        # Generated/runtime artwork
└── Jukebox API/
    ├── README.txt
    └── password.txt  # Optional; user-created and never shown by Jukebox
```

Legacy `.sym-data` installations are declared for migration into UserData. UserData remains profile scoped and is preserved independently of managed release updates.

## Optional server password and remote access

Create `UserData/Jukebox API/password.txt` and put only the desired password in the file to enable both:

- the server-side browser password gate for `/`, `/manage`, `/mini-sym`, media, artwork, and the browser API;
- authenticated remote REST and MCP management.

The password is read directly from UserData, is never displayed in the Jukebox UI, and is never returned by an endpoint. Delete or empty the file to disable the browser gate. With no configured password, the normal browser app remains open, but `/api/v1`, `/api/agent/bootstrap`, and `/mcp` return the same `401 Unauthorized` response as an incorrect password.

After a successful browser login, Jukebox stores a signed persistent session in a secure `HttpOnly` cookie—not the password in browser storage. For embedded browsers that discard third-party cookies between launches, Jukebox also stores only the signed, revocable browser-session proof in local storage and silently exchanges it for a fresh `HttpOnly` cookie. The configured password is never written to local storage. Sessions survive reloads, browser relaunches, and managed app restarts; changing the UserData password invalidates them. HTTPS sessions use a partitioned cross-site cookie so the SYM-OS embedded app viewer can remember its own login safely.

Remote requests authenticate with:

```http
Authorization: Bearer <password>
```

Do not put the password in a URL or query string.

## Remote REST API

The canonical management API is rooted at `/api/v1`. Its OpenAPI description is available to authenticated callers at `/api/v1/openapi.json`.

### Stream audio and artwork

`PUT /api/v1/files/{relative-library-path}` streams the request body to a temporary file and atomically places it in the library. It supports MP3, M4A, AAC, OGG, WAV, FLAC, JPG, PNG, and WebP without buffering the complete file in memory.

```bash
BASE='https://your-jukebox.example'
KEY="$(cat 'UserData/Jukebox API/password.txt')"

curl --fail-with-body \
  -H "Authorization: Bearer $KEY" \
  --upload-file '01 Come Together.flac' \
  "$BASE/api/v1/files/The%20Beatles%20-%20Abbey%20Road/01%20Come%20Together.flac"

curl --fail-with-body \
  -H "Authorization: Bearer $KEY" \
  --upload-file cover.jpg \
  "$BASE/api/v1/files/The%20Beatles%20-%20Abbey%20Road/cover.jpg"

curl --fail-with-body -X POST \
  -H "Authorization: Bearer $KEY" \
  "$BASE/api/v1/library/rescan"
```

Upload an album's artwork beside its tracks, then rescan once after the batch. The `conflict` query parameter accepts `error` (default), `skip`, `replace`, or `rename`. An optional `X-Content-SHA256` header makes Jukebox verify the streamed file before committing it.

### Library and playlist operations

```text
GET    /api/v1/context
GET    /api/v1/storage
GET    /api/v1/tracks
GET    /api/v1/tracks/{id}
DELETE /api/v1/tracks/{id}
GET    /api/v1/albums
GET    /api/v1/albums/{slug}
DELETE /api/v1/albums/{slug}
GET    /api/v1/playlists
POST   /api/v1/playlists
GET    /api/v1/playlists/{slug}
PUT    /api/v1/playlists/{slug}
DELETE /api/v1/playlists/{slug}
POST   /api/v1/playlists/{slug}/tracks
DELETE /api/v1/playlists/{slug}/tracks
POST   /api/v1/library/rescan
```

Playlist JSON uses `name` and `track_ids`. Add/remove calls use `{ "track_ids": ["..."] }`.

## MCP

Authenticated MCP Streamable HTTP JSON-RPC is available at `POST /mcp` and declared in `sym-app.json`. It exposes library, album, playlist, deletion, rescan, and upload-instruction tools. Large binary files deliberately stay on the streaming REST route rather than being base64-encoded into MCP JSON.

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

Embedded tags and cover artwork require Mutagen and Pillow. They are declared in `pyproject.toml` and `requirements.txt`; SYM worker images include the matching system packages. For standalone development, run `python3 -m pip install -r requirements.txt`. `npm start` then launches Jukebox with the active Python environment. Managed releases never write a virtual environment into immutable app source.

## Development checks

Run finite checks only; do not leave a manual server running beside the SYM-managed instance.

```bash
npm ci
python3 -m compileall -q jukebox
```

For managed start or restart, use SYM-Node's profile-scoped app lifecycle action.
