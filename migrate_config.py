"""
Migrates an existing setup from the old German names to the English ones.

- config.toml: renames sections and keys (values and comments are kept),
  a backup is written to config.toml.bak
- profile folder: renames "profil" to "profile" (contains the Google session)

Safe to run multiple times: an already migrated setup is left untouched.

Usage:
    python migrate_config.py [data folder]     (default: current folder)
    python migrate_config.py data --dry-run    (only show what would change)
"""
import argparse
import re
import shutil
import sys
import tomllib
from pathlib import Path

SECTIONS = {"allgemein": "general"}

KEYS = {
    "general": {
        "intervall_sekunden": "interval_seconds",
        "abgelaufen_nach_fehlversuchen": "expired_after_failures",
        "beenden_nach_abgelaufenen_runden": "exit_after_expired_rounds",
    },
    "mqtt": {
        "aktiv": "enabled",
        "benutzer": "username",
        "passwort": "password",
        "ca_datei": "ca_file",
        "tls_unsicher": "tls_insecure",
    },
    "homeassistant": {
        "praefix": "prefix",
    },
}

HEADER = re.compile(r"^(\s*\[\s*)([A-Za-z0-9_-]+)(\s*\].*)$")
KEY = re.compile(r"""^(\s*)(["']?)([A-Za-z0-9_-]+)(["']?)(\s*=.*)$""")


def migrate_text(text):
    """Returns the migrated text and a list of performed renames."""
    section = None
    changes = []
    out = []
    for line in text.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        ending = line[len(body):]
        m = HEADER.match(body)
        if m:
            name = m.group(2)
            if name in SECTIONS:
                changes.append(f"[{name}] -> [{SECTIONS[name]}]")
                name = SECTIONS[name]
                body = f"{m.group(1)}{name}{m.group(3)}"
            section = name
        else:
            m = KEY.match(body)
            if m and section in KEYS and m.group(3) in KEYS[section]:
                old, new = m.group(3), KEYS[section][m.group(3)]
                changes.append(f"[{section}] {old} -> {new}")
                body = f"{m.group(1)}{m.group(2)}{new}{m.group(4)}{m.group(5)}"
        out.append(body + ending)
    return "".join(out), changes


def expected(old):
    """Applies the mapping to the parsed old config (used to verify the text migration)."""
    new = {}
    for section, values in old.items():
        target = SECTIONS.get(section, section)
        if not isinstance(values, dict):
            new[target] = values
            continue
        mapping = KEYS.get(target, {})
        new[target] = {mapping.get(k, k): v for k, v in values.items()}
    return new


def migrate_config(path, dry_run):
    if not path.is_file():
        print(f"{path} not found, skipping config migration")
        return True
    text = path.read_text(encoding="utf-8")
    try:
        old = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        print(f"ERROR: {path} is not valid TOML, nothing changed: {e}")
        return False

    new_text, changes = migrate_text(text)
    if not changes:
        print(f"{path}: already migrated, nothing to do")
        return True

    try:
        new = tomllib.loads(new_text)
    except tomllib.TOMLDecodeError as e:
        print(f"ERROR: migrated {path} would be invalid TOML, nothing changed: {e}")
        return False
    if new != expected(old):
        print(f"ERROR: migration result of {path} does not match the expected values, "
              "nothing changed (are old and new keys mixed?)")
        return False

    for change in changes:
        print(f"  {change}")
    if dry_run:
        print(f"{path}: {len(changes)} change(s) (dry run, nothing written)")
        return True
    backup = path.with_name(path.name + ".bak")
    shutil.copy2(path, backup)
    path.write_text(new_text, encoding="utf-8")
    print(f"{path}: migrated, backup saved as {backup}")
    return True


def migrate_profile(data_dir, dry_run):
    old, new = data_dir / "profil", data_dir / "profile"
    if not old.is_dir():
        return True
    if new.exists():
        print(f"WARNING: both {old} and {new} exist, leaving both untouched "
              f"({new} is used)")
        return True
    if dry_run:
        print(f"{old} -> {new} (dry run, nothing renamed)")
        return True
    old.rename(new)
    print(f"{old} renamed to {new}")
    return True


def main():
    parser = argparse.ArgumentParser(description="Migrate config.toml and profile folder to English names.")
    parser.add_argument("data_dir", nargs="?", default=".", help="folder with config.toml (default: .)")
    parser.add_argument("--dry-run", action="store_true", help="only show what would change")
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    ok = migrate_config(data_dir / "config.toml", args.dry_run)
    ok = migrate_profile(data_dir, args.dry_run) and ok
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
