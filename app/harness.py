"""The persona runner.

A package is ``workflow.json``. ``manifest.json`` names that file. The worker
stages the package and any custom skills, then runs one model call per step.
"""

from __future__ import annotations

import json
import re
import shutil
import tempfile
from pathlib import Path

from app.llm import MODEL_RE
from app.agent import (
    MAX_CODE,
    PLATFORM_SKILLS,
    estimate_agent_words,
    static_calls,
    validate_agent,
)
from app.package import custom_skills, job_work_dir, list_files
from app.prompts import ASKS

_NAME = re.compile(r"^[a-z][a-z0-9-]{0,40}$")


def read_manifest(root: Path) -> dict:
    path = root / "manifest.json"
    if not path.is_file():
        raise ValueError("the package has no manifest")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError("the package manifest could not be read") from exc
    if not isinstance(data, dict):
        raise ValueError("the package manifest could not be read")
    if data.get("harness") != "workflow" or data.get("entry") != "workflow.json":
        raise ValueError("a persona is a workflow.json file")
    if (root / "agent.py").is_file():
        raise ValueError("a persona is a workflow.json file")
    if not (root / "workflow.json").is_file():
        raise ValueError("the package is missing workflow.json")
    return {"harness": "workflow", "entry": "workflow.json"}


def inspect_root(root: Path) -> dict:
    manifest = read_manifest(root)
    return {**manifest, "files": list_files(root)}


def starter_files() -> tuple[dict[str, str], str]:
    """Files for a new persona, and the call-count source stored beside them."""
    spec = default_workflow()
    return _workflow_files(spec), gate_source(spec)


def compile_package(root: Path) -> dict:
    """Read a package into the call-count source and the skills stored beside it."""
    manifest = read_manifest(root)
    skills = custom_skills(root)
    _check_skill_names(skills)
    spec = load_workflow(root)
    _check_step_skills(spec, skills)
    code = gate_source(spec)
    validate_agent(code, skills)
    calls = static_calls(code)
    compendium = any(item["skill"] == "author-compendium" or item["call"] == "compendium" for item in calls)
    readback = any(item["skill"] == "author-readback" or item["call"] == "readback" for item in calls)
    if readback and not compendium:
        raise ValueError("readback requires the compendium skill")
    return {**manifest, "code": code, "skills": skills, "compendium": compendium, "readback": readback}


def estimate_root(root: Path, words: int) -> dict:
    info = compile_package(root)
    return estimate_agent_words(words, info["code"])


def required_skills(root: Path) -> set[str]:
    read_manifest(root)
    names = {step["skill"] for step in load_workflow(root)["steps"]}
    for item in custom_skills(root):
        names.add(item["name"])
    return names


def stage(job_id: str, agent_root: Path, manuscript: str, form: dict) -> Path:
    """Copy the manuscript, the form, and any custom skills into the job workspace.

    The built-in skill names have no files. The ask on the step is the question.
    """
    work = job_work_dir(job_id)
    if work.exists():
        shutil.rmtree(work)
    inputs = work / "inputs"
    (work / "outputs").mkdir(parents=True)
    skills = work / "skills"
    inputs.mkdir(parents=True)
    skills.mkdir()
    (inputs / "manuscript.txt").write_text(manuscript, encoding="utf-8")
    (inputs / "form.json").write_text(json.dumps(form, indent=2) + "\n", encoding="utf-8")
    for name in sorted(required_skills(agent_root)):
        if name in PLATFORM_SKILLS:
            continue
        source = agent_root / "skills" / name
        if source.is_dir() and (source / "SKILL.md").is_file():
            shutil.copytree(source, skills / name)
    return work


def staged_skill_body(work: Path, name: str) -> str | None:
    if not name or _NAME.fullmatch(name) is None:
        return None
    path = work / "skills" / name / "SKILL.md"
    if path.is_file():
        return path.read_text(encoding="utf-8")
    return None


def run_package(agent_root: Path, _manuscript: str, form: dict, call) -> dict:
    """Run workflow.json. The manuscript is already staged beside the package."""
    read_manifest(agent_root)
    return run_workflow(agent_root, form, call)


