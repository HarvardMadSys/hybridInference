"""No backend module reads a database-backed setting straight from the environment.

A setting the registry manages (``serving.config.app_config_registry``) resolves
database row, then environment, then default — through ``config_value`` or the
``Settings`` overlay. A raw ``os.getenv("SMTP_HOST")`` skips the row: the value
an administrator saves in the console does nothing there, and nothing says so.

This scans ``apps/backend`` (tests are not scanned) for raw environment reads —
``os.getenv``, ``os.environ[...]`` / ``.get`` / ``.setdefault`` / ``.pop``,
``in os.environ``, and the same through ``from os import ...`` and aliases — and
fails on one that reads a registered setting, a numbered provider key or a
runtime setting outside the modules below. Names the registry leaves to the
environment (``ENVIRONMENT_ONLY``, such as ``DB_ENABLED``) and names it does not
know are read raw freely. A read whose variable cannot be told from the source
must sit in an allowed module.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

import pytest

from serving.config import app_config_registry as registry

BACKEND = Path(__file__).resolve().parents[3] / "apps" / "backend"

#: Modules that read the environment however they must, and why.
ALLOWED_MODULES: dict[str, str] = {
    "serving/config/app_config.py": (
        "the resolver itself: config_value() falls back to the environment, and it "
        "matches Settings-backed names case-insensitively the way Settings does"
    ),
    "serving/config/runtime_settings.py": (
        "the runtime-settings environment import, which copies each runtime setting's "
        "variable into site_settings at boot"
    ),
}

#: Registered settings one other module reads raw on purpose, and why.
ALLOWED_READS: dict[tuple[str, str], str] = {
    ("serving/utils/logging.py", name): (
        "logging cannot import app_config (everything imports logging): it reads "
        "through a resolver that is os.getenv only until app_config installs "
        "config_value, at its own import"
    )
    for name in ("LOG_LEVEL", "LOG_FORMAT")
}


@dataclass(frozen=True)
class EnvRead:
    """One raw environment read found in the source."""

    module: str
    line: int
    #: The variables read; ``None`` when the source does not tell which.
    names: tuple[str, ...] | None

    def __str__(self) -> str:
        what = ", ".join(self.names) if self.names is not None else "a variable chosen at runtime"
        return f"{self.module}:{self.line} reads {what}"


def _module_name(path: Path) -> str:
    return ".".join(path.relative_to(BACKEND).with_suffix("").parts)


def _string_constants(tree: ast.Module) -> dict[str, tuple[str, ...]]:
    """Module-level names bound to a string, or to a tuple/list/set of strings."""

    def strings(node: ast.expr) -> tuple[str, ...] | None:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return (node.value,)
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in ("frozenset", "set", "tuple")
            and len(node.args) == 1
            and not node.keywords
        ):
            return strings(node.args[0])
        if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
            found = [strings(item) for item in node.elts]
            if all(item is not None and len(item) == 1 for item in found):
                return tuple(item[0] for item in found)  # type: ignore[index]
        return None

    constants: dict[str, tuple[str, ...]] = {}
    for statement in tree.body:
        targets: list[ast.expr] = []
        if isinstance(statement, ast.Assign):
            targets, value = statement.targets, statement.value
        elif isinstance(statement, ast.AnnAssign) and statement.value is not None:
            targets, value = [statement.target], statement.value
        else:
            continue
        found = strings(value)
        for target in targets:
            if isinstance(target, ast.Name) and found is not None:
                constants[target.id] = found
    return constants


def find_env_reads(
    source: str,
    module: str,
    imported_constants: dict[str, dict[str, tuple[str, ...]]] | None = None,
) -> list[EnvRead]:
    """Return every raw environment read in *source*.

    Args:
        source: A Python module's text.
        module: Its path relative to ``apps/backend``, for the report.
        imported_constants: Module-level string constants of the other backend
            modules, by dotted module name, so ``os.getenv(NAME)`` resolves a
            ``NAME`` imported from one of them.
    """
    tree = ast.parse(source)
    parents: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node

    constants = _string_constants(tree)
    imported = imported_constants or {}
    os_names = {"os"}
    getenv_names: set[str] = set()
    environ_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "os" or (alias.name.startswith("os.") and alias.asname is None):
                    os_names.add(alias.asname or "os")
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            for alias in node.names:
                local = alias.asname or alias.name
                if node.module == "os" and alias.name == "getenv":
                    getenv_names.add(local)
                elif node.module == "os" and alias.name == "environ":
                    environ_names.add(local)
                elif alias.name in imported.get(node.module, {}):
                    constants.setdefault(local, imported[node.module][alias.name])

    def kind(node: ast.AST) -> str | None:
        """``getenv`` for ``os.getenv``/``os.environ.get``, ``environ`` for ``os.environ``."""
        if isinstance(node, ast.Name):
            if node.id in getenv_names:
                return "getenv"
            if node.id in environ_names:
                return "environ"
            return None
        if isinstance(node, ast.Attribute):
            if isinstance(node.value, ast.Name) and node.value.id in os_names:
                return {"getenv": "getenv", "environ": "environ"}.get(node.attr)
            if node.attr == "get" and kind(node.value) == "environ":
                return "getenv"
        return None

    # Names bound to os.getenv or os.environ — `_get = os.getenv` — read too.
    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                targets, value = node.targets, node.value
            elif isinstance(node, (ast.AnnAssign, ast.NamedExpr)) and node.value is not None:
                targets, value = [node.target], node.value
            else:
                continue
            found = kind(value)
            if found is None:
                continue
            bucket = getenv_names if found == "getenv" else environ_names
            for target in targets:
                if isinstance(target, ast.Name) and target.id not in bucket:
                    bucket.add(target.id)
                    changed = True

    def loop_values(name: str, node: ast.AST) -> tuple[str, ...] | None:
        """The strings *name* takes, when it is the variable of a loop over constants."""
        current = parents.get(node)
        while current is not None:
            loops: list[tuple[ast.expr, ast.expr]] = []
            if isinstance(current, ast.For):
                loops.append((current.target, current.iter))
            elif isinstance(current, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)):
                loops.extend((loop.target, loop.iter) for loop in current.generators)
            for target, iterable in loops:
                if isinstance(target, ast.Name) and target.id == name:
                    # Resolved from outside this loop: `for key in key` is not a constant.
                    return resolve(iterable, current)
            current = parents.get(current)
        return None

    def resolve(expr: ast.expr | None, at: ast.AST) -> tuple[str, ...] | None:
        if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
            return (expr.value,)
        if isinstance(expr, (ast.Tuple, ast.List, ast.Set)):
            found = [resolve(item, at) for item in expr.elts]
            if all(item is not None for item in found):
                return tuple(name for item in found for name in item)  # type: ignore[union-attr]
            return None
        if isinstance(expr, ast.Name):
            looped = loop_values(expr.id, at)
            if looped is not None:
                return looped
            return constants.get(expr.id)
        return None

    def first_argument(call: ast.Call) -> ast.expr | None:
        if call.args:
            return call.args[0]
        return next((kw.value for kw in call.keywords if kw.arg in ("key", "name")), None)

    found_reads: list[tuple[int, int, EnvRead]] = []

    def record(node: ast.AST, names: tuple[str, ...] | None) -> None:
        line, column = getattr(node, "lineno", 0), getattr(node, "col_offset", 0)
        found_reads.append((line, column, EnvRead(module, line, names)))

    for node in ast.walk(tree):
        found = kind(node)
        if found is None:
            continue
        if isinstance(node, ast.Name) and not isinstance(node.ctx, ast.Load):
            continue  # an alias being bound or rebound, not read
        parent = parents.get(node)
        if isinstance(parent, ast.Attribute) and kind(parent) is not None:
            continue  # `os.environ` inside `os.environ.get`: the outer node is the read
        if isinstance(parent, (ast.Assign, ast.AnnAssign, ast.NamedExpr)) and parent.value is node:
            continue  # an alias, followed above
        if found == "getenv":
            if isinstance(parent, ast.Call) and parent.func is node:
                record(node, resolve(first_argument(parent), parent))
            else:
                record(node, None)  # handed on, called elsewhere
            continue
        # os.environ itself
        if isinstance(parent, ast.Subscript) and parent.value is node:
            if isinstance(parent.ctx, ast.Load):
                record(node, resolve(parent.slice, parent))
            continue  # a write or a delete
        if (
            isinstance(parent, ast.Compare)
            and node in parent.comparators
            and all(isinstance(op, (ast.In, ast.NotIn)) for op in parent.ops)
        ):
            record(node, resolve(parent.left, parent))
            continue
        if isinstance(parent, ast.Attribute) and parent.value is node:
            call = parents.get(parent)
            if parent.attr in ("setdefault", "pop") and isinstance(call, ast.Call):
                record(node, resolve(first_argument(call), call))
                continue
            if parent.attr in ("update", "clear"):
                continue  # writes
        record(node, None)  # iterated, copied or handed on
    # ast.walk is breadth-first; report in source order.
    return [read for _line, _column, read in sorted(found_reads, key=lambda item: item[:2])]


def _registered(name: str) -> bool:
    key = name.upper()
    return (
        registry.static_entry(key) is not None
        or registry.numbered_key_entry(key) is not None
        or registry.is_runtime_setting_name(key)
    )


def _backend_reads() -> list[EnvRead]:
    paths = sorted(path for path in BACKEND.rglob("*.py") if "__pycache__" not in path.parts)
    trees = {path: ast.parse(path.read_text(encoding="utf-8")) for path in paths}
    constants = {_module_name(path): _string_constants(tree) for path, tree in trees.items()}
    reads: list[EnvRead] = []
    for path in paths:
        module = path.relative_to(BACKEND).as_posix()
        reads.extend(find_env_reads(path.read_text(encoding="utf-8"), module, constants))
    return reads


def _violations(reads: list[EnvRead]) -> list[str]:
    problems = []
    for read in reads:
        if read.module in ALLOWED_MODULES:
            continue
        if read.names is None:
            problems.append(f"{read}: name the variable, or read it through config_value()")
            continue
        for name in read.names:
            if _registered(name) and (read.module, name.upper()) not in ALLOWED_READS:
                problems.append(
                    f"{read.module}:{read.line} reads {name} from the environment, skipping the "
                    "value stored in the database: read it through config_value() (or the "
                    "Settings overlay)"
                )
    return problems


def test_no_module_reads_a_database_backed_setting_from_the_environment() -> None:
    problems = _violations(_backend_reads())
    assert problems == [], "\n".join(problems)


def test_a_raw_read_of_a_stored_setting_fails_the_guard() -> None:
    source = 'import os\nos.environ.get("CHUTES_BASE_URL")\nos.getenv(key)\nos.getenv("DB_HOST")\n'

    assert _violations(find_env_reads(source, "serving/somewhere.py")) == [
        "serving/somewhere.py:2 reads CHUTES_BASE_URL from the environment, skipping the value "
        "stored in the database: read it through config_value() (or the Settings overlay)",
        "serving/somewhere.py:3 reads a variable chosen at runtime: name the variable, or read "
        "it through config_value()",
    ]
    # The same reads where they are allowed.
    assert _violations(find_env_reads(source, "serving/config/app_config.py")) == []
    logging_source = 'import os\n_resolve = os.getenv\n_resolve("LOG_LEVEL", "INFO")\n'
    assert _violations(find_env_reads(logging_source, "serving/utils/logging.py")) == []
    assert _violations(find_env_reads(logging_source, "serving/utils/other.py")) != []


def test_the_allowlist_is_still_needed() -> None:
    """An entry nothing needs any more is removed, so the list stays short."""
    reads = _backend_reads()
    for module in ALLOWED_MODULES:
        assert (BACKEND / module).is_file(), f"{module} no longer exists"
        assert any(read.module == module for read in reads), f"{module} reads nothing raw now"
    for module, name in ALLOWED_READS:
        assert any(
            read.module == module and read.names is not None and name in read.names
            for read in reads
        ), f"{module} no longer reads {name} raw"
        assert _registered(name), f"{name} is not a registered setting"


def test_environment_only_names_are_never_registered() -> None:
    """Reading ENVIRONMENT_ONLY names raw is fine only because the registry never holds them."""
    assert [name for name in registry.ENVIRONMENT_ONLY if _registered(name)] == []


_EVERY_SPELLING = """
import os
import os as operating_system
from os import environ, getenv
from os import getenv as fetch

