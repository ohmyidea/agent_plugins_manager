"""Command-line entry point."""

import argparse
import sys
from collections.abc import Iterable
from functools import partial
from pathlib import Path

from .config import (
    DEFAULT_CONFIGS_DIR,
    DEFAULT_OUTPUT_DIR,
    PLUGIN_RECIPES_DIR,
    PluginSpec,
    find_configs_dir,
    load_specs,
)
from .marketplace import MARKETPLACE_PATHS, write_marketplaces
from .publisher import DEFAULT_COMMIT_MESSAGE, publish
from .updater import Updater
from .utils.tree import SyncError, Tree, exists


def _configs_dir(path: Path | None) -> Path:
    if path is None:
        try:
            return find_configs_dir().resolve()
        except SyncError as error:
            raise SyncError(
                "Configuration directory not found. Pass --configs."
            ) from error
    configs = path.resolve()
    if not (configs / PLUGIN_RECIPES_DIR).is_dir():
        raise SyncError(
            f"Plugin recipe directory missing: {configs / PLUGIN_RECIPES_DIR}"
        )
    return configs


def _parser() -> argparse.ArgumentParser:
    # fmt: off
    make_parser = partial(
        argparse.ArgumentParser,
        formatter_class=partial(argparse.HelpFormatter, max_help_position=36),
    )
    # Unspecified subcommand options must leave values parsed before the command intact.
    configs = make_parser(add_help=False, argument_default=argparse.SUPPRESS)
    configs.add_argument("--configs", type=Path, help=f"configuration directory (default name: {DEFAULT_CONFIGS_DIR})")
    output = make_parser(add_help=False, argument_default=argparse.SUPPRESS)
    output.add_argument("--output", type=Path, help=f"marketplace directory (default name: {DEFAULT_OUTPUT_DIR})")

    parser = make_parser(
        prog="plugins-manager",
        description="Generate, update, or publish a plugin marketplace.",
        parents=[configs, output],
    )
    commands = parser.add_subparsers(dest="command", title="commands", metavar="COMMAND", parser_class=make_parser)

    listing = commands.add_parser("list", parents=[configs], help="list plugin recipes")
    listing.set_defaults(run=_run_list)

    generate = commands.add_parser("generate", parents=[configs, output], help="generate plugins and marketplace files locally")
    generate.set_defaults(run=_run_generate)
    generate.add_argument("names", nargs="*", help="plugin names (default: all configured plugins)")
    generate.add_argument("--exclude", nargs="+", action="extend", default=[], metavar="PLUGIN", help="skip one or more plugins and their marketplace entries")
    generate.add_argument("--type", dest="marketplace_types", action="append", choices=MARKETPLACE_PATHS, help="marketplace type (default: all)")
    generate.add_argument("--dry-run", action="store_true", help="validate plugins without writing output")

    update = commands.add_parser("update", parents=[configs, output], help="refresh local plugins and marketplace files")
    update.set_defaults(run=_run_update)
    update.add_argument("names", nargs="*", help="plugin names (default: all configured plugins)")
    update.add_argument("--exclude", nargs="+", action="extend", default=[], metavar="PLUGIN", help="skip one or more plugins and their marketplace entries")
    update.add_argument("--dry-run", action="store_true", help="validate plugins without writing output")

    publication = commands.add_parser(
        "publish", parents=[output], help="push generated marketplace output to GitHub",
        description=f"Push an existing marketplace. The default input is ./{DEFAULT_OUTPUT_DIR}",
    )
    publication.set_defaults(run=_run_publish)
    publication.add_argument("--repo", required=True, help="GitHub owner/repository or Git URL")
    publication.add_argument("--branch", required=True, help="destination branch")
    publication.add_argument("--message", default=DEFAULT_COMMIT_MESSAGE, help="Git commit message")
    publication.add_argument("--dry-run", action="store_true", help="preview Git changes without pushing")
    # fmt: on
    return parser