def run_workflow(root: Path, form: dict, call) -> dict:
    """One model call per step. A focus step runs only when the form has a focus."""
    spec = load_workflow(root)
    produced: dict[str, str] = {}
    direct: dict[str, str] = {}
    buckets: dict[str, list[tuple[str, str]]] = {}
    focus = (form.get("focus") or "").strip()
    for step in spec["steps"]:
        if step.get("when") == "focus" and not focus:
            continue
        attach = step.get("attach") or ""
        attachment = produced.get(attach, "") if attach else ""
        text = call(step["skill"], step["ask"], attachment, step["name"])
        text = "" if text is None else str(text)
        produced[step["name"]] = text
        if not text.strip():
            continue
        if step.get("produces"):
            direct[step["produces"]] = text if text.endswith("\n") else text + "\n"
        if step.get("into"):
            heading = step.get("heading") or step["name"]
            buckets.setdefault(step["into"], []).append((heading, text.strip()))
    documents = dict(direct)
    title = spec.get("title") or "Developmental edit"
    for kind, sections in buckets.items():
        parts = [f"# {title}", ""]
        for heading, text in sections:
            parts.extend([f"## {heading}", "", text, ""])
        documents[kind] = "\n".join(parts).strip() + "\n"
    return documents


def default_workflow() -> dict:
    """The starting workflow: compendium, the letter, and a readback."""
    steps = [
        {
            "name": "compendium",
            "skill": "author-compendium",
            "ask": "Compile what the manuscript establishes. Tag inferences DRAFT or OPEN. Do not mark them LOCKED.",
            "produces": "compendium",
        }
    ]
    headings = (
        ("promise", "Promise and genre fit"),
        ("plot", "Plot and structure"),
        ("character", "Character"),
        ("stakes", "Stakes and pacing"),
    )
    for key, heading in headings:
        steps.append(
            {
                "name": key,
                "skill": "author-dev",
                "ask": ASKS[key],
                "into": "letter",
                "heading": heading,
                "attach": "compendium",
            }
        )
    steps.append(
        {
            "name": "focus",
            "skill": "author-dev",
            "ask": ASKS["focus"],
            "into": "letter",
            "heading": "Author focus",
            "attach": "compendium",
            "when": "focus",
        }
    )
    steps.append(
        {
            "name": "readback",
            "skill": "author-readback",
            "ask": ASKS["readback"],
            "produces": "readback",
            "attach": "compendium",
        }
    )
    return {"title": "Developmental edit", "steps": steps}


def load_workflow(root: Path) -> dict:
    path = root / "workflow.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("workflow.json could not be read") from exc
    return parse_workflow(data)


def parse_workflow_text(text: str) -> dict:
    """Validate workflow.json text. Invalid JSON is refused before the file is saved."""
    if not isinstance(text, str):
        raise ValueError("workflow.json is not valid JSON")
    try:
        data = json.loads(text.lstrip("\ufeff"))
    except json.JSONDecodeError as exc:
        raise ValueError("workflow.json is not valid JSON") from exc
    return parse_workflow(data)


def accept_new_workflow(data: dict) -> dict:
    """Check a new workflow.json. A new file has no custom skills yet."""
    spec = parse_workflow(data)
    root = Path(tempfile.mkdtemp(prefix="workflow-"))
    try:
        for name, text in _workflow_files(spec).items():
            (root / name).write_text(text, encoding="utf-8")
        compile_package(root)
    finally:
        shutil.rmtree(root, ignore_errors=True)
    return spec


