#!/usr/bin/env python3

import csv
import os
import re
import signal
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path


MUSIC_DIR = Path("music")
CSV_FILE = Path("Liked_Songs.csv")
ALIASES_FILE = Path("artist_aliases.tsv")
TRACK_OVERRIDES_FILE = Path("track_overrides.tsv")
FAILED_FILE = Path("failed.txt")
LOG_FILE = Path("download.log")
STATE_DIR = Path(".download_state")
TRACKS_FILE = STATE_DIR / "tracks.tsv"
TRACK_URI_DIR = STATE_DIR / "by_track_uri"
PROGRESS_FILE = STATE_DIR / "progress.tsv"
RESOLVED_TARGETS_FILE = STATE_DIR / "resolved_targets.tsv"
PARALLEL_JOBS = int(os.environ.get("PARALLEL_JOBS", "6"))
YT_DLP_PROXY = os.environ.get("YT_DLP_PROXY", "http://127.0.0.1:2080")
YT_DLP_CONCURRENT_FRAGMENTS = int(os.environ.get("YT_DLP_CONCURRENT_FRAGMENTS", "1"))
FFMPEG_THREADS = int(os.environ.get("FFMPEG_THREADS", "1"))
YT_DLP_COOKIES_FILE = os.environ.get("YT_DLP_COOKIES_FILE", "")
YT_DLP_COOKIES_FROM_BROWSER = os.environ.get("YT_DLP_COOKIES_FROM_BROWSER", "")

SUFFIX_PATTERN = re.compile(
    r"\s*[-–]\s*(\d{4}\s+remaster|remaster(ed)?(?:\s+\d{4})?|demo|edit|radio edit|mono|stereo|live|version)\b.*$",
    re.IGNORECASE,
)
INVALID_FILENAME_CHARS = re.compile(r'[/\\:*?"<>|]')
WHITESPACE_PATTERN = re.compile(r"\s+")
PAREN_CONTENT_PATTERN = re.compile(r"\s*[\(\[\{].*?[\)\]\}]\s*")
FEAT_PATTERN = re.compile(r"\s*(feat\.?|ft\.?).*$", re.IGNORECASE)


@dataclass(frozen=True)
class Track:
    track_uri: str
    artists: str
    artist_parts: list[str]
    display_artists: str
    canonical_artists: str
    track_name: str
    album_name: str
    year: str
    search_targets: list[str]


def normalize_spaces(value: str) -> str:
    return " ".join((value or "").replace("\ufeff", "").split())


def clean_track_name(track_name: str) -> str:
    track_name = normalize_spaces(track_name)
    return SUFFIX_PATTERN.sub("", track_name).strip() or track_name


def strip_parenthetical(value: str) -> str:
    simplified = PAREN_CONTENT_PATTERN.sub(" ", value)
    simplified = normalize_spaces(simplified)
    return simplified or value


def strip_feature_suffix(value: str) -> str:
    simplified = FEAT_PATTERN.sub("", value)
    simplified = normalize_spaces(simplified)
    return simplified or value


def simplify_search_text(value: str) -> str:
    return strip_feature_suffix(strip_parenthetical(value))


def extract_year(release_date: str) -> str:
    release_date = normalize_spaces(release_date)
    return release_date[:4] if len(release_date) >= 4 and release_date[:4].isdigit() else ""


def unique_preserve_order(values: list[str]) -> list[str]:
    seen = set()
    result = []
    for value in values:
        if not value:
            continue
        key = value.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result


def is_cyrillic(value: str) -> bool:
    return any("а" <= ch.lower() <= "я" or ch.lower() == "ё" for ch in value)


def preferred_artist_name(variants: list[str], fallback: str) -> str:
    for variant in variants:
        if "ё" in variant.lower() and is_cyrillic(variant):
            return variant
    for variant in variants:
        if is_cyrillic(variant):
            return variant
    return fallback


def load_aliases(path: Path) -> dict[str, list[str]]:
    aliases: dict[str, list[str]] = {}
    if not path.exists():
        return aliases

    with path.open(encoding="utf-8") as file:
        for raw_line in file:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            parts = [normalize_spaces(part) for part in raw_line.split("\t")]
            if len(parts) < 2 or not parts[0]:
                continue
            alias = parts[0]
            canonicals = unique_preserve_order(parts[1:])
            if canonicals:
                aliases[alias.casefold()] = canonicals
    return aliases


