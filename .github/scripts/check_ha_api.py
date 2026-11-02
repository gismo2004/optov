"""Check what this integration uses of Home Assistant against the Home Assistant it runs in.

Reads the integration's own source, so there is no list to keep: every name it imports from
`homeassistant` must still exist, and every direct call of one must still fit its signature.
Methods on objects, base classes and changed behaviour are out of its reach.

    python3 check_ha_api.py custom_components/optov
"""

import ast
import importlib
import inspect
import pathlib
import sys

import annotationlib
import homeassistant.const


def imported(tree, path, report):
    """The Home Assistant objects a file imports, by the name it uses for them."""
    used = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        if not (node.module or "").startswith("homeassistant"):
            continue
        try:
            module = importlib.import_module(node.module)
        except Exception as err:  # noqa: BLE001 -- a module that fails to load is the finding
            report(path, node, f"cannot import {node.module}: {err}")
            continue
        for alias in node.names:
            found = getattr(module, alias.name, None)
            if found is None:
                try:
                    found = importlib.import_module(f"{node.module}.{alias.name}")
                except Exception:  # noqa: BLE001
                    report(path, node, f"{node.module}.{alias.name} is gone")
                    continue
            used[alias.asname or alias.name] = found
    return used


def called(node, used, path, report):
    """What a call refers to, if it is something imported from Home Assistant."""
    func = node.func
    if isinstance(func, ast.Name):
        return used.get(func.id)
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        module = used.get(func.value.id)
        if inspect.ismodule(module):
            found = getattr(module, func.attr, None)
            if found is None:
                report(path, node, f"{func.value.id}.{func.attr} is gone")
            return found
    return None


def check_call(node, target, path, report):
    """Report a call whose arguments no longer fit. Returns whether it could be checked."""
    if any(isinstance(arg, ast.Starred) for arg in node.args):
        return False
    if any(keyword.arg is None for keyword in node.keywords):
        return False
    try:
        # Python 3.14 evaluates annotations late, and Home Assistant's name types that exist
        # only for type checkers.
        signature = inspect.signature(
            target, annotation_format=annotationlib.Format.FORWARDREF
        )
    except Exception:  # noqa: BLE001 -- not everything callable has a signature
        return False
    arguments = {keyword.arg: None for keyword in node.keywords}
    try:
        signature.bind(*[None] * len(node.args), **arguments)
    except TypeError as err:
        report(path, node, f"{ast.unparse(node.func)}: {err}; signature {signature}")
    return True


def main() -> int:
    problems = []

    def report(path, node, text):
        problems.append(f"{path}:{node.lineno} {text}")

    names = calls = 0
    for path in sorted(pathlib.Path(sys.argv[1]).rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        used = imported(tree, path, report)
        names += len(used)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            target = called(node, used, path, report)
            if target is None or inspect.ismodule(target) or not callable(target):
                continue
            calls += check_call(node, target, path, report)

    print(f"Home Assistant {homeassistant.const.__version__}")
    print(f"{names} names, {calls} calls checked, {len(problems)} problems")
    print(*problems, sep="\n")
    # Nothing to compare would look like a pass, and would be a broken check.
    return 1 if problems or not calls else 0


if __name__ == "__main__":
    sys.exit(main())