from serving.utils.workers import WORKER_COUNT_ENV_KEYS

SMTP = "SMTP_HOST"
RAG = ("RAG_TOP_K", "RAG_MAX_TOKENS")
_get = os.getenv
_env = os.environ


def reads(name):
    os.getenv("CHUTES_BASE_URL")
    os.environ["MINIMAX_API_KEY2"]
    os.environ.get("signup_enabled", "")
    getenv("LOG_LEVEL")
    fetch("SITE_NAME")
    environ.get("RAG_EMBEDDER")
    operating_system.getenv("RAG_API_KEY")
    _get(SMTP)
    _env.setdefault("SMTP_PORT", "25")
    "SMTP_USER" in os.environ
    for key in RAG:
        os.environ.get(key)
    [os.getenv(key) for key in WORKER_COUNT_ENV_KEYS]
    os.getenv(name)
    dict(os.environ)
    os.environ["WRITTEN"] = "not a read"
    os.environ.update({"ALSO": "written"})
"""


def test_the_scanner_sees_every_spelling() -> None:
    """A guard that quietly matches nothing is worse than none."""
    constants = {"serving.utils.workers": {"WORKER_COUNT_ENV_KEYS": ("WEB_CONCURRENCY",)}}
    reads = find_env_reads(_EVERY_SPELLING, "example.py", constants)

    named = [name for read in reads if read.names is not None for name in read.names]
    assert named == [
        "CHUTES_BASE_URL",
        "MINIMAX_API_KEY2",
        "signup_enabled",
        "LOG_LEVEL",
        "SITE_NAME",
        "RAG_EMBEDDER",
        "RAG_API_KEY",
        "SMTP_HOST",
        "SMTP_PORT",
        "SMTP_USER",
        "RAG_TOP_K",
        "RAG_MAX_TOKENS",
        "WEB_CONCURRENCY",
    ]
    # os.getenv(name) and dict(os.environ): the source does not say which variable.
    assert sum(read.names is None for read in reads) == 2
    assert all(_registered(name) for name in named if name != "WEB_CONCURRENCY")
    assert not _registered("WEB_CONCURRENCY")


@pytest.mark.parametrize(
    "snippet",
    [
        'import os\nos.getenv("DB_ENABLED", "true")\n',
        'import os\nos.environ.get("EXPERIMENT_MODE")\n',
        'import os\nos.environ["LOG_FILE"]\n',
    ],
)
def test_environment_only_and_unknown_names_pass(snippet) -> None:
    reads = find_env_reads(snippet, "example.py")
    assert [read.names for read in reads] != []
    assert not any(_registered(name) for read in reads for name in read.names or ())
