"""Review budgets and backend dependency cycles without importing application code."""

import ast
from importlib.util import resolve_name
from pathlib import Path

FILE_LIMIT = 400
FUNCTION_LIMIT = 100
GENERATED = "frontend/src/api/schema.d.ts"
FUNCTION_EXCEPTIONS = {}
KNOWN_EDGE = ("milo_home.database", "milo_home.database_inspection")
SOURCE_ROOTS = ("backend/src", "frontend/src", "scripts")
SUFFIXES = {".py", ".ts", ".vue", ".css", ".mjs"}


def source_files(root: Path) -> list[Path]:
    return sorted(
        path
        for directory in SOURCE_ROOTS
        for path in (root / directory).rglob("*")
        if path.is_file()
        and path.suffix in SUFFIXES
        and path.relative_to(root).as_posix() != GENERATED
        and "__pycache__" not in path.parts
    )


def local_imports(tree: ast.AST, module: str, package_file: bool) -> set[str]:
    package = module if package_file else module.rpartition(".")[0]
    targets = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            targets.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            name = "." * node.level + (node.module or "")
            base = resolve_name(name, package) if node.level else name
            targets.add(base)
            targets.update(f"{base}.{alias.name}" for alias in node.names)
    return targets


def dependency_cycles(graph: dict[str, set[str]]) -> list[str]:
    visited = set()
    stack = []
    active = set()
    issues = []

    def visit(module):
        if module in active:
            cycle = stack[stack.index(module) :] + [module]
            issues.append("依赖环：" + " -> ".join(cycle))
            return
        if module in visited:
            return
        visited.add(module)
        active.add(module)
        stack.append(module)
        for target in sorted(graph[module]):
            visit(target)
        stack.pop()
        active.remove(module)

    for module in sorted(graph):
        visit(module)
    return issues


def inspect(root: Path) -> tuple[list[str], list[str]]:
    issues = []
    notes = []
    graph = {}
    for path in source_files(root):
        relative = path.relative_to(root).as_posix()
        try:
            raw = path.read_bytes()
            content = raw.decode("utf-8")
        except (OSError, UnicodeError):
            issues.append(f"{relative}：无法读取 UTF-8 源码")
            continue
        if b"\r" in raw or (raw and not raw.endswith(b"\n")):
            issues.append(f"{relative}：需要 LF 换行和文件末尾换行")
        lines = len(content.splitlines())
        if lines > FILE_LIMIT:
            issues.append(f"{relative}：{lines} 行，超过 {FILE_LIMIT} 行 review 预算")
        if path.suffix != ".py":
            continue
        try:
            tree = ast.parse(content, filename=relative)
        except SyntaxError as error:
            issues.append(f"{relative}:{error.lineno}：Python 语法错误")
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                size = node.end_lineno - node.lineno + 1
                key = (relative, node.name)
                limit = FUNCTION_EXCEPTIONS.get(key, FUNCTION_LIMIT)
                if size > limit:
                    issues.append(f"{relative}:{node.lineno} {node.name}：{size} 行，预算 {limit}")
                if key in FUNCTION_EXCEPTIONS:
                    notes.append(f"既有债务：{relative} {node.name} {size}/{limit} 行")
        if relative.startswith("backend/src/"):
            parts = list(path.relative_to(root / "backend/src").with_suffix("").parts)
            package_file = parts[-1] == "__init__"
            if package_file:
                parts.pop()
            module = ".".join(parts)
            graph[module] = local_imports(tree, module, package_file)
    for module, targets in graph.items():
        targets.intersection_update(graph)
        targets.discard(module)
    source, target = KNOWN_EDGE
    if target in graph.get(source, set()):
        graph[source].remove(target)
        notes.append(f"既有债务：延迟依赖 {source} -> {target}")
    issues.extend(dependency_cycles(graph))
    return issues, notes


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    issues, notes = inspect(root)
    for note in notes:
        print(note)
    for issue in issues:
        print(issue)
    print(f"结构检查：{len(issues)} 个阻断项；已有债务见上，仅静态检查，不代表运行验收。")
    return int(bool(issues))


if __name__ == "__main__":
    raise SystemExit(main())
