"""Prefix-command names for weekly stats. No bot, no Discord, no config.

Source maps come from an AST scan. Live maps come from the bot's command
tree. Lookup keys are casefolded. Values are canonical names: a group is
``palworld``, a subcommand is ``play shuffle``.
"""

from __future__ import annotations

import ast
from pathlib import Path


def alias_map_from_source(root: str | Path) -> dict[str, str]:
    """Map alias and command name to the canonical prefix command.

    Scans ``root/cogs/**/*.py`` and ``root/PooperScooper.py`` for
    ``@commands.command`` / ``@commands.group`` and for ``group.command`` /
    ``group.group`` on those groups. ``help`` is always present.
    """
    root = Path(root)
    mapping: dict[str, str] = {}
    paths = []
    cogs = root / "cogs"
    if cogs.is_dir():
        paths.extend(path for path in cogs.rglob("*.py") if path.is_file())
    entry = root / "PooperScooper.py"
    if entry.is_file():
        paths.append(entry)
    for path in paths:
        try:
            source = path.read_text(encoding="utf-8")
        except OSError:
            continue
        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError:
            continue
        _scan_module(tree, mapping)
    mapping["help"] = "help"
    return mapping


def alias_map_from_bot(bot) -> dict[str, str]:
    """Map alias and qualified name from ``bot`` prefix commands.

    Top-level aliases replace the first token (``!p`` -> ``play``).
    Subcommand aliases keep the parent (``play sh`` -> ``play shuffle``).
    A short subcommand name does not replace a top-level command of the
    same name. ``help`` is always present.
    """
    mapping: dict[str, str] = {}
    roots = getattr(bot, "commands", None)
    if roots:
        for command in list(roots):
            _walk_bot_command(command, None, mapping)
    mapping["help"] = "help"
    return mapping


def canonical_command_name(raw: str | None, mapping: dict[str, str]) -> str | None:
    """Canonical name for one invocation, or None when it is not registered.

    Strips one leading ``!`` and uses the first whitespace-separated token.
    ``!play https://x`` is ``play``. ``!playhttps://x`` is a different token.
    """
    if raw is None or not mapping:
        return None
    text = str(raw).lstrip()
    if not text:
        return None
    if text.startswith("!"):
        text = text[1:]
    if not text:
        return None
    token = text.split(None, 1)[0]
    if not token:
        return None
    return mapping.get(token.casefold())


def _scan_module(tree: ast.AST, mapping: dict[str, str]) -> None:
    found: list[tuple[ast.AST, _Dec]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            parsed = _parse_decorator(decorator)
            if parsed is not None:
                found.append((node, parsed))
    groups: dict[str, str] = {}
    pending = []
    for node, info in found:
        if info.owner != "commands":
            pending.append((node, info))
            continue
        canonical = info.explicit or node.name
        _register(mapping, canonical, info.aliases, parent=None)
        if info.kind == "group":
            groups[node.name] = canonical
    # Parents are known after the commands.* pass. Repeat so a nested
    # group.group can parent the commands under it.
    for _ in range(4):
        if not pending:
            break
        next_pending = []
        for node, info in pending:
            parent = groups.get(info.owner)
            if parent is None:
                next_pending.append((node, info))
                continue
            leaf = info.explicit or node.name
            canonical = f"{parent} {leaf}"
            _register(mapping, canonical, info.aliases, parent=parent)
            if info.kind == "group":
                groups[node.name] = canonical
        if len(next_pending) == len(pending):
            break
        pending = next_pending


class _Dec:
    def __init__(self, kind: str, owner: str, explicit: str | None, aliases: tuple[str, ...]) -> None:
        self.kind = kind
        self.owner = owner
        self.explicit = explicit
        self.aliases = aliases


def _parse_decorator(decorator: ast.AST) -> _Dec | None:
    call = decorator if isinstance(decorator, ast.Call) else None
    func = call.func if call is not None else decorator
    if not isinstance(func, ast.Attribute):
        return None
    if func.attr not in {"command", "group"}:
        return None
    if not isinstance(func.value, ast.Name):
        return None
    explicit = _explicit_name(call)
    aliases = _aliases(call)
    return _Dec(func.attr, func.value.id, explicit, aliases)


def _explicit_name(call: ast.Call | None) -> str | None:
    if call is None:
        return None
    for keyword in call.keywords:
        if keyword.arg == "name":
            value = _const_str(keyword.value)
            if value:
                return value
    if call.args:
        value = _const_str(call.args[0])
        if value:
            return value
    return None


def _aliases(call: ast.Call | None) -> tuple[str, ...]:
    if call is None:
        return ()
    for keyword in call.keywords:
        if keyword.arg != "aliases":
            continue
        node = keyword.value
        if not isinstance(node, (ast.List, ast.Tuple)):
            return ()
        names = []
        for item in node.elts:
            value = _const_str(item)
            if value:
                names.append(value)
        return tuple(names)
    return ()


def _const_str(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.strip():
        return node.value.strip()
    return None


def _register(mapping: dict[str, str], canonical: str, aliases: tuple[str, ...], *, parent: str | None) -> None:
    if not canonical:
        return
    mapping[canonical.casefold()] = canonical
    if parent is None:
        for alias in aliases:
            mapping[alias.casefold()] = canonical
        return
    leaf = canonical.split()[-1]
    mapping.setdefault(leaf.casefold(), canonical)
    for alias in aliases:
        mapping[f"{parent} {alias}".casefold()] = canonical
        mapping.setdefault(alias.casefold(), canonical)


def _walk_bot_command(command, parent: str | None, mapping: dict[str, str]) -> None:
    name = str(getattr(command, "name", "") or "").strip()
    if not name:
        return
    qualified = getattr(command, "qualified_name", None)
    if qualified:
        canonical = str(qualified).strip()
    elif parent:
        canonical = f"{parent} {name}"
    else:
        canonical = name
    if not canonical:
        return
    aliases = tuple(str(alias).strip() for alias in (getattr(command, "aliases", ()) or ()) if str(alias).strip())
    _register(mapping, canonical, aliases, parent=parent)
    children = getattr(command, "commands", None)
    if not children:
        return
    for child in list(children):
        _walk_bot_command(child, canonical, mapping)