def _run_list(args: argparse.Namespace, _parser: argparse.ArgumentParser) -> None:
    configs = _configs_dir(args.configs)
    specs = load_specs(configs / PLUGIN_RECIPES_DIR)
    header = ("Plugin", "Layout", "Repository", "Ref")
    rows = [(spec.name, spec.layout, spec.repo, spec.ref) for spec in specs.values()]
    widths = [max(map(len, column)) for column in zip(header, *rows)]

    print(f"Configs: {configs}")
    print()
    print(" | ".join(label.ljust(width) for label, width in zip(header, widths)))
    print("-+-".join("-" * width for width in widths))
    for row in rows:
        print(" | ".join(value.ljust(width) for value, width in zip(row, widths)).rstrip())


def _make_updater(configs: Path, output: Path | None) -> Updater:
    return Updater(configs, output=output or configs.parent / DEFAULT_OUTPUT_DIR)


def _existing_marketplace_types(output: Path) -> list[str]:
    if output.is_dir():
        tree = Tree(output)
        types = []
        for kind, relative in MARKETPLACE_PATHS.items():
            if exists(tree.path(relative)):
                tree.file(relative)
                types.append(kind)
        if types:
            return types
    raise SyncError(f"No generated marketplace found in {output}. Run generate first.")


def _build(
    updater: Updater,
    specs: dict[str, PluginSpec],
    names: Iterable[str],
    *,
    dry_run: bool,
    types: Iterable[str] | None,
) -> None:
    for name in names:
        spec = specs[name]
        print(f"{name}: staging {spec.ref}", flush=True)
        try:
            result = updater.update(spec, dry_run=dry_run)
        except (SyncError, OSError, ValueError) as error:
            raise SyncError(f"Plugin {name!r} failed: {error}") from error
        status = "updated" if result.published else "validated"
        print(
            f"{name}: {status} {result.version} ({result.commit[:12]}) -> {result.destination}"
        )
    if not dry_run:
        for path in write_marketplaces(
            updater.output_root, specs.values(), types=types
        ):
            print(f"Marketplace: {path}")


def _select_plugins(
    specs: dict[str, PluginSpec], args: argparse.Namespace, parser: argparse.ArgumentParser
) -> tuple[dict[str, PluginSpec], list[str]]:
    names = list(dict.fromkeys(args.names or specs))
    mentioned = dict.fromkeys([*names, *args.exclude])
    unknown = [name for name in mentioned if name not in specs]
    if unknown:
        parser.error(f"unknown plugin(s): {', '.join(unknown)}")

    excluded = set(args.exclude)
    available = {name: spec for name, spec in specs.items() if name not in excluded}
    selected = [name for name in names if name in available]
    return available, selected


def _run_generate(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    configs = _configs_dir(args.configs)
    specs = load_specs(configs / PLUGIN_RECIPES_DIR)
    specs, selected = _select_plugins(specs, args, parser)
    if not selected:
        print("No plugins selected.")
        return

    specs = {name: specs[name] for name in selected}
    updater = _make_updater(configs, args.output)
    _build(updater, specs, selected, dry_run=args.dry_run, types=args.marketplace_types)
    if not args.dry_run:
        print(f"Local marketplace: {updater.output_root}")


def _run_update(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    configs = _configs_dir(args.configs)
    updater = _make_updater(configs, args.output)
    types = _existing_marketplace_types(updater.output_root)
    specs = load_specs(configs / PLUGIN_RECIPES_DIR)
    specs, selected = _select_plugins(specs, args, parser)
    _build(updater, specs, selected, dry_run=args.dry_run, types=types)


def _run_publish(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    if args.configs is not None:
        parser.error(
            "publish does not accept --configs. Use --output to select the marketplace."
        )
    output = args.output or Path(DEFAULT_OUTPUT_DIR)
    result = publish(
        output, args.repo, args.branch, message=args.message, dry_run=args.dry_run
    )
    if args.dry_run:
        status = "Prepared"
    elif result.pushed:
        status = "Published"
    else:
        status = "Up to date"
    print(
        f"{status}: {result.repo} branch {result.branch}, {result.changed_files} file changes"
    )
    if result.commit is not None:
        print(f"Commit: {result.commit}")


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    defaults = argparse.Namespace(configs=None, output=None)
    args = parser.parse_args(argv, namespace=defaults)
    if args.command is None:
        parser.print_help()
        return 0
    try:
        args.run(args, parser)
    except (SyncError, OSError, ValueError) as error:
        print(f"plugins-manager: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0
