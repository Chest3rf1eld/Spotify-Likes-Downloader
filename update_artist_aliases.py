#!/usr/bin/env python3

import argparse
import csv
import re
from pathlib import Path


CYRILLIC_TO_LATIN = {
    "а": "a",
    "б": "b",
    "в": "v",
    "г": "g",
    "д": "d",
    "е": "e",
    "ё": "e",
    "ж": "zh",
    "з": "z",
    "и": "i",
    "й": "i",
    "к": "k",
    "л": "l",
    "м": "m",
    "н": "n",
    "о": "o",
    "п": "p",
    "р": "r",
    "с": "s",
    "т": "t",
    "у": "u",
    "ф": "f",
    "х": "kh",
    "ц": "ts",
    "ч": "ch",
    "ш": "sh",
    "щ": "shch",
    "ъ": "",
    "ы": "y",
    "ь": "",
    "э": "e",
    "ю": "yu",
    "я": "ya",
}

NON_ALNUM = re.compile(r"[^a-z0-9]+")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Scan Liked_Songs.csv, find conservative artist alias matches, and update artist_aliases.tsv."
    )
    parser.add_argument("--csv", default="Liked_Songs.csv", help="Path to Spotify CSV export.")
    parser.add_argument("--aliases", default="artist_aliases.tsv", help="Path to alias TSV file.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print new alias candidates without writing the TSV file.",
    )
    parser.add_argument(
        "--review-disputed",
        action="store_true",
        help="Interactively review disputed alias candidates and optionally append approved ones to the TSV file.",
    )
    return parser.parse_args()


def normalize_spaces(value: str) -> str:
    return " ".join((value or "").replace("\ufeff", "").split())


def is_cyrillic_name(value: str) -> bool:
    return any("а" <= ch.lower() <= "я" or ch.lower() == "ё" for ch in value)


def is_latin_name(value: str) -> bool:
    return any("a" <= ch.lower() <= "z" for ch in value)


def transliterate_cyrillic(value: str) -> str:
    out = []
    for ch in value.lower():
        out.append(CYRILLIC_TO_LATIN.get(ch, ch))
    return "".join(out)


def normalize_key(value: str) -> str:
    value = value.lower()
    value = value.replace("&", "and")
    value = value.replace("+", "plus")
    value = NON_ALNUM.sub("", value)
    return value


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


def load_alias_lines(path: Path) -> tuple[list[str], dict[str, list[str]]]:
    if not path.exists():
        return [], {}

    lines = path.read_text(encoding="utf-8").splitlines()
    aliases = {}
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [normalize_spaces(part) for part in raw_line.split("\t")]
        if len(parts) < 2 or not parts[0]:
            continue
        canonicals = unique_preserve_order(parts[1:])
        if not canonicals:
            continue
        aliases[parts[0].casefold()] = canonicals
    return lines, aliases


def load_artists(csv_path: Path) -> tuple[set[str], dict[str, int]]:
    artists = set()
    counts = {}

    with csv_path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames or "Artist Name(s)" not in reader.fieldnames:
            raise SystemExit("CSV must contain 'Artist Name(s)' column")

        for row in reader:
            raw_value = normalize_spaces(row.get("Artist Name(s)", ""))
            for part in raw_value.split(";"):
                artist = normalize_spaces(part)
                if not artist:
                    continue
                artists.add(artist)
                counts[artist] = counts.get(artist, 0) + 1

    return artists, counts


def build_candidates(artists: set[str], existing_aliases: dict[str, list[str]]) -> list[tuple[str, list[str]]]:
    latin_artists = [artist for artist in artists if is_latin_name(artist) and not is_cyrillic_name(artist)]
    cyrillic_artists = [artist for artist in artists if is_cyrillic_name(artist)]

    by_translit = {}
    for artist in cyrillic_artists:
        key = normalize_key(transliterate_cyrillic(artist))
        if key:
            by_translit.setdefault(key, []).append(artist)

    candidates = []
    for artist in latin_artists:
        if artist.casefold() in existing_aliases:
            continue

        key = normalize_key(artist)
        matches = by_translit.get(key, [])
        if len(matches) != 1:
            continue

        canonical = matches[0]
        if canonical.casefold() == artist.casefold():
            continue
        candidates.append((artist, [canonical]))

    candidates.sort(key=lambda item: (item[1].casefold(), item[0].casefold()))
    return candidates


def build_disputed_candidates(artists: set[str], existing_aliases: dict[str, list[str]]) -> list[tuple[str, list[str]]]:
    latin_artists = [artist for artist in artists if is_latin_name(artist) and not is_cyrillic_name(artist)]
    cyrillic_artists = [artist for artist in artists if is_cyrillic_name(artist)]

    by_translit = {}
    for artist in cyrillic_artists:
        key = normalize_key(transliterate_cyrillic(artist))
        if key:
            by_translit.setdefault(key, []).append(artist)

    disputed = []
    for artist in latin_artists:
        if artist.casefold() in existing_aliases:
            continue

        key = normalize_key(artist)
        matches = sorted(set(by_translit.get(key, [])), key=str.casefold)
        if len(matches) > 1:
            disputed.append((artist, matches))

    disputed.sort(key=lambda item: item[0].casefold())
    return disputed


