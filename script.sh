#!/usr/bin/env bash

set -u

MUSIC_DIR="music"
CSV_FILE="Liked_Songs.csv"
FAILED_FILE="failed.txt"
LOG_FILE="download.log"
STATE_DIR=".download_state"
TRACKS_FILE="$STATE_DIR/tracks.tsv"
PROGRESS_FILE="$STATE_DIR/progress.tsv"
LOCK_FILE="$STATE_DIR/progress.lock"
PARALLEL_JOBS="${PARALLEL_JOBS:-12}"

mkdir -p "$MUSIC_DIR" "$STATE_DIR"
: > "$FAILED_FILE"
: > "$LOG_FILE"
: > "$TRACKS_FILE"

python3 - "$CSV_FILE" "$TRACKS_FILE" <<'PY'
import csv
import re
import sys
from pathlib import Path

csv_path = Path(sys.argv[1])
tracks_path = Path(sys.argv[2])

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

with csv_path.open(newline="", encoding="utf-8-sig") as f:
    reader = csv.DictReader(f)
    required_fields = ["Track Name", "Artist Name(s)", "Album Name", "Release Date"]
    missing = [field for field in required_fields if field not in (reader.fieldnames or [])]
    if missing:
        raise SystemExit(f"CSV is missing required columns: {', '.join(missing)}")

    with tracks_path.open("w", encoding="utf-8") as out:
        for row in reader:
            track_name = normalize_spaces(row.get("Track Name", ""))
            artists = normalize_spaces(row.get("Artist Name(s)", ""))
            album = normalize_spaces(row.get("Album Name", ""))
            year = extract_year(row.get("Release Date", ""))

            if not track_name or not artists:
                continue

            primary_artist = normalize_spaces(artists.split(";")[0])
            search_track = clean_track_name(track_name)
            search_parts = [primary_artist, search_track]
            if album:
                search_parts.append(album)
            if year:
                search_parts.append(year)
            search_parts.append("audio")
            search_query = " ".join(part for part in search_parts if part)

            fields = [artists, track_name, album, year, search_query]
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
  local artists="$1"
  local track_name="$2"
  local search_query="$3"

  printf '%s - %s | search: %s\n' "$artists" "$track_name" "$search_query" >> "$FAILED_FILE"
}

download_track() {
  local artists="$1"
  local track_name="$2"
  local album_name="$3"
  local year="$4"
  local search_query="$5"
  local safe_artists safe_track output_base output_file

  safe_artists=$(sanitize_filename_part "$artists")
  safe_track=$(sanitize_filename_part "$track_name")
  output_base="$MUSIC_DIR/${safe_artists} - ${safe_track}"
  output_file="${output_base}.mp3"

  if is_complete_mp3 "$output_file"; then
    update_progress "skipped"
    return 0
  fi

  rm -f "${output_base}.mp3" "${output_base}.webm" "${output_base}.m4a" "${output_base}.opus" "${output_base}.webp" "${output_base}.jpg" "${output_base}.png" "${output_base}.part"

  printf '[search] %s - %s | query: %s\n' "$artists" "$track_name" "$search_query" >> "$LOG_FILE"

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
    >> "$LOG_FILE" 2>&1 && is_complete_mp3 "$output_file"; then
    rm -f "${output_base}.webm" "${output_base}.m4a" "${output_base}.opus" "${output_base}.webp" "${output_base}.jpg" "${output_base}.png" "${output_base}.part"
    update_progress "downloaded"
    return 0
  fi

  log_failure "$artists" "$track_name" "$search_query"
  rm -f "${output_base}.mp3" "${output_base}.webm" "${output_base}.m4a" "${output_base}.opus" "${output_base}.webp" "${output_base}.jpg" "${output_base}.png" "${output_base}.part"
  update_progress "failed"
  return 1
}

process_track_line() {
  local line="$1"
  local artists track_name album_name year search_query

  IFS=$'\t' read -r artists track_name album_name year search_query <<< "$line"
  download_track "$artists" "$track_name" "$album_name" "$year" "$search_query"
}

export MUSIC_DIR CSV_FILE FAILED_FILE LOG_FILE STATE_DIR TRACKS_FILE PROGRESS_FILE LOCK_FILE TOTAL_TRACKS START_TS
export -f is_complete_mp3 sanitize_filename_part format_duration render_progress update_progress log_failure download_track process_track_line

printf 'Starting download from %s: %d tracks, %d parallel jobs\n' "$CSV_FILE" "$TOTAL_TRACKS" "$PARALLEL_JOBS"

while IFS= read -r line; do
  printf '%s\0' "$line"
done < "$TRACKS_FILE" | xargs -0 -P "$PARALLEL_JOBS" -I {} bash -c 'process_track_line "$1"' _ "{}"
