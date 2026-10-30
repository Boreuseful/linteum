"""Discovery, YAML parsing, field validation and path guarding (§4 / §8.3).

Discovery is deliberately **code-free**: it reads ``plugin.yaml`` and the
declared skill/mode paths but never imports the entry module or calls
``setup``.  This is what makes ``plugin.list`` and CLI ``plugin validate`` safe
to run against untrusted directories.

Every path a manifest declares is resolved through :func:`_within_root`, which
rejects absolute paths, ``..`` segments, NUL bytes, symlinks and real paths that
escape the plugin root (I-security, §4.5 / §12.2).
"""

from __future__ import annotations

import hashlib
import unicodedata
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Optional

import yaml

from .errors import (
    PluginLimit,
    PluginManifestError,
    PluginPathError,
    PluginUnsupported,
)
from .model import (
    ENTRY_PATTERN,
    PLUGIN_ID_PATTERN,
    PLUGIN_VERSION_PATTERN,
    RUNTIME_INPROC,
    SIDE_EFFECTS,
    TOOL_BACKENDS,
    TOOL_NAME_PATTERN,
    TRUST_VALUES,
    PluginCatalog,
    PluginEntry,
    PluginIssue,
    PluginLimits,
    PluginManifest,
    ToolDeclaration,
)

#: The only manifest file name.
MANIFEST_NAME = "plugin.yaml"
#: Key names declared by future API versions and rejected loudly (§15.2).
_FUTURE_TOP_LEVEL = frozenset(
    {"subscribes", "publishes", "needs_secrets", "limits", "ordering"}
)
_FUTURE_CAPABILITIES = frozenset(
    {"adapters", "hooks", "subagents", "mcp_servers"}
)
_TOP_LEVEL_KEYS = frozenset(
    {
        "id",
        "version",
        "api_version",
        "description",
        "entry",
        "runtime",
        "trust",
        "capabilities",
    }
)
_CAPABILITY_KEYS = frozenset({"tools", "skills", "modes"})
_TOOL_KEYS = frozenset(
    {
        "name",
        "side_effect",
        "timeout",
        "concurrency",
        "idempotent",
        "requires_approval",
        "backend",
    }
)

_MAX_DESCRIPTION_CHARS = 512


# ---------------------------------------------------------------------------
# YAML with duplicate-key rejection (§4.3)
# ---------------------------------------------------------------------------

class _UniqueKeyLoader(yaml.SafeLoader):
    """``SafeLoader`` that rejects duplicate mapping keys instead of last-wins."""


def _construct_unique_mapping(loader: _UniqueKeyLoader, node, deep: bool = False):
    mapping: dict = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            hash(key)
        except TypeError as exc:  # pragma: no cover - YAML keys are hashable
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping", node.start_mark,
                f"unhashable key {key!r}", key_node.start_mark,
            ) from exc
        if key in mapping:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping", node.start_mark,
                f"duplicate key {key!r}", key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping
)


# ---------------------------------------------------------------------------
# Small field helpers
# ---------------------------------------------------------------------------

def _manifest_error(message: str, *, key: Optional[str] = None, hint: str = ""):
    return PluginManifestError(message, key=key, hint=hint)


def _unknown_keys(
    mapping: Mapping, allowed: frozenset, *, path: str, future: frozenset = frozenset(),
) -> None:
    for key in mapping:
        if not isinstance(key, str) or key.startswith("x-"):
            continue
        if key in future:
            raise PluginUnsupported(
                f"{path}.{key} is not supported in api_version 1",
                key=f"{path}.{key}".lstrip("."),
                hint="remove the field or target a future plugin api_version",
            )
        if key not in allowed:
            raise _manifest_error(
                f"unknown field {key!r} under {path or 'plugin.yaml'}",
                key=f"{path}.{key}".lstrip("."),
            )


