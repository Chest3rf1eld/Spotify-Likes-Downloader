# Spotify Likes Downloader

Этот проект скачивает треки из CSV-экспорта Spotify `Liked Songs` в локальные `mp3`-файлы с помощью `yt-dlp` и `ffmpeg`.

Он рассчитан на большие библиотеки и хранит локальное состояние, поэтому повторные запуски продолжают работу с уже существующей коллекцией, а не начинают всё заново.

## Что делает проект

- Читает `Liked_Songs.csv`
- Строит несколько YouTube-поисковых запросов для каждого трека
- Скачивает аудио через `yt-dlp`
- Конвертирует результат в `mp3`
- Хранит прогресс и привязку `track_uri -> файл` в `.download_state/`
- Записывает нескачанные треки в `failed.txt`
- Поддерживает нормализацию артистов через `artist_aliases.tsv`
- Поддерживает ручные override-запросы через `track_overrides.tsv`

## Требования

- Python 3.10+
- `yt-dlp`
- `ffprobe`
- `ffmpeg`

## Файлы

- `download_music.py`: основной скрипт скачивания
- `script.sh`: обёртка для запуска
- `update_artist_aliases.py`: утилита для поддержки `artist_aliases.tsv`
- `Liked_Songs.csv`: входной экспорт Spotify
- `artist_aliases.tsv`: таблица алиасов артистов
- `track_overrides.tsv`: кастомные search target'ы для конкретных Spotify track URI
- `music/`: итоговые скачанные файлы
- `.download_state/`: локальное состояние для продолжения работы и дедупликации
- `failed.txt`: треки, которые не удалось скачать в текущем запуске

## Сначала экспортируйте список треков

Перед запуском нужен CSV-экспорт Spotify, сохранённый как `Liked_Songs.csv`.

Один из простых вариантов — получить его через Exportify:

https://exportify.net/

1. Открыть Exportify
2. Войти через Spotify
3. Экспортировать `Liked Songs`
4. Сохранить полученный CSV в корне проекта под именем `Liked_Songs.csv`

Скрипт поддерживает только тот же формат CSV, который выдаёт Exportify.
Если использовать другой источник, структура и названия колонок должны полностью совпадать.

## Запуск

Напрямую:

```bash
python3 download_music.py
```

С указанием собственного пути к CSV:

```bash
python3 download_music.py --csv /path/to/Liked_Songs.csv
```

Через обёртку:

```bash
./script.sh
```

## Переменные окружения

- `PARALLEL_JOBS`: число параллельных задач, по умолчанию `4`
- `SPOTIFY_CSV_FILE`: путь к Spotify CSV-экспорту, по умолчанию `Liked_Songs.csv`
- `YT_DLP_PROXY`: proxy для `yt-dlp`, по умолчанию `http://127.0.0.1:2080`
- `YT_DLP_CONCURRENT_FRAGMENTS`: число параллельных фрагментов `yt-dlp`, по умолчанию `1`
- `FFMPEG_THREADS`: число потоков ffmpeg, по умолчанию `1`
- `YT_DLP_COOKIES_FILE`: необязательный путь к cookies-файлу
- `YT_DLP_COOKIES_FROM_BROWSER`: необязательное имя браузера для `yt-dlp --cookies-from-browser`
- `ENABLE_FILE_LOG`: `1`/`true` для включения подробного `download.log`
- `PROGRESS_FLUSH_EVERY`: как часто сбрасывать прогресс на диск, по умолчанию `25`

Пример:

```bash
PARALLEL_JOBS=6 \
SPOTIFY_CSV_FILE=/path/to/Liked_Songs.csv \
YT_DLP_PROXY=http://127.0.0.1:2080 \
YT_DLP_COOKIES_FROM_BROWSER=firefox \
python3 download_music.py
```

## Имена файлов и коллизии

Скрипт старается хранить отдельный файл для каждого Spotify `track_uri`.

Базовое имя:

```text
Artist - Track.mp3
```

Если этот путь уже занят другим `track_uri`, используются fallback-варианты:

```text
Artist - Track [Album].mp3
Artist - Track [Album] [Year].mp3
Artist - Track [suffix].mp3
```

Это позволяет избегать коллизий между разными Spotify-треками с одинаковыми видимыми метаданными.

## Алиасы артистов

`artist_aliases.tsv` позволяет сопоставлять альтернативные написания или транслитерации с предпочтительным каноническим именем артиста.

Формат:

```text
Alias<TAB>Canonical Name
```

Пример:

```text
Noize MC	Нойз МС
Zemfira	Земфира
```

Для поддержки алиасов:

```bash
python3 update_artist_aliases.py --dry-run
python3 update_artist_aliases.py --review-disputed
```

## Track Overrides

`track_overrides.tsv` позволяет переопределять автоматически сгенерированные поисковые запросы для конкретного Spotify track URI.

Формат:

```text
spotify:track:...<TAB>ytsearch1:custom query
```

Вместо `ytsearch...` можно использовать прямые URL.

## Приватность

В проекте могут быть приватные локальные данные, если такие файлы попали в репозиторий.

Не стоит публиковать их без явного намерения:

- `Liked_Songs.csv`
- `failed.txt`
- `download.log`
- `.download_state/`

## Ограничения

- Успех скачивания зависит от доступности YouTube и качества поисковых запросов
- Для части треков могут понадобиться алиасы или ручные overrides
- Разные Spotify-треки могут в итоге вести к одному и тому же аудиоисточнику
- Качество метаданных зависит от источника, который нашёл `yt-dlp`
