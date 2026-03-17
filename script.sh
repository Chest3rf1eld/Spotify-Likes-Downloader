mkdir -p music
: > failed.txt
: > download.log

cat songs.txt | xargs -d '\n' -I {} -P 6 bash -c '
track="$1"

yt-dlp \
  --proxy http://127.0.0.1:2080 \
  "ytsearch1:$track official audio" \
  -x \
  --audio-format mp3 \
  --embed-thumbnail \
  --add-metadata \
  --no-playlist \
  --ignore-errors \
  --concurrent-fragments 4 \
  -o "music/%(artist)s - %(title)s.%(ext)s" \
  >> download.log 2>&1

if [ $? -ne 0 ]; then
  echo "$track" >> failed.txt
fi
' _ {}
