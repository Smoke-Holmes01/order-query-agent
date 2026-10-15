"""Generate local demonstration secrets without reading or overwriting existing files."""

from pathlib import Path
import secrets


def main():
    root = Path(__file__).resolve().parents[1]
    pairs = (
        (".env", ".env.example", "replace_with_a_random_local_password"),
        (".env.admin", ".env.admin.example", "replace_with_a_different_random_local_password"),
    )
    for target, example, placeholder in pairs:
        path = root / target
        if path.exists():
            print(f"{target} exists; preserved without reading or overwriting.")
            continue
        template = (root / example).read_text(encoding="utf-8")
        if template.count(placeholder) != 1:
            raise SystemExit(f"{example} must contain exactly one password placeholder")
        try:
            with path.open("x", encoding="utf-8", newline="\n") as stream:
                stream.write(template.replace(placeholder, secrets.token_urlsafe(32)))
        except FileExistsError:
            print(f"{target} was concurrently created; preserved.")
        else:
            print(f"{target} created with a random local password (not displayed).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