def _require_str(value: Any, *, field: str, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise _manifest_error(
            f"{field} must be a non-empty string", key=field,
        )
    return value


def _optional_bool(value: Any, *, field: str, default: bool) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise _manifest_error(f"{field} must be a boolean", key=field)
    return value


def _number(value: Any, *, field: str, default: float) -> float:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _manifest_error(f"{field} must be a number", key=field)
    return float(value)


def _integer(value: Any, *, field: str, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise _manifest_error(f"{field} must be an integer", key=field)
    return value


def normalise_description(value: Any) -> str:
    """One safe display line, no control characters, at most 512 chars."""
    if value is None:
        return ""
    if not isinstance(value, str):
        raise _manifest_error("description must be a string", key="description")
    text = value.replace("\r", " ").replace("\n", " ").replace("\t", " ")
    text = "".join(
        ch for ch in text if unicodedata.category(ch) not in ("Cc", "Cf")
    )
    text = " ".join(text.split())
    return text[:_MAX_DESCRIPTION_CHARS]


# ---------------------------------------------------------------------------
# Path guard (§4.5 / §12.2)
# ---------------------------------------------------------------------------

def _safe_relative(value: Any, *, field: str) -> PurePosixPath:
    text = _require_str(value, field=field)
    if "\x00" in text:
        raise PluginPathError(f"{field} contains a NUL byte", key=field)
    if "\\" in text:
        raise PluginPathError(
            f"{field} must use '/' as the path separator", key=field,
        )
    path = PurePosixPath(text)
    if path.is_absolute():
        raise PluginPathError(f"{field} must be relative", key=field)
    if not path.parts or any(part in ("..", "") for part in path.parts):
        raise PluginPathError(
            f"{field} must not contain '..' or empty segments", key=field,
        )
    return path


def _within_root(root: Path, relative: PurePosixPath, *, field: str) -> Path:
    """Resolve ``relative`` under ``root`` and prove it stays inside.

    The declared item itself must not be a symlink; the real path must be
    contained in the real plugin root.  Returns the canonical path.
    """
    target = root.joinpath(*relative.parts)
    try:
        if target.is_symlink():
            raise PluginPathError(
                f"{field} must not be a symlink", key=field,
            )
        if not target.exists():
            raise PluginPathError(f"{field} does not exist", key=field)
        real = target.resolve(strict=True)
        root_real = root.resolve(strict=True)
    except PluginPathError:
        raise
    except OSError as exc:
        raise PluginPathError(
            f"{field} cannot be resolved: {type(exc).__name__}",
            key=field, detail=str(exc), exc_type=type(exc).__name__,
        ) from exc
    if real != root_real and root_real not in real.parents:
        raise PluginPathError(
            f"{field} escapes the plugin root", key=field,
        )
    return real


# ---------------------------------------------------------------------------
# Manifest parsing
# ---------------------------------------------------------------------------

def _read_text(root: Path, limits: PluginLimits) -> tuple[str, bytes]:
    manifest_path = root / MANIFEST_NAME
    try:
        if manifest_path.is_symlink():
            raise PluginPathError(
                f"{MANIFEST_NAME} must not be a symlink", key="plugin.yaml",
            )
        if not manifest_path.is_file():
            raise PluginManifestError(
                f"{MANIFEST_NAME} is required", key="plugin.yaml",
            )
        size = manifest_path.stat().st_size
        if size > limits.max_manifest_bytes:
            raise PluginLimit(
                f"{MANIFEST_NAME} exceeds {limits.max_manifest_bytes} bytes",
                key="plugin.yaml",
                context={"bytes": size, "limit": limits.max_manifest_bytes},
            )
        raw = manifest_path.read_bytes()
    except (PluginManifestError, PluginPathError, PluginLimit):
        raise
    except OSError as exc:
        raise PluginManifestError(
            f"{MANIFEST_NAME} is unreadable: {type(exc).__name__}",
            key="plugin.yaml", detail=str(exc), exc_type=type(exc).__name__,
        ) from exc
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise PluginManifestError(
            f"{MANIFEST_NAME} must be UTF-8", key="plugin.yaml",
            detail=str(exc), exc_type=type(exc).__name__,
        ) from exc
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text, text.encode("utf-8")


def _parse_tool(
    raw: Any, *, index: int, limits: PluginLimits,
) -> ToolDeclaration:
    field_prefix = f"capabilities.tools[{index}]"
    if not isinstance(raw, Mapping):
        raise _manifest_error(
            f"{field_prefix} must be a mapping", key=field_prefix,
        )
    _unknown_keys(raw, _TOOL_KEYS, path=field_prefix)
    name = _require_str(raw.get("name"), field=f"{field_prefix}.name")
    if not TOOL_NAME_PATTERN.match(name):
        raise _manifest_error(
            f"invalid tool name {name!r}", key=f"{field_prefix}.name",
        )
    if name.startswith("mcp__"):
        raise _manifest_error(
            "tool names must not use the reserved 'mcp__' prefix",
            key=f"{field_prefix}.name",
        )
    if ":" in name:
        raise _manifest_error(
            "tool names must not contain ':'", key=f"{field_prefix}.name",
        )
    side_effect = _require_str(
        raw.get("side_effect"), field=f"{field_prefix}.side_effect",
    )
    if side_effect not in SIDE_EFFECTS:
        raise _manifest_error(
            f"unknown side_effect {side_effect!r}", key=f"{field_prefix}.side_effect",
        )
    timeout = _number(
        raw.get("timeout"), field=f"{field_prefix}.timeout", default=30.0,
    )
    if not 0 < timeout <= 600:
        raise _manifest_error(
            "timeout must be within (0, 600]", key=f"{field_prefix}.timeout",
        )
    concurrency = _integer(
        raw.get("concurrency"), field=f"{field_prefix}.concurrency", default=4,
    )
    if not 1 <= concurrency <= 32:
        raise _manifest_error(
            "concurrency must be within [1, 32]",
            key=f"{field_prefix}.concurrency",
        )
    backend = raw.get("backend", "inproc")
    if not isinstance(backend, str) or backend not in TOOL_BACKENDS:
        raise PluginUnsupported(
            f"backend {backend!r} is not supported in api_version 1",
            key=f"{field_prefix}.backend",
            hint="use 'inproc' or 'thread'",
        )
    return ToolDeclaration(
        name=name,
        side_effect=side_effect,
        timeout=timeout,
        concurrency=concurrency,
        idempotent=_optional_bool(
            raw.get("idempotent"), field=f"{field_prefix}.idempotent", default=True,
        ),
        requires_approval=_optional_bool(
            raw.get("requires_approval"),
            field=f"{field_prefix}.requires_approval",
            default=False,
        ),
        backend=backend,
    )


def _parse_entry(raw: Any, root: Path) -> PluginEntry:
    value = _require_str(raw, field="entry")
    if value.count(":") != 1:
        raise _manifest_error(
            "entry must be 'relative/file.py:callable'", key="entry",
        )
    path_text, qualname = value.rsplit(":", 1)
    relative = _safe_relative(path_text, field="entry")
    if relative.suffix != ".py":
        raise PluginManifestError(
            "entry must point to a .py file", key="entry",
        )
    if not ENTRY_PATTERN.match(qualname):
        raise _manifest_error(
            f"invalid entry callable {qualname!r}", key="entry",
        )
    path = _within_root(root, relative, field="entry")
    if not path.is_file():
        raise PluginPathError("entry must be a regular file", key="entry")
    return PluginEntry(path=path, qualname=qualname)


def _parse_paths(
    raw: Any, *, field: str, root: Path, container: bool,
) -> tuple[Path, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise _manifest_error(f"{field} must be a list", key=field)
    result: list[Path] = []
    for index, item in enumerate(raw):
        item_field = f"{field}[{index}]"
        relative = _safe_relative(item, field=item_field)
        path = _within_root(root, relative, field=item_field)
        if container:
            if not path.is_dir():
                raise PluginPathError(
                    f"{item_field} must be a directory", key=item_field,
                )
            skill_md = path / "SKILL.md"
            if skill_md.is_symlink() or not skill_md.is_file():
                raise PluginPathError(
                    f"{item_field} must contain a regular SKILL.md",
                    key=item_field,
                )
        else:
            if not path.is_file():
                raise PluginPathError(
                    f"{item_field} must be a regular file", key=item_field,
                )
            if path.suffix != ".md":
                raise PluginManifestError(
                    f"{item_field} must end with '.md'", key=item_field,
                )
        result.append(path)
    return tuple(result)


def read_plugin_manifest(
    root: Path, *, source: Optional[str] = None, limits: Optional[PluginLimits] = None,
) -> PluginManifest:
    """Read and validate one plugin directory (no import, no code execution)."""
    limits = limits or PluginLimits()
    root = Path(root)
    if root.is_symlink():
        raise PluginPathError("plugin directory must not be a symlink", key="root")
    if not root.is_dir():
        raise PluginPathError("plugin root is not a directory", key="root")
    source = source or root.name
    text, encoded = _read_text(root, limits)

    try:
        raw = yaml.load(text, Loader=_UniqueKeyLoader)  # noqa: S506 - SafeLoader subclass
    except yaml.YAMLError as exc:
        raise PluginManifestError(
            f"invalid YAML: {exc}", key="plugin.yaml",
            detail=str(exc), exc_type=type(exc).__name__,
        ) from exc
    if not isinstance(raw, Mapping):
        raise _manifest_error("plugin.yaml must be a mapping", key="plugin.yaml")

    _unknown_keys(raw, _TOP_LEVEL_KEYS, path="", future=_FUTURE_TOP_LEVEL)

    plugin_id = _require_str(raw.get("id"), field="id")
    if not PLUGIN_ID_PATTERN.match(plugin_id):
        raise _manifest_error(f"invalid plugin id {plugin_id!r}", key="id")
    version = _require_str(raw.get("version"), field="version")
    if not PLUGIN_VERSION_PATTERN.match(version):
        raise _manifest_error(
            f"invalid plugin version {version!r}", key="version",
        )

    api_version = raw.get("api_version", 1)
    if isinstance(api_version, bool) or not isinstance(api_version, int):
        raise _manifest_error("api_version must be an integer", key="api_version")
    if api_version != 1:
        raise PluginUnsupported(
            f"api_version {api_version} is not supported by this host",
            key="api_version",
        )

    runtime = raw.get("runtime", RUNTIME_INPROC)
    if runtime != RUNTIME_INPROC:
        raise PluginUnsupported(
            f"runtime {runtime!r} is not supported in api_version 1",
            key="runtime", hint="v0.1 only supports 'in-proc'",
        )

    trust = raw.get("trust", "third-party")
    if trust not in TRUST_VALUES:
        raise _manifest_error(f"unknown trust level {trust!r}", key="trust")

    description = normalise_description(raw.get("description"))

    capabilities = raw.get("capabilities")
    if not isinstance(capabilities, Mapping):
        raise _manifest_error(
            "capabilities must be a mapping with at least one of "
            "tools/skills/modes",
            key="capabilities",
        )
    _unknown_keys(
        capabilities, _CAPABILITY_KEYS, path="capabilities",
        future=_FUTURE_CAPABILITIES,
    )

    raw_tools = capabilities.get("tools", [])
    if raw_tools is None:
        raw_tools = []
    if not isinstance(raw_tools, list):
        raise _manifest_error("capabilities.tools must be a list",
                              key="capabilities.tools")
    if len(raw_tools) > limits.max_tools_per_plugin:
        raise PluginLimit(
            f"too many tool declarations (max {limits.max_tools_per_plugin})",
            key="capabilities.tools",
            context={"count": len(raw_tools)},
        )
    tools: list[ToolDeclaration] = []
    seen_tools: set[str] = set()
    for index, item in enumerate(raw_tools):
        declaration = _parse_tool(item, index=index, limits=limits)
        if declaration.name in seen_tools:
            raise PluginManifestError(
                f"duplicate tool declaration {declaration.name!r}",
                key=f"capabilities.tools[{index}].name",
            )
        seen_tools.add(declaration.name)
        tools.append(declaration)

    skill_dirs = _parse_paths(
        capabilities.get("skills"), field="capabilities.skills",
        root=root, container=True,
    )
    mode_paths = _parse_paths(
        capabilities.get("modes"), field="capabilities.modes",
        root=root, container=False,
    )

    warnings: list[str] = []
    entry: Optional[PluginEntry] = None
    raw_entry = raw.get("entry")
    if raw_entry is not None:
        entry = _parse_entry(raw_entry, root)
        if not tools:
            warnings.append(
                f"plugin.entry_unused: {plugin_id} declares entry but no tools"
            )
    elif tools:
        raise _manifest_error(
            "entry is required when capabilities.tools is non-empty",
            key="entry",
        )

    if not (tools or skill_dirs or mode_paths):
        raise _manifest_error(
            "plugin must declare at least one capability",
            key="capabilities",
        )

    digest = "sha256:" + hashlib.sha256(encoded).hexdigest()
    return PluginManifest(
        id=plugin_id,
        version=version,
        root=root,
        source=source,
        description=description,
        entry=entry,
        runtime=runtime,
        trust=trust,
        api_version=api_version,
        tools=tuple(tools),
        skill_dirs=skill_dirs,
        mode_paths=mode_paths,
        digest=digest,
        warnings=tuple(warnings),
    )


# ---------------------------------------------------------------------------
# Discovery (§8.3)
# ---------------------------------------------------------------------------

def _list_children(container: Path) -> tuple[list[Path], list[str]]:
    warnings: list[str] = []
    try:
        if not container.exists():
            return [], warnings  # a missing default directory is fine
        if not container.is_dir():
            warnings.append(
                f"plugin.directory_unreadable: {container.name or 'root'} is not a directory"
            )
            return [], warnings
        children = sorted(container.iterdir(), key=lambda item: item.name)
    except OSError as exc:
        warnings.append(
            f"plugin.directory_unreadable: {container.name or 'root'} "
            f"({type(exc).__name__})"
        )
        return [], warnings
    return children, warnings


def _peek_id(root: Path, limits: PluginLimits) -> str:
    """Best-effort id extraction from an otherwise invalid manifest."""
    try:
        text, _ = _read_text(root, limits)
        raw = yaml.load(text, Loader=_UniqueKeyLoader)  # noqa: S506
        if isinstance(raw, Mapping):
            value = raw.get("id")
            if isinstance(value, str) and PLUGIN_ID_PATTERN.match(value):
                return value
    except Exception:  # noqa: BLE001 - diagnostics only
        return ""
    return ""


def scan_plugins(
    dirs: list[Path] | tuple[Path, ...], *, limits: Optional[PluginLimits] = None,
) -> PluginCatalog:
    """Scan container directories; validate manifests; never import code."""
    limits = limits or PluginLimits()
    manifests: list[PluginManifest] = []
    invalid: list[PluginIssue] = []
    warnings: list[str] = []
    seen_ids: dict[str, str] = {}

    for container in dirs:
        children, container_warnings = _list_children(Path(container).expanduser())
        warnings.extend(container_warnings)
        for child in children:
            if child.name.startswith("."):
                continue
            try:
                if child.is_symlink():
                    warnings.append(
                        "plugin.directory_unreadable: "
                        f"{child.name} is a symlink and was skipped"
                    )
                    continue
                if not child.is_dir():
                    continue
            except OSError:
                warnings.append(
                    f"plugin.directory_unreadable: {child.name} is unreadable"
                )
                continue
            try:
                manifest = read_plugin_manifest(
                    child, source=child.name, limits=limits,
                )
            except (PluginManifestError, PluginUnsupported, PluginPathError,
                    PluginLimit) as exc:
                invalid.append(PluginIssue(
                    source=child.name, code=exc.code, error=str(exc),
                    hint=exc.hint, id=_peek_id(child, limits), root=child,
                ))
                continue

            previous = seen_ids.get(manifest.id)
            if previous is not None:
                message = (
                    f"plugin.duplicate_id: {manifest.id!r} in {child.name} "
                    f"was already provided by {previous}"
                )
                warnings.append(message)
                invalid.append(PluginIssue(
                    source=child.name, code="E_PLUGIN_MANIFEST", error=message,
                    id=manifest.id, root=child,
                ))
                continue
            if len(manifests) >= limits.max_plugins:
                message = (
                    f"plugin limit reached ({limits.max_plugins}); "
                    f"{child.name} was skipped"
                )
                warnings.append(message)
                invalid.append(PluginIssue(
                    source=child.name, code="E_PLUGIN_LIMIT", error=message,
                    id=manifest.id, root=child,
                ))
                continue
            seen_ids[manifest.id] = child.name
            warnings.extend(manifest.warnings)
            manifests.append(manifest)

    return PluginCatalog(
        manifests=tuple(manifests),
        invalid=tuple(invalid),
        warnings=tuple(warnings),
    )


__all__ = [
    "MANIFEST_NAME",
    "normalise_description",
    "read_plugin_manifest",
    "scan_plugins",
]