def load_track_overrides(path: Path) -> dict[str, list[str]]:
    overrides: dict[str, list[str]] = {}
    if not path.exists():
        return overrides

    with path.open(encoding="utf-8") as file:
        for raw_line in file:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            parts = [normalize_spaces(part) for part in raw_line.split("\t")]
            if len(parts) < 2 or not parts[0]:
                continue
            overrides[parts[0]] = unique_preserve_order(parts[1:])
    return overrides


def load_resolved_targets(path: Path) -> dict[str, str]:
    resolved: dict[str, str] = {}
    if not path.exists():
        return resolved

    with path.open(encoding="utf-8") as file:
        for raw_line in file:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            parts = raw_line.rstrip("\n").split("\t", 1)
            if len(parts) != 2:
                continue
            track_uri = normalize_spaces(parts[0])
            target = normalize_spaces(parts[1])
            if track_uri and target:
                resolved[track_uri] = target
    return resolved


def canonicalize_artist_variants(artist: str, aliases: dict[str, list[str]]) -> list[str]:
    artist = normalize_spaces(artist)
    return unique_preserve_order(aliases.get(artist.casefold(), []) + [artist])


def sanitize_filename_part(value: str) -> str:
    value = normalize_spaces(value.replace("\n", " "))
    return INVALID_FILENAME_CHARS.sub("_", value)


def track_uri_key(track_uri: str) -> str:
    return sanitize_filename_part(track_uri)


def track_uri_marker(track_uri: str) -> Path:
    return TRACK_URI_DIR / f"{track_uri_key(track_uri)}.path"


def output_path_for_base(output_base: Path, suffix: str) -> Path:
    return output_base.parent / f"{output_base.name}{suffix}"


