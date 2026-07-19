# Jukebox agent contract

Jukebox exposes a profile-scoped music library through authenticated REST and MCP interfaces.

## Required start

1. Call `jukebox_get_context` through the installed app's local `/mcp`, or read `GET /api/agent/bootstrap`.
2. The password is owned by the installed instance at `UserData/Jukebox API/password.txt`. Never ask Jukebox to display or return its value.
3. If the file is absent or empty, remote REST and MCP are intentionally disabled and return `401 Unauthorized`.
4. Never put the password in a URL, query string, chat message, log, activity event, report, or committed file. Send it only in the `Authorization: Bearer …` header.

## Upload contract

- Upload audio and artwork as raw streaming file bodies with `PUT /api/v1/files/{relative-library-path}`.
- Preserve album folders. Place `cover.jpg`, `folder.png`, or another supported artwork file beside the album's tracks.
- Use `conflict=error|skip|replace|rename`; default is `error`.
- For a batch, upload files without rescanning each one, then call `POST /api/v1/library/rescan` once.
- Do not base64-encode large audio into MCP JSON. Use MCP for discovery/management and REST for bytes.

## Managed app boundary

The App Store release is immutable. Write music, artwork, playlists, and the optional password only inside declared `UserData`. Never edit an installed managed release in place. Use the app-local API/MCP so the UI and generated artwork stay synchronized.