def parse_workflow(data) -> dict:
    """Validate a workflow.json object and return the stored form."""
    if not isinstance(data, dict) or not isinstance(data.get("steps"), list) or not data["steps"]:
        raise ValueError("a workflow needs at least one step")
    title = data.get("title") or "Developmental edit"
    if not isinstance(title, str) or not title.strip() or len(title) > 80 or "\n" in title:
        raise ValueError("the workflow title is not valid")
    seen = set()
    steps = []
    for raw in data["steps"]:
        step = _step(raw, seen)
        attach = step.get("attach")
        earlier = {item["name"] for item in steps}
        if attach and attach not in earlier:
            raise ValueError(f"{attach} is not an earlier step")
        if step.get("produces") and step.get("into"):
            raise ValueError(f"{step['name']} stores one document")
        steps.append(step)
    stored = {"title": title.strip(), "steps": steps}
    model = _model_name(data.get("model"))
    if model:
        stored["model"] = model
    return stored


def _model_name(value) -> str:
    if value in (None, ""):
        return ""
    if not isinstance(value, str):
        raise ValueError("the workflow model is not valid")
    model = value.strip()
    if not model:
        return ""
    if MODEL_RE.fullmatch(model) is None:
        raise ValueError("the workflow model is not valid")
    return model


def gate_source(spec: dict) -> str:
    """Python the call counter can read. The worker runs workflow.json, not this source."""
    lines = ["def run(manuscript, form, skill):"]
    plain = []
    focus = []
    for step in spec["steps"]:
        call = f"    skill({json.dumps(step['skill'])}, \"step\", name={json.dumps(step['name'])})"
        if step.get("when") == "focus":
            focus.append(call)
        else:
            plain.append(call)
    lines.extend(plain or ["    pass"])
    if focus:
        lines.append('    if form.get("focus"):')
        lines.extend("    " + call for call in focus)
    lines.extend(["    return {}", ""])
    code = "\n".join(lines)
    if len(code) > MAX_CODE:
        raise ValueError("this workflow has too many steps")
    return code


def _workflow_files(spec: dict) -> dict[str, str]:
    return {
        "manifest.json": json.dumps({"harness": "workflow", "entry": "workflow.json"}, indent=2) + "\n",
        "workflow.json": json.dumps(spec, indent=2) + "\n",
    }


def _step(raw, seen: set[str]) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("a workflow step could not be read")
    name = raw.get("name")
    skill = raw.get("skill")
    ask = raw.get("ask")
    if not isinstance(name, str) or _NAME.fullmatch(name) is None:
        raise ValueError("a step name uses lowercase letters, numbers, and hyphens")
    if name in seen:
        raise ValueError(f"{name} is listed twice")
    seen.add(name)
    if not isinstance(skill, str) or _NAME.fullmatch(skill) is None:
        raise ValueError(f"{name} needs a skill")
    if not isinstance(ask, str) or not ask.strip():
        raise ValueError(f"{name} needs an ask")
    step = {"name": name, "skill": skill, "ask": ask}
    when = raw.get("when") or ""
    if when not in ("", "focus"):
        raise ValueError(f"{name} has an unknown when")
    if when:
        step["when"] = when
    for key in ("produces", "into", "attach"):
        value = raw.get(key) or ""
        if not value:
            continue
        if not isinstance(value, str) or _NAME.fullmatch(value) is None:
            raise ValueError(f"{name} has a bad {key}")
        step[key] = value
    heading = raw.get("heading") or ""
    if heading:
        if not isinstance(heading, str) or len(heading) > 80 or "\n" in heading:
            raise ValueError(f"{name} has a bad heading")
        step["heading"] = heading
    return step


def _check_skill_names(skills: list[dict]) -> None:
    seen = set()
    for item in skills:
        name = item["name"]
        if _NAME.fullmatch(name) is None:
            raise ValueError("a skill name uses lowercase letters, numbers, and hyphens")
        if name in seen:
            raise ValueError(f"{name} is listed twice")
        seen.add(name)
        if not (item.get("body") or "").strip():
            raise ValueError(f"{name} needs skill text")


def _check_step_skills(spec: dict, skills: list[dict]) -> None:
    known = set(PLATFORM_SKILLS) | {item["name"] for item in skills}
    for step in spec["steps"]:
        if step["skill"] not in known:
            raise ValueError(
                f"unknown skill {step['skill']}. Platform skills are {', '.join(PLATFORM_SKILLS)}"
            )