def is_complete_mp3(file_path: Path) -> bool:
    if not file_path.exists() or file_path.stat().st_size <= 0:
        return False

    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "csv=p=0",
            str(file_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return False

    first_line = result.stdout.strip().splitlines()
    if not first_line:
        return False

    try:
        return int(float(first_line[0])) > 0
    except ValueError:
        return False


def register_track_uri(track_uri: str, output_file: Path) -> None:
    track_uri_marker(track_uri).write_text(str(output_file) + "\n", encoding="utf-8")


def is_track_uri_downloaded(track_uri: str) -> bool:
    marker = track_uri_marker(track_uri)
    if not marker.exists():
        return False

    output_file = Path(marker.read_text(encoding="utf-8").splitlines()[0])
    if is_complete_mp3(output_file):
        return True

    marker.unlink(missing_ok=True)
    return False


def cleanup_output_sidecars(output_base: Path) -> None:
    for suffix in (".webm", ".m4a", ".opus", ".webp", ".jpg", ".png", ".part"):
        output_path_for_base(output_base, suffix).unlink(missing_ok=True)


def format_duration(total_seconds: int) -> str:
    total_seconds = max(total_seconds, 0)
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    seconds = total_seconds % 60
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


class Progress:
    def __init__(self, total_tracks: int) -> None:
        self.total_tracks = total_tracks
        self.start_ts = int(time.time())
        self.completed = 0
        self.skipped = 0
        self.failed = 0
        self.downloaded = 0
        self.lock = threading.Lock()
        PROGRESS_FILE.write_text("0\t0\t0\t0\n", encoding="utf-8")

    def update(self, status: str) -> None:
        with self.lock:
            self.completed += 1
            if status == "skipped":
                self.skipped += 1
            elif status == "failed":
                self.failed += 1
            elif status == "downloaded":
                self.downloaded += 1

            PROGRESS_FILE.write_text(
                f"{self.completed}\t{self.skipped}\t{self.failed}\t{self.downloaded}\n",
                encoding="utf-8",
            )
            self.render()

    def render(self) -> None:
        elapsed = int(time.time()) - self.start_ts
        eta = int((elapsed * (self.total_tracks - self.completed)) / self.completed) if self.completed else 0
        bar_width = 28
        filled = int(self.completed * bar_width / self.total_tracks) if self.total_tracks else 0
        bar = "#" * filled + "-" * (bar_width - filled)
        sys.stdout.write(
            f"\r[{bar}] {self.completed}/{self.total_tracks} | "
            f"skipped:{self.skipped} failed:{self.failed} | "
            f"elapsed:{format_duration(elapsed)} eta:{format_duration(eta)}"
        )
        if self.completed == self.total_tracks:
            sys.stdout.write("\n")
        sys.stdout.flush()

    def print_message(self, message: str) -> None:
        with self.lock:
            sys.stdout.write("\n" + message + "\n")
            self.render()


class ShutdownController:
    def __init__(self) -> None:
        self.stop_event = threading.Event()
        self.signal_name = ""
        self.lock = threading.Lock()

    def request_stop(self, signal_name: str) -> None:
        with self.lock:
            if self.stop_event.is_set():
                return
            self.signal_name = signal_name
            self.stop_event.set()

    def is_stopping(self) -> bool:
        return self.stop_event.is_set()


class Downloader:
    def __init__(self, tracks: list[Track], shutdown: ShutdownController) -> None:
        self.tracks = tracks
        self.shutdown = shutdown
        self.progress = Progress(len(tracks))
        self.log_lock = threading.Lock()
        self.fail_lock = threading.Lock()
        self.resolved_lock = threading.Lock()
        self.failed_entries: dict[str, str] = {}
        self.resolved_targets = load_resolved_targets(RESOLVED_TARGETS_FILE)

    def append_log(self, message: str) -> None:
        with self.log_lock:
            with LOG_FILE.open("a", encoding="utf-8") as file:
                file.write(message)

    def log_failure(self, track: Track) -> None:
        with self.fail_lock:
            self.failed_entries[track.track_uri] = (
                f"{track.track_uri} | {track.display_artists} - {track.track_name} | "
                f"search: {'|||'.join(track.search_targets)}"
            )

    def clear_failure(self, track: Track) -> None:
        with self.fail_lock:
            self.failed_entries.pop(track.track_uri, None)

    def flush_failures(self) -> None:
        with self.fail_lock:
            unresolved = []
            for track in self.tracks:
                entry = self.failed_entries.get(track.track_uri)
                if not entry:
                    continue
                safe_artists = sanitize_filename_part(track.display_artists)
                safe_track = sanitize_filename_part(track.track_name)
                output_base = MUSIC_DIR / f"{safe_artists} - {safe_track}"
                output_file = output_path_for_base(output_base, ".mp3")
                if is_track_uri_downloaded(track.track_uri) or is_complete_mp3(output_file):
                    continue
                unresolved.append(entry)
            FAILED_FILE.write_text(("\n".join(unresolved) + "\n") if unresolved else "", encoding="utf-8")

    def yt_dlp_base_args(self) -> list[str]:
        cookies_args: list[str] = []
        if YT_DLP_COOKIES_FILE:
            cookies_args = ["--cookies", YT_DLP_COOKIES_FILE]
        elif YT_DLP_COOKIES_FROM_BROWSER:
            cookies_args = ["--cookies-from-browser", YT_DLP_COOKIES_FROM_BROWSER]
        return ["yt-dlp", "--proxy", YT_DLP_PROXY, *cookies_args]

    def cached_targets_for(self, track: Track) -> list[str]:
        with self.resolved_lock:
            cached = self.resolved_targets.get(track.track_uri, "")
        if not cached:
            return []
        return [cached] if cached not in track.search_targets else [cached]

    def save_resolved_target(self, track_uri: str, target: str) -> None:
        if not target:
            return
        with self.resolved_lock:
            self.resolved_targets[track_uri] = target
            lines = [f"{uri}\t{resolved}" for uri, resolved in sorted(self.resolved_targets.items())]
            RESOLVED_TARGETS_FILE.write_text(("\n".join(lines) + "\n") if lines else "", encoding="utf-8")

    def resolve_target(self, search_target: str) -> str:
        if search_target.startswith("http://") or search_target.startswith("https://"):
            return search_target
        if not search_target.startswith("ytsearch"):
            return search_target

        command = [
            *self.yt_dlp_base_args(),
            "--flat-playlist",
            "--playlist-end",
            "1",
            "--print",
            "webpage_url",
            search_target,
        ]
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            start_new_session=True,
            check=False,
        )
        if result.returncode != 0:
            return search_target

        for line in result.stdout.splitlines():
            resolved = line.strip()
            if resolved.startswith("http://") or resolved.startswith("https://"):
                return resolved
        return search_target

    def download_with_queries(self, output_base: Path, search_targets: list[str]) -> tuple[bool, str]:
        for search_target in search_targets:
            if not search_target:
                continue

            self.append_log(f"[search] {output_base} | target: {search_target}\n")
            yt_target = search_target if ":" in search_target.split("/", 1)[0] or search_target.startswith("http") else f"ytsearch1:{search_target}"
            effective_target = self.resolve_target(yt_target)
            if effective_target != yt_target:
                self.append_log(f"[resolve] {output_base} | {yt_target} -> {effective_target}\n")
            command = [
                *self.yt_dlp_base_args(),
                effective_target,
                "-x",
                "--audio-format",
                "mp3",
                "--audio-quality",
                "0",
                "--embed-thumbnail",
                "--convert-thumbnails",
                "jpg",
                "--add-metadata",
                "--parse-metadata",
                "title:%(meta_title)s",
                "--parse-metadata",
                "artist:%(meta_artist)s",
                "--no-playlist",
                "--ignore-errors",
                "--concurrent-fragments",
                str(YT_DLP_CONCURRENT_FRAGMENTS),
                "--no-keep-video",
                "--postprocessor-args",
                f"ffmpeg:-threads {FFMPEG_THREADS}",
                "--output",
                f"{output_base}.%(ext)s",
            ]
            with LOG_FILE.open("a", encoding="utf-8") as log_file:
                result = subprocess.run(
                    command,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                    check=False,
                )
            if result.returncode == 0:
                return True, effective_target

        return False, ""

    def download_track(self, track: Track) -> str:
        safe_artists = sanitize_filename_part(track.display_artists)
        safe_track = sanitize_filename_part(track.track_name)
        output_base = MUSIC_DIR / f"{safe_artists} - {safe_track}"
        output_file = output_path_for_base(output_base, ".mp3")

        if is_track_uri_downloaded(track.track_uri):
            self.progress.update("skipped")
            return "skipped"

        if is_complete_mp3(output_file):
            register_track_uri(track.track_uri, output_file)
            self.progress.update("skipped")
            return "skipped"

        output_file.unlink(missing_ok=True)
        cleanup_output_sidecars(output_base)

        search_targets = unique_preserve_order(self.cached_targets_for(track) + track.search_targets)
        success, resolved_target = self.download_with_queries(output_base, search_targets)
        if success and is_complete_mp3(output_file):
            cleanup_output_sidecars(output_base)
            register_track_uri(track.track_uri, output_file)
            self.save_resolved_target(track.track_uri, resolved_target)
            self.clear_failure(track)
            self.progress.update("downloaded")
            return "downloaded"

        self.log_failure(track)
        output_file.unlink(missing_ok=True)
        cleanup_output_sidecars(output_base)
        self.progress.update("failed")
        return "failed"

    def run(self) -> None:
        print(f"Starting download from {CSV_FILE}: {len(self.tracks)} tracks, {PARALLEL_JOBS} parallel jobs")
        with ThreadPoolExecutor(max_workers=PARALLEL_JOBS) as executor:
            pending_tracks = iter(self.tracks)
            running_futures = set()

            def submit_next() -> bool:
                if self.shutdown.is_stopping():
                    return False
                try:
                    track = next(pending_tracks)
                except StopIteration:
                    return False
                running_futures.add(executor.submit(self.download_track, track))
                return True

            for _ in range(min(PARALLEL_JOBS, len(self.tracks))):
                if not submit_next():
                    break

            shutdown_notice_printed = False
            while running_futures:
                done, running_futures = wait(running_futures, return_when=FIRST_COMPLETED)
                for future in done:
                    future.result()
                    if not self.shutdown.is_stopping():
                        submit_next()

                if self.shutdown.is_stopping() and not shutdown_notice_printed:
                    signal_name = self.shutdown.signal_name or "stop signal"
                    self.progress.print_message(
                        f"Received {signal_name}. Finishing active downloads and stopping without starting new ones."
                    )
                    shutdown_notice_printed = True

            if self.shutdown.is_stopping():
                self.progress.print_message("Graceful shutdown complete.")
        self.flush_failures()


