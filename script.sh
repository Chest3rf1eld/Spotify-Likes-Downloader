#!/usr/bin/env bash

set -u

MUSIC_DIR="music"
CSV_FILE="Liked_Songs.csv"
ALIASES_FILE="artist_aliases.tsv"
FAILED_FILE="failed.txt"
LOG_FILE="download.log"
STATE_DIR=".download_state"
TRACKS_FILE="$STATE_DIR/tracks.tsv"
TRACK_URI_DIR="$STATE_DIR/by_track_uri"
PROGRESS_FILE="$STATE_DIR/progress.tsv"
LOCK_FILE="$STATE_DIR/progress.lock"
PARALLEL_JOBS="${PARALLEL_JOBS:-12}"

mkdir -p "$MUSIC_DIR" "$STATE_DIR" "$TRACK_URI_DIR"
: > "$FAILED_FILE"
: > "$LOG_FILE"
: > "$TRACKS_FILE"

python3 - "$CSV_FILE" "$TRACKS_FILE" "$ALIASES_FILE" <<'PY'
import csv
import re
import sys
from pathlib import Path

csv_path = Path(sys.argv[1])
tracks_path = Path(sys.argv[2])
aliases_path = Path(sys.argv[3])

if not csv_path.exists():
    raise SystemExit(f"CSV file not found: {csv_path}")

suffix_pattern = re.compile(
    r"\s*[-–]\s*(\d{4}\s+remaster|remaster(ed)?(?:\s+\d{4})?|demo|edit|radio edit|mono|stereo|live|version)\b.*$",
    re.IGNORECASE,
)

def normalize_spaces(value: str) -> str:
    return " ".join((value or "").replace("\ufeff", "").split())

def clean_track_name(track_name: str) -> str:
    track_name = normalize_spaces(track_name)
    return suffix_pattern.sub("", track_name).strip() or track_name

def extract_year(release_date: str) -> str:
    release_date = normalize_spaces(release_date)
    return release_date[:4] if len(release_date) >= 4 and release_date[:4].isdigit() else ""

def unique_preserve_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if not value:
            continue
        key = value.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result

