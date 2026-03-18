# Spotify Likes Downloader

This project downloads tracks from a Spotify `Liked Songs` CSV export into local `mp3` files using `yt-dlp` and `ffmpeg`.

It is designed for large libraries and keeps local state so repeated runs can continue from the existing collection instead of starting from scratch.

## What it does

- Reads `Liked_Songs.csv`
- Builds multiple YouTube search queries for each track
- Downloads audio with `yt-dlp`
- Converts output to `mp3`
- Stores progress and track-to-file mapping in `.download_state/`
- Writes unresolved tracks to `failed.txt`
- Supports artist alias normalization with `artist_aliases.tsv`
- Supports per-track search overrides with `track_overrides.tsv`

## Requirements

- Python 3.10+
- `yt-dlp`
- `ffprobe`
- `ffmpeg`

## Files

- `download_music.py`: main downloader
- `script.sh`: small wrapper that runs the downloader
- `update_artist_aliases.py`: helper for maintaining `artist_aliases.tsv`
- `Liked_Songs.csv`: Spotify export input
- `artist_aliases.tsv`: artist alias mapping
- `track_overrides.tsv`: custom search targets for specific Spotify track URIs
- `music/`: downloaded output
- `.download_state/`: local state used for resume and deduplication
- `failed.txt`: tracks that could not be downloaded in the current run

## Export Your Spotify Likes First

Before running the downloader, you need a Spotify CSV export saved as `Liked_Songs.csv`.

One simple option is to export your library with Exportify:

https://exportify.net/

1. Open Exportify
2. Sign in with Spotify
3. Export your `Liked Songs`
4. Save the resulting CSV as `Liked_Songs.csv` in the project root

The downloader only supports the same CSV structure that Exportify produces.
If you use another source, the file must contain the same column names and layout.

## Usage

Run directly:

```bash
python3 download_music.py
```

Specify a custom CSV path:

```bash
python3 download_music.py --csv /path/to/Liked_Songs.csv
```

Or through the wrapper:

```bash
./script.sh
```

## Environment Variables

- `PARALLEL_JOBS`: number of concurrent track jobs, default `4`
- `SPOTIFY_CSV_FILE`: path to a Spotify CSV export, default `Liked_Songs.csv`
- `YT_DLP_PROXY`: proxy for `yt-dlp`, default `http://127.0.0.1:2080`
- `YT_DLP_CONCURRENT_FRAGMENTS`: fragment concurrency for `yt-dlp`, default `1`
- `FFMPEG_THREADS`: ffmpeg thread count, default `1`
- `YT_DLP_COOKIES_FILE`: optional cookies file path
- `YT_DLP_COOKIES_FROM_BROWSER`: optional browser name for `yt-dlp --cookies-from-browser`
- `ENABLE_FILE_LOG`: set to `1`/`true` to enable detailed `download.log`
- `PROGRESS_FLUSH_EVERY`: how often to flush progress to disk, default `25`

Example:

```bash
PARALLEL_JOBS=6 \
SPOTIFY_CSV_FILE=/path/to/Liked_Songs.csv \
YT_DLP_PROXY=http://127.0.0.1:2080 \
YT_DLP_COOKIES_FROM_BROWSER=firefox \
python3 download_music.py
```

## Naming and Deduplication

The downloader keeps one file per Spotify `track_uri`.

Default filename:

```text
Artist - Track.mp3
```

If that path is already used by another `track_uri`, it falls back to:

```text
Artist - Track [Album].mp3
Artist - Track [Album] [Year].mp3
Artist - Track [suffix].mp3
```

This avoids collisions between different Spotify tracks with the same visible metadata.

## Artist Aliases

`artist_aliases.tsv` lets you map alternate spellings or transliterations to a preferred canonical artist name.

Format:

```text
Alias<TAB>Canonical Name
```

Example:

```text
Noize MC	Нойз МС
Zemfira	Земфира
```

To help maintain aliases:

```bash
python3 update_artist_aliases.py --dry-run
python3 update_artist_aliases.py --review-disputed
```

## Track Overrides

`track_overrides.tsv` lets you override the generated search targets for a specific Spotify track URI.

Format:

```text
spotify:track:...<TAB>ytsearch1:custom query
```

You can also provide direct URLs instead of `ytsearch...` queries.

## Privacy

This repository may contain private data if you keep local runtime files in version control.

Do not publish these files unless you explicitly want to share them:

- `Liked_Songs.csv`
- `failed.txt`
- `download.log`
- `.download_state/`

## Limitations

- Download success depends on YouTube availability and search quality
- Some tracks may require aliases or explicit overrides
- Different Spotify tracks can still be effectively the same audio source
- Metadata quality depends on the matched source returned by `yt-dlp`
