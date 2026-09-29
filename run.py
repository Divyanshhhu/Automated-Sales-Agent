"""CLI entrypoint.

Usage:
    python run.py run --profile NAME [--limit N]
    python run.py profiles
    python run.py import-profile PATH --name NAME [--replace]
    python run.py export-profile NAME PATH
    python run.py serve [--port 8000]

Example (first time):
    python run.py import-profile config/product_profile.json --name Default
    python run.py run --profile Default --limit 25
"""
import argparse
import json
import logging
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()
# Confidence labels contain emoji; Windows falls back to cp1252 when output is
# piped or redirected to a file, which can't encode them and crashes the run.
for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

sys.path.insert(0, str(Path(__file__).resolve().parent))

from src.config import ConfigError, load_config  # noqa: E402
from src.pipeline import run_pipeline  # noqa: E402
from src.profiles import (  # noqa: E402
    InvalidProfileNameError,
    ProfileExistsError,
    ProfileNotFoundError,
    create_profile,
    get_profile_by_name,
    list_profiles,
    update_profile,
)
from src.review import refresh_signals  # noqa: E402


def _positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"must be a positive integer, got {value!r}") from None
    if number < 1:
        raise argparse.ArgumentTypeError(f"must be a positive integer, got {number}")
    return number


def cmd_run(args: argparse.Namespace) -> None:
    profile = get_profile_by_name(args.profile)
    run_id = run_pipeline(profile, args.limit)
    print(f"Run {run_id} finished.")


def cmd_profiles(_: argparse.Namespace) -> None:
    profiles = list_profiles()
    if not profiles:
        print("No profiles yet. Import one with:")
        print("  python run.py import-profile config/product_profile.json --name Default")
        return
    for p in profiles:
        print(f"{p.id:>3}  {p.name}  (updated {p.updated_at[:19]})")


def cmd_import_profile(args: argparse.Namespace) -> None:
    config = load_config(args.path)
    try:
        existing = get_profile_by_name(args.name)
    except ProfileNotFoundError:
        profile = create_profile(args.name, config)
        print(f"Created profile {profile.name!r} (id {profile.id}).")
        return
    if not args.replace:
        raise ProfileExistsError(
            f"A profile named {args.name!r} already exists -- pass --replace to overwrite its config"
        )
    profile = update_profile(existing.id, config=config)
    relabelled = refresh_signals(profile.id)
    print(f"Updated profile {profile.name!r} (id {profile.id}); {relabelled} memo signal(s) re-labelled.")


def cmd_export_profile(args: argparse.Namespace) -> None:
    profile = get_profile_by_name(args.name)
    with open(args.path, "w", encoding="utf-8") as f:
        json.dump(profile.config, f, indent=2, ensure_ascii=False)
        f.write("\n")
    print(f"Exported profile {profile.name!r} to {args.path}.")


def cmd_serve(args: argparse.Namespace) -> None:
    import uvicorn

    from src.web.app import create_app

    # Always 127.0.0.1: the UI has no login, so it must not be reachable from
    # other machines on the network.
    print(f"Open http://127.0.0.1:{args.port} in your browser (Ctrl+C to stop).")
    uvicorn.run(create_app(), host="127.0.0.1", port=args.port, log_level="info")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AI sales prospect research pipeline")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="run the pipeline for a profile")
    p_run.add_argument("--profile", required=True, help="profile name")
    p_run.add_argument("--limit", type=_positive_int, default=25, help="max companies to discover")
    p_run.set_defaults(func=cmd_run)

    p_list = sub.add_parser("profiles", help="list saved profiles")
    p_list.set_defaults(func=cmd_profiles)

    p_import = sub.add_parser("import-profile", help="create a profile from a JSON file")
    p_import.add_argument("path")
    p_import.add_argument("--name", required=True)
    p_import.add_argument("--replace", action="store_true", help="overwrite an existing profile's config")
    p_import.set_defaults(func=cmd_import_profile)

    p_export = sub.add_parser("export-profile", help="write a profile's config to a JSON file")
    p_export.add_argument("name")
    p_export.add_argument("path")
    p_export.set_defaults(func=cmd_export_profile)

    p_serve = sub.add_parser("serve", help="start the local web UI")
    p_serve.add_argument("--port", type=_positive_int, default=8000)
    p_serve.set_defaults(func=cmd_serve)
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    try:
        args.func(args)
    except (ConfigError, ProfileNotFoundError, ProfileExistsError, InvalidProfileNameError) as exc:
        sys.exit(str(exc))


if __name__ == "__main__":
    main()