def load_aliases(path: Path) -> dict[str, list[str]]:
    aliases: dict[str, list[str]] = {}
    if not path.exists():
      return aliases

    with path.open(encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            parts = [normalize_spaces(part) for part in line.split("\t")]
            if len(parts) < 2 or not parts[0]:
                continue
            alias = parts[0]
            canonicals = unique_preserve_order(parts[1:])
            if not canonicals:
                continue
            aliases[alias.casefold()] = canonicals
    return aliases

def canonicalize_artist_variants(artist: str, aliases: dict[str, list[str]]) -> list[str]:
    artist = normalize_spaces(artist)
    return unique_preserve_order(aliases.get(artist.casefold(), []) + [artist])

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

aliases = load_aliases(aliases_path)

with csv_path.open(newline="", encoding="utf-8-sig") as f:
    reader = csv.DictReader(f)
    required_fields = ["Track URI", "Track Name", "Artist Name(s)", "Album Name", "Release Date"]
    missing = [field for field in required_fields if field not in (reader.fieldnames or [])]
    if missing:
        raise SystemExit(f"CSV is missing required columns: {', '.join(missing)}")

    with tracks_path.open("w", encoding="utf-8") as out:
        for row in reader:
            track_uri = normalize_spaces(row.get("Track URI", ""))
            track_name = normalize_spaces(row.get("Track Name", ""))
            artists = normalize_spaces(row.get("Artist Name(s)", ""))
            album = normalize_spaces(row.get("Album Name", ""))
            year = extract_year(row.get("Release Date", ""))

            if not track_uri or not track_name or not artists:
                continue

            artist_parts = [normalize_spaces(part) for part in artists.split(";") if normalize_spaces(part)]
            canonical_parts = [canonicalize_artist_variants(part, aliases) for part in artist_parts]
            display_artist_parts = [
                preferred_artist_name(variants, artist_part)
                for artist_part, variants in zip(artist_parts, canonical_parts)
            ]
            primary_artist = artist_parts[0]
            canonical_primary_variants = canonical_parts[0]
            search_track = clean_track_name(track_name)

            search_candidates: list[str] = []
            for artist_variant in canonical_primary_variants + [""]:
                parts = [artist_variant, search_track, album, year, "audio"]
                query = " ".join(part for part in parts if part)
                if query and query not in search_candidates:
                    search_candidates.append(query)

            fields = [
                track_uri,
                artists,
                ";".join(display_artist_parts) if display_artist_parts else artists,
                ";".join("/".join(variants) for variants in canonical_parts) if canonical_parts else artists,
                track_name,
                album,
                year,
                "|||".join(search_candidates),
            ]
            out.write("\t".join(field.replace("\t", " ").replace("\n", " ") for field in fields) + "\n")
PY

TOTAL_TRACKS=$(grep -cve '^[[:space:]]*$' "$TRACKS_FILE")
START_TS=$(date +%s)

printf '0\t0\t0\t0\n' > "$PROGRESS_FILE"
: > "$LOCK_FILE"

if [ "$TOTAL_TRACKS" -eq 0 ]; then
  echo "No valid tracks found in $CSV_FILE"
  exit 0
fi

is_complete_mp3() {
  local file="$1"
  local duration

  if [ ! -s "$file" ]; then
    return 1
  fi

  duration=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$file" 2>/dev/null | awk 'NR==1 { print int($1) }')
  [ -n "$duration" ] && [ "$duration" -gt 0 ]
}

sanitize_filename_part() {
  local value="$1"

  value=$(printf '%s' "$value" | tr '\n' ' ' | sed 's/[[:space:]]\+/ /g; s/^ //; s/ $//')
  value=$(printf '%s' "$value" | sed 's#[/\\:*?"<>|]#_#g')
  printf '%s' "$value"
}

track_uri_key() {
  sanitize_filename_part "$1"
}

track_uri_marker() {
  local track_uri="$1"
  printf '%s/%s.path' "$TRACK_URI_DIR" "$(track_uri_key "$track_uri")"
}

register_track_uri() {
  local track_uri="$1"
  local output_file="$2"
  printf '%s\n' "$output_file" > "$(track_uri_marker "$track_uri")"
}

is_track_uri_downloaded() {
  local track_uri="$1"
  local marker output_file

  marker=$(track_uri_marker "$track_uri")
  if [ ! -f "$marker" ]; then
    return 1
  fi

  output_file=$(head -n 1 "$marker")
  if is_complete_mp3 "$output_file"; then
    return 0
  fi

  rm -f "$marker"
  return 1
}

format_duration() {
  local total_seconds="$1"
  local hours minutes seconds

  if [ "$total_seconds" -lt 0 ]; then
    total_seconds=0
  fi

  hours=$((total_seconds / 3600))
  minutes=$(((total_seconds % 3600) / 60))
  seconds=$((total_seconds % 60))

  printf '%02d:%02d:%02d' "$hours" "$minutes" "$seconds"
}

render_progress() {
  local completed="$1"
  local skipped="$2"
  local failed="$3"
  local elapsed eta filled empty bar
  local bar_width=28

  elapsed=$(( $(date +%s) - START_TS ))
  if [ "$completed" -gt 0 ]; then
    eta=$(( (elapsed * (TOTAL_TRACKS - completed)) / completed ))
  else
    eta=0
  fi

  filled=$(( completed * bar_width / TOTAL_TRACKS ))
  empty=$(( bar_width - filled ))
  printf -v bar '%*s' "$filled" ''
  bar=${bar// /#}
  printf -v bar '%s%*s' "$bar" "$empty" ''
  bar=${bar// /-}

  printf '\r[%s] %d/%d | skipped:%d failed:%d | elapsed:%s eta:%s' \
    "$bar" "$completed" "$TOTAL_TRACKS" "$skipped" "$failed" \
    "$(format_duration "$elapsed")" "$(format_duration "$eta")"

  if [ "$completed" -eq "$TOTAL_TRACKS" ]; then
    printf '\n'
  fi
}

update_progress() {
  local status="$1"
  local completed skipped failed downloaded

  exec 9>"$LOCK_FILE"
  flock 9

  IFS=$'\t' read -r completed skipped failed downloaded < "$PROGRESS_FILE"
  completed=$((completed + 1))

  case "$status" in
    skipped)
      skipped=$((skipped + 1))
      ;;
    failed)
      failed=$((failed + 1))
      ;;
    downloaded)
      downloaded=$((downloaded + 1))
      ;;
  esac

  printf '%d\t%d\t%d\t%d\n' "$completed" "$skipped" "$failed" "$downloaded" > "$PROGRESS_FILE"
  render_progress "$completed" "$skipped" "$failed"

  flock -u 9
  exec 9>&-
}