def build_tracks() -> list[Track]:
    aliases = load_aliases(ALIASES_FILE)
    overrides = load_track_overrides(TRACK_OVERRIDES_FILE)
    seen_uris = set()
    tracks: list[Track] = []

    if not CSV_FILE.exists():
        raise SystemExit(f"CSV file not found: {CSV_FILE}")

    with CSV_FILE.open(newline="", encoding="utf-8-sig") as file:
        reader = csv.DictReader(file)
        required_fields = {"Track URI", "Track Name", "Artist Name(s)", "Album Name", "Release Date"}
        missing_fields = required_fields.difference(reader.fieldnames or [])
        if missing_fields:
            missing = ", ".join(sorted(missing_fields))
            raise SystemExit(f"CSV is missing required columns: {missing}")

        for row in reader:
            track_uri = normalize_spaces(row.get("Track URI", ""))
            track_name = normalize_spaces(row.get("Track Name", ""))
            artists = normalize_spaces(row.get("Artist Name(s)", ""))
            album = normalize_spaces(row.get("Album Name", ""))
            year = extract_year(row.get("Release Date", ""))

            if not track_uri or not track_name or not artists or track_uri in seen_uris:
                continue
            seen_uris.add(track_uri)

            artist_parts = [normalize_spaces(part) for part in artists.split(";") if normalize_spaces(part)]
            canonical_parts = [canonicalize_artist_variants(part, aliases) for part in artist_parts]
            display_artist_parts = [
                preferred_artist_name(variants, artist_part)
                for artist_part, variants in zip(artist_parts, canonical_parts)
            ]
            primary_variants = canonical_parts[0]
            search_track = clean_track_name(track_name)
            simplified_track = strip_parenthetical(search_track)
            minimal_track = simplify_search_text(search_track)
            simplified_album = simplify_search_text(album)
            all_artists_joined = " ".join(display_artist_parts)
            album_without_track = album if album.casefold() != track_name.casefold() else ""
            short_track = len(minimal_track.split()) <= 2

            search_targets: list[str] = []

            def add_target(*parts: str, search_size: int = 1) -> None:
                query = " ".join(part for part in parts if part)
                if not query:
                    return
                target = f"ytsearch{search_size}:{query}"
                if target not in search_targets:
                    search_targets.append(target)

            for override in overrides.get(track_uri, []):
                if override not in search_targets:
                    search_targets.append(override)

            artist_variants = unique_preserve_order(
                primary_variants + display_artist_parts[:2] + artist_parts[:2] + [all_artists_joined]
            )
            track_variants = unique_preserve_order([search_track, simplified_track, minimal_track])

            for artist_variant in artist_variants:
                for track_variant in track_variants:
                    add_target(artist_variant, track_variant, album_without_track, "audio")
                    add_target(artist_variant, track_variant, "official audio")
                    add_target(artist_variant, track_variant, "audio")
                    add_target(track_variant, artist_variant, "audio")
                    add_target(artist_variant, track_variant)
                    if short_track:
                        add_target(artist_variant, track_variant, simplified_album, "audio")
                        add_target(track_variant, simplified_album, artist_variant)
                    add_target(artist_variant, track_variant, album_without_track, year, "audio")
                    add_target(artist_variant, track_variant, year, "audio")

            for track_variant in track_variants:
                add_target(track_variant, simplified_album, "audio")
                add_target(track_variant, "official audio")
                add_target(track_variant, "audio")
                add_target(track_variant)
                add_target(track_variant, search_size=5)
                add_target(track_variant, simplified_album, year, "audio")
                add_target(track_variant, year, "audio")

            tracks.append(
                Track(
                    track_uri=track_uri,
                    artists=artists,
                    artist_parts=artist_parts,
                    display_artists=";".join(display_artist_parts) if display_artist_parts else artists,
                    canonical_artists=";".join("/".join(variants) for variants in canonical_parts) if canonical_parts else artists,
                    track_name=track_name,
                    album_name=album,
                    year=year,
                    search_targets=search_targets,
                )
            )

    with TRACKS_FILE.open("w", encoding="utf-8") as file:
        for track in tracks:
            fields = [
                track.track_uri,
                track.artists,
                track.display_artists,
                track.canonical_artists,
                track.track_name,
                track.album_name,
                track.year,
                "|||".join(track.search_targets),
            ]
            file.write("\t".join(field.replace("\t", " ").replace("\n", " ") for field in fields) + "\n")

    return tracks