def write_aliases(path: Path, lines: list[str], existing_aliases: dict[str, list[str]], new_aliases: list[tuple[str, list[str]]]) -> None:
    merged_aliases = {key: list(values) for key, values in existing_aliases.items()}
    alias_names = {}

    for raw_line in lines:
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        parts = [normalize_spaces(part) for part in raw_line.split("\t")]
        if len(parts) < 2 or not parts[0]:
            continue
        alias_names[parts[0].casefold()] = parts[0]

    for alias, canonicals in new_aliases:
        key = alias.casefold()
        alias_names[key] = alias
        merged_aliases[key] = unique_preserve_order(merged_aliases.get(key, []) + canonicals)

    output = []
    written = set()
    for raw_line in lines:
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            output.append(raw_line)
            continue

        parts = [normalize_spaces(part) for part in raw_line.split("\t")]
        if len(parts) < 2 or not parts[0]:
            output.append(raw_line)
            continue

        alias = parts[0]
        key = alias.casefold()
        canonicals = merged_aliases.get(key, unique_preserve_order(parts[1:]))
        if not canonicals:
            continue
        output.append("\t".join([alias] + canonicals))
        written.add(key)

    for key in sorted(merged_aliases.keys()):
        if key in written:
            continue
        alias = alias_names[key]
        output.append("\t".join([alias] + merged_aliases[key]))

    path.write_text("\n".join(output) + "\n", encoding="utf-8")


def prompt_disputed_candidates(
    disputed_candidates: list[tuple[str, list[str]]],
    counts: dict[str, int],
) -> list[tuple[str, list[str]]]:
    approved = []

    print("Disputed alias candidates:")
    print("Enter numbers separated by commas to approve several variants, 'a' for all, 's' to skip, or 'q' to stop review.\n")

    for index, (alias, options) in enumerate(disputed_candidates, 1):
        print(f"[{index}] Alias: {alias} (seen {counts.get(alias, 0)}x)")
        for option_index, option in enumerate(options, 1):
            print(f"  {option_index}. {option} (seen {counts.get(option, 0)}x)")

        while True:
            choice = input("Choice: ").strip().lower()
            if choice in {"s", ""}:
                print("  skipped\n")
                break
            if choice == "q":
                return approved
            if choice == "a":
                approved.append((alias, options))
                print(f"  approved: {alias} -> {', '.join(options)}\n")
                break
            selected_values = []
            valid = True
            for token in [item.strip() for item in choice.split(",") if item.strip()]:
                if not token.isdigit():
                    valid = False
                    break
                selected_index = int(token) - 1
                if not 0 <= selected_index < len(options):
                    valid = False
                    break
                selected_values.append(options[selected_index])
            selected_values = unique_preserve_order(selected_values)
            if valid and selected_values:
                approved.append((alias, selected_values))
                print(f"  approved: {alias} -> {', '.join(selected_values)}\n")
                break
            print("  invalid input")

    return approved


def main() -> None:
    args = parse_args()
    csv_path = Path(args.csv)
    aliases_path = Path(args.aliases)

    if not csv_path.exists():
        raise SystemExit(f"CSV file not found: {csv_path}")

    lines, existing_aliases = load_alias_lines(aliases_path)
    artists, counts = load_artists(csv_path)
    candidates = build_candidates(artists, existing_aliases)
    disputed_candidates = build_disputed_candidates(artists, existing_aliases)

    if not candidates and not disputed_candidates:
        print("No new alias candidates found.")
        return

    if args.dry_run:
        for alias, canonicals in candidates:
            print(f"{counts.get(alias, 0):>4}  {alias}\t{', '.join(canonicals)}")
        if disputed_candidates:
            print("\nDisputed candidates:")
            for alias, options in disputed_candidates:
                joined_options = ", ".join(options)
                print(f"{counts.get(alias, 0):>4}  {alias}\t{joined_options}")
        print(f"\n{len(candidates)} new alias candidates.")
        if disputed_candidates:
            print(f"{len(disputed_candidates)} disputed alias candidates.")
        return

    approved_disputed = []
    if args.review_disputed and disputed_candidates:
        approved_disputed = prompt_disputed_candidates(disputed_candidates, counts)
        for alias, canonicals in approved_disputed:
            existing_aliases[alias.casefold()] = unique_preserve_order(existing_aliases.get(alias.casefold(), []) + canonicals)

    all_new_aliases = candidates + [
        (alias, canonicals)
        for alias, canonicals in approved_disputed
        if alias.casefold() not in {existing.casefold() for existing, _ in candidates}
    ]

    if not all_new_aliases:
        print("No aliases approved for writing.")
        return

    write_aliases(aliases_path, lines, existing_aliases, all_new_aliases)
    for alias, canonicals in all_new_aliases:
        print(f"added: {alias}\t{', '.join(canonicals)}")
    print(f"\nUpdated {aliases_path} with {len(all_new_aliases)} new aliases.")


if __name__ == "__main__":
    main()
