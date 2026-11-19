"""A persona is an agent: code plus the AI skills that code calls.

`run(manuscript, form, skill)` does the work. `skill(name, ask)` is one model
call against a platform skill or a skill written on the persona. The code
returns the documents to store. It cannot import, open files, or reach
private attributes.
"""

from __future__ import annotations

import ast
import json
import re
import sys

from app.estimate import (
    ATTACHMENT,
    COMPENDIUM_OUT,
    DEV_OUT,
    READBACK_OUT,
    call_micros,
    micros_to_cents,
    words_to_prompt,
)
from app.prompts import ASKS

PLATFORM_SKILLS = ("author-dev", "author-compendium", "author-readback")
CUSTOM_SLOTS = 3
MAX_CODE = 20_000
MAX_SKILL_BODY = 30_000
_LOOP_CALLS = 8
_STEP_LIMIT = 20_000
_DOC_KEY = re.compile(r"^[a-z][a-z0-9-]{0,40}$")
_SKILL_NAME = re.compile(r"^[a-z][a-z0-9-]{0,40}$")

_SAFE_BUILTINS = {
    "abs": abs,
    "all": all,
    "any": any,
    "bool": bool,
    "dict": dict,
    "enumerate": enumerate,
    "filter": filter,
    "float": float,
    "int": int,
    "len": len,
    "list": list,
    "map": map,
    "max": max,
    "min": min,
    "range": range,
    "repr": repr,
    "reversed": reversed,
    "round": round,
    "set": set,
    "sorted": sorted,
    "str": str,
    "sum": sum,
    "tuple": tuple,
    "zip": zip,
    "True": True,
    "False": False,
    "None": None,
}

_DENIED_CALLS = {
    "__import__",
    "open",
    "exec",
    "eval",
    "compile",
    "getattr",
    "setattr",
    "delattr",
    "globals",
    "locals",
    "vars",
    "dir",
    "type",
    "object",
    "super",
    "input",
    "help",
    "breakpoint",
    "memoryview",
    "classmethod",
    "staticmethod",
    "property",
}

_ALLOWED_NODES = {
    ast.Module,
    ast.FunctionDef,
    ast.arguments,
    ast.arg,
    ast.Return,
    ast.Assign,
    ast.AugAssign,
    ast.If,
    ast.For,
    ast.While,
    ast.Expr,
    ast.Pass,
    ast.Break,
    ast.Continue,
    ast.Call,
    ast.keyword,
    ast.Name,
    ast.Constant,
    ast.List,
    ast.Tuple,
    ast.Dict,
    ast.Subscript,
    ast.Slice,
    ast.Attribute,
    ast.BinOp,
    ast.UnaryOp,
    ast.BoolOp,
    ast.Compare,
    ast.IfExp,
    ast.ListComp,
    ast.DictComp,
    ast.GeneratorExp,
    ast.comprehension,
    ast.JoinedStr,
    ast.FormattedValue,
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.FloorDiv,
    ast.Mod,
    ast.Pow,
    ast.BitAnd,
    ast.BitOr,
    ast.BitXor,
    ast.LShift,
    ast.RShift,
    ast.And,
    ast.Or,
    ast.Not,
    ast.Invert,
    ast.UAdd,
    ast.USub,
    ast.Eq,
    ast.NotEq,
    ast.Lt,
    ast.LtE,
    ast.Gt,
    ast.GtE,
    ast.In,
    ast.NotIn,
    ast.Is,
    ast.IsNot,
    ast.Load,
    ast.Store,
}


