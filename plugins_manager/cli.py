"""Command-line entry point."""

import argparse
import sys
from collections.abc import Iterable
from contextlib import ExitStack
from dataclasses import replace
from functools import partial
from pathlib import Path, PurePosixPath

from .config import (
    DEFAULT_CONFIGS_DIR,
    DEFAULT_OUTPUT_DIR,
    PLUGIN_RECIPES_DIR,
    PluginSpec,
    find_configs_dir,
    load_specs,
)
from .marketplace import (
    MARKETPLACE_PATHS, catalog_has_other_reference, catalog_types_for_plugin, write_marketplaces,
)
from .publisher import DEFAULT_COMMIT_MESSAGE, publish
from .updater import UpdateResult, Updater
from .utils.locks import plugin_lock
from .utils.tree import SyncError, Tree, exists, is_link


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
    output.add_argument("--output", type=Path, help="base directory for builds, or marketplace root for publish")

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
    generate.add_argument("--type", dest="marketplace_types", action="append", choices=MARKETPLACE_PATHS, help="platform to generate (repeatable)")
    generate.add_argument("--combined", action="store_true", help=f"share one marketplace under all/{DEFAULT_OUTPUT_DIR}")
    generate.add_argument("--dry-run", action="store_true", help="validate plugins without writing output")

    update = commands.add_parser("update", parents=[configs, output], help="refresh local plugins and marketplace files")
    update.set_defaults(run=_run_update)
    update.add_argument("names", nargs="*", help="plugin names (default: all configured plugins)")
    update.add_argument("--exclude", nargs="+", action="extend", default=[], metavar="PLUGIN", help="skip one or more plugins and their marketplace entries")
    update.add_argument("--type", dest="marketplace_types", action="append", choices=MARKETPLACE_PATHS, help="existing platform to update (repeatable)")
    update.add_argument("--combined", action="store_true", help=f"update all/{DEFAULT_OUTPUT_DIR}")
    update.add_argument("--dry-run", action="store_true", help="validate plugins without writing output")

    publication = commands.add_parser(
        "publish", parents=[output], help="push generated marketplace output to GitHub",
        description=f"Push an existing marketplace. The default input is ./all/{DEFAULT_OUTPUT_DIR}",
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


def _platform_output(base: Path, platform: str) -> Path:
    directory = base / platform
    output = directory / DEFAULT_OUTPUT_DIR
    for path in (directory, output):
        if is_link(path) or (exists(path) and not path.is_dir()):
            raise SyncError(f"Expected a real directory: {path}")
    return output


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


def _report_update(name: str, result: UpdateResult, platform: str | None = None) -> None:
    label = f"{name} [{platform}]" if platform else name
    status = "updated" if result.published else "validated"
    print(f"{label}: {status} {result.version} ({result.commit[:12]}) -> {result.destination}")
    for kind, version in result.platform_versions.items():
        if version != result.version:
            print(f"  {kind} version: {version or 'unspecified'}")


def _build_combined(
    updater: Updater,
    specs: Iterable[PluginSpec],
    *,
    dry_run: bool,
    types: Iterable[str],
) -> None:
    updated_catalogs = set()
    for spec in specs:
        catalog_types = catalog_types_for_plugin(updater.output_root, spec.name, types)

        def sync_catalogs() -> None:
            updated_catalogs.update(write_marketplaces(
                updater.output_root, [spec], types=catalog_types
            ))

        print(f"{spec.name}: staging {spec.ref}", flush=True)
        try:
            result = updater.update(
                spec, dry_run=dry_run, marketplace_types=catalog_types, on_publish=sync_catalogs,
            )
        except (SyncError, OSError, ValueError) as error:
            raise SyncError(f"Plugin {spec.name!r} failed: {error}") from error
        _report_update(spec.name, result)
    for path in sorted(updated_catalogs):
        print(f"Marketplace: {path}")


def _build_split(
    configs: Path, outputs: dict[str, Path], specs: Iterable[PluginSpec], *, dry_run: bool,
) -> None:
    updaters = {kind: Updater(configs, output=path) for kind, path in outputs.items()}
    source = Updater(configs)
    updated_catalogs = set()

    def sync(kind: str, spec: PluginSpec) -> None:
        updated_catalogs.update(write_marketplaces(outputs[kind], [spec], types=(kind,)))

    for spec in specs:
        active = [kind for kind in outputs if kind in spec.platforms]
        if active:
            print(f"{spec.name}: staging {spec.ref}", flush=True)
            try:
                with ExitStack() as stack:
                    reserved = {}
                    if not dry_run:
                        for kind in sorted(active):
                            reserved[kind] = stack.enter_context(plugin_lock(outputs[kind], spec.name))
                    prepared = stack.enter_context(source.prepare(spec))
                    for kind in active:
                        target = spec.platforms[kind]
                        platform_spec = replace(spec, platforms={kind: target})
                        target_root = PurePosixPath(target.root)
                        excluded = {
                            other.root for other in spec.platforms.values()
                            if other.root != "." and not target_root.is_relative_to(other.root)
                        }
                        try:
                            result = updaters[kind].update(
                                platform_spec, dry_run=dry_run, prepared=prepared, exclude_roots=excluded,
                                marketplace_types=(kind,), on_publish=partial(sync, kind, platform_spec),
                                lock_directory=reserved.get(kind),
                            )
                        except (SyncError, OSError, ValueError) as error:
                            raise SyncError(f"{kind}: {error}") from error
                        _report_update(spec.name, result, kind)
            except (SyncError, OSError, ValueError) as error:
                raise SyncError(f"Plugin {spec.name!r} failed: {error}") from error

        for kind, output in outputs.items():
            if kind in spec.platforms or dry_run or not output.is_dir():
                continue
            if exists(output / "plugins" / spec.name) or exists(output / MARKETPLACE_PATHS[kind]):
                if catalog_has_other_reference(output, kind, spec.name):
                    sync(kind, spec)
                else:
                    updaters[kind].remove(spec.name, on_publish=partial(sync, kind, spec))

    for path in sorted(updated_catalogs):
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

    _run_build(args, configs, [specs[name] for name in selected], existing=False)


def _run_update(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    configs = _configs_dir(args.configs)
    specs = load_specs(configs / PLUGIN_RECIPES_DIR)
    specs, selected = _select_plugins(specs, args, parser)
    _run_build(args, configs, [specs[name] for name in selected], existing=True)


def _run_build(args: argparse.Namespace, configs: Path, specs: list[PluginSpec], *, existing: bool) -> None:
    base = (args.output or configs.parent).resolve()
    kinds = tuple(dict.fromkeys(args.marketplace_types or MARKETPLACE_PATHS))
    if args.combined:
        output = _platform_output(base, "all")
        if existing:
            installed = _existing_marketplace_types(output)
            kinds = tuple(kind for kind in installed if not args.marketplace_types or kind in kinds)
            if not kinds:
                raise SyncError(f"No selected marketplace found in {output}. Run generate --combined first.")
        _build_combined(Updater(configs, output=output), specs, dry_run=args.dry_run, types=kinds)
    else:
        outputs = {kind: _platform_output(base, kind) for kind in kinds}
        if existing:
            outputs = {
                kind: path for kind, path in outputs.items()
                if (path / "plugins").is_dir() or exists(path / MARKETPLACE_PATHS[kind])
            }
            if not outputs:
                raise SyncError(f"No generated platform marketplace found in {base}. Run generate first.")
        _build_split(configs, outputs, specs, dry_run=args.dry_run)


def _run_publish(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    if args.configs is not None:
        parser.error(
            "publish does not accept --configs. Use --output to select the marketplace."
        )
    output = args.output or Path("all") / DEFAULT_OUTPUT_DIR
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