def ensure_environment() -> None:
    for path in (MUSIC_DIR, STATE_DIR, TRACK_URI_DIR):
        path.mkdir(parents=True, exist_ok=True)

    FAILED_FILE.write_text("", encoding="utf-8")
    LOG_FILE.write_text("", encoding="utf-8")
    TRACKS_FILE.write_text("", encoding="utf-8")
    RESOLVED_TARGETS_FILE.touch(exist_ok=True)

    if shutil.which("yt-dlp") is None:
        raise SystemExit("yt-dlp is required but was not found in PATH")
    if shutil.which("ffprobe") is None:
        raise SystemExit("ffprobe is required but was not found in PATH")


def install_signal_handlers(shutdown: ShutdownController) -> None:
    def handle_signal(signum: int, _frame: object) -> None:
        signal_name = signal.Signals(signum).name
        shutdown.request_stop(signal_name)

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)


def main() -> int:
    ensure_environment()
    shutdown = ShutdownController()
    install_signal_handlers(shutdown)
    tracks = build_tracks()
    if not tracks:
        print(f"No valid tracks found in {CSV_FILE}")
        return 0
    print(
        "Settings: "
        f"parallel_jobs={PARALLEL_JOBS}, "
        f"concurrent_fragments={YT_DLP_CONCURRENT_FRAGMENTS}, "
        f"ffmpeg_threads={FFMPEG_THREADS}"
    )
    Downloader(tracks, shutdown).run()
    return 130 if shutdown.is_stopping() else 0


if __name__ == "__main__":
    raise SystemExit(main())