def default_agent_source() -> str:
    """The starting agent: compendium, four diagnoses, optional focus, readback."""
    lines = [
        "def run(manuscript, form, skill):",
        "    bible = skill(",
        '        "author-compendium",',
        '        "Compile what the manuscript establishes. Tag inferences DRAFT or OPEN. Do not mark them LOCKED.",',
        '        name="compendium",',
        "    )",
        '    letter = ["# Developmental edit", ""]',
    ]
    headings = (
        ("promise", "Promise and genre fit"),
        ("plot", "Plot and structure"),
        ("character", "Character"),
        ("stakes", "Stakes and pacing"),
    )
    for key, heading in headings:
        ask = ASKS[key].replace('"', '\\"')
        lines.append(f'    reply = skill("author-dev", "{ask}", name="{key}", attachment=bible)')
        lines.append("    if reply:")
        lines.append(f'        letter.extend(["## {heading}", "", reply.strip(), ""])')
    focus = ASKS["focus"].replace('"', '\\"')
    readback = ASKS["readback"].replace('"', '\\"')
    lines.extend(
        [
            '    focus = (form.get("focus") or "").strip()',
            "    if focus:",
            "        reply = skill(",
            '            "author-dev",',
            f'            "{focus}",',
            '            name="focus",',
            "            attachment=bible,",
            "        )",
            "        if reply:",
            '            letter.extend(["## Author focus", "", reply.strip(), ""])',
            "    readback = skill(",
            '        "author-readback",',
            f'        "{readback}",',
            '        name="readback",',
            "        attachment=bible,",
            "    )",
            "    return {",
            '        "letter": "\\n".join(letter).strip() + "\\n",',
            '        "compendium": bible,',
            '        "readback": readback,',
            "    }",
            "",
        ]
    )
    return "\n".join(lines)


DEFAULT_AGENT = default_agent_source()


def parse_skills(raw: str | None) -> list[dict]:
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("custom skills could not be read") from exc
    if not isinstance(data, list):
        raise ValueError("custom skills could not be read")
    skills = []
    for item in data:
        if not isinstance(item, dict):
            raise ValueError("custom skills could not be read")
        skills.append({"name": str(item.get("name") or ""), "body": str(item.get("body") or "")})
    return skills


def skill_names(skills: list[dict]) -> set[str]:
    return {item["name"] for item in skills if item.get("name")}


def validate_agent(code: str, skills: list[dict] | None = None) -> ast.Module:
    """Reject code that is not a single run() function over safe operations."""
    if not (code or "").strip():
        raise ValueError("write the agent code")
    if len(code) > MAX_CODE:
        raise ValueError("agent code is over 20,000 characters")
    known = set(PLATFORM_SKILLS) | skill_names(skills or [])
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        raise ValueError("agent code could not be read") from exc
    if len(tree.body) != 1 or not isinstance(tree.body[0], ast.FunctionDef):
        raise ValueError("agent code must define run(manuscript, form, skill)")
    fn = tree.body[0]
    if fn.name != "run" or [arg.arg for arg in fn.args.args] != ["manuscript", "form", "skill"]:
        raise ValueError("agent code must define run(manuscript, form, skill)")
    if fn.args.vararg or fn.args.kwarg or fn.args.kwonlyargs or fn.args.defaults or fn.args.posonlyargs:
        raise ValueError("agent code must define run(manuscript, form, skill)")
    if getattr(fn, "type_params", None):
        raise ValueError("agent code must define run(manuscript, form, skill)")
    for node in ast.walk(tree):
        if type(node) not in _ALLOWED_NODES:
            raise ValueError("agent code cannot import, open files, or define classes")
        if isinstance(node, ast.Name) and (node.id in _DENIED_CALLS or node.id.startswith("_")):
            raise ValueError("agent code cannot import, open files, or define classes")
        if isinstance(node, ast.Attribute) and (node.attr.startswith("_") or node.attr in _DENIED_CALLS):
            raise ValueError("agent code cannot import, open files, or define classes")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _DENIED_CALLS:
            raise ValueError("agent code cannot import, open files, or define classes")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "skill":
            if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                if node.args[0].value not in known:
                    raise ValueError(
                        f"unknown skill {node.args[0].value}. Platform skills are {', '.join(PLATFORM_SKILLS)}"
                    )
    return tree