log_failure() {
  local track_uri="$1"
  local artists="$2"
  local track_name="$3"
  local search_queries="$4"

  printf '%s | %s - %s | search: %s\n' "$track_uri" "$artists" "$track_name" "$search_queries" >> "$FAILED_FILE"
}

download_with_queries() {
  local output_base="$1"
  local search_queries="$2"
  local search_query

  IFS='|||' read -r -a search_query_list <<< "$search_queries"
  for search_query in "${search_query_list[@]}"; do
    [ -n "$search_query" ] || continue

    printf '[search] %s | query: %s\n' "$output_base" "$search_query" >> "$LOG_FILE"
    if yt-dlp \
      --proxy http://127.0.0.1:2080 \
      "ytsearch1:${search_query}" \
      -x \
      --audio-format mp3 \
      --audio-quality 0 \
      --embed-thumbnail \
      --convert-thumbnails jpg \
      --add-metadata \
      --parse-metadata "title:%(meta_title)s" \
      --parse-metadata "artist:%(meta_artist)s" \
      --no-playlist \
      --ignore-errors \
      --concurrent-fragments 4 \
      --no-keep-video \
      --output "${output_base}.%(ext)s" \
      >> "$LOG_FILE" 2>&1; then
      return 0
    fi
  done

  return 1
}

cleanup_output_sidecars() {
  local output_base="$1"
  rm -f \
    "${output_base}.webm" \
    "${output_base}.m4a" \
    "${output_base}.opus" \
    "${output_base}.webp" \
    "${output_base}.jpg" \
    "${output_base}.png" \
    "${output_base}.part"
}

download_track() {
  local track_uri="$1"
  local artists="$2"
  local canonical_artists="$3"
  local track_name="$4"
  local album_name="$5"
  local year="$6"
  local search_queries="$7"
  local safe_artists safe_track output_base output_file

  safe_artists=$(sanitize_filename_part "$artists")
  safe_track=$(sanitize_filename_part "$track_name")
  output_base="$MUSIC_DIR/${safe_artists} - ${safe_track}"
  output_file="${output_base}.mp3"

  if is_track_uri_downloaded "$track_uri"; then
    update_progress "skipped"
    return 0
  fi

  if is_complete_mp3 "$output_file"; then
    register_track_uri "$track_uri" "$output_file"
    update_progress "skipped"
    return 0
  fi

  rm -f "${output_base}.mp3"
  cleanup_output_sidecars "$output_base"

  if download_with_queries "$output_base" "$search_queries" && is_complete_mp3 "$output_file"; then
    cleanup_output_sidecars "$output_base"
    register_track_uri "$track_uri" "$output_file"
    update_progress "downloaded"
    return 0
  fi

  log_failure "$track_uri" "$artists" "$track_name" "$search_queries"
  rm -f "${output_base}.mp3"
  cleanup_output_sidecars "$output_base"
  update_progress "failed"
  return 1
}

process_track_line() {
  local line="$1"
  local track_uri artists display_artists canonical_artists track_name album_name year search_queries

  IFS=$'\t' read -r track_uri artists display_artists canonical_artists track_name album_name year search_queries <<< "$line"
  download_track "$track_uri" "$display_artists" "$canonical_artists" "$track_name" "$album_name" "$year" "$search_queries"
}

export MUSIC_DIR CSV_FILE ALIASES_FILE FAILED_FILE LOG_FILE STATE_DIR TRACKS_FILE TRACK_URI_DIR PROGRESS_FILE LOCK_FILE TOTAL_TRACKS START_TS
export -f is_complete_mp3 sanitize_filename_part track_uri_key track_uri_marker register_track_uri is_track_uri_downloaded format_duration render_progress update_progress log_failure download_with_queries cleanup_output_sidecars download_track process_track_line

printf 'Starting download from %s: %d tracks, %d parallel jobs\n' "$CSV_FILE" "$TOTAL_TRACKS" "$PARALLEL_JOBS"

while IFS= read -r line; do
  printf '%s\0' "$line"
done < "$TRACKS_FILE" | xargs -0 -P "$PARALLEL_JOBS" -I {} bash -c 'process_track_line "$1"' _ "{}"
