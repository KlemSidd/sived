"""Run one queued job.

Demo mode closes the job with a short note and does not build a prompt.
A job with agent code runs the package snapshotted at checkout. The manifest
picks the harness. Each skill() call is one model call. The documents the
harness returns are what gets stored. A queued job with no agent snapshot
still runs the fixed compendium, diagnose, and readback sequence.
"""

from __future__ import annotations

from app import config
from app import llm
from app.agent import PLATFORM_SKILLS, parse_skills
from app.db import open_db, utcnow
from app.harness import load_workflow, run_package, stage, staged_skill_body
from app.package import ensure_job_package
from app.estimate import (
    DEV_MAX,
    READBACK_MAX,
    COMPENDIUM_MAX,
    call_micros,
)
from app.prompts import ASKS, build_prompt, sha256_text, visible_words
from app.services import (
    finish_demo,
    inference_defaults,
    inference_rows,
    new_id,
    notify_author,
    read_object,
    remember_inference_models,
    write_object,
)

HEADINGS = {
    "promise": "Promise and genre fit",
    "plot": "Plot and structure",
    "character": "Character",
    "stakes": "Stakes and pacing",
    "focus": "Author focus",
}


def run_job(job_id: str, path=None) -> str:
    """Run one queued workload on this machine.

    This claim is manual. It never calls the model API, even when the
    automatic worker is set to sync.
    """
    conn = open_db(path)
    try:
        job = conn.execute("SELECT id, status FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if job is None:
            raise ValueError("workload not found")
        if job["status"] != "queued":
            raise ValueError("that workload is not queued")
        updated = conn.execute(
            """
            UPDATE jobs
            SET status = 'running', started_at = ?, lease_owner = 'manual',
                lease_expires_at = datetime('now', '+30 minutes')
            WHERE id = ? AND status = 'queued'
            """,
            (utcnow(), job_id),
        )
        if updated.rowcount == 0:
            raise ValueError("that workload is not queued")
        conn.commit()
        try:
            _run_job(conn, job_id, local=True)
        except Exception as exc:
            conn.execute(
                """
                UPDATE jobs
                SET status = 'failed', error_code = ?, finished_at = ?
                WHERE id = ? AND status = 'running'
                """,
                (_failure_code(exc), utcnow(), job_id),
            )
            conn.commit()
            notify_author(conn, job_id)
            raise
        return job_id
    finally:
        conn.close()


def run_once(path=None) -> str | None:
    conn = open_db(path)
    job = conn.execute(
        """
        SELECT id FROM jobs
        WHERE status = 'queued'
        ORDER BY created_at
        LIMIT 1
        """
    ).fetchone()
    if job is None:
        conn.close()
        return None
    # open_db may already have written rows. total_changes counts those too,
    # so the claim uses this statement's rowcount.
    claimed = conn.execute(
        """
        UPDATE jobs
        SET status = 'running', started_at = ?, lease_owner = 'worker',
            lease_expires_at = datetime('now', '+30 minutes')
        WHERE id = ? AND status = 'queued'
        """,
        (utcnow(), job["id"]),
    )
    if claimed.rowcount != 1:
        conn.close()
        return None
    conn.commit()
    try:
        _run_job(conn, job["id"])
    except Exception as exc:
        conn.execute(
            """
            UPDATE jobs
            SET status = 'failed', error_code = ?, finished_at = ?
            WHERE id = ? AND status = 'running'
            """,
            (_failure_code(exc), utcnow(), job["id"]),
        )
        conn.commit()
        notify_author(conn, job["id"])
        raise
    finally:
        conn.close()
    return job["id"]


def _run_job(conn, job_id: str, *, local: bool = False) -> None:
    if config.DEMO_MODE:
        finish_demo(conn, job_id)
        conn.commit()
        notify_author(conn, job_id)
        return
    job = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if (job["agent_code_snapshot"] or "").strip():
        _run_agent(conn, job, local=local)
        return
    route = _route_for(conn, job, "", local=local)
    manuscript = read_object(job["manuscript_object_key"])
    asks = [
        r["ask"]
        for r in conn.execute(
            "SELECT ask FROM job_passes WHERE job_id = ? AND enabled = 1 ORDER BY position",
            (job_id,),
        ).fetchall()
    ]
    form = (
        f"Title: {job['title']}\nGenre: {job['genre']}\nAudience: {job['audience']}\n"
        f"Comps: {job['comps'] or ''}\nDo not change: {job['do_not_change'] or ''}\n"
    )
    calls = []
    if job["compendium_enabled_snapshot"]:
        calls.append(("compendium", "author-compendium", "compendium"))
    for ask in asks:
        calls.append((ask, "author-dev", ask))
    if (job["focus"] or "").strip():
        calls.append(("focus", "author-dev", "focus"))
    spent = 0
    tokens = 0
    diagnoses = []
    compendium_text = ""
    readback_text = ""
    stopped = False
    for name, skill, ask in calls:
        if stopped:
            break
        attachment = compendium_text if name != "compendium" and compendium_text else ""
        prompt = build_prompt(
            skill=skill,
            ask=ask if ask != "compendium" else "promise",
            persona=job["instructions_snapshot"],
            manuscript=manuscript,
            form=form if name != "focus" else form + f"Focus: {job['focus']}",
            attachment=attachment,
        )
        if name == "compendium":
            prompt = prompt.replace(
                ASKS["promise"],
                "Compile what the manuscript establishes. Tag inferences DRAFT or OPEN. Do not mark them LOCKED.",
                1,
            )
        max_tokens = COMPENDIUM_MAX if name == "compendium" else DEV_MAX
        reply, spent, tokens, stopped = _bounded_call(
            conn, job, name, prompt, max_tokens, spent, tokens, local=local, route=route
        )
        if stopped:
            break
        if name in HEADINGS and visible_words(reply) > 500:
            reply = ""
        if name == "compendium":
            compendium_text = reply
        elif reply:
            diagnoses.append((name, reply))
    if job["readback_enabled_snapshot"] and compendium_text and not stopped:
        prompt = build_prompt(
            skill="author-readback",
            ask="readback",
            persona=job["instructions_snapshot"],
            manuscript=manuscript,
            form=form,
            attachment=compendium_text,
        )
        reply, spent, tokens, stopped = _bounded_call(
            conn, job, "readback", prompt, READBACK_MAX, spent, tokens, local=local, route=route
        )
        if reply and not stopped:
            readback_text = reply
    if not diagnoses:
        conn.execute(
            "UPDATE jobs SET status = 'failed', error_code = 'no_diagnosis', finished_at = ?, cost_usd_micros = ?, tokens_used = ? WHERE id = ?",
            (utcnow(), spent, tokens, job_id),
        )
        conn.commit()
        notify_author(conn, job_id)
        return
    letter = _assemble(diagnoses)
    org = job["organization_id"]
    editor = job["editor_id"]
    author = job["author_id"]
    letter_key = f"artifacts/{org}/{editor}/{author}/{job_id}/letter.md"
    write_object(letter_key, letter)
    _artifact(conn, job, "letter", letter_key)
    if compendium_text:
        key = f"artifacts/{org}/{editor}/{author}/{job_id}/compendium.md"
        write_object(key, compendium_text)
        _artifact(conn, job, "compendium", key)
    if readback_text:
        key = f"artifacts/{org}/{editor}/{author}/{job_id}/readback.md"
        write_object(key, readback_text)
        _artifact(conn, job, "readback", key)
    conn.execute(
        """
        UPDATE jobs
        SET status = 'ready', finished_at = ?, cost_usd_micros = ?, tokens_used = ?
        WHERE id = ?
        """,
        (utcnow(), spent, tokens, job_id),
    )
    _add_spend(conn, spent)
    conn.commit()
    notify_author(conn, job_id)


def _run_agent(conn, job, *, local: bool = False) -> None:
    """Stage the snapshotted package and run its workflow.json."""
    job_id = job["id"]
    form = {
        "title": job["title"] or "",
        "genre": job["genre"] or "",
        "audience": job["audience"] or "",
        "comps": job["comps"] or "",
        "do_not_change": job["do_not_change"] or "",
        "focus": job["focus"] or "",
    }
    agent_root = ensure_job_package(job)
    route = _route_for(conn, job, _named_model(agent_root), local=local)
    manuscript = read_object(job["manuscript_object_key"])
    work = stage(job_id, agent_root, manuscript, form)
    custom = {
        item["name"]: item["body"]
        for item in parse_skills(job["agent_skills_snapshot"])
        if item.get("name")
    }
    state = {"spent": 0, "tokens": 0, "stopped": False, "called": 0, "useful": False}

    def skill_fn(skill_name, ask, attachment="", name=""):
        state["called"] += 1
        if state["stopped"]:
            return ""
        skill_name = "" if skill_name is None else str(skill_name)
        call_name = str(name or skill_name or "dev")
        body = staged_skill_body(work, skill_name)
        if body is None:
            body = custom.get(skill_name)
        if body is None and skill_name not in PLATFORM_SKILLS:
            raise ValueError(f"unknown skill {skill_name}")
        prompt = build_prompt(
            skill=skill_name,
            ask="promise",
            persona=job["instructions_snapshot"] or "",
            manuscript=manuscript,
            form=_form_text(form),
            attachment="" if attachment is None else str(attachment),
            skill_body=body,
            question="" if ask is None else str(ask),
        )
        reply, state["spent"], state["tokens"], stopped = _bounded_call(
            conn,
            job,
            call_name,
            prompt,
            _max_tokens(call_name, skill_name),
            state["spent"],
            state["tokens"],
            local=local,
            route=route,
        )
        if stopped:
            state["stopped"] = True
            return ""
        if call_name in HEADINGS and visible_words(reply) > 500:
            reply = ""
        if reply.strip():
            state["useful"] = True
        return reply

    documents = run_package(agent_root, manuscript, form, skill_fn)
    if state["called"] and not state["useful"]:
        _mark_failed(conn, job_id, "no_diagnosis", state["spent"], state["tokens"])
        return
    if not documents:
        _mark_failed(conn, job_id, "no_diagnosis", state["spent"], state["tokens"])
        return
    org = job["organization_id"]
    editor = job["editor_id"]
    author = job["author_id"]
    for kind, text in documents.items():
        (work / "outputs" / f"{kind}.md").write_text(text, encoding="utf-8")
        key = f"artifacts/{org}/{editor}/{author}/{job_id}/{kind}.md"
        write_object(key, text)
        _artifact(conn, job, kind, key)
    conn.execute(
        """
        UPDATE jobs
        SET status = 'ready', finished_at = ?, cost_usd_micros = ?, tokens_used = ?
        WHERE id = ?
        """,
        (utcnow(), state["spent"], state["tokens"], job_id),
    )
    _add_spend(conn, state["spent"])
    conn.commit()
    notify_author(conn, job_id)


def _form_text(form: dict) -> str:
    return (
        f"Title: {form.get('title', '')}\n"
        f"Genre: {form.get('genre', '')}\n"
        f"Audience: {form.get('audience', '')}\n"
        f"Comps: {form.get('comps', '')}\n"
        f"Do not change: {form.get('do_not_change', '')}\n"
        f"Focus: {form.get('focus', '')}\n"
    )


def _max_tokens(call_name: str, skill_name: str) -> int:
    if call_name in ("compendium", "author-compendium") or skill_name == "author-compendium":
        return COMPENDIUM_MAX
    if call_name in ("readback", "author-readback") or skill_name == "author-readback":
        return READBACK_MAX
    return DEV_MAX


def _mark_failed(conn, job_id: str, code: str, spent: int, tokens: int) -> None:
    conn.execute(
        """
        UPDATE jobs
        SET status = 'failed', error_code = ?, finished_at = ?, cost_usd_micros = ?, tokens_used = ?
        WHERE id = ?
        """,
        (code, utcnow(), spent, tokens, job_id),
    )
    conn.commit()
    notify_author(conn, job_id)


def _assemble(diagnoses: list[tuple[str, str]]) -> str:
    parts = ["# Developmental edit", ""]
    for name, text in diagnoses:
        parts.append(f"## {HEADINGS[name]}")
        parts.append("")
        parts.append(text.strip())
        parts.append("")
    return "\n".join(parts).strip() + "\n"


def _artifact(conn, job, kind: str, key: str) -> None:
    conn.execute(
        """
        INSERT INTO artifacts (id, job_id, editor_id, organization_id, kind, object_key, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (new_id(), job["id"], job["editor_id"], job["organization_id"], kind, key, utcnow()),
    )


def _add_spend(conn, micros: int) -> None:
    day = utcnow()[:10]
    conn.execute(
        """
        INSERT INTO spend_guard (day, spent_usd_micros) VALUES (?, ?)
        ON CONFLICT(day) DO UPDATE SET spent_usd_micros = spent_usd_micros + excluded.spent_usd_micros
        """,
        (day, micros),
    )


def _over_daily(conn, spent_this_job: int, extra: int) -> bool:
    day = utcnow()[:10]
    row = conn.execute("SELECT spent_usd_micros FROM spend_guard WHERE day = ?", (day,)).fetchone()
    prior = int(row["spent_usd_micros"]) if row else 0
    return prior + spent_this_job + extra > config.DAILY_STOP_MICROS


def _failure_code(exc: BaseException) -> str:
    if isinstance(exc, llm.InferenceError):
        return str(exc)
    return type(exc).__name__


def _named_model(root) -> str:
    if not (root / "workflow.json").is_file():
        return ""
    return load_workflow(root).get("model") or ""


def _route_for(conn, job, workflow_model: str, *, local: bool):
    """Providers saved on the practice, or None when the process setup still applies.

    A named model is used when any saved provider lists it. Defaults are used
    when the file names none, or when none list the named one. A failed call
    tries the next provider for that same model and does not switch models.
    The manuscript is not sent until a listed model is chosen.
    """
    if local or config.DEMO_MODE:
        return None
    rows = inference_rows(conn, job["organization_id"])
    if not rows:
        return None
    model = (workflow_model or "").strip()
    defaults = inference_defaults(conn, job["organization_id"])
    if not model and not defaults:
        raise llm.InferenceError("this workflow does not name a model")
    accounts = _refreshed_accounts(conn, rows)
    if model:
        picked = llm.candidates(model, accounts)
        if picked:
            _use_model(conn, job, picked[0]["model"])
            return {"model": model, "targets": picked}
        if not defaults:
            raise llm.InferenceError(f"no configured provider offers {model}")
    groups = llm.fallback_targets(defaults, accounts)
    if not groups:
        if model:
            raise llm.InferenceError(f"no configured provider offers {model} or a default model")
        raise llm.InferenceError("no configured provider offers a default model")
    targets = [target for _name, group in groups for target in group]
    _use_model(conn, job, targets[0]["model"])
    return {"model": groups[0][0], "targets": targets}


def _refreshed_accounts(conn, rows) -> list:
    accounts = []
    for row in rows:
        try:
            account = llm.account_from_row(row)
        except ValueError:
            continue
        fresh = llm.refreshed_models(account)
        if fresh is not None:
            account["models"] = fresh
            remember_inference_models(conn, row["id"], fresh)
        accounts.append(account)
    return accounts


def _use_model(conn, job, model: str) -> None:
    conn.execute("UPDATE jobs SET model_id = ? WHERE id = ?", (model, job["id"]))


def _bounded_call(conn, job, name: str, prompt: str, max_tokens: int, spent: int, tokens: int, *, local: bool = False, route=None):
    """Run one call when the dollar cap, token cap, and the daily stop allow it.

    A stop returns no reply and leaves the spent totals unchanged. The caller
    keeps any diagnoses already stored. The caps are an operating limit.
    """
    prompt_tokens = max(1, len(prompt) // 4)
    projected = call_micros(prompt_tokens, max_tokens)
    blocked = (
        spent + projected > job["cost_cap_usd_micros"]
        or tokens + prompt_tokens + max_tokens > job["token_cap"]
        or _over_daily(conn, spent, projected)
    )
    if blocked:
        return "", spent, tokens, True
    reply, usage = complete(name, job, prompt, max_tokens, local=local, route=route)
    in_tok = int(usage["input_tokens"])
    out_tok = int(usage["output_tokens"])
    micros = call_micros(in_tok, out_tok)
    conn.execute(
        """
        INSERT INTO provider_calls (
          id, job_id, call_name, prompt_sha256, input_tokens, output_tokens, status, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, 'done', ?)
        """,
        (new_id(), job["id"], name, sha256_text(prompt), in_tok, out_tok, utcnow()),
    )
    conn.execute(
        """
        INSERT INTO token_costs (
          id, job_id, editor_id, organization_id, call_name, cost_usd_micros, input_tokens, output_tokens, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (new_id(), job["id"], job["editor_id"], job["organization_id"], name, micros, in_tok, out_tok, utcnow()),
    )
    return reply, spent + micros, tokens + in_tok + out_tok, False


def complete(name: str, job, prompt: str, max_tokens: int, *, local: bool = False, route=None) -> tuple[str, dict]:
    if config.DEMO_MODE:
        raise RuntimeError("demo mode does not call the model")
    if not local and route is not None:
        return llm.complete_routed(route["targets"], prompt, max_tokens, route["model"])
    if not local and llm.ready():
        return llm.complete(prompt, max_tokens)
    return _local_complete(name, job), {"input_tokens": max(1, len(prompt) // 4), "output_tokens": 120}


def _local_complete(name: str, job) -> str:
    title = job["title"]
    genre = job["genre"]
    audience = job["audience"]
    if name == "compendium":
        locked = job["do_not_change"] or ""
        lines = [
            f"# {job['slug']}-compendium",
            "",
            f"- DRAFT: The manuscript is titled {title}.",
            f"- DRAFT: The stated genre is {genre}.",
            f"- DRAFT: The stated audience is {audience}.",
        ]
        if locked:
            lines.append(f"- LOCKED: {locked}")
        else:
            lines.append("- OPEN: No do-not-change constraint was given.")
        return "\n".join(lines) + "\n"
    if name == "readback":
        return _local_readback(job, "")
    heading = HEADINGS.get(name, name)
    return (
        f"The {heading.lower()} of {title} does not yet pay off what a {genre} story owes {audience}.\n\n"
        f"Left alone, the reader loses the reason to stay with the book.\n\n"
        f"Preferred fix: cut or reorder the sequence that delays that payoff, at the cost of a subplot the draft currently keeps.\n\n"
        f"Cheaper alternative: leave the sequence and add one concrete consequence in the next scene, which rules out keeping that scene unchanged.\n"
    )


def _local_readback(job, compendium: str) -> str:
    return (
        f"# Readback\n\n"
        f"- DRAFT: {job['title']} is the manuscript this run read.\n"
        f"- OPEN: Whether {job['genre']} is the final shelf is not locked by the author.\n"
    )


def main() -> None:
    job_id = run_once()
    print(job_id or "idle")


if __name__ == "__main__":
    main()