def normalize_skills(skills: list[dict]) -> list[dict]:
    if len(skills) > CUSTOM_SLOTS:
        raise ValueError(f"a persona can have {CUSTOM_SLOTS} custom skills")
    cleaned = []
    seen = set()
    for item in skills:
        name = (item.get("name") or "").strip()
        body = item.get("body") or ""
        if not name and not body.strip():
            continue
        if not _SKILL_NAME.match(name):
            raise ValueError("a skill name uses lowercase letters, numbers, and hyphens")
        if name in PLATFORM_SKILLS:
            raise ValueError(f"{name} is already a platform skill")
        if name in seen:
            raise ValueError(f"{name} is listed twice")
        if not body.strip():
            raise ValueError(f"{name} needs skill text")
        if len(body) > MAX_SKILL_BODY:
            raise ValueError(f"{name} is over 30,000 characters")
        seen.add(name)
        cleaned.append({"name": name, "body": body})
    return cleaned


def static_calls(code: str) -> list[dict]:
    """Each skill() in the source. A call inside a loop counts as eight."""
    tree = ast.parse(code)
    parents: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Name) or node.func.id != "skill":
            continue
        skill = _const(node.args[0]) if node.args else None
        call = None
        for keyword in node.keywords:
            if keyword.arg == "name":
                call = _const(keyword.value)
        found.append({"skill": skill, "call": call, "count": _LOOP_CALLS if _in_loop(node, parents) else 1})
    return found


def estimate_agent_words(words: int, code: str) -> dict:
    """Token estimate for the skill() calls written in the agent."""
    base = words_to_prompt(words)
    specs = []
    try:
        specs = static_calls(code)
    except SyntaxError:
        specs = []
    has_compendium = any(
        item["skill"] == "author-compendium" or item["call"] == "compendium" for item in specs
    )
    calls = []
    total = 0
    in_tok = 0
    out_tok = 0
    for item in specs:
        kind = item["call"] or item["skill"] or "dev"
        output, _max = _output_tokens(kind)
        prompt = base + (ATTACHMENT if has_compendium and kind not in ("compendium", "author-compendium") else 0)
        for _ in range(item["count"]):
            micros = call_micros(prompt, output)
            total += micros
            in_tok += prompt
            out_tok += output
            calls.append({"name": kind, "prompt_tokens": prompt, "output_tokens": output})
    return {
        "calls": calls,
        "input_tokens": in_tok,
        "output_tokens": out_tok,
        "cost_usd_micros": total,
        "token_cents": micros_to_cents(total),
        "words": words,
        "prompt_base": base,
    }


def execute(code: str, skills: list[dict], manuscript: str, form: dict, skill_fn) -> dict:
    """Run the agent. skill_fn(name, ask, attachment='', name='') performs one model call."""
    tree = validate_agent(code, skills)
    budget = {"n": 0, "on": True}

    def trace(frame, event, arg):
        if event == "line" and budget["on"]:
            budget["n"] += 1
            if budget["n"] > _STEP_LIMIT:
                raise RuntimeError("agent code ran too long")
        return trace

    def wrapped(skill_name, ask, attachment="", name=""):
        budget["on"] = False
        try:
            return skill_fn(skill_name, ask, attachment=attachment or "", name=name or "")
        finally:
            budget["on"] = True

    env = {"__builtins__": _SAFE_BUILTINS}
    local: dict = {}
    previous = sys.gettrace()
    sys.settrace(trace)
    try:
        exec(compile(tree, "<agent>", "exec"), env, local)
        result = local["run"](manuscript, form, wrapped)
    finally:
        sys.settrace(previous)
    if not isinstance(result, dict):
        raise ValueError("the agent must return a dict of documents")
    documents = {}
    for key, value in result.items():
        if not isinstance(key, str) or _DOC_KEY.match(key) is None:
            raise ValueError("a document name must be a short lowercase word")
        if not isinstance(value, str):
            raise ValueError(f"{key} must be text")
        if value.strip():
            documents[key] = value if value.endswith("\n") else value + "\n"
    return documents


def _const(node: ast.AST):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _in_loop(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> bool:
    current = node
    while current in parents:
        current = parents[current]
        if isinstance(current, (ast.For, ast.While, ast.comprehension)):
            return True
    return False


def _output_tokens(kind: str) -> tuple[int, int]:
    if kind in ("compendium", "author-compendium"):
        return COMPENDIUM_OUT, 0
    if kind in ("readback", "author-readback"):
        return READBACK_OUT, 0
    return DEV_OUT, 0
