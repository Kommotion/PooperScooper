"""Prefix-command names for weekly stats. No bot, no Discord, no config.

Source maps come from an AST scan. Live maps come from the bot's command
tree. Lookup keys are casefolded. Values are canonical names: a group is
``palworld``, a subcommand is ``play shuffle``.
"""

from __future__ import annotations

import ast
from pathlib import Path


def alias_map_from_source(root: str | Path, modules: list[str] | None = None) -> dict[str, str]:
    """Map alias and command name to the canonical prefix command.

    With ``modules`` None, scans ``root/cogs/**/*.py`` and
    ``root/PooperScooper.py``. Otherwise scans only those module files
    (``cogs.general`` is ``root/cogs/general.py``). Looks for
    ``@commands.command`` / ``@commands.group`` and for ``group.command`` /
    ``group.group`` on those groups. ``help`` is always present.
    """
    root = Path(root)
    mapping: dict[str, str] = {}
    paths = []
    if modules is None:
        cogs = root / "cogs"
        if cogs.is_dir():
            paths.extend(path for path in cogs.rglob("*.py") if path.is_file())
        entry = root / "PooperScooper.py"
        if entry.is_file():
            paths.append(entry)
    else:
        for module in modules:
            path = root.joinpath(*str(module).split(".")).with_suffix(".py")
            if path.is_file():
                paths.append(path)
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
    allowed = _prefix_decorator_owners(tree)
    found: list[tuple[ast.AST, _Dec]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            parsed = _parse_decorator(decorator, allowed)
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


def _prefix_decorator_owners(tree: ast.AST) -> set[str]:
    """Function names that are prefix groups, plus ``commands``.

    A slash group such as ``imagegen_group`` is not included, so
    ``@imagegen_group.command`` stays an app command.
    """
    allowed = {"commands"}
    functions = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    for _ in range(6):
        grew = False
        for node in functions:
            for decorator in node.decorator_list:
                parsed = _parse_decorator(decorator, allowed)
                if parsed is None or parsed.kind != "group":
                    continue
                if node.name in allowed:
                    continue
                allowed.add(node.name)
                grew = True
        if not grew:
            break
    return allowed


def _parse_decorator(decorator: ast.AST, allowed: set[str] | None = None) -> _Dec | None:
    """Prefix ``commands.command`` / ``commands.group``, or a prefix group's child.

    ``allowed`` is the set of owners from the prefix scan. Slash groups are
    absent from that set, so ``@imagegen_group.command`` returns None and the
    app-command parser can see the body gate.
    """
    call = decorator if isinstance(decorator, ast.Call) else None
    func = call.func if call is not None else decorator
    if not isinstance(func, ast.Attribute):
        return None
    if func.attr not in {"command", "group"}:
        return None
    if not isinstance(func.value, ast.Name):
        return None
    owners = allowed if allowed is not None else {"commands"}
    if func.value.id not in owners:
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


def owner_only_names_from_source(root: str | Path) -> set[str]:
    """Qualified names of owner-only prefix and slash commands.

    A command counts when its decorator is ``@commands.is_owner()``, when it
    is the hidden ``candyadmin`` group or one of that group's subcommands, or
    when a slash command's first check returns unless the caller is the owner.
    Public commands that only let the owner bypass a guild check stay out.
    """
    root = Path(root)
    names: set[str] = set()
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
        names.update(_owner_names_in_tree(tree))
    return names


def owner_only_names_from_bot(bot) -> set[str]:
    """Qualified names whose prefix check is ``commands.is_owner()``.

    ``candyadmin`` and its subcommands are included. The source scan still
    covers slash commands the prefix tree does not list.
    """
    names: set[str] = set()
    roots = getattr(bot, "commands", None)
    if roots:
        for command in list(roots):
            _walk_owner_command(command, False, names)
    return names


_OWNER_CACHE: frozenset[str] | None = None


def is_owner_only_name(name: str | None) -> bool:
    """True when ``name`` is in the cached source owner-only set."""
    global _OWNER_CACHE
    if not name or not str(name).strip():
        return False
    if _OWNER_CACHE is None:
        root = Path(__file__).resolve().parents[2]
        _OWNER_CACHE = frozenset(owner_only_names_from_source(root))
    return str(name).strip().casefold() in {item.casefold() for item in _OWNER_CACHE}


def _owner_names_in_tree(tree: ast.AST) -> set[str]:
    slash_groups = _slash_group_names(tree)
    prefix_owners = _prefix_decorator_owners(tree)
    found: list[tuple[ast.AST, _Dec, bool, bool]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        owner_flag = _function_has_owner_decorator(node)
        candy = owner_flag or _function_is_candyadmin(node)
        gate = _slash_body_is_owner_gate(node)
        for decorator in node.decorator_list:
            parsed = _parse_decorator(decorator, prefix_owners)
            if parsed is not None:
                found.append((node, parsed, candy, False))
                continue
            app = _parse_app_command(decorator, slash_groups)
            if app is not None:
                found.append((node, app, candy or gate, True))
    names: set[str] = set()
    groups: dict[str, tuple[str, bool]] = {var: (label, False) for var, label in slash_groups.items()}
    pending = []
    for node, info, candy, is_slash in found:
        if info.owner != "commands":
            pending.append((node, info, candy, is_slash))
            continue
        canonical = info.explicit or node.name
        if candy or _is_candyadmin_name(canonical):
            names.add(canonical)
            if info.kind == "group":
                groups[node.name] = (canonical, True)
        elif info.kind == "group":
            groups[node.name] = (canonical, False)
    for _ in range(6):
        if not pending:
            break
        next_pending = []
        for node, info, candy, is_slash in pending:
            parent = groups.get(info.owner)
            if parent is None and not is_slash:
                next_pending.append((node, info, candy, is_slash))
                continue
            if parent is None:
                parent_name, parent_owner = "", False
            else:
                parent_name, parent_owner = parent
            leaf = info.explicit or node.name
            canonical = f"{parent_name} {leaf}".strip() if parent_name else leaf
            owner = candy or parent_owner or _is_candyadmin_name(canonical) or _is_candyadmin_name(parent_name)
            if owner and canonical:
                names.add(canonical)
            if info.kind == "group":
                groups[node.name] = (canonical, owner)
        if len(next_pending) == len(pending):
            break
        pending = next_pending
    return names


def _slash_group_names(tree: ast.AST) -> dict[str, str]:
    found: dict[str, str] = {}
    for node in ast.walk(tree):
        value = None
        target = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            target = node.targets[0].id
            value = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target = node.target.id
            value = node.value
        if not target or not isinstance(value, ast.Call):
            continue
        func = value.func
        if not isinstance(func, ast.Attribute) or func.attr != "Group":
            continue
        label = _explicit_name(value)
        if label:
            found[target] = label
    return found


def _function_has_owner_decorator(node: ast.AST) -> bool:
    for decorator in getattr(node, "decorator_list", ()):
        func = decorator.func if isinstance(decorator, ast.Call) else decorator
        if not isinstance(func, ast.Attribute) or func.attr != "is_owner":
            continue
        base = func.value
        if isinstance(base, ast.Name) and base.id == "commands":
            return True
    return False


def _function_is_candyadmin(node: ast.AST) -> bool:
    for decorator in getattr(node, "decorator_list", ()):
        func = decorator.func if isinstance(decorator, ast.Call) else decorator
        label = ""
        if isinstance(func, ast.Name):
            label = func.id
        elif isinstance(func, ast.Attribute):
            label = func.attr
        if "candyadmin" in label.casefold():
            return True
    return False


def _is_candyadmin_name(name: str) -> bool:
    folded = str(name or "").strip().casefold()
    return folded == "candyadmin" or folded.startswith("candyadmin ")


def _slash_body_is_owner_gate(node: ast.AST) -> bool:
    """True when the command returns immediately unless the caller is the owner.

    A check that returns true for the owner and then allows other people is
    not this shape, so public bypasses stay off the list.
    """
    body = list(getattr(node, "body", ()) or ())
    if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant):
        body = body[1:]
    if not body or not isinstance(body[0], ast.If):
        return False
    test = ast.dump(body[0].test)
    if "is_owner" not in test:
        return False
    return any(isinstance(child, ast.Return) for child in ast.walk(body[0]))


def _parse_app_command(decorator: ast.AST, slash_groups: dict[str, str]) -> _Dec | None:
    call = decorator if isinstance(decorator, ast.Call) else None
    func = call.func if call is not None else decorator
    if not isinstance(func, ast.Attribute) or func.attr != "command":
        return None
    if not isinstance(func.value, ast.Name):
        return None
    owner = func.value.id
    if owner == "app_commands":
        explicit = _explicit_name(call)
        return _Dec("command", "commands", explicit, ())
    if owner not in slash_groups:
        return None
    explicit = _explicit_name(call)
    return _Dec("command", owner, explicit, ())


def _bot_check_is_owner(command) -> bool:
    for check in list(getattr(command, "checks", None) or []):
        qual = str(getattr(check, "__qualname__", "") or "")
        if qual.startswith("is_owner"):
            return True
    return False


def _walk_owner_command(command, parent_owner: bool, names: set[str]) -> None:
    name = str(getattr(command, "name", "") or "").strip()
    if not name:
        return
    qualified = getattr(command, "qualified_name", None)
    canonical = str(qualified).strip() if qualified else name
    owner = parent_owner or _bot_check_is_owner(command) or _is_candyadmin_name(canonical)
    if owner and canonical:
        names.add(canonical)
    children = getattr(command, "commands", None)
    if not children:
        return
    for child in list(children):
        _walk_owner_command(child, owner, names)


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
