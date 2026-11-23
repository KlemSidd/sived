"""Server-rendered desk for one editor practice."""

from __future__ import annotations

import hashlib
import html
import json
import re
import ssl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

from app import acme
from app import config
from app import llm
from app import patreon
from app.origin import resolve_public_origin
from app.db import open_db, utcnow
from app.agent import PLATFORM_SKILLS, estimate_agent_words
from app.estimate import micros_to_cents
from app.harness import (
    accept_new_workflow,
    default_workflow,
    inspect_root,
    load_workflow,
    parse_workflow,
    parse_workflow_text,
)
from app.package import ensure_persona_package, list_files
from app.manuscript import manuscript_text
from app.mcp_server import handle_message
from app.money import usd
from app.theme import css_root, resolve_theme
from app.services import (
    PERMISSIONS,
    seed_local_accounts,
    accept_author_invite,
    accept_editor_invite,
    allowed,
    author_for_user,
    author_notices_account,
    become_customer,
    clear_author_notices,
    clear_inference,
    refresh_inference_catalog,
    clear_invitation_mail,
    create_job,
    create_persona,
    editor_public,
    editor_for_user,
    install_persona_zip,
    invite_editor,
    issue_agent_token,
    jobs_visible,
    list_agent_tokens,
    user_for_agent_token,
    accept_patreon_member,
    account_roles,
    apply_patreon_webhook,
    author_may_enter,
    begin_patreon_login,
    exchange_patreon_code,
    inference_defaults,
    inference_public,
    inference_rows,
    invitation_mail_account,
    invite_admin,
    MembershipDenied,
    new_id,
    open_owner_practice,
    pending_editor_invites,
    permission_flags,
    post_message,
    practice_admin,
    practice_editors,
    practice_model_cost_micros,
    publish_persona,
    read_object,
    revoke_agent_token,
    remove_admin,
    remove_editor,
    rename_own_practice,
    request_human,
    revoke_editor_invite,
    send_invite,
    save_workflow,
    suspend_practice,
    take_patreon_state,
    search_manuscripts,
    set_assistant_permissions,
    set_author_notices,
    set_inference,
    set_inference_defaults,
    set_invitation_mail,
    patreon_ready,
    practice_appearance,
    set_patreon_integration,
    set_practice_appearance,
    token_hash,
    update_persona_profile,
    update_profile,
    user_is_owner,
    user_is_system_admin,
    write_manuscript,
)

_MAX_BODY = 9 * 1024 * 1024


def esc(value) -> str:
    return html.escape("" if value is None else str(value))


def _header_value(value) -> str:
    return str("" if value is None else value).replace("\n", "").replace("\r", "")


def _model_cost(micros) -> str:
    return usd(micros_to_cents(int(micros or 0)))


_MD_LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
_MD_CODE = re.compile(r"`([^`]+)`")
_SKILL_HELP = {
    "author-compendium": "compiles what the manuscript establishes",
    "author-dev": "diagnoses one question",
    "author-readback": "previews the compendium",
}


def _safe_href(url: str) -> str | None:
    if url.startswith("/") and not url.startswith("//") and "\\" not in url:
        return esc(url)
    if url.startswith("https://") and "\\" not in url and " " not in url:
        return esc(url)
    return None


def _md_links(text: str) -> str:
    parts = []
    cursor = 0
    for match in _MD_LINK.finditer(text):
        parts.append(esc(text[cursor:match.start()]))
        href = _safe_href(match.group(2))
        if href is None:
            parts.append(esc(match.group(0)))
        else:
            parts.append(f'<a href="{href}">{esc(match.group(1))}</a>')
        cursor = match.end()
    parts.append(esc(text[cursor:]))
    return "".join(parts)


def _inline(text: str) -> str:
    parts = []
    cursor = 0
    for match in _MD_CODE.finditer(text):
        parts.append(_md_links(text[cursor:match.start()]))
        parts.append(f"<code>{esc(match.group(1))}</code>")
        cursor = match.end()
    parts.append(_md_links(text[cursor:]))
    return "".join(parts)


def _md_starts(line: str) -> bool:
    if line.startswith(("```", "|", "- ")):
        return True
    if line.startswith("# ") or line.startswith("## ") or line.startswith("### "):
        return True
    return re.match(r"\d+\. ", line) is not None


def _md_table(rows: list[str]) -> str:
    parsed = []
    for row in rows:
        cells = [cell.strip() for cell in row.strip().strip("|").split("|")]
        if cells and all(cell and set(cell) <= set("-: ") for cell in cells):
            continue
        parsed.append(cells)
    if not parsed:
        return ""
    head = "".join(f"<th>{_inline(cell)}</th>" for cell in parsed[0])
    body = "".join(
        "<tr>" + "".join(f"<td>{_inline(cell)}</td>" for cell in row) + "</tr>"
        for row in parsed[1:]
    )
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def _render_markdown(text: str) -> str:
    """Headings, paragraphs, fenced code, lists, links, inline code, and tables."""
    lines = text.replace("\r\n", "\n").split("\n")
    parts: list[str] = []
    index = 0
    total = len(lines)
    while index < total:
        line = lines[index]
        if not line.strip():
            index += 1
            continue
        if line.startswith("```"):
            index += 1
            body = []
            while index < total and not lines[index].startswith("```"):
                body.append(esc(lines[index]))
                index += 1
            if index < total:
                index += 1
            parts.append("<pre><code>" + "\n".join(body) + "</code></pre>")
            continue
        if line.startswith("|"):
            rows = []
            while index < total and lines[index].startswith("|"):
                rows.append(lines[index])
                index += 1
            parts.append(_md_table(rows))
            continue
        if line.startswith("# ") or line.startswith("## ") or line.startswith("### "):
            level = len(line) - len(line.lstrip("#"))
            parts.append(f"<h{level}>{_inline(line[level:].strip())}</h{level}>")
            index += 1
            continue
        if line.startswith("- "):
            items = []
            while index < total and lines[index].startswith("- "):
                items.append(f"<li>{_inline(lines[index][2:].strip())}</li>")
                index += 1
            parts.append("<ul>" + "".join(items) + "</ul>")
            continue
        if re.match(r"\d+\. ", line):
            items = []
            while index < total and re.match(r"\d+\. ", lines[index]):
                item = re.sub(r"^\d+\. ", "", lines[index], count=1)
                items.append(f"<li>{_inline(item.strip())}</li>")
                index += 1
            parts.append("<ol>" + "".join(items) + "</ol>")
            continue
        buf = [line.strip()]
        index += 1
        while index < total and lines[index].strip() and not _md_starts(lines[index]):
            buf.append(lines[index].strip())
            index += 1
        parts.append("<p>" + _inline(" ".join(buf)) + "</p>")
    return "\n".join(parts)


def _workflow_doc(user) -> bytes:
    text = (config.ROOT / "docs" / "workflow.md").read_text(encoding="utf-8")
    return layout("workflow.json", _nav(user), _render_markdown(text))


def _account_list_on() -> bool:
    return bool(config.DEV_LOGIN or config.DEMO_MODE)


def _demo_banner() -> str:
    if not config.DEMO_MODE:
        return ""
    return "<p class='note'>Demo. Submitting a manuscript stores it and does not run the editing workflow.</p>"


_BEHAVIOR = """<script>
(function () {
  var count = 0;
  document.querySelectorAll("label").forEach(function (label) {
    if (label.htmlFor || label.querySelector("input, select, textarea")) return;
    var form = label.closest("form");
    var node = label.nextElementSibling;
    while (node) {
      var tag = node.tagName;
      if (tag === "INPUT" || tag === "SELECT" || tag === "TEXTAREA") break;
      if (form && !form.contains(node)) return;
      node = node.nextElementSibling;
    }
    if (!node) return;
    if (!node.id) {
      count += 1;
      node.id = "field-" + count;
    }
    label.htmlFor = node.id;
  });
  var desktop = window.matchMedia("(pointer: fine) and (min-width: 720px)").matches;
  if (desktop && !document.querySelector("[role=alert], [role=status]")) {
    var target = document.querySelector("[data-autofocus]");
    if (target) target.focus();
  }
  function submitPrimary(form) {
    var primary = form.querySelector("[data-primary]");
    if (primary) form.requestSubmit(primary);
    else form.requestSubmit();
  }
  document.addEventListener("keydown", function (event) {
    if (event.key !== "Enter" || !event.target || !event.target.form) return;
    var form = event.target.form;
    if (event.target.tagName === "TEXTAREA") {
      if (!(event.metaKey || event.ctrlKey)) return;
      event.preventDefault();
      submitPrimary(form);
      return;
    }
    if (event.target.tagName !== "INPUT" && event.target.tagName !== "SELECT") return;
    var submits = form.querySelectorAll("button:not([type]), button[type=submit]");
    if (submits.length < 2) return;
    var fields = Array.prototype.filter.call(
      form.querySelectorAll("input, select, textarea"),
      function (field) { return field.type !== "hidden" && !field.disabled; }
    );
    if (event.target !== fields[fields.length - 1]) {
      event.preventDefault();
      return;
    }
    event.preventDefault();
    submitPrimary(form);
  });
})();
</script>"""


def _guard(prompt: str) -> str:
    text = prompt.replace("\\", "").replace("'", "")
    return f" onsubmit=\"return confirm('{text}')\""


def _current(here: str, href: str) -> str:
    if href == "/":
        on = here == "/"
    elif "/e/" in href:
        on = here == href
    else:
        on = bool(here) and (here == href or here.startswith(href + "/"))
    return " aria-current='page'" if on else ""


def layout(title: str, nav: str, body: str, theme=None) -> bytes:
    resolved = resolve_theme(theme)
    root = css_root(resolved)
    page = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="theme-color" content="{resolved["paper"]}">
<title>{esc(title)}</title>
<style>
  {root}
  :root {{
    --serif: "Iowan Old Style", Palatino, "Palatino Linotype", "Book Antiqua", Georgia, serif;
    --sans: "Source Sans 3", "Source Sans Pro", Inter, "Segoe UI", system-ui, sans-serif;
    --mono: "JetBrains Mono", ui-monospace, "SFMono-Regular", Menlo, Consolas, monospace;
    --muted: color-mix(in srgb, var(--ink) 62%, var(--paper));
  }}
  *, *::before, *::after {{ box-sizing: border-box; }}
  body {{
    margin: 0;
    font: 16.5px/1.55 var(--sans);
    color: var(--ink);
    background: var(--paper);
    touch-action: manipulation;
    -webkit-tap-highlight-color: color-mix(in srgb, var(--accent) 28%, transparent);
  }}
  @media (pointer: fine) {{
    html {{ overscroll-behavior: none; }}
  }}
  .skip {{
    position: absolute;
    left: max(0.75rem, env(safe-area-inset-left));
    top: 0.75rem;
    transform: translateY(-160%);
    background: var(--accent);
    color: var(--on-accent);
    padding: 0.55rem 0.85rem;
    border-radius: 8px;
    z-index: 3;
    text-decoration: none;
  }}
  .skip:focus {{ transform: none; }}
  ::selection {{ background: color-mix(in srgb, var(--accent) 32%, var(--paper)); }}
  header, main {{ max-width: 72rem; margin: 0 auto; }}
  header {{
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 1.5rem;
    padding: max(1.15rem, env(safe-area-inset-top)) max(1.5rem, env(safe-area-inset-right)) 1rem max(1.5rem, env(safe-area-inset-left));
    border-bottom: 1px solid var(--ink);
  }}
  .mark {{
    font-family: var(--serif);
    font-weight: 400;
    font-size: 1.45rem;
    letter-spacing: -0.03em;
    line-height: 1;
    color: var(--ink);
    text-decoration: none;
  }}
  header nav {{
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: flex-end;
    gap: 0.7rem 1.05rem;
    font-size: 0.92rem;
    color: var(--muted);
    min-width: 0;
  }}
  .who {{ overflow-wrap: anywhere; }}
  header form {{ margin: 0; }}
  header nav a {{ color: var(--ink); text-decoration: none; }}
  header nav a:hover {{ color: var(--accent); }}
  main {{ padding: 1.75rem max(1.5rem, env(safe-area-inset-right)) max(4rem, env(safe-area-inset-bottom)) max(1.5rem, env(safe-area-inset-left)); }}
  a {{
    color: var(--accent);
    text-decoration-thickness: 1px;
    text-underline-offset: 0.18em;
  }}
  a:hover {{ color: color-mix(in srgb, var(--accent) 78%, var(--ink)); }}
  h1, h2 {{
    font-family: var(--serif);
    font-weight: 400;
    letter-spacing: -0.02em;
    line-height: 1.15;
  }}
  h1 {{ font-size: clamp(2rem, 4vw, 2.75rem); margin: 0 0 0.85rem; text-wrap: balance; overflow-wrap: anywhere; }}
  h1, h2, [id] {{ scroll-margin-top: 1.25rem; }}
  h2 {{ font-size: 1.55rem; margin: 1.35rem 0 0.45rem; }}
  h3 {{
    font-family: var(--sans);
    font-weight: 600;
    font-size: 0.98rem;
    letter-spacing: 0;
    margin: 1.15rem 0 0.25rem;
  }}
  h1 code, h2 code {{
    font-family: var(--serif);
    font-size: inherit;
    background: none;
    padding: 0;
  }}
  p {{ margin: 0.55rem 0; }}
  main > p, .card > p, .note {{ max-width: 68ch; }}
  nav.desk {{
    display: flex;
    flex-wrap: wrap;
    gap: 0.35rem 1.15rem;
    margin: 0 0 1.6rem;
    padding: 0 0 0.75rem;
    border-bottom: 1px solid var(--line);
    font-size: 0.94rem;
  }}
  nav.desk a {{ color: var(--ink); text-decoration: none; }}
  nav.desk a:hover {{ color: var(--accent); text-decoration: underline; }}
  .spread {{
    display: grid;
    grid-template-columns: 1fr;
    gap: 1rem;
    align-items: start;
  }}
  .card {{
    background: var(--card);
    border: 1px solid var(--line);
    border-radius: 12px;
    padding: 1.15rem 1.25rem 1.2rem;
    margin: 0 0 1rem;
    box-shadow: none;
  }}
  .spread .card {{ margin: 0; }}
  .card h2:first-child, .card h2:first-of-type {{ margin-top: 0; }}
  main > form {{
    background: var(--card);
    border: 1px solid var(--line);
    border-radius: 12px;
    padding: 1.15rem 1.25rem 1.3rem;
    margin: 1rem 0;
    max-width: 42rem;
  }}
  main > form:has(textarea.code), main > form:has(pre) {{ max-width: none; }}
  .card form {{
    background: none;
    border: 0;
    border-radius: 0;
    padding: 0;
    margin: 0.75rem 0 0;
    max-width: 42rem;
  }}
  label {{
    display: block;
    margin: 0.9rem 0 0.3rem;
    font-size: 0.82rem;
    font-weight: 600;
    letter-spacing: 0.01em;
  }}
  label:has(input[type="checkbox"]) {{
    font-size: 1rem;
    font-weight: 400;
    letter-spacing: 0;
  }}
  input, textarea, select {{
    width: 100%;
    font: 1rem/1.4 var(--sans);
    color: var(--ink);
    background: var(--paper);
    border: 1px solid var(--line);
    border-radius: 8px;
    padding: 0.55rem 0.7rem;
  }}
  :focus {{ outline: none; }}
  a:focus-visible, button:focus-visible, input:focus-visible, textarea:focus-visible, select:focus-visible, summary:focus-visible {{
    outline: 2px solid var(--accent);
    outline-offset: 3px;
  }}
  input:focus-visible, textarea:focus-visible, select:focus-visible {{
    border-color: var(--accent);
  }}
  nav.desk a[aria-current="page"], header nav a[aria-current="page"] {{
    color: var(--ink);
    box-shadow: inset 0 -2px 0 var(--accent);
  }}
  .nav-gap {{
    width: 1px;
    align-self: stretch;
    background: var(--line);
    margin: 0 0.15rem;
  }}
  nav.sections {{
    display: flex;
    flex-wrap: wrap;
    gap: 0.4rem 1rem;
    margin: 0 0 1.25rem;
    font-size: 0.92rem;
  }}
  a.action {{
    display: inline-flex;
    align-items: center;
    min-height: 2.75rem;
    padding: 0.45rem 0.9rem;
    border-radius: 8px;
    background: var(--accent);
    color: var(--on-accent);
    text-decoration: none;
    font-weight: 600;
  }}
  a.action:hover {{ color: var(--on-accent); filter: brightness(0.94); }}
  tbody tr {{ position: relative; }}
  td a.row::after {{
    content: "";
    position: absolute;
    inset: 0;
  }}
  textarea {{ min-height: 8rem; resize: vertical; }}
  input[type="color"] {{
    width: 3.25rem;
    height: 2.6rem;
    padding: 0.2rem;
  }}
  input[type="file"] {{
    padding: 0.45rem;
    background: transparent;
    border-style: dashed;
  }}
  input[type="checkbox"] {{ width: auto; margin: 0 0.4rem 0 0; vertical-align: -0.1em; }}
  textarea.code, pre {{
    font: 13.5px/1.55 var(--mono);
    background: #181715;
    color: #faf9f5;
    border: 1px solid #3a342e;
    border-radius: 12px;
    padding: 1rem 1.1rem;
    white-space: pre-wrap;
  }}
  textarea.code {{ min-height: 22rem; width: 100%; }}
  textarea.draft {{ display: none; }}
  pre.manuscript {{
    font: 1.0625rem/1.65 var(--serif);
    background: var(--card);
    color: var(--ink);
    border: 1px solid var(--line);
    max-width: 68ch;
    padding: 1.35rem 1.5rem 1.5rem;
  }}
  body.dark textarea.code, body.dark pre:not(.manuscript) {{
    background: #100e0c;
    border-color: #3a342e;
  }}
  button {{
    font: 600 0.875rem/1 var(--sans);
    background: transparent;
    color: var(--ink);
    border: 1px solid var(--line);
    border-radius: 8px;
    padding: 0.5rem 0.9rem;
    cursor: pointer;
  }}
  button:hover {{ border-color: var(--accent); color: var(--accent); }}
  main p > button, main form > button {{
    background: var(--accent);
    color: var(--on-accent);
    border-color: transparent;
  }}
  main p > button:hover, main form > button:hover {{
    filter: brightness(0.94);
    color: var(--on-accent);
  }}
  main p > button.quiet,
  main form > button.quiet,
  main li form > button,
  main td form > button,
  header button {{
    background: transparent;
    color: var(--ink);
    border: 1px solid var(--line);
    filter: none;
  }}
  main p > button.quiet:hover,
  main form > button.quiet:hover,
  main li form > button:hover,
  main td form > button:hover,
  header button:hover {{
    color: var(--accent);
    border-color: var(--accent);
    filter: none;
  }}
  .note {{
    background: var(--note);
    border-radius: 8px;
    padding: 0.85rem 1rem;
    margin: 0 0 1rem;
    font-family: var(--serif);
  }}
  .error {{ color: var(--error); font-weight: 600; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 0.95rem; }}
  th {{
    font-weight: 600;
    font-size: 0.72rem;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: var(--muted);
    padding: 0.4rem 0.55rem 0.55rem;
    border-bottom: 1px solid var(--ink);
    text-align: left;
    vertical-align: bottom;
  }}
  td {{
    text-align: left;
    border-bottom: 1px solid var(--line);
    padding: 0.65rem 0.55rem;
    vertical-align: top;
    overflow-wrap: break-word;
    font-variant-numeric: tabular-nums;
  }}
  code {{
    font-family: var(--mono);
    font-size: 0.88em;
    background: color-mix(in srgb, var(--ink) 7%, var(--paper));
    padding: 0.05em 0.32em;
    border-radius: 4px;
  }}
  pre code {{ background: none; padding: 0; color: inherit; font-size: inherit; }}
  ul, ol {{ padding-left: 1.2rem; }}
  li {{ margin: 0.28rem 0; }}
  @media (min-width: 860px) {{
    .spread {{ grid-template-columns: 1fr 1fr; }}
    .spread .wide {{ grid-column: 1 / -1; }}
  }}
  @media (max-width: 720px) {{
    header {{ flex-direction: column; align-items: flex-start; padding: max(1rem, env(safe-area-inset-top)) max(1rem, env(safe-area-inset-right)) 1rem max(1rem, env(safe-area-inset-left)); }}
    header nav {{ justify-content: flex-start; }}
    main {{ padding: 1.25rem max(1rem, env(safe-area-inset-right)) max(3rem, env(safe-area-inset-bottom)) max(1rem, env(safe-area-inset-left)); }}
    h1 {{ font-size: 2rem; }}
    .nav-gap {{ display: none; }}
    main > table,
    .card:has(> table),
    .pipeline-results {{
      max-width: 100%;
      overflow-x: auto;
      overflow-y: clip;
    }}
  }}
  @media (pointer: coarse) {{
    button, nav.desk a, header nav a, .action {{
      min-height: 44px;
    }}
    nav.desk a, header nav a {{
      display: inline-flex;
      align-items: center;
    }}
  }}
  .pipeline {{
    display: grid;
    gap: 1rem;
    align-items: start;
  }}
  .pipeline .card {{ margin: 0; }}
  .pipeline-results {{ min-width: 0; }}
  @media (min-width: 860px) {{
    .pipeline {{ grid-template-columns: 18rem 1fr; }}
    .pipeline-filters {{ order: -1; }}
  }}
  @media (prefers-reduced-motion: reduce) {{
    a.action:hover,
    main p > button:hover,
    main form > button:hover {{ filter: none; }}
  }}
  .place {{
    display: grid;
    gap: 1.5rem;
    align-items: start;
  }}
  .pass {{ min-width: 0; }}
  .pass > form {{
    background: var(--card);
    border: 1px solid var(--line);
    border-radius: 12px;
    padding: 1.15rem 1.25rem 1.3rem;
    margin: 1rem 0;
    max-width: 42rem;
  }}
  .shelf {{ min-width: 0; }}
  .practice {{
    font-family: var(--serif);
    color: var(--muted);
    margin: 0 0 0.2rem;
  }}
  .shelf h2 {{ margin-top: 0; }}
  .shelf-list {{
    list-style: none;
    padding: 0;
    margin: 0.2rem 0 0.8rem;
    max-height: 24rem;
    overflow: auto;
  }}
  .shelf-list li {{
    margin: 0;
    padding: 0.55rem 0;
    border-bottom: 1px solid var(--line);
  }}
  .shelf-list a {{ color: var(--ink); text-decoration: none; }}
  .shelf-list a:hover {{ color: var(--accent); }}
  .shelf-list a[aria-current="page"] {{ box-shadow: inset 2px 0 0 var(--accent); padding-left: 0.55rem; }}
  .shelf-list span {{ color: var(--muted); }}
  @media (min-width: 860px) {{
    .place {{ grid-template-columns: 16rem 1fr; }}
    .shelf {{ order: -1; }}
  }}
  .tabs {{
    display: flex;
    flex-wrap: wrap;
    gap: 0.25rem;
    margin: 0 0 1.25rem;
    padding: 0;
    border-bottom: 1px solid var(--line);
  }}
  .tabs a, .tabs button {{
    display: inline-flex;
    align-items: center;
    min-height: 44px;
    padding: 0.4rem 0.8rem;
    margin-bottom: -1px;
    border: 1px solid transparent;
    border-radius: 8px 8px 0 0;
    background: transparent;
    color: var(--ink);
    font: 1rem/1.2 var(--sans);
    font-weight: 400;
    text-decoration: none;
    cursor: pointer;
  }}
  .tabs a:hover, .tabs button:hover {{ color: var(--accent); }}
  .tabs a[aria-selected="true"], .tabs button[aria-selected="true"] {{
    color: var(--ink);
    background: var(--card);
    border-color: var(--line);
    border-bottom-color: var(--card);
  }}
  main form .tabs button, main form .tabs button:hover {{
    background: transparent;
    border-color: transparent;
    filter: none;
    font-weight: 400;
  }}
  main form .tabs button:hover {{ color: var(--accent); }}
  main form .tabs button[aria-selected="true"] {{
    color: var(--ink);
    background: var(--card);
    border-color: var(--line);
    border-bottom-color: var(--card);
  }}
  .panel > form {{
    background: var(--card);
    border: 1px solid var(--line);
    border-radius: 12px;
    padding: 1.15rem 1.25rem 1.3rem;
    margin: 1rem 0;
    max-width: 42rem;
  }}
  .panel > form:has(textarea.code), .panel > form:has(pre) {{ max-width: none; }}
  ol.models {{
    list-style: none;
    padding: 0;
    margin: 0 0 0.5rem;
  }}
  ol.models li {{
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 0.4rem 0.75rem;
    padding: 0.45rem 0;
    border-bottom: 1px solid var(--line);
  }}
  ol.models form {{
    display: flex;
    flex-wrap: wrap;
    gap: 0.35rem;
    margin: 0;
    max-width: none;
  }}
</style>
</head>
<body class="{resolved["scheme"]}">
<a class="skip" href="#content">Skip to content</a>
<header><a class="mark" href="/">SIVED</a><nav aria-label="Account">{nav}</nav></header>
<main id="content">
{_demo_banner()}
{body}
</main>
{_BEHAVIOR}
</body>
</html>"""
    return page.encode()


class App(BaseHTTPRequestHandler):
    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_DELETE(self):
        if urlparse(self.path).path != "/mcp":
            self.send_error(404)
            return
        self.send_response(200)
        _hsts(self)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, fmt, *args):
        return

    def _handle(self, method: str):
        parsed = urlparse(self.path)
        path = parsed.path
        if acme.write_challenge(self):
            return
        if path == "/healthz":
            body = b"ok"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            _hsts(self)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/mcp":
            _serve_mcp(self, method)
            return
        if path == "/skills/desk-persona" and method == "GET":
            _serve_skill(self)
            return
        if path == "/patreon/webhook" and method == "POST":
            _serve_patreon_webhook(self)
            return
        query = parse_qs(parsed.query)
        conn = open_db()
        user = None
        scheme, host = public_origin(self)
        try:
            form = _read_form(self) if method == "POST" else {}
            cookie = self.headers.get("Cookie", "")
            user = _user(conn, cookie)
            status, body, headers = route(
                conn, method, path, query, form, user, host, scheme, cookie
            )
        except MembershipDenied as exc:
            if user is not None:
                conn.execute("DELETE FROM sessions WHERE user_id = ?", (user["id"],))
                conn.commit()
            status, body, headers = (
                400,
                layout("Problem", _nav(None), f"<p class='error' role='alert' tabindex='-1' autofocus>{esc(exc)}</p><p><a href='/login'>Back to sign in</a></p>"),
                [("Set-Cookie", _clear_cookie(secure=scheme == "https"))],
            )
        except ValueError as exc:
            status, body, headers = 400, layout("Problem", _nav(user), f"<p class='error' role='alert' tabindex='-1' autofocus>{esc(exc)}</p><p><a href='/'>Back to the desk</a></p>"), []
        finally:
            conn.close()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        _hsts(self)
        for key, value in headers:
            self.send_header(key, _header_value(value))
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def route(conn, method, path, query, form, user, host="", scheme="http", cookie=""):
    secure = scheme == "https"
    if path == "/docs/workflow" and method == "GET":
        return 200, _workflow_doc(user), []
    if path == "/login" and method == "GET":
        return 200, _login(conn), []
    if path == "/dev/login" and method == "POST":
        return _dev_login(conn, form, secure=secure)
    if path == "/logout" and method == "POST":
        return _logout(conn, cookie, secure=secure)
    if path.startswith("/invite/") and method == "GET":
        return 200, _invite_form(path.split("/")[-1]), []
    if path.startswith("/invite/") and method == "POST":
        return _accept_invite(conn, path.split("/")[-1], form)
    if path.startswith("/e/") and path.endswith("/patreon") and method == "GET" and path.count("/") == 3:
        return _patreon_start(conn, path.split("/")[2], host, scheme)
    if path == "/patreon/callback" and method == "GET":
        return _patreon_callback(conn, query, host, scheme)
    if path.startswith("/e/") and method == "GET" and path.count("/") == 2:
        return 200, _editor_public_page(conn, path.split("/")[2]), []
    if path.startswith("/e/") and path.endswith("/join") and method == "POST":
        if not _account_list_on():
            raise ValueError("sign in with Patreon")
        user_id = become_customer(conn, path.split("/")[2], form.get("name", ""), form.get("email", ""))
        return _start_session(conn, user_id, secure=secure)
    if user is None:
        return 303, b"", [("Location", "/login")]
    _check_csrf(user, form, method)
    if path == "/agent-tokens" and method == "POST":
        return _create_agent_token(conn, user, form)
    if path.startswith("/agent-tokens/") and path.endswith("/revoke") and method == "POST":
        revoke_agent_token(conn, user["id"], path.split("/")[2])
        return 303, b"", [("Location", _next_path(form))]
    if path == "/profile" and method == "GET":
        return 200, _profile_page(conn, user, query), []
    if path == "/profile" and method == "POST":
        update_profile(
            conn,
            user["id"],
            name=form.get("name", ""),
            contact_email=form.get("contact_email", ""),
            phone=form.get("phone", ""),
        )
        return 303, b"", [("Location", "/profile?saved=profile")]
    if path == "/admin" or path.startswith("/admin/") or path.startswith("/owner/"):
        if not user_is_system_admin(user):
            raise ValueError("that is not assigned to this login")
        return _system_admin(conn, user, method, path, form, host, scheme, query)
    if path == "/local-agent" and method == "GET" and user["role"] in ("owner", "admin", "editor"):
        return 200, _local_agent_page(conn, user, host, scheme), []
    if path == "/" and method == "GET":
        return 200, _home(conn, user, host), []
    if path == "/desk" and method == "GET":
        return 303, b"", [("Location", "/")]
    if _has_active_desk(conn, user) and (_desk_path(path) or user["role"] in ("owner", "editor")):
        return _editor(conn, user, method, path, form, host, query, scheme)
    if user["role"] == "author" or author_for_user(conn, user["id"]):
        return _author(conn, user, method, path, form, query)
    return 404, layout("Missing", _nav(user, path), _missing()), []


_DESK_ROOTS = ("/personas", "/manuscripts", "/pipeline", "/costs", "/assistants", "/local-agent")


def _desk_path(path: str) -> bool:
    if path == "/desk" or path.startswith("/editor/") or path.startswith("/jobs/"):
        return True
    return any(path == root or path.startswith(root + "/") for root in _DESK_ROOTS)


def _nav(user, here: str = "") -> str:
    if user is None:
        current = _current(here, "/login")
        return f"<a href='/login'{current}>Sign in</a>"
    links = []
    if user_is_system_admin(user) and not user.get("has_desk"):
        links.append(f"<a href='/admin'{_current(here, '/admin')}>System Admin</a>")
    links.append(f"<a href='/profile'{_current(here, '/profile')}>Profile</a>")
    links.append(f"<span class='who' translate='no'>{esc(user['email'])}</span>")
    links.append(
        "<form style='display:inline' method='post' action='/logout'>"
        f"<input type='hidden' name='csrf' value='{esc(user['csrf'])}'><button>Sign out</button></form>"
    )
    return " ".join(links)


def _login(conn) -> bytes:
    if not _account_list_on():
        return layout(
            "Sign in",
            _nav(None),
            "<p>Editors are invited by the platform owner. Authors open the editor's page and press Continue with Patreon.</p>",
        )
    rows = conn.execute("SELECT email, role FROM users WHERE deleted_at IS NULL ORDER BY role, email").fetchall()
    options = "".join(f"<option value='{esc(r['email'])}'>{esc(r['email'])} ({esc(r['role'])})</option>" for r in rows)
    if config.DEMO_MODE:
        sign_in_note = "Demo sign-in lists every account."
    else:
        sign_in_note = "Local sign-in is on."
    body = f"""
    <h1>Sign in</h1>
    <p class="note">{sign_in_note}</p>
    {_baked_accounts(conn)}
    <form method="post" action="/dev/login">
      <label>Account</label>
      <select name="email" data-autofocus autocomplete="username">{options}</select>
      <p><button>Sign in</button></p>
    </form>
    """
    return layout("Sign in", _nav(None), body)


def _baked_accounts(conn) -> str:
    editor = conn.execute(
        """
        SELECT e.id FROM editors e
        JOIN users u ON u.id = e.user_id
        WHERE u.email = 'owner@example.com' AND u.is_owner = 1
          AND e.status = 'active' AND e.is_org_admin = 1
        """
    ).fetchone()
    author = conn.execute(
        "SELECT id FROM users WHERE email = 'author@example.com' AND role = 'author' AND deleted_at IS NULL"
    ).fetchone()
    if editor is None or author is None:
        return ""
    return (
        "<p>Choose <strong>owner@example.com</strong> for SIVED. "
        "Choose <strong>author@example.com</strong> for the author desk. "
        f"The public author page is <a href='/e/{esc(editor['id'])}'>/e/{esc(editor['id'])}</a>.</p>"
    )


def _start_session(conn, user_id: str, *, secure: bool = False):
    sid = new_id()
    csrf = new_id()
    conn.execute(
        "INSERT INTO sessions (id, user_id, csrf_token, created_at, expires_at) VALUES (?, ?, ?, ?, datetime('now', '+7 days'))",
        (token_hash(sid), user_id, csrf, utcnow()),
    )
    conn.commit()
    return 303, b"", [("Location", "/"), ("Set-Cookie", _session_cookie(sid, secure=secure))]


def _dev_login(conn, form, *, secure: bool = False):
    if not _account_list_on():
        raise ValueError("local sign-in is off")
    user = conn.execute(
        "SELECT * FROM users WHERE email = ? AND deleted_at IS NULL",
        (form.get("email", "").lower(),),
    ).fetchone()
    if user is None:
        raise ValueError("unknown account")
    return _start_session(conn, user["id"], secure=secure)


def _cookie_sid(cookie: str) -> str:
    sid = ""
    for part in (cookie or "").split(";"):
        part = part.strip()
        if part.startswith("sid="):
            sid = part[4:]
    return sid


def _logout(conn, cookie: str, *, secure: bool = False):
    sid = _cookie_sid(cookie)
    if sid:
        conn.execute("DELETE FROM sessions WHERE id = ?", (token_hash(sid),))
        conn.commit()
    return 303, b"", [("Location", "/login"), ("Set-Cookie", _clear_cookie(secure=secure))]


def _user(conn, cookie: str):
    sid = _cookie_sid(cookie)
    if not sid:
        return None
    row = conn.execute(
        """
        SELECT u.*, s.csrf_token AS csrf FROM sessions s
        JOIN users u ON u.id = s.user_id
        WHERE s.id = ? AND s.expires_at > datetime('now') AND u.deleted_at IS NULL
        """,
        (token_hash(sid),),
    ).fetchone()
    if row is None:
        return None
    user = dict(row)
    editor = editor_for_user(conn, user["id"])
    user["has_desk"] = editor is not None and editor["status"] in ("active", "suspended")
    return user


def _check_csrf(user, form, method):
    if method != "POST" or user is None:
        return
    if form.get("csrf") != user["csrf"]:
        raise ValueError("form expired")


def _invite_form(raw: str) -> bytes:
    body = f"""
    <h1>Accept invite</h1>
    <form method="post" action="/invite/{esc(raw)}">
      <label>Your name</label>
      <input name="name" required autocomplete="name" data-autofocus>
      <p><button>Accept invite</button></p>
    </form>
    """
    return layout("Invite", _nav(None), body)


def _editor_public_page(conn, editor_id: str) -> bytes:
    editor = editor_public(conn, editor_id)
    if editor is None:
        return layout("Editor", _nav(None), "<p>This page is not an editor.</p>")
    theme = practice_appearance(conn, editor["organization_id"])
    if editor["status"] != "active" or not editor["is_org_admin"]:
        return layout(editor["org_name"], _nav(None), "<p>This editor is not taking customers.</p>", theme)
    personas = conn.execute(
        "SELECT name FROM personas WHERE editor_id = ? AND status = 'active' ORDER BY created_at",
        (editor_id,),
    ).fetchall()
    offers = "".join(f"<li>{esc(p['name'])}</li>" for p in personas)
    if not offers:
        offers = "<li>Nothing is on offer yet.</li>"
    if _account_list_on():
        join = f"""
        <p>This local desk does not ask Patreon. Leave dev and demo sign-in off before a real author uses it.</p>
        <form method="post" action="/e/{esc(editor_id)}/join">
          <label>Your name</label>
          <input name="name" required autocomplete="name" data-autofocus>
          <label>Email</label>
          <input name="email" type="email" required autocomplete="email" spellcheck="false" inputmode="email">
          <p><button>Become a customer</button></p>
        </form>
        """
    elif patreon_ready(editor):
        join = f"<p><a class='action' href='/e/{esc(editor_id)}/patreon'>Continue with Patreon</a></p>"
    else:
        join = "<p>This editor has not connected Patreon.</p>"
    body = f"""
    <h1>{esc(editor['org_name'])}</h1>
    <h2>On offer</h2>
    <ul>{offers}</ul>
    {join}
    """
    return layout(editor["org_name"], _nav(None), body, theme)


def _patreon_start(conn, editor_id: str, host: str, scheme: str = "http"):
    state = begin_patreon_login(conn, editor_id)
    editor = editor_public(conn, editor_id)
    return 303, b"", [("Location", patreon.authorize_url(state, host, editor["patreon_client_id"], scheme))]


def _patreon_callback(conn, query, host: str, scheme: str = "http"):
    if query.get("error"):
        raise ValueError("Patreon did not authorize this login")
    code = (query.get("code") or [""])[0]
    state = (query.get("state") or [""])[0]
    if not code or not state:
        raise ValueError("Patreon did not authorize this login")
    editor_id = take_patreon_state(conn, state)
    member = exchange_patreon_code(conn, editor_id, code, host, scheme)
    user_id = accept_patreon_member(conn, editor_id, member)
    return _start_session(conn, user_id, secure=scheme == "https")


def _accept_invite(conn, raw: str, form):
    name = form.get("name", "").strip()
    digest = token_hash(raw)
    kind = conn.execute("SELECT id FROM editor_invites WHERE token_hash = ?", (digest,)).fetchone()
    if kind:
        accept_editor_invite(conn, raw, name)
    else:
        accept_author_invite(conn, raw, name)
    return 303, b"", [("Location", "/login")]


def _has_active_desk(conn, user) -> bool:
    editor = editor_for_user(conn, user["id"])
    return editor is not None and editor["status"] == "active"


def _home(conn, user, host="") -> bytes:
    editor = editor_for_user(conn, user["id"])
    if editor is not None and editor["status"] == "active":
        return _editor_dashboard(conn, user)
    if editor is not None and editor["status"] == "suspended":
        raise ValueError("this practice is suspended")
    if editor is not None and editor["status"] == "removed":
        raise ValueError("you are no longer an editor on this practice")
    if author_for_user(conn, user["id"]):
        author_may_enter(conn, user)
        return _author_page(conn, user)
    if user_is_system_admin(user):
        return _system_admin_page(conn, user)
    return layout(
        "SIVED",
        _nav(user, "/"),
        "<p>This login has no desk.</p><p><a href='/profile'>Open your profile</a></p>",
    )


def _system_admin(conn, user, method, path, form, host="", scheme="http", query=None):
    origin = f"{scheme}://{host}" if host else ""
    if path == "/admin" and method == "GET":
        return 200, _system_admin_page(conn, user, query), []
    if path in ("/admin/invites/editors", "/owner/invites/editors") and method == "POST":
        email = form["email"]
        raw = invite_editor(conn, user["id"], email, None)
        return _invite_page(conn, user, "Editor invite", raw, email, origin)
    if path == "/admin/invites/admins" and method == "POST":
        email = form.get("email", "")
        raw = invite_admin(conn, user["id"], email)
        return _invite_page(conn, user, "Admin invite", raw, email, origin)
    if path == "/admin/invitation-mail" and method == "POST":
        set_invitation_mail(conn, user, **_mail_fields(form))
        return 303, b"", [("Location", "/admin?part=mail")]
    if path == "/admin/invitation-mail/remove" and method == "POST":
        clear_invitation_mail(conn, user)
        return 303, b"", [("Location", "/admin?part=mail")]
    if path == "/admin/practice" and method == "POST":
        rename_own_practice(conn, user["id"], form.get("name", ""))
        return 303, b"", [("Location", "/")]
    if path == "/owner/practice" and method == "POST":
        open_owner_practice(conn, user["id"], form.get("name", ""))
        rename_own_practice(conn, user["id"], form.get("name", ""))
        return 303, b"", [("Location", "/")]
    if (
        (path.startswith("/admin/editors/") or path.startswith("/owner/editors/"))
        and path.endswith("/suspend")
        and method == "POST"
    ):
        suspend_practice(conn, user, path.split("/")[3], form.get("reason", ""))
        return 303, b"", [("Location", "/admin")]
    if path.startswith("/admin/admins/") and path.endswith("/remove") and method == "POST":
        remove_admin(conn, user, path.split("/")[3])
        return 303, b"", [("Location", "/admin")]
    return 404, _admin_frame(conn, user, "Missing", _missing(), "/admin"), []


def _invite_page(conn, user, label: str, raw: str, email: str, origin: str):
    result = send_invite(conn, email, raw, origin)
    note = ""
    if result == "sent":
        note = "<p>The invitation was emailed. The link is also here.</p>"
    elif result == "failed":
        note = "<p>The invitation was not emailed. The link is here.</p>"
    body = (
        f"<h1>{esc(label)}</h1>{note}"
        f"<p>{esc(label)}: <a href='/invite/{esc(raw)}'>/invite/{esc(raw)}</a></p>"
        "<p><a href='/admin'>Back to System Admin</a></p>"
    )
    return 200, _admin_frame(conn, user, label, body, "/admin"), []


def _missing() -> str:
    return "<p>Not found.</p><p><a href='/'>Back to the desk</a></p>"


def _admin_frame(conn, user, title: str, body: str, here: str = "") -> bytes:
    editor = editor_for_user(conn, user["id"])
    if editor is not None and editor["status"] == "active":
        return _desk_layout(conn, user, editor, title, body, here)
    return layout(title, _nav(user, here), body)


def _system_admin_page(conn, user, query=None) -> bytes:
    fresh = conn.execute("SELECT * FROM users WHERE id = ?", (user["id"],)).fetchone()
    owner = user_is_owner(fresh)
    standing = "You are the Owner." if owner else "You are an Admin."
    roles = ", ".join(account_roles(conn, fresh))
    mine = editor_for_user(conn, user["id"])
    has_practice = mine is not None and mine["status"] == "active" and mine["is_org_admin"]
    part = _query_text(query or {}, "part")
    tabs = [("editors", "/admin", "Editors")]
    if has_practice:
        tabs.append(("assistants", "/admin?part=assistants", "Assistants"))
    tabs.append(("admins", "/admin?part=admins", "Admins"))
    if owner:
        tabs.append(("mail", "/admin?part=mail", "Invitation mail"))
    if part not in {key for key, _href, _label in tabs}:
        part = "editors"
    if part == "assistants":
        if has_practice:
            panel = f"""
            <h2>Assistants</h2>
            <p>An assistant joins this practice. Choose what they can do. They do not get their own page.</p>
            <form method="post" action="/editor/invites/editors">
              <input type="hidden" name="csrf" value="{esc(user['csrf'])}">
              <label>Assistant email</label>
              <input name="email" type="email" required autocomplete="email" spellcheck="false" inputmode="email">
              {_permission_fields(mine, None)}
              <p><button>Add an assistant</button></p>
            </form>
            """
        else:
            panel = """
            <h2>Assistants</h2>
            <p>This login is not an editor. Assistants are added by the editor of a practice.</p>
            """
    elif part == "admins":
        if owner:
            admins = conn.execute(
                """
                SELECT id, email, name FROM users
                WHERE deleted_at IS NULL AND is_admin = 1
                ORDER BY email
                """
            ).fetchall()
            admin_rows = "".join(
                "<li>"
                f"{esc(row['name'] or row['email'])} ({esc(row['email'])}) "
                f"<form style='display:inline' method='post' action='/admin/admins/{esc(row['id'])}/remove'{_guard('Remove this admin?')}>"
                f"<input type='hidden' name='csrf' value='{esc(user['csrf'])}'>"
                "<button>Remove admin</button></form>"
                "</li>"
                for row in admins
            )
            panel = f"""
            <h2>Admins</h2>
            <p>An admin uses this same page. An admin cannot change the owner, and the owner cannot be removed.</p>
            <form method="post" action="/admin/invites/admins">
              <input type="hidden" name="csrf" value="{esc(user['csrf'])}">
              <label>Admin email</label>
              <input name="email" type="email" required autocomplete="email" spellcheck="false" inputmode="email">
              <p><button>Add an admin</button></p>
            </form>
            <ul>{admin_rows}</ul>
            """
        else:
            panel = """
            <h2>Admins</h2>
            <p>You are an admin. You cannot add an admin, remove an admin, or change the owner.</p>
            """
    elif part == "mail":
        account = invitation_mail_account(conn, fresh["id"])
        via = _query_text(query or {}, "via")
        if via not in ("smtp", "https"):
            via = account["transport"] if account is not None else "smtp"
        panel = _mail_card(
            user,
            account,
            title="Invitation mail",
            action="/admin/invitation-mail",
            remove_action="/admin/invitation-mail/remove",
            button="Save invitation mail",
            remove_button="Remove invitation mail",
            via=via,
            provider_choice=_query_text(query or {}, "provider"),
            page="/admin?part=mail",
        )
    else:
        practice = ""
        if has_practice:
            practice = f"<p>{esc(mine['org_name'])}. <a href=\"/settings\">Settings</a></p>"
        orgs = conn.execute(
            """
            SELECT o.name, u.email, u.is_owner, u.role, e.status AS editor_status, e.id AS editor_id
            FROM organizations o
            JOIN editors e ON e.organization_id = o.id AND e.is_org_admin = 1
            JOIN users u ON u.id = e.user_id
            ORDER BY o.created_at DESC
            """
        ).fetchall()
        rows = []
        for org in orgs:
            if user_is_owner(org):
                action = "Owner"
            else:
                action = (
                    f"<form method='post' action='/admin/editors/{esc(org['editor_id'])}/suspend'{_guard('Suspend this practice?')}>"
                    f"<input type='hidden' name='csrf' value='{esc(user['csrf'])}'>"
                    f"<input name='reason' aria-label='Reason' placeholder='Why this practice stops…' autocomplete='off'>"
                    "<button>Suspend</button></form>"
                )
            rows.append(
                f"<tr><td>{esc(org['name'])}</td><td>{esc(org['email'])}</td>"
                f"<td>{esc(org['editor_status'])}</td><td>{action}</td></tr>"
            )
        panel = f"""
        {practice}
        <h2>Editors</h2>
        <p>Each editor has their own practice and public page. Authors become that editor's customer.</p>
        <form method="post" action="/admin/invites/editors">
          <input type="hidden" name="csrf" value="{esc(user['csrf'])}">
          <label>Editor email</label>
          <input name="email" type="email" required autocomplete="email" spellcheck="false" inputmode="email">
          <p><button>Add an editor</button></p>
        </form>
        <h2>Practices</h2>
        <table><tr><th>Practice</th><th>Editor</th><th>Status</th><th></th></tr>{''.join(rows)}</table>
        """
    body = f"""
    <h1>System Admin</h1>
    <p>{esc(standing)}</p>
    <p>Roles: {esc(roles)}.</p>
    {_tab_links(tabs, part, "System Admin")}
    <div class="panel" role="tabpanel">
      {panel}
    </div>
    """
    return _admin_frame(conn, user, "System Admin", body, "/admin")


def _profile_page(conn, user, query=None) -> bytes:
    fresh = conn.execute("SELECT * FROM users WHERE id = ?", (user["id"],)).fetchone()
    roles = ", ".join(account_roles(conn, fresh)) or "None"
    saved = ((query or {}).get("saved") or [""])[0] == "profile"
    banner = "<p class='note' role='status'>Profile saved.</p>" if saved else ""
    body = f"""
    <h1>Profile</h1>
    {banner}
    <p>Roles: {esc(roles)}.</p>
    <p>Sign-in email: <span translate="no">{esc(fresh['email'])}</span></p>
    <form method="post" action="/profile">
      <input type="hidden" name="csrf" value="{esc(user['csrf'])}">
      <label>Name</label>
      <input name="name" value="{esc(fresh['name'] or '')}" required autocomplete="name" data-autofocus>
      <label>Contact email</label>
      <input name="contact_email" type="email" value="{esc(fresh['contact_email'] or '')}" autocomplete="email" spellcheck="false" inputmode="email">
      <label>Phone</label>
      <input name="phone" type="tel" value="{esc(fresh['phone'] or '')}" autocomplete="tel" inputmode="tel">
      <p><button>Save profile</button></p>
    </form>
    """
    return _admin_frame(conn, user, "Profile", body, "/profile")


def _require_active_editor(conn, user):
    editor = editor_for_user(conn, user["id"])
    if editor is None:
        raise ValueError("this login has no practice")
    if editor["status"] != "active":
        if editor["status"] == "removed":
            raise ValueError("you are no longer an editor on this practice")
        raise ValueError("this practice is suspended")
    return editor


def _assigned_desk(conn, editor, perm: str):
    """The practice editor's row, after checking this login may do the work."""
    if not allowed(editor, perm):
        raise ValueError("that is not assigned to this login")
    return practice_admin(conn, editor)


def _editor(conn, user, method, path, form, host="", query=None, scheme="http"):
    editor = _require_active_editor(conn, user)
    if method == "GET" and path in (
        "/settings",
        "/settings/practice",
        "/settings/appearance",
        "/settings/patreon",
        "/settings/notices",
        "/settings/inference",
        "/settings/defaults",
    ):
        return 200, _settings_page(conn, user, editor, host, scheme, query or {}, path), []
    if path == "/settings/practice" and method == "POST":
        rename_own_practice(conn, user["id"], form.get("name", ""))
        return 303, b"", [("Location", "/settings/practice?saved=practice")]
    if path == "/settings/appearance" and method == "POST":
        set_practice_appearance(
            conn,
            editor,
            profile=form.get("profile", ""),
            mode=form.get("mode", ""),
            accent=form.get("accent", ""),
            paper=form.get("paper", ""),
            ink=form.get("ink", ""),
        )
        return 303, b"", [("Location", "/settings/appearance?saved=appearance")]
    if path == "/editor/patreon" and method == "POST":
        set_patreon_integration(
            conn,
            editor,
            campaign_id=form.get("campaign_id", ""),
            client_id=form.get("client_id", ""),
            client_secret=form.get("client_secret", ""),
            webhook_secret=form.get("webhook_secret", ""),
        )
        return 303, b"", [("Location", "/settings/patreon?saved=patreon")]
    if path == "/settings/author-notices" and method == "POST":
        set_author_notices(
            conn,
            editor,
            notice_origin=f"{scheme}://{host}" if host else "",
            **_mail_fields(form),
        )
        return 303, b"", [("Location", "/settings/notices?saved=notices")]
    if path == "/settings/author-notices/remove" and method == "POST":
        clear_author_notices(conn, editor)
        return 303, b"", [("Location", "/settings/notices?saved=notices-removed")]
    if path == "/settings/inference" and method == "POST":
        set_inference(
            conn,
            editor,
            label=form.get("label", ""),
            provider=form.get("provider", ""),
            base_url=form.get("base_url", ""),
            secret=form.get("inference_secret", ""),
        )
        return 303, b"", [("Location", "/settings/inference?saved=inference")]
    if path == "/settings/inference/remove" and method == "POST":
        clear_inference(conn, editor, form.get("label", ""))
        return 303, b"", [("Location", "/settings/inference?saved=inference-removed")]
    if path == "/settings/inference/refresh" and method == "POST":
        try:
            refresh_inference_catalog(conn, editor, form.get("label", ""))
        except ValueError as exc:
            failed = "/settings/defaults" if form.get("back") == "defaults" else "/settings/inference"
            return 400, _settings_page(
                conn, user, editor, host, scheme, _defaults_query(form), failed, error=str(exc)
            ), []
        if form.get("back") == "defaults":
            return 303, b"", [("Location", _defaults_location(form, "catalog"))]
        return 303, b"", [("Location", "/settings/inference?saved=catalog")]
    if path == "/settings/inference/defaults" and method == "POST":
        try:
            _apply_default_change(conn, editor, form)
        except ValueError as exc:
            return 400, _settings_page(
                conn, user, editor, host, scheme, _defaults_query(form), "/settings/defaults", error=str(exc)
            ), []
        return 303, b"", [("Location", _defaults_location(form))]
    if path == "/personas/new/wizard" and method == "POST":
        return _post_new_wizard(conn, user, editor, form)
    if path in ("/personas", "/personas/new") and method == "POST":
        return _post_new_persona(conn, user, editor, form)
    if (
        path.startswith("/personas/")
        and path.endswith("/workflow")
        and method == "POST"
        and path.count("/") == 3
    ):
        return _post_persona_workflow(conn, user, editor, path.split("/")[2], form)
    if path.startswith("/personas/") and path.endswith("/package") and method == "POST":
        desk = _assigned_desk(conn, editor, "offers")
        persona_id = path.split("/")[2]
        uploaded = form.get("package")
        if not (
            isinstance(uploaded, tuple)
            and len(uploaded) == 2
            and isinstance(uploaded[1], (bytes, bytearray))
        ):
            raise ValueError("upload a package zip")
        install_persona_zip(conn, desk, persona_id, bytes(uploaded[1]))
        return 303, b"", [("Location", f"/personas/{persona_id}?part=package")]
    if path.startswith("/personas/") and method == "POST" and path.count("/") == 2:
        desk = _assigned_desk(conn, editor, "offers")
        persona_id = path.split("/")[2]
        update_persona_profile(
            conn,
            desk,
            persona_id,
            name=form.get("name", ""),
            max_words=form.get("max_words") or 180000,
        )
        return 303, b"", [("Location", f"/personas/{persona_id}?part=details")]
    if path == "/editor/invites/editors" and method == "POST":
        raw = invite_editor(
            conn,
            user["id"],
            form["email"],
            editor["organization_id"],
            permission_flags(form),
        )
        body = (
            "<h1>Assistant invite</h1>"
            f"<p>Assistant invite: <a href='/invite/{esc(raw)}'>/invite/{esc(raw)}</a></p>"
            "<p><a href='/assistants'>Back to assistants</a></p>"
        )
        return 200, _desk_layout(conn, user, editor, "Assistant invite", body, "/assistants"), []
    if path.startswith("/editor/invites/") and path.endswith("/revoke") and method == "POST":
        revoke_editor_invite(conn, editor, path.split("/")[3])
        return 303, b"", [("Location", "/assistants")]
    if path.startswith("/editor/assistants/") and method == "POST":
        set_assistant_permissions(conn, editor, path.split("/")[3], permission_flags(form))
        return 303, b"", [("Location", "/assistants")]
    if path.startswith("/editor/editors/") and path.endswith("/remove") and method == "POST":
        remove_editor(conn, editor, path.split("/")[3])
        return 303, b"", [("Location", "/assistants")]
    if path.startswith("/editor/personas/") and path.endswith("/publish") and method == "POST":
        desk = _assigned_desk(conn, editor, "offers")
        persona_id = path.split("/")[3]
        publish_persona(conn, desk, persona_id)
        return 303, b"", [("Location", f"/personas/{persona_id}")]
    if path.startswith("/jobs/") and path.endswith("/thread") and method == "POST":
        job_id = path.split("/")[2]
        post_message(
            conn,
            job_id=job_id,
            sender_role="editor",
            sender_id=editor["id"],
            body=form.get("body", ""),
            actor_editor=editor,
        )
        return 303, b"", [("Location", f"/jobs/{job_id}?sent=1")]
    if path.startswith("/jobs/") and method == "GET":
        return 200, _job_page(conn, user, editor, path.split("/")[2], query=query), []
    if method == "GET":
        page = _editor_get(conn, user, editor, path, host, query)
        if page is not None:
            return 200, page, []
    return 404, _desk_layout(conn, user, editor, "Missing", _missing(), ""), []


def _editor_get(conn, user, editor, path, host, query):
    if path == "/personas":
        return _personas_page(conn, user, editor)
    if path == "/personas/new":
        how = _query_text(query or {}, "how")
        return _persona_new_page(conn, user, editor, how=how)
    if path == "/personas/new/wizard":
        return _new_wizard_page(conn, user, editor, part=_query_text(query or {}, "part"))
    if path.startswith("/personas/") and path.count("/") == 2:
        return _persona_page(
            conn, user, editor, path.split("/")[2], part=_query_text(query or {}, "part")
        )
    if path == "/manuscripts":
        return _manuscripts_page(conn, user, editor)
    if path == "/pipeline":
        return _pipeline_page(conn, user, editor, query or {})
    if path == "/costs":
        return _costs_page(conn, user, editor)
    if path == "/assistants":
        return _assistants_page(conn, user, editor)
    return None


def _permission_fields(actor, selected: dict | None) -> str:
    fields = []
    defaults = {"manuscripts", "messages", "pipeline"}
    for key, label in PERMISSIONS:
        if not allowed(actor, key):
            continue
        if selected is None:
            on = key in defaults
        else:
            on = bool(selected.get(key))
        mark = " checked" if on else ""
        fields.append(
            f"<label><input type='checkbox' name='perm_{key}' value='1'{mark}> {esc(label)}</label>"
        )
    return "".join(fields)


def _section_nav(user, editor, desk, here: str = "") -> str:
    work = [f"<a href='/'{_current(here, '/')}>Desk</a>"]
    office = []
    if user_is_system_admin(user):
        office.append(f"<a href='/admin'{_current(here, '/admin')}>System Admin</a>")
    if allowed(editor, "offers"):
        work.append(f"<a href='/personas'{_current(here, '/personas')}>Personas</a>")
    if allowed(editor, "manuscripts"):
        work.append(f"<a href='/manuscripts'{_current(here, '/manuscripts')}>Manuscripts</a>")
    if allowed(editor, "pipeline"):
        work.append(f"<a href='/pipeline'{_current(here, '/pipeline')}>Pipeline</a>")
    if allowed(editor, "payouts"):
        office.append(f"<a href='/costs'{_current(here, '/costs')}>Costs</a>")
    if allowed(editor, "assistants"):
        office.append(f"<a href='/assistants'{_current(here, '/assistants')}>Assistants</a>")
    if editor["is_org_admin"]:
        office.append(f"<a href='/settings'{_current(here, '/settings')}>Settings</a>")
    office.append(f'<a href="/local-agent"{_current(here, "/local-agent")}>Local agent</a>')
    page = f"/e/{desk['id']}"
    office.append(f'<a href="{esc(page)}"{_current(here, page)}>Your page</a>')
    gap = "<span class='nav-gap' aria-hidden='true'></span>" if office else ""
    return "<nav class='desk' aria-label='Desk'>" + " ".join(work) + gap + " ".join(office) + "</nav>"


def _desk_layout(conn, user, editor, title: str, body: str, here: str = "") -> bytes:
    desk = practice_admin(conn, editor)
    theme = practice_appearance(conn, editor["organization_id"])
    return layout(title, _nav(user, here), _section_nav(user, editor, desk, here) + body, theme)


def _patreon_card(user, editor, host: str, scheme: str = "http") -> str:
    if not editor["is_org_admin"]:
        return ""
    keys = editor.keys()
    campaign = editor["patreon_campaign_id"] if "patreon_campaign_id" in keys else ""
    client_id = editor["patreon_client_id"] if "patreon_client_id" in keys else ""
    has_secret = bool(editor["patreon_client_secret"]) if "patreon_client_secret" in keys else False
    has_hook = bool(editor["patreon_webhook_secret"]) if "patreon_webhook_secret" in keys else False
    status = "Connected" if campaign and client_id and has_secret else "Not connected"
    secret_line = "saved" if has_secret else "not saved"
    hook_line = "saved" if has_hook else "not saved"
    return f"""
    <div class="card" id="patreon">
      <p>Status: {esc(status)}</p>
      <p>Campaign: {esc(campaign or "Not set")}</p>
      <p>Redirect URI</p>
      <p><code>{esc(patreon.redirect_uri(host, scheme))}</code></p>
      <p>Webhook URL</p>
      <p><code>{esc(patreon.webhook_url(host, scheme))}</code></p>
      <p><a href="https://www.patreon.com/portal/registration/register-clients">Clients &amp; API Keys</a>
      <a href="https://www.patreon.com/portal/registration/register-webhooks">My Webhooks</a></p>
      <p>Client secret: {esc(secret_line)}. Webhook secret: {esc(hook_line)}.</p>
      <form method="post" action="/editor/patreon">
        <input type="hidden" name="csrf" value="{esc(user['csrf'])}">
        <label>Patreon campaign id</label>
        <input name="campaign_id" value="{esc(campaign)}" required autocomplete="off" spellcheck="false" autocapitalize="off">
        <label>Client id</label>
        <input name="client_id" value="{esc(client_id)}" required autocomplete="off" spellcheck="false" autocapitalize="off">
        <label>Client secret</label>
        <input name="client_secret" type="password" autocomplete="off" spellcheck="false">
        <label>Webhook secret</label>
        <input name="webhook_secret" type="password" autocomplete="off" spellcheck="false">
        <p><button>Save Patreon</button></p>
      </form>
    </div>
    """


_SAVED = {
    "practice": "Practice name saved.",
    "appearance": "Appearance saved.",
    "patreon": "Patreon saved.",
    "notices": "Author notices saved.",
    "notices-removed": "Author notices removed.",
    "inference": "Provider saved.",
    "inference-removed": "Provider removed.",
    "defaults": "Default models saved.",
    "catalog": "Model list updated.",
}


_SETTINGS_NOTES = {
    "/settings/practice": {"practice"},
    "/settings/appearance": {"appearance"},
    "/settings/patreon": {"patreon"},
    "/settings/notices": {"notices", "notices-removed"},
    "/settings/inference": {"inference", "inference-removed", "catalog"},
    "/settings/defaults": {"defaults", "catalog"},
}


def _settings_note(query, section: str) -> str:
    saved_key = ((query or {}).get("saved") or [""])[0]
    allowed = _SETTINGS_NOTES.get(section, set(_SAVED))
    saved = _SAVED.get(saved_key, "") if saved_key in allowed else ""
    if not saved:
        return ""
    return f"<p class='note' role='status'>{esc(saved)}</p>"


_SETTINGS_TABS = (
    ("/settings/practice", "Practice"),
    ("/settings/appearance", "Appearance"),
    ("/settings/patreon", "Patreon"),
    ("/settings/notices", "Author notices"),
    ("/settings/inference", "Inference"),
    ("/settings/defaults", "Default models"),
)


def _settings_tabs(section: str) -> str:
    current = "/settings/practice" if section == "/settings" else section
    items = [(href, href, label) for href, label in _SETTINGS_TABS]
    return _tab_links(items, current, "Settings")


def _tab_links(items, current: str, label: str) -> str:
    links = []
    for key, href, text in items:
        selected = "true" if key == current else "false"
        links.append(
            f'<a href="{esc(href)}" role="tab" aria-selected="{selected}">{esc(text)}</a>'
        )
    return f'<div class="tabs" role="tablist" aria-label="{esc(label)}">' + "".join(links) + "</div>"


def _href(path: str, **params) -> str:
    kept = {key: value for key, value in params.items() if value}
    if not kept:
        return path
    joiner = "&" if "?" in path else "?"
    return path + joiner + urlencode(kept)


def _settings_frame(conn, user, editor, title: str, body: str, query, section: str) -> bytes:
    noted = "/settings/practice" if section == "/settings" else section
    page = f"""
    <h1>Settings</h1>
    {_settings_tabs(section)}
    {_settings_note(query, noted)}
    <div class="panel" role="tabpanel">
      <h2>{esc(title)}</h2>
      {body}
    </div>
    """
    return _desk_layout(conn, user, editor, "Settings", page, "/settings")


def _practice_form(user, editor) -> str:
    return f"""
    <form method="post" action="/settings/practice">
      <input type="hidden" name="csrf" value="{esc(user['csrf'])}">
      <label>Name</label>
      <input name="name" value="{esc(editor['org_name'])}" autocomplete="organization" required>
      <p><button>Save practice name</button></p>
    </form>
    """


def _appearance_form(user, editor, appearance) -> str:
    colors = resolve_theme(appearance)
    accent = _full_color(appearance["accent"] or colors["accent"])
    paper = _full_color(appearance["paper"] or colors["paper"])
    ink = _full_color(appearance["ink"] or colors["ink"])
    profiles = "".join(
        f'<option value="{name}"{_selected(name, appearance["profile"])}>{label}</option>'
        for name, label in (
            ("desk", "Desk"),
            ("ink", "Ink"),
            ("forest", "Forest"),
            ("sea", "Sea"),
            ("custom", "Custom"),
        )
    )
    modes = "".join(
        f'<option value="{name}"{_selected(name, appearance["mode"])}>{label}</option>'
        for name, label in (("light", "Light"), ("dark", "Dark"))
    )
    return f"""
    <form method="post" action="/settings/appearance">
      <input type="hidden" name="csrf" value="{esc(user['csrf'])}">
      <label>Profile</label>
      <select name="profile">{profiles}</select>
      <label>Mode</label>
      <select name="mode">{modes}</select>
      <label>Custom accent</label>
      <input name="accent" type="color" value="{esc(accent)}">
      <label>Custom background</label>
      <input name="paper" type="color" value="{esc(paper)}">
      <label>Custom text</label>
      <input name="ink" type="color" value="{esc(ink)}">
      <p>Custom colors are used when the profile is Custom. The public page, the author's pages, and this desk use the saved colors.</p>
      <p><button>Save appearance</button></p>
    </form>
    """


_CATALOG_PAGE = 30


def _keep_catalog(catalog: str, q: str) -> str:
    parts = []
    if catalog:
        parts.append(f'<input type="hidden" name="catalog" value="{esc(catalog)}">')
    if q:
        parts.append(f'<input type="hidden" name="q" value="{esc(q)}">')
    return "".join(parts)


def _defaults_query(form) -> dict:
    catalog = (form.get("catalog") or form.get("label") or "").strip()
    q = (form.get("q") or "").strip()
    query = {}
    if catalog:
        query["catalog"] = [catalog]
    if q:
        query["q"] = [q]
    return query


def _defaults_location(form, saved: str = "defaults") -> str:
    return _href(
        "/settings/defaults",
        saved=saved,
        catalog=(form.get("catalog") or "").strip(),
        q=(form.get("q") or "").strip(),
    )


def _defaults_form(user, defaults: list[str], providers: list[dict], catalog: str = "", q: str = "", error: str = "") -> str:
    error_line = f"<p class='error' role='alert' tabindex='-1' autofocus>{esc(error)}</p>" if error else ""
    kept = _keep_catalog(catalog, q)
    last = len(defaults) - 1
    rows = []
    for index, name in enumerate(defaults):
        moves = []
        if index > 0:
            moves.append('<button name="move" value="up">Move up</button>')
        if index < last:
            moves.append('<button name="move" value="down">Move down</button>')
        move_form = ""
        if moves:
            move_form = f"""
            <form method="post" action="/settings/inference/defaults">
              <input type="hidden" name="csrf" value="{esc(user['csrf'])}">
              <input type="hidden" name="model" value="{esc(name)}">
              {kept}
              {''.join(moves)}
            </form>
            """
        rows.append(
            "<li>"
            f"<code>{esc(name)}</code>"
            f"{move_form}"
            f'<form method="post" action="/settings/inference/defaults"{_guard("Remove this default model?")}>'
            f'<input type="hidden" name="csrf" value="{esc(user["csrf"])}">'
            f'<input type="hidden" name="model" value="{esc(name)}">'
            f"{kept}"
            '<button name="remove" value="1">Remove</button>'
            "</form></li>"
        )
    listed = f'<ol class="models">{"".join(rows)}</ol>' if rows else "<p>No default models.</p>"
    return f"""
    {error_line}
    <p>A workflow's named model is used when any provider lists it. These are used otherwise. Move up and move down set the order. A default with no published price stays in this order.</p>
    {listed}
    <h3>Models your providers list</h3>
    {_provider_catalog(user, defaults, providers, catalog, q)}
    <h3>Add an id the lists do not show</h3>
    <form method="post" action="/settings/inference/defaults">
      <input type="hidden" name="csrf" value="{esc(user['csrf'])}">
      {kept}
      <label>Model id</label>
      <input name="model" autocomplete="off" spellcheck="false" autocapitalize="off">
      <p>Use this when you already know an id the last check did not include. It is used only when a provider lists it.</p>
      <p><button name="add" value="1">Add model</button></p>
    </form>
    """


def _provider_label(name: str) -> str:
    return dict(_INFERENCE_PROVIDERS).get(name, name)


def _catalog_order(names: list[str], prices: dict) -> list[str]:
    def key(name: str):
        price = prices.get(name) if isinstance(prices, dict) else None
        if isinstance(price, dict) and "prompt" in price and "completion" in price:
            return (0, int(price["prompt"]), int(price["completion"]), name)
        return (1, 0, 0, name)

    return sorted(names, key=key)


def _price_suffix(name: str, prices: dict) -> str:
    price = prices.get(name) if isinstance(prices, dict) else None
    if isinstance(price, dict) and "prompt" in price and "completion" in price:
        return f" ({esc(llm.price_label(price['prompt'], price['completion']))})"
    return ""


def _provider_catalog(user, defaults: list[str], providers: list[dict], catalog: str, q: str) -> str:
    if not providers:
        return (
            "<p>No provider is saved, so there is no list of models to choose from. "
            '<a href="/settings/inference">Connect a provider</a>. '
            "The desk asks that provider which models it offers, and those models show up here.</p>"
        )
    labels = [item["label"] for item in providers]
    current = catalog if catalog in labels else labels[0]
    tabs = _tab_links(
        [(item["label"], _href("/settings/defaults", catalog=item["label"]), item["label"]) for item in providers],
        current,
        "Provider",
    )
    item = next(entry for entry in providers if entry["label"] == current)
    prices = item.get("prices") or {}
    ordered = _catalog_order(list(item["models"]), prices)
    needle = q.strip().lower()
    matched = [name for name in ordered if needle in name.lower()] if needle else ordered
    total = len(ordered)
    shown = matched[:_CATALOG_PAGE]
    provider = _provider_label(item["provider"])
    count = f"{esc(provider)} lists {total} model" if total == 1 else f"{esc(provider)} lists {total} models"
    checked = f" Last checked {esc(item['checked_at'])}." if item.get("checked_at") else ""
    if needle and not matched:
        window = "<p>No model on this list matches that.</p>"
    elif len(matched) > _CATALOG_PAGE:
        if needle:
            window = (
                f"<p>Showing {_CATALOG_PAGE} of {len(matched)} that match. "
                "Type more of the name.</p>"
            )
        else:
            window = (
                f"<p>Showing {_CATALOG_PAGE} of {total}. "
                "Type part of a name to see the rest.</p>"
            )
    else:
        window = ""
    rows = []
    kept = _keep_catalog(current, q)
    chosen = set(defaults)
    for name in shown:
        if name in chosen:
            action = "<span>In the default list</span>"
        else:
            action = (
                '<form method="post" action="/settings/inference/defaults">'
                f'<input type="hidden" name="csrf" value="{esc(user["csrf"])}">'
                f'<input type="hidden" name="model" value="{esc(name)}">'
                f"{kept}"
                '<button name="add" value="1">Add</button>'
                "</form>"
            )
        rows.append(f"<li><code>{esc(name)}</code>{_price_suffix(name, prices)}{action}</li>")
    listing = f'<ol class="models">{"".join(rows)}</ol>' if rows else ""
    return f"""
    <p>Open a provider and add a model from the list it returned. A price is dollars per million tokens, input then output, and only when that provider published both. Priced models are first, cheapest input price first. A model with no published price follows, by name. Adding an id uses that id. A run picks the cheapest saved provider that lists it.</p>
    {tabs}
    <div class="catalog">
      <p>{count} at {esc(item["base_url"])}.{checked}</p>
      <form method="post" action="/settings/inference/refresh">
        <input type="hidden" name="csrf" value="{esc(user["csrf"])}">
        <input type="hidden" name="label" value="{esc(current)}">
        <input type="hidden" name="back" value="defaults">
        {kept}
        <p>Check again asks this provider for its current model list. A failed check keeps the list already saved.</p>
        <p><button class="quiet">Check again</button></p>
      </form>
      <form method="get" action="/settings/defaults">
        <input type="hidden" name="catalog" value="{esc(current)}">
        <label>Find a model</label>
        <input name="q" value="{esc(q)}" autocomplete="off" spellcheck="false" autocapitalize="off">
        <p><button>Find</button></p>
      </form>
      {window}
      {listing}
    </div>
    """


def _settings_page(conn, user, editor, host: str, scheme: str = "http", query=None, section: str = "/settings", error: str = "") -> bytes:
    if not editor["is_org_admin"]:
        raise ValueError("only the editor can change settings")
    if section == "/settings":
        return _settings_frame(conn, user, editor, "Practice", _practice_form(user, editor), query, section)
    if section == "/settings/practice":
        return _settings_frame(conn, user, editor, "Practice", _practice_form(user, editor), query, section)
    if section == "/settings/appearance":
        appearance = practice_appearance(conn, editor["organization_id"])
        return _settings_frame(
            conn, user, editor, "Appearance", _appearance_form(user, editor, appearance), query, section
        )
    if section == "/settings/patreon":
        return _settings_frame(
            conn, user, editor, "Patreon", _patreon_card(user, editor, host, scheme), query, section
        )
    if section == "/settings/notices":
        account = author_notices_account(conn, editor["organization_id"])
        via = _query_text(query or {}, "via")
        if via not in ("smtp", "https"):
            via = account["transport"] if account is not None else "smtp"
        body = _mail_card(
            user,
            account,
            title="",
            action="/settings/author-notices",
            remove_action="/settings/author-notices/remove",
            button="Save author notices",
            remove_button="Remove author notices",
            anchor="notices",
            via=via,
            provider_choice=_query_text(query or {}, "provider"),
            page="/settings/notices",
        )
        return _settings_frame(conn, user, editor, "Author notices", body, query, section)
    if section == "/settings/inference":
        chosen = _query_text(query or {}, "provider")
        body = _inference_card(
            user,
            inference_public(inference_rows(conn, editor["organization_id"])),
            chosen,
            error,
        )
        return _settings_frame(conn, user, editor, "Inference", body, query, section)
    if section == "/settings/defaults":
        defaults = inference_defaults(conn, editor["organization_id"])
        providers = inference_public(inference_rows(conn, editor["organization_id"]))
        body = _defaults_form(
            user,
            defaults,
            providers,
            catalog=_query_text(query or {}, "catalog"),
            q=_query_text(query or {}, "q"),
            error=error,
        )
        return _settings_frame(conn, user, editor, "Default models", body, query, section)
    raise ValueError("not found")


_INFERENCE_PROVIDERS = (
    ("xai", "xAI"),
    ("openai", "OpenAI"),
    ("anthropic", "Anthropic"),
    ("gemini", "Gemini"),
    ("groq", "Groq"),
    ("openrouter", "OpenRouter"),
    ("ollama", "Ollama"),
    ("compatible", "Other OpenAI-compatible server"),
)


def _inference_card(user, providers: list[dict], chosen: str = "", error: str = "") -> str:
    """Saved providers and the models they listed. The key is not written back."""
    names = {key for key, _label in _INFERENCE_PROVIDERS}
    if chosen not in names:
        chosen = "xai"
    error_line = f"<p class='error' role='alert' tabindex='-1' autofocus>{esc(error)}</p>" if error else ""
    listed = []
    for item in providers:
        models = _format_models(item["models"], item.get("prices") or {})
        browse = _href("/settings/defaults", catalog=item["label"])
        listed.append(
            f"""
        <h3>{esc(item["label"])}</h3>
        <p>{esc(item["provider"])}. {esc(item["base_url"])}</p>
        <p>API key: {esc(item["secret"])}.</p>
        <p>Models: {models}</p>
        <p><a href="{esc(browse)}">Browse all {len(item["models"])}</a></p>
        <form method="post" action="/settings/inference/refresh">
          <input type="hidden" name="csrf" value="{esc(user["csrf"])}">
          <input type="hidden" name="label" value="{esc(item["label"])}">
          <p><button class="quiet">Check again</button></p>
        </form>
        <form method="post" action="/settings/inference/remove"{_guard("Remove this provider?")}>
          <input type="hidden" name="csrf" value="{esc(user["csrf"])}">
          <input type="hidden" name="label" value="{esc(item["label"])}">
          <p><button class="quiet">Remove {esc(item["label"])}</button></p>
        </form>
        """
        )
    tabs = _tab_links(
        [(key, _href("/settings/inference", provider=key), label) for key, label in _INFERENCE_PROVIDERS],
        chosen,
        "Provider",
    )
    base = ""
    if chosen in ("ollama", "compatible"):
        base = """
        <label>Base URL</label>
        <input name="base_url" type="url" inputmode="url" autocomplete="off" spellcheck="false">
        <p>Leave this blank for Ollama on this machine. Another OpenAI-compatible server needs its URL.</p>
        """
    return f"""
    <div class="card" id="inference">
      {error_line}
      <p>A workflow names the model. A run uses a provider here that lists it, cheapest published price first. Save the same label again to replace one. Leave the key blank to keep the saved one. This page shows the first 40 names from each provider. <a href="/settings/defaults">Default models</a> is the full list, and that is where a default is chosen. Until a provider is saved, the desk uses the process setup.</p>
      {"".join(listed) or "<p>No provider is saved.</p>"}
      <h3>Add a provider</h3>
      {tabs}
      <form method="post" action="/settings/inference">
        <input type="hidden" name="csrf" value="{esc(user["csrf"])}">
        <input type="hidden" name="provider" value="{esc(chosen)}">
        <label>Label</label>
        <input name="label" required autocomplete="off" spellcheck="false">
        {base}
        <label>API key</label>
        <input name="inference_secret" type="password" autocomplete="off" spellcheck="false">
        <p><button>Save provider</button></p>
      </form>
    </div>
    """


def _apply_default_change(conn, editor, form) -> None:
    current = inference_defaults(conn, editor["organization_id"])
    if form.get("remove"):
        name = (form.get("model") or "").strip()
        if name not in current:
            raise ValueError("that default model is not in the list")
        updated = [item for item in current if item != name]
    elif form.get("move"):
        name = (form.get("model") or "").strip()
        direction = form.get("move")
        if name not in current or direction not in ("up", "down"):
            raise ValueError("that default model is not in the list")
        index = current.index(name)
        updated = list(current)
        if direction == "up" and index > 0:
            updated[index - 1], updated[index] = updated[index], updated[index - 1]
        elif direction == "down" and index < len(updated) - 1:
            updated[index + 1], updated[index] = updated[index], updated[index + 1]
    elif form.get("add"):
        typed = (form.get("model") or "").strip()
        name = typed or (form.get("listed") or "").strip()
        if not name:
            raise ValueError("name a model")
        updated = [*current, name]
    else:
        raise ValueError("name a model")
    set_inference_defaults(conn, editor, updated)


def _known_model_choices(conn, editor, skip: list[str]) -> list[tuple[str, str]]:
    seen = set(skip)
    choices = []
    for item in inference_public(inference_rows(conn, editor["organization_id"])):
        prices = item.get("prices") or {}
        for name in item["models"]:
            if name in seen:
                continue
            seen.add(name)
            label = name
            price = prices.get(name) if isinstance(prices, dict) else None
            if isinstance(price, dict) and "prompt" in price and "completion" in price:
                label = f"{name} ({llm.price_label(price['prompt'], price['completion'])})"
            choices.append((name, label))
    return choices


def _format_models(models: list[str], prices: dict) -> str:
    shown = models[:40]
    parts = []
    for name in shown:
        piece = f"<code>{esc(name)}</code>"
        price = prices.get(name) if isinstance(prices, dict) else None
        if isinstance(price, dict) and "prompt" in price and "completion" in price:
            piece += f" ({esc(llm.price_label(price['prompt'], price['completion']))})"
        parts.append(piece)
    names = ", ".join(parts) or "none"
    if len(models) > 40:
        names += f", and {len(models) - 40} more"
    return names


def _model_catalog(conn, editor) -> str:
    items = inference_public(inference_rows(conn, editor["organization_id"]))
    defaults = inference_defaults(conn, editor["organization_id"])
    if not items:
        return (
            "<p>No inference provider is connected. Settings is where a provider is saved. "
            "The file names the model, and the run picks a provider that lists it.</p>"
        )
    lines = []
    for item in items:
        names = _format_models(item["models"], item.get("prices") or {})
        lines.append(f"<li>{esc(item['label'])} ({esc(item['provider'])}): {names}</li>")
    tail = ""
    if defaults:
        names = ", ".join(f"<code>{esc(name)}</code>" for name in defaults)
        tail = (
            "<p>Default models, used when a file names none or no provider lists the named one: "
            f"{names}.</p>"
        )
    full = ""
    if editor["is_org_admin"]:
        full = '<p><a href="/settings/defaults">Every model these providers list is on Default models.</a></p>'
    return "<h2>Models this practice can run</h2><ul>" + "".join(lines) + "</ul>" + tail + full


def _mail_fields(form: dict) -> dict:
    return {
        "transport": form.get("transport", ""),
        "from_address": form.get("from_address", ""),
        "smtp_host": form.get("smtp_host", ""),
        "smtp_port": form.get("smtp_port", ""),
        "smtp_security": form.get("smtp_security", ""),
        "smtp_username": form.get("smtp_username", ""),
        "secret": form.get("mail_secret", ""),
        "https_provider": form.get("https_provider", ""),
        "mailgun_domain": form.get("mailgun_domain", ""),
        "mailgun_region": form.get("mailgun_region", ""),
    }


_MAIL_PROVIDERS = (
    ("resend", "Resend"),
    ("postmark", "Postmark"),
    ("sendgrid", "SendGrid"),
    ("mailgun", "Mailgun"),
)


def _mail_card(
    user,
    account,
    *,
    title,
    action,
    remove_action,
    button,
    remove_button,
    anchor="",
    via="",
    provider_choice="",
    page="",
) -> str:
    """One checked mailbox. The secret is not written back into the page."""
    transport = account["transport"] if account is not None else "smtp"
    provider = account["https_provider"] if account is not None else "resend"
    security = account["smtp_security"] if account is not None and account["smtp_security"] else "starttls"
    region = account["mailgun_region"] if account is not None and account["mailgun_region"] else "us"
    from_address = account["from_address"] if account is not None else ""
    smtp_host = account["smtp_host"] if account is not None else ""
    smtp_username = account["smtp_username"] if account is not None else ""
    domain = account["mailgun_domain"] if account is not None else ""
    port = account["smtp_port"] if account is not None and account["smtp_port"] else 587
    secret_line = "saved" if account is not None and account["secret"] else "not saved"
    remove = ""
    if account is not None:
        remove = f"""
        <form method="post" action="{esc(remove_action)}"{_guard("Remove this mailbox?")}>
          <input type="hidden" name="csrf" value="{esc(user['csrf'])}">
          <p><button class="quiet">{esc(remove_button)}</button></p>
        </form>
        """
    marked = f' id="{esc(anchor)}"' if anchor else ""
    heading = f"<h2>{esc(title)}</h2>" if title else ""
    chosen = via if via in ("smtp", "https") else transport
    if chosen not in ("smtp", "https"):
        chosen = "smtp"
    https_choice = provider_choice if provider_choice in {key for key, _label in _MAIL_PROVIDERS} else provider
    if https_choice not in {key for key, _label in _MAIL_PROVIDERS}:
        https_choice = "resend"
    mail_page = page or "/settings/notices"
    transport_tabs = _tab_links(
        (
            ("smtp", _href(mail_page, via="smtp"), "SMTP"),
            ("https", _href(mail_page, via="https"), "HTTPS"),
        ),
        chosen,
        "Transport",
    )
    provider_tabs = ""
    https_fields = ""
    if chosen == "https":
        provider_tabs = _tab_links(
            tuple(
                (key, _href(mail_page, via="https", provider=key), label)
                for key, label in _MAIL_PROVIDERS
            ),
            https_choice,
            "HTTPS provider",
        )
        if https_choice == "mailgun":
            https_fields = f"""
            <label>Mailgun domain</label>
            <input name="mailgun_domain" value="{esc(domain)}" autocomplete="off" spellcheck="false">
            <label>Mailgun region</label>
            <select name="mailgun_region">
              <option value="us"{_selected("us", region)}>United States</option>
              <option value="eu"{_selected("eu", region)}>Europe</option>
            </select>
            """
    smtp_fields = ""
    if chosen == "smtp":
        smtp_fields = f"""
        <label>SMTP host</label>
        <input name="smtp_host" value="{esc(smtp_host)}" autocomplete="off" spellcheck="false">
        <label>SMTP port</label>
        <input name="smtp_port" value="{esc(port)}" inputmode="numeric" autocomplete="off">
        <label>SMTP security</label>
        <select name="smtp_security">
          <option value="starttls"{_selected("starttls", security)}>STARTTLS</option>
          <option value="tls"{_selected("tls", security)}>TLS</option>
        </select>
        <label>SMTP username</label>
        <input name="smtp_username" value="{esc(smtp_username)}" autocomplete="off" spellcheck="false">
        """
    provider_field = (
        f'<input type="hidden" name="https_provider" value="{esc(https_choice)}">'
        if chosen == "https"
        else ""
    )
    return f"""
    <div class="card"{marked}>
      {heading}
      <p>From: {esc(from_address or "Not set")}</p>
      <p>Password or API key: {esc(secret_line)}.</p>
      <p>SMTP uses the mail server. An HTTPS API uses port 443 when a host blocks outbound SMTP.</p>
      {transport_tabs}
      {provider_tabs}
      <form method="post" action="{esc(action)}">
        <input type="hidden" name="csrf" value="{esc(user['csrf'])}">
        <input type="hidden" name="transport" value="{chosen}">
        {provider_field}
        <label>From address</label>
        <input name="from_address" type="email" value="{esc(from_address)}" autocomplete="email" spellcheck="false" required>
        {smtp_fields}
        {https_fields}
        <label>Password or API key</label>
        <input name="mail_secret" type="password" autocomplete="off" spellcheck="false">
        <p><button>{esc(button)}</button></p>
      </form>
      {remove}
    </div>
    """


def _full_color(value: str) -> str:
    body = value[1:] if value.startswith("#") else value
    if len(body) == 3:
        body = "".join(ch * 2 for ch in body)
    return "#" + body.lower()


def _editor_dashboard(conn, user) -> bytes:
    editor = _require_active_editor(conn, user)
    jobs = jobs_visible(conn, editor) if allowed(editor, "manuscripts") else []
    if jobs:
        job = _in_hand(jobs)
        body = _work_place(conn, user, editor, job, jobs, author=None, query=None)
        return _desk_layout(conn, user, editor, job["title"], body, "/")
    desk = practice_admin(conn, editor)
    sections = []
    if allowed(editor, "offers"):
        personas = conn.execute(
            "SELECT * FROM personas WHERE editor_id = ? ORDER BY created_at",
            (desk["id"],),
        ).fetchall()
        active = [row for row in personas if row["status"] == "active"]
        drafts = [row for row in personas if row["status"] == "draft"]
        items = "".join(
            "<li>"
            f"<a href='/personas/{esc(row['id'])}'>{esc(row['name'])}</a> "
            f"{esc(_persona_entry(row))} {esc(row['status'])}"
            "</li>"
            for row in active
        )
        draft_items = "".join(
            "<li>"
            f"<a href='/personas/{esc(row['id'])}'>{esc(row['name'])}</a> is a draft"
            "</li>"
            for row in drafts
        )
        sections.append(
            f"""
            <div class="card">
              <h2>Active personas</h2>
              <ul>{items or "<li>No active persona.</li>"}</ul>
              {f"<ul>{draft_items}</ul>" if draft_items else ""}
              <p><a href="/personas">Manage personas</a></p>
            </div>
            """
        )
    search = "<p><a href='/pipeline'>Search manuscripts</a></p>" if allowed(editor, "pipeline") else ""
    body = f"""
    <h1>{esc(editor['org_name'])}</h1>
    <p>No manuscripts yet. Authors send them from <a href="/e/{esc(desk['id'])}">your page</a>.</p>
    {search}
    {''.join(sections)}
    """
    return _desk_layout(conn, user, editor, editor["org_name"], body, "/")


def _personas_page(conn, user, editor) -> bytes:
    if not allowed(editor, "offers"):
        raise ValueError("that is not assigned to this login")
    desk = practice_admin(conn, editor)
    rows = conn.execute(
        "SELECT * FROM personas WHERE editor_id = ? ORDER BY created_at",
        (desk["id"],),
    ).fetchall()
    listed = "".join(
        "<tr>"
        f'<td><a class="row" href="/personas/{esc(row["id"])}">{esc(row["name"])}</a></td>'
        f"<td><code>{esc(_persona_entry(row))}</code></td>"
        f"<td>{esc(row['status'])}</td>"
        "</tr>"
        for row in rows
    )
    body = f"""
    <h1>Personas</h1>
    <p>A persona is a <code>workflow.json</code> file. The worker runs one model call for each step.
    <a href="/docs/workflow">How workflow.json works</a>.</p>
    <p><a class="action" href="/personas/new">Create a workflow.json</a></p>
    <p>Upload a file, edit the JSON, or
    <a href="/personas/new/wizard">open the wizard</a>.
    The wizard only creates a new file.</p>
    <p>A local agent can write the file too.
    <a href="/local-agent">Local agent</a> gives the agent a token.
    Install the <a href="/skills/desk-persona">desk persona skill</a>.</p>
    {_model_catalog(conn, editor)}
    {f"<table><tr><th>Name</th><th>File</th><th>Status</th></tr>{listed}</table>" if rows else "<p>No personas yet.</p>"}
    """
    return _desk_layout(conn, user, editor, "Personas", body, "/personas")


_CREATE_TABS = (
    ("upload", "/personas/new", "Upload"),
    ("edit", "/personas/new?how=edit", "Edit"),
    ("wizard", "/personas/new/wizard", "Wizard"),
)


def _persona_new_page(conn, user, editor, *, error="", name="", max_words="180000", workflow=None, how="") -> bytes:
    if not allowed(editor, "offers"):
        raise ValueError("that is not assigned to this login")
    if how not in ("upload", "edit"):
        how = "upload"
    text = _starter_json() if workflow is None else workflow
    error_line = f"<p class='error' role='alert' tabindex='-1' autofocus>{esc(error)}</p>" if error else ""
    if how == "edit":
        panel = f"""
        <h2>Edit the file</h2>
        <form method="post" action="/personas/new">
          <input type="hidden" name="csrf" value="{esc(user['csrf'])}">
          <input type="hidden" name="route" value="editor">
          <label>Name</label>
          <input name="name" value="{esc(name)}" required data-autofocus autocomplete="off">
          <label>Max words</label>
          <input name="max_words" value="{esc(max_words)}" inputmode="numeric" autocomplete="off">
          <label><code>workflow.json</code></label>
          <p>The desk checks the JSON before it saves. Invalid JSON is left unsaved.</p>
          <textarea class="code" name="workflow">{esc(text)}</textarea>
          <p><button>Save workflow.json</button></p>
        </form>
        """
    else:
        panel = f"""
        <h2>Upload a file</h2>
        <form method="post" action="/personas/new" enctype="multipart/form-data">
          <input type="hidden" name="csrf" value="{esc(user['csrf'])}">
          <input type="hidden" name="route" value="upload">
          <label>Name</label>
          <input name="name" value="{esc(name)}" required data-autofocus autocomplete="off">
          <label>Max words</label>
          <p>The model cost estimate uses this length. The default is 180,000.</p>
          <input name="max_words" value="{esc(max_words)}" inputmode="numeric" autocomplete="off">
          <label><code>workflow.json</code></label>
          <input type="file" name="workflow" accept=".json,application/json" required>
          <p>The desk checks the JSON before it saves. A new file uses the three platform skills.</p>
          <p><button>Upload workflow.json</button></p>
        </form>
        """
    body = f"""
    <h1><code>workflow.json</code></h1>
    <p>A persona is a <code>workflow.json</code> file. Each step is one model call.
    <a href="/docs/workflow">How workflow.json works</a>.</p>
    <p>A local agent can also write the file after the persona exists.
    <a href="/local-agent">Local agent</a>.
    The <a href="/skills/desk-persona">desk persona skill</a> uses <code>install_agent_files</code>.</p>
    {_tab_links(_CREATE_TABS, how, "Create a workflow.json")}
    {error_line}
    <div class="panel" role="tabpanel">
      {panel}
    </div>
    """
    return _desk_layout(conn, user, editor, "workflow.json", body, "/personas")


def _persona_model_line(conn, editor, spec) -> str:
    if spec.get("model"):
        return f"<p>This file asks for <code>{esc(spec['model'])}</code>.</p>"
    org = editor["organization_id"]
    if inference_rows(conn, org) and inference_defaults(conn, org):
        return "<p>This file does not name a model. A run uses a default model from Settings.</p>"
    return "<p>This file does not name a model. Name one when a provider is connected on Settings.</p>"


def _persona_page(conn, user, editor, persona_id: str, *, error="", workflow_text=None, part="") -> bytes:
    persona = _open_persona(conn, editor, persona_id)
    root = ensure_persona_package(persona)
    info = inspect_root(root)
    files = list_files(root)
    shown = files[:200]
    more = ""
    if len(files) > len(shown):
        more = f"<li>{len(files) - len(shown)} more files</li>"
    file_list = "".join(f"<li><code>{esc(path)}</code></li>" for path in shown) + more
    code = persona["agent_code"] or ""
    estimate = estimate_agent_words(persona["max_words"], code) if code else None
    estimate_line = ""
    if estimate:
        estimate_line = (
            f"<p>At {persona['max_words']} words the model cost estimate is "
            f"{esc(usd(estimate['token_cents']))}.</p>"
        )
    saved = ""
    workflow_path = root / "workflow.json"
    if workflow_path.is_file():
        saved = workflow_path.read_text(encoding="utf-8")
    shown = saved if workflow_text is None else workflow_text
    spec = None
    if info["entry"] == "workflow.json":
        try:
            spec = load_workflow(root)
        except ValueError:
            spec = None
    if spec is not None:
        lead = (
            "<p>This persona is a <code>workflow.json</code> file. "
            '<a href="/docs/workflow">How workflow.json works</a>.</p>'
            "<p>Change it in the editor, or upload a replacement file. "
            "The desk checks the JSON before it saves. "
            'A <a href="/local-agent">local agent</a> can write the file too.</p>'
            + _step_list(spec["steps"])
            + _persona_model_line(conn, editor, spec)
        )
    elif info["entry"] == "workflow.json":
        lead = (
            "<p>This <code>workflow.json</code> could not be read. "
            "Fix it in the editor. The desk checks the JSON before it saves.</p>"
        )
    else:
        lead = (
            "<p>This package is not a <code>workflow.json</code> file. "
            "Upload a <code>workflow.json</code> file or paste one in the editor. "
            '<a href="/docs/workflow">How workflow.json works</a>.</p>'
        )
    error_line = f"<p class='error' role='alert' tabindex='-1' autofocus>{esc(error)}</p>" if error else ""
    persona_tabs = (
        ("edit", f"/personas/{esc(persona['id'])}", "Edit"),
        ("upload", f"/personas/{esc(persona['id'])}?part=upload", "Upload"),
        ("details", f"/personas/{esc(persona['id'])}?part=details", "Details"),
        ("package", f"/personas/{esc(persona['id'])}?part=package", "Package"),
    )
    if part not in ("edit", "upload", "details", "package"):
        part = "edit"
    if part == "upload":
        panel = f"""
        <h2>Upload a file</h2>
        <form method="post" action="/personas/{esc(persona['id'])}/workflow" enctype="multipart/form-data">
          <input type="hidden" name="csrf" value="{esc(user['csrf'])}">
          <label><code>workflow.json</code></label>
          <input type="file" name="workflow" accept=".json,application/json" required>
          <p><button>Upload workflow.json</button></p>
        </form>
        """
    elif part == "details":
        panel = f"""
        <h2>Details</h2>
        <form method="post" action="/personas/{esc(persona['id'])}">
          <input type="hidden" name="csrf" value="{esc(user['csrf'])}">
          <label>Name</label>
          <input name="name" value="{esc(persona['name'])}" required autocomplete="off">
          <label>Max words</label>
          <input name="max_words" value="{persona['max_words']}" inputmode="numeric" autocomplete="off">
          <p><button>Save persona</button></p>
        </form>
        """
    elif part == "package":
        panel = f"""
        <h2>Files</h2>
        <ul>{file_list}</ul>
        <form method="post" action="/personas/{esc(persona['id'])}/package" enctype="multipart/form-data">
          <input type="hidden" name="csrf" value="{esc(user['csrf'])}">
          <label>Package zip</label>
          <input type="file" name="package" accept=".zip,application/zip" required>
          <p>A zip replaces these files. Include <code>manifest.json</code>. The usual entry is <code>workflow.json</code>. Text only.</p>
          <p><button>Install package</button></p>
        </form>
        """
    else:
        panel = f"""
        <h2>Edit the file</h2>
        <form method="post" action="/personas/{esc(persona['id'])}/workflow">
          <input type="hidden" name="csrf" value="{esc(user['csrf'])}">
          <label><code>workflow.json</code></label>
          <p>The desk checks the JSON before it saves. Invalid JSON is left unsaved.</p>
          <textarea class="code" name="workflow">{esc(shown)}</textarea>
          <p><button>Save workflow.json</button></p>
        </form>
        """
    body = f"""
    <h1>{esc(persona['name'])}</h1>
    {lead}
    <p>Status {esc(persona['status'])}. {len(files)} files. Checkout copies them onto the manuscript. A later edit does not change a manuscript that was already submitted.</p>
    {estimate_line}
    <form method="post" action="/editor/personas/{esc(persona['id'])}/publish">
      <input type="hidden" name="csrf" value="{esc(user['csrf'])}">
      <p><button>Publish</button></p>
    </form>
    {_tab_links(persona_tabs, part, "Persona")}
    {error_line}
    <div class="panel" role="tabpanel">
      {panel}
    </div>
    """
    return _desk_layout(conn, user, editor, persona["name"], body, "/personas")


def _starter_json() -> str:
    return json.dumps(default_workflow(), indent=2) + "\n"


def _wizard_part_tabs(part: str) -> str:
    buttons = []
    for key, text in (("steps", "Steps"), ("add", "Add a step"), ("file", "File")):
        selected = "true" if key == part else "false"
        buttons.append(
            f'<button type="submit" name="part" value="{key}" role="tab" aria-selected="{selected}">{text}</button>'
        )
    return '<div class="tabs" role="tablist" aria-label="Wizard">' + "".join(buttons) + "</div>"


def _new_wizard_page(
    conn, user, editor, *, spec=None, name="", max_words="180000", error="", pending=None, part=""
) -> bytes:
    if not allowed(editor, "offers"):
        raise ValueError("that is not assigned to this login")
    if part not in ("steps", "add", "file"):
        part = "steps"
    spec = default_workflow() if spec is None else spec
    pending = pending or {}
    raw = json.dumps(spec, indent=2) + "\n"
    title = pending.get("title") if error and "title" in pending else spec["title"]
    model_value = pending.get("model") if error and "model" in pending else (spec.get("model") or "")
    error_line = f"<p class='error' role='alert' tabindex='-1' autofocus>{esc(error)}</p>" if error else ""
    if part == "add":
        selected_skill = pending.get("skill") or ""
        skills = "".join(
            f'<option value="{esc(item)}"{_selected(item, selected_skill)}>'
            f"{esc(item)} — {esc(_SKILL_HELP[item])}</option>"
            for item in PLATFORM_SKILLS
        )
        selected_attach = pending.get("attach") or ""
        earlier = f'<option value=""{_selected("", selected_attach)}>None</option>' + "".join(
            f'<option value="{esc(step["name"])}"{_selected(step["name"], selected_attach)}>{esc(step["name"])}</option>'
            for step in spec["steps"]
        )
        destination = pending.get("destination") or "produces"
        when = pending.get("when") or ""
        panel = f"""
        <h2>Add a step</h2>
        <input type="hidden" name="persona_name" value="{esc(name)}">
        <input type="hidden" name="max_words" value="{esc(str(max_words))}">
        <input type="hidden" name="title" value="{esc(title)}">
        <input type="hidden" name="model" value="{esc(model_value)}">
        <label>Step name <code>name</code></label>
        <p>The id in the file. Lowercase letters, numbers, and hyphens. One word such as <code>promise</code>.</p>
        <input name="name" value="{esc(pending.get('name') or '')}" autocomplete="off" spellcheck="false" autocapitalize="off" data-autofocus>
        <label>Skill <code>skill</code></label>
        <p>Which instructions this call uses. These three are built in, and the ask is the question. A custom skill is a file in the package, <code>skills/{{name}}/SKILL.md</code>, added after this file exists.</p>
        <select name="skill">{skills}</select>
        <label>Ask <code>ask</code></label>
        <p>The question for this manuscript. One question per step.</p>
        <textarea name="ask">{esc(pending.get('ask') or '')}</textarea>
        <label>Where the answer goes</label>
        <p><code>produces</code> stores the answer as its own document, such as the compendium. <code>into</code> adds the answer as a section of a document, such as the letter.</p>
        <select name="destination">
          <option value="produces"{_selected("produces", destination)}>Its own document (produces)</option>
          <option value="into"{_selected("into", destination)}>A section of a document (into)</option>
        </select>
        <label>Document name</label>
        <p>The document id. <code>compendium</code> and <code>readback</code> are their own documents. <code>letter</code> collects the diagnosis sections.</p>
        <input name="document" value="{esc(pending.get('document') or '')}" autocomplete="off" spellcheck="false" autocapitalize="off">
        <label>Section heading <code>heading</code></label>
        <p>Required when the answer joins a document. Leave this blank when the answer is its own document.</p>
        <input name="heading" value="{esc(pending.get('heading') or '')}">
        <label>Include an earlier step <code>attach</code></label>
        <p>Optional. That step's text is included in this call. The manuscript and the author's form are already on every call.</p>
        <select name="attach">{earlier}</select>
        <label>When <code>when</code></label>
        <p>Always runs this step. Focus runs it only when the author filled in a focus. The model cost estimate still counts that step.</p>
        <select name="when">
          <option value=""{_selected("", when)}>Always</option>
          <option value="focus"{_selected("focus", when)}>Only when the author filled in a focus</option>
        </select>
        <p><button name="add" value="1" data-primary>Add this step</button></p>
        """
    elif part == "file":
        panel = f"""
        <h2><code>workflow.json</code></h2>
        <input type="hidden" name="persona_name" value="{esc(name)}">
        <input type="hidden" name="max_words" value="{esc(str(max_words))}">
        <input type="hidden" name="title" value="{esc(title)}">
        <input type="hidden" name="model" value="{esc(model_value)}">
        <pre>{esc(raw)}</pre>
        """
    else:
        can_remove = len(spec["steps"]) > 1
        removes = "".join(
            _step_sentence(step)
            + (
                ""
                if not can_remove
                else (
                    f'<p><button class="quiet" name="remove" value="{esc(step["name"])}" '
                    "onclick=\"return confirm('Remove this step?')\">"
                    f"Remove {esc(step['name'])}</button></p>"
                )
            )
            for step in spec["steps"]
        )
        choices = _known_model_choices(conn, editor, [])
        listed = ""
        model_list = ""
        if choices:
            options = "".join(
                f'<option value="{esc(item)}">{esc(label)}</option>' for item, label in choices
            )
            listed = f'<datalist id="practice-models">{options}</datalist>'
            model_list = ' list="practice-models"'
        panel = f"""
        <h2>Steps</h2>
        <label>Name</label>
        <input name="persona_name" value="{esc(name)}" data-autofocus autocomplete="off">
        <label>Max words</label>
        <p>The model cost estimate uses this length. The default is 180,000.</p>
        <input name="max_words" value="{esc(str(max_words))}" inputmode="numeric" autocomplete="off">
        {removes}
        <label>Title <code>title</code></label>
        <p>The name stored at the top of the file. The developmental letter uses Developmental edit.</p>
        <input name="title" value="{esc(title)}">
        <p><button class="quiet" name="save_title" value="1">Update title</button></p>
        <label>Model <code>model</code></label>
        <p>The model this file asks for. Letters, digits, and <code>. _ : / - +</code>. Choose one a provider listed, or type an id. Leave it blank until a provider is connected.</p>
        <input name="model" value="{esc(model_value)}" autocomplete="off" spellcheck="false" autocapitalize="off"{model_list}>
        {listed}
        <p><button class="quiet" name="save_model" value="1">Update model</button></p>
        """
    body = f"""
    <h1>New <code>workflow.json</code></h1>
    <p>This wizard creates a new file. It does not change a workflow you already saved.
    <a href="/docs/workflow">How workflow.json works</a>.</p>
    <p>It starts as the developmental edit. Add or remove steps, then create the file.</p>
    {_tab_links(_CREATE_TABS, "wizard", "Create a workflow.json")}
    {error_line}
    <form method="post" action="/personas/new/wizard">
      <input type="hidden" name="csrf" value="{esc(user['csrf'])}">
      <textarea class="draft" name="draft">{esc(raw)}</textarea>
      {_wizard_part_tabs(part)}
      <div class="panel" role="tabpanel">
        {panel}
      </div>
      <p><button name="create" value="1">Create this file</button></p>
    </form>
    """
    return _desk_layout(conn, user, editor, "workflow.json", body, "/personas")


def _selected(value: str, chosen: str) -> str:
    return " selected" if value == chosen else ""


def _post_new_persona(conn, user, editor, form):
    desk = _assigned_desk(conn, editor, "offers")
    route = form.get("route") or ""
    name = form.get("name") or ""
    max_words = form.get("max_words") or "180000"
    editor_text = None
    try:
        if route == "upload":
            editor_text = _uploaded_workflow_text(form)
            spec = accept_new_workflow(parse_workflow_text(editor_text))
        elif route == "editor":
            editor_text = form.get("workflow") if isinstance(form.get("workflow"), str) else ""
            spec = accept_new_workflow(parse_workflow_text(editor_text))
        else:
            raise ValueError("choose how to create the file")
        persona_id = create_persona(conn, desk, name=name, max_words=max_words)
        save_workflow(conn, desk, persona_id, spec)
    except ValueError as exc:
        return 400, _persona_new_page(
            conn,
            user,
            editor,
            error=str(exc),
            name=name,
            max_words=str(max_words),
            workflow=editor_text,
            how="edit" if route == "editor" else "upload",
        ), []
    return 303, b"", [("Location", f"/personas/{persona_id}")]


def _post_persona_workflow(conn, user, editor, persona_id: str, form):
    desk = _assigned_desk(conn, editor, "offers")
    _open_persona(conn, editor, persona_id)
    echo = None
    try:
        text, echo = _posted_workflow(form)
        save_workflow(conn, desk, persona_id, parse_workflow_text(text))
    except ValueError as exc:
        return 400, _persona_page(
            conn, user, editor, persona_id, error=str(exc), workflow_text=echo
        ), []
    return 303, b"", [("Location", f"/personas/{persona_id}")]


def _post_new_wizard(conn, user, editor, form):
    desk = _assigned_desk(conn, editor, "offers")
    name = form.get("persona_name") or ""
    max_words = form.get("max_words") or "180000"
    try:
        base = _draft_spec(form)
    except ValueError as exc:
        return _wizard_error(conn, user, editor, default_workflow(), form, exc, name, max_words, "steps")
    try:
        spec = _mutate_wizard(base, form)
        if form.get("create"):
            spec = accept_new_workflow(spec)
            persona_id = create_persona(conn, desk, name=name, max_words=max_words)
            save_workflow(conn, desk, persona_id, spec)
            return 303, b"", [("Location", f"/personas/{persona_id}")]
    except ValueError as exc:
        failed = "add" if form.get("add") else "steps"
        return _wizard_error(conn, user, editor, base, form, exc, name, max_words, failed)
    if form.get("add") or form.get("remove") or form.get("save_title") or form.get("save_model"):
        shown = "steps"
    else:
        shown = form.get("part") if form.get("part") in ("steps", "add", "file") else "steps"
    return 200, _new_wizard_page(
        conn, user, editor, spec=spec, name=name, max_words=str(max_words), part=shown
    ), []


def _wizard_error(conn, user, editor, spec, form, exc, name, max_words, part):
    return 400, _new_wizard_page(
        conn,
        user,
        editor,
        spec=spec,
        name=name,
        max_words=str(max_words),
        error=str(exc),
        pending=form,
        part=part,
    ), []


def _draft_spec(form) -> dict:
    raw = form.get("draft")
    if not isinstance(raw, str) or not raw.strip():
        return default_workflow()
    return parse_workflow_text(raw)


def _mutate_wizard(spec: dict, form) -> dict:
    spec = _with_title(spec, form)
    spec = _with_model(spec, form)
    if form.get("remove"):
        return _without_step(spec, form["remove"])
    if form.get("add"):
        return _carry(spec, steps=[*spec["steps"], _wizard_step(form)])
    if form.get("save_title") or form.get("save_model") or form.get("create") or form.get("part"):
        return spec
    raise ValueError("choose what to change")


def _with_model(spec: dict, form) -> dict:
    if "model" not in form:
        return spec
    return _carry(spec, model=(form.get("model") or "").strip())


def _carry(spec: dict, **changes) -> dict:
    steps = changes["steps"] if "steps" in changes else spec["steps"]
    title = changes["title"] if "title" in changes else spec["title"]
    model = changes["model"] if "model" in changes else (spec.get("model") or "")
    data = {"title": title, "steps": steps}
    if model:
        data["model"] = model
    return parse_workflow(data)


def _with_title(spec: dict, form) -> dict:
    if "title" not in form:
        return spec
    return parse_workflow({**spec, "title": form.get("title") or ""})


def _without_step(spec: dict, target: str) -> dict:
    dependents = [step["name"] for step in spec["steps"] if step.get("attach") == target]
    if dependents:
        raise ValueError(f"{target} is included by {', '.join(dependents)}. Remove those steps first.")
    steps = [step for step in spec["steps"] if step["name"] != target]
    if len(steps) == len(spec["steps"]):
        raise ValueError(f"{target} is not a step")
    return _carry(spec, steps=steps)


def _uploaded_workflow_text(form) -> str:
    text, _echo = _posted_workflow(form)
    return text


def _posted_workflow(form) -> tuple[str, str | None]:
    uploaded = form.get("workflow")
    if isinstance(uploaded, tuple):
        if len(uploaded) != 2 or not isinstance(uploaded[1], (bytes, bytearray)):
            raise ValueError("upload a workflow.json file")
        filename, blob = uploaded
        if not str(filename).lower().endswith(".json"):
            raise ValueError("upload a workflow.json file")
        try:
            text = bytes(blob).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("workflow.json could not be read") from exc
        return text, text
    if isinstance(uploaded, str):
        return uploaded, uploaded
    raise ValueError("workflow.json is not valid JSON")


def _wizard_step(form) -> dict:
    destination = form.get("destination") or ""
    if destination not in ("produces", "into"):
        raise ValueError("choose where the answer goes")
    skill = form.get("skill") or ""
    if skill not in PLATFORM_SKILLS:
        raise ValueError("choose a platform skill. A custom skill is a file your local agent adds")
    document = (form.get("document") or "").strip()
    if not document:
        raise ValueError("name the document this step writes")
    step = {
        "name": (form.get("name") or "").strip(),
        "skill": skill,
        "ask": (form.get("ask") or "").strip(),
        destination: document,
    }
    heading = (form.get("heading") or "").strip()
    if destination == "into":
        if not heading:
            raise ValueError("a section needs a heading")
        step["heading"] = heading
    attach = (form.get("attach") or "").strip()
    if attach:
        step["attach"] = attach
    when = form.get("when") or ""
    if when:
        step["when"] = when
    return step


def _open_persona(conn, editor, persona_id: str):
    if not allowed(editor, "offers"):
        raise ValueError("that is not assigned to this login")
    desk = practice_admin(conn, editor)
    persona = conn.execute(
        "SELECT * FROM personas WHERE id = ? AND editor_id = ?",
        (persona_id, desk["id"]),
    ).fetchone()
    if persona is None:
        raise ValueError("persona not found")
    return persona


def _step_sentence(step) -> str:
    sentence = f"<strong>{esc(step['name'])}</strong> uses <code>{esc(step['skill'])}</code>."
    if step.get("produces"):
        sentence += (
            f" The answer is its own document, <code>{esc(step['produces'])}</code> (<code>produces</code>)."
        )
    elif step.get("into"):
        sentence += (
            f" The answer is a section of <code>{esc(step['into'])}</code>"
            f" under {esc(step.get('heading') or '')} (<code>into</code>)."
        )
    if step.get("attach"):
        sentence += f" It includes the earlier step <code>{esc(step['attach'])}</code> (<code>attach</code>)."
    if step.get("when") == "focus":
        sentence += " It runs only when the author filled in a focus (<code>when</code> is <code>focus</code>)."
    return f"<p>{sentence}</p><p>Ask: {esc(step['ask'])}</p>"


def _step_list(steps) -> str:
    items = "".join(f"<li>{_step_sentence(step)}</li>" for step in steps)
    return f"<ul>{items}</ul>"


def _persona_entry(persona) -> str:
    try:
        root = ensure_persona_package(persona)
        return inspect_root(root)["entry"]
    except ValueError:
        return "workflow.json"


def _manuscripts_page(conn, user, editor) -> bytes:
    if not allowed(editor, "manuscripts"):
        raise ValueError("that is not assigned to this login")
    show_cost = allowed(editor, "payouts")
    jobs = jobs_visible(conn, editor)
    rows = "".join(
        "<tr>"
        f"<td><a class='row' href='/jobs/{esc(job['id'])}'>{esc(job['title'])}</a></td>"
        f"<td>{esc(job['author_email'])}</td>"
        f"<td>{esc(job['status'])}</td>"
        + (f"<td>{esc(_model_cost(job['cost_usd_micros']))}</td>" if show_cost else "")
        + "</tr>"
        for job in jobs
    )
    cost_head = "<th>Model cost</th>" if show_cost else ""
    if jobs:
        listed = f"<table><tr><th>Title</th><th>Author</th><th>Status</th>{cost_head}</tr>{rows}</table>"
    else:
        desk = practice_admin(conn, editor)
        listed = (
            "<p>No manuscripts yet. Authors send them from "
            f"<a href='/e/{esc(desk['id'])}'>your page</a>.</p>"
        )
    body = f"""
    <h1>Manuscripts</h1>
    {listed}
    """
    return _desk_layout(conn, user, editor, "Manuscripts", body, "/manuscripts")


def _pipeline_page(conn, user, editor, query) -> bytes:
    if not allowed(editor, "pipeline"):
        raise ValueError("that is not assigned to this login")
    return _desk_layout(conn, user, editor, "Pipeline", _pipeline_card(conn, editor, query), "/pipeline")


def _costs_page(conn, user, editor) -> bytes:
    if not allowed(editor, "payouts"):
        raise ValueError("that is not assigned to this login")
    spent = practice_model_cost_micros(conn, editor["organization_id"])
    visible = jobs_visible(conn, editor)
    rows = "".join(
        "<tr>"
        f"<td><a class='row' href='/jobs/{esc(job['id'])}'>{esc(job['title'])}</a></td>"
        f"<td>{esc(job['status'])}</td>"
        f"<td>{esc(_model_cost(job['cost_usd_micros']))}</td>"
        "</tr>"
        for job in visible
    )
    table = (
        f"<table><tr><th>Title</th><th>Status</th><th>Model cost</th></tr>{rows}</table>"
        if visible
        else "<p>No manuscripts yet.</p>"
    )
    body = f"""
    <h1>Costs</h1>
    <div class="card">
      <h2>Model cost</h2>
      <p>Model cost {esc(_model_cost(spent))}. This is an operating expense of the practice. Authors do not see it.</p>
      {table}
    </div>
    """
    return _desk_layout(conn, user, editor, "Costs", body, "/costs")


def _assistants_page(conn, user, editor) -> bytes:
    if not allowed(editor, "assistants"):
        raise ValueError("that is not assigned to this login")
    return _desk_layout(conn, user, editor, "Assistants", _assistants_card(conn, user, editor), "/assistants")


def _local_agent_page(conn, user, host="", scheme: str = "http") -> bytes:
    editor = editor_for_user(conn, user["id"])
    card = _local_agent_card(conn, user, "/local-agent", _mcp_url(host, scheme))
    if editor is None or editor["status"] not in ("active", "suspended"):
        body = f"""
        <h1>Local agent</h1>
        <p>This login has no practice, so it cannot write a persona.</p>
        {card}
        """
        return layout("Local agent", _nav(user), body)
    return _desk_layout(conn, user, editor, "Local agent", card, "/local-agent")


def _assistants_card(conn, user, editor) -> str:
    pending = "".join(
        "<li>"
        f"{esc(row['email'])} "
        f"<form style='display:inline' method='post' action='/editor/invites/{esc(row['id'])}/revoke'{_guard('Revoke this invite?')}>"
        f"<input type='hidden' name='csrf' value='{esc(user['csrf'])}'><button>Revoke</button></form>"
        "</li>"
        for row in pending_editor_invites(conn, editor)
    )
    people = "".join(
        "<li>"
        f"{esc(row['email'])} "
        f"<form method='post' action='/editor/assistants/{esc(row['id'])}'>"
        f"<input type='hidden' name='csrf' value='{esc(user['csrf'])}'>"
        f"{_permission_fields(editor, permission_flags(row))}"
        f"<p><button>Save permissions</button></p></form>"
        f"<form style='display:inline' method='post' action='/editor/editors/{esc(row['id'])}/remove'{_guard('Remove this assistant?')}>"
        f"<input type='hidden' name='csrf' value='{esc(user['csrf'])}'><button>Remove</button></form>"
        "</li>"
        for row in practice_editors(conn, editor)
        if not row["is_org_admin"]
    )
    waiting = f"<ul>{pending}{people}</ul>" if pending or people else ""
    return f"""
    <div class="card">
      <h2>Assistants</h2>
      <p>They sign in to this desk. You choose what each one can do. Remove them and the desk is unchanged.</p>
      <form method="post" action="/editor/invites/editors">
        <input type="hidden" name="csrf" value="{esc(user['csrf'])}">
        <label>Email</label>
        <input name="email" type="email" required autocomplete="email" spellcheck="false" inputmode="email">
        {_permission_fields(editor, None)}
        <p><button>Invite</button></p>
      </form>
      {waiting}
    </div>
    """


_PIPELINE_FIELDS = (
    "q",
    "genre",
    "audience",
    "comps_contains",
    "word_count_min",
    "word_count_max",
    "human_requested",
)


def _query_text(query, key: str) -> str:
    values = query.get(key) or []
    if not values or not isinstance(values[0], str):
        return ""
    return values[0]


def _optional_int(value: str) -> int | None:
    value = (value or "").strip()
    if not value:
        return None
    return int(value)


def _choice(value: str, label: str, current: str) -> str:
    selected = " selected" if value == current else ""
    return f'<option value="{esc(value)}"{selected}>{esc(label)}</option>'


def _pipeline_card(conn, editor, query) -> str:
    searched = any(key in query for key in _PIPELINE_FIELDS)
    human = _query_text(query, "human_requested") if searched else "1"
    if human not in ("", "0", "1"):
        raise ValueError("human request filter is invalid")
    filters = {
        "q": _query_text(query, "q"),
        "genre": _query_text(query, "genre"),
        "audience": _query_text(query, "audience"),
        "comps_contains": _query_text(query, "comps_contains"),
        "word_count_min": _optional_int(_query_text(query, "word_count_min")),
        "word_count_max": _optional_int(_query_text(query, "word_count_max")),
        "human_requested": None if human == "" else int(human),
    }
    rows = search_manuscripts(conn, editor, **filters)
    listed = "".join(
        "<tr>"
        f"<td><a class='row' href='/jobs/{esc(row['id'])}'>{esc(row['title'])}</a></td>"
        f"<td>{esc(row['author_email'])}</td>"
        f"<td>{esc(row['genre'] or '')}</td>"
        f"<td>{row['word_count'] or 0}</td>"
        f"<td>{esc(row['comps'] or '')}</td>"
        f"<td>{'Yes' if row['human_requested'] else 'No'}</td>"
        "</tr>"
        for row in rows
    )
    if rows:
        summary = f"{len(rows)} manuscripts."
        table = (
            "<table><tr><th>Title</th><th>Author</th><th>Genre</th><th>Words</th>"
            f"<th>Comps</th><th>Asked</th></tr>{listed}</table>"
        )
    elif searched:
        summary = "No manuscripts match."
        table = ""
    else:
        summary = "No one has asked for a human editor."
        table = ""
    words_min = "" if filters["word_count_min"] is None else filters["word_count_min"]
    words_max = "" if filters["word_count_max"] is None else filters["word_count_max"]
    return f"""
    <h1>Pipeline</h1>
    <p>Search the manuscripts on this desk to find authors you may want to edit by hand. The search does not pause or change the automated run. A human edit is arranged with the editor, off this desk.</p>
    <div class="pipeline">
      <div class="card pipeline-results">
        <p>{summary}</p>
        {table}
      </div>
      <form class="card pipeline-filters" method="get" action="/pipeline">
        <label>Search</label>
        <input name="q" type="search" value="{esc(filters['q'])}" autocomplete="off" spellcheck="false">
        <label>Genre</label>
        <input name="genre" value="{esc(filters['genre'])}" autocomplete="off">
        <label>Audience</label>
        <input name="audience" value="{esc(filters['audience'])}" autocomplete="off">
        <label>Comps contain</label>
        <input name="comps_contains" value="{esc(filters['comps_contains'])}" autocomplete="off">
        <label>Minimum words</label>
        <input name="word_count_min" inputmode="numeric" value="{words_min}" autocomplete="off">
        <label>Maximum words</label>
        <input name="word_count_max" inputmode="numeric" value="{words_max}" autocomplete="off">
        <label>Author asked for a human editor</label>
        <select name="human_requested">
          {_choice('', 'Either', human)}
          {_choice('1', 'Yes', human)}
          {_choice('0', 'No', human)}
        </select>
        <p><button>Search</button></p>
      </form>
    </div>
    """


def _author(conn, user, method, path, form, query=None):
    author_may_enter(conn, user)
    author = author_for_user(conn, user["id"])
    if path == "/submit" and method == "GET":
        return 200, _submit_page(conn, user, author), []
    if path == "/jobs" and method == "POST":
        uploaded = form.get("manuscript")
        if not (
            isinstance(uploaded, tuple)
            and len(uploaded) == 2
            and isinstance(uploaded[1], (bytes, bytearray))
        ):
            raise ValueError("upload a manuscript file")
        filename, data = uploaded
        manuscript = manuscript_text(str(filename), bytes(data))
        digest = hashlib.sha256(manuscript.encode()).hexdigest()
        key = f"manuscripts/{author['organization_id']}/{author['editor_id']}/{author['id']}/{digest[:16]}.txt"
        write_manuscript(key, manuscript)
        job_id = create_job(
            conn,
            author,
            persona_id=form["persona_id"],
            title=form.get("title", ""),
            genre=form.get("genre", ""),
            audience=form.get("audience", ""),
            comps=form.get("comps", ""),
            do_not_change=form.get("do_not_change", ""),
            focus=form.get("focus", ""),
            manuscript=manuscript,
            object_key=key,
            digest=digest,
        )
        return 303, b"", [("Location", f"/jobs/{job_id}")]
    if path.startswith("/jobs/") and path.endswith("/human-request") and method == "POST":
        job_id = path.split("/")[2]
        request_human(conn, author["id"], job_id)
        return 303, b"", [("Location", f"/jobs/{job_id}?asked=1")]
    if path.startswith("/jobs/") and path.endswith("/thread") and method == "POST":
        job_id = path.split("/")[2]
        post_message(conn, job_id=job_id, sender_role="author", sender_id=author["id"], body=form.get("body", ""))
        return 303, b"", [("Location", f"/jobs/{job_id}?sent=1")]
    if path.startswith("/jobs/") and method == "GET":
        return 200, _job_page(conn, user, None, path.split("/")[2], author, query), []
    return 404, layout("Missing", _nav(user, path), _missing()), []


def _author_page(conn, user) -> bytes:
    author = author_for_user(conn, user["id"])
    jobs = _author_jobs(conn, author)
    theme = practice_appearance(conn, author["organization_id"])
    if jobs:
        job = _in_hand(jobs)
        body = _work_place(conn, user, None, job, jobs, author=author, query=None)
        return layout(job["title"], _nav(user), body, theme)
    personas = conn.execute(
        "SELECT * FROM personas WHERE editor_id = ? AND status = 'active' ORDER BY created_at",
        (author["editor_id"],),
    ).fetchall()
    offers = "".join(f"<li>{esc(p['name'])}</li>" for p in personas)
    body = f"""
    <h1>Your manuscripts</h1>
    <p><a class="action" href="/submit">New manuscript</a></p>
    <p>No manuscripts yet.</p>
    <h2>What this editor offers</h2>
    <ul>{offers or "<li>Nothing is on offer yet.</li>"}</ul>
    """
    return layout("Your manuscripts", _nav(user), body, theme)


def _submit_page(conn, user, author) -> bytes:
    personas = conn.execute(
        "SELECT * FROM personas WHERE editor_id = ? AND status = 'active' ORDER BY created_at",
        (author["editor_id"],),
    ).fetchall()
    options = "".join(
        f"<option value='{esc(p['id'])}'>{esc(p['name'])}</option>"
        for p in personas
    )
    body = f"""
    <h1>New manuscript</h1>
    <p><a href="/">Your manuscripts</a></p>
    <form method="post" action="/jobs" enctype="multipart/form-data">
      <input type="hidden" name="csrf" value="{esc(user['csrf'])}">
      <label>Persona</label>
      <select name="persona_id">{options}</select>
      <label>Title</label>
      <input name="title" required data-autofocus autocomplete="off">
      <label>Genre</label>
      <input name="genre" required autocomplete="off">
      <label>Audience</label>
      <input name="audience" required autocomplete="off">
      <label>Comps</label>
      <input name="comps" autocomplete="off">
      <label>Do not change</label>
      <input name="do_not_change" autocomplete="off">
      <label>Focus, optional</label>
      <input name="focus" autocomplete="off">
      <label>Manuscript file</label>
      <input type="file" name="manuscript" accept=".txt,.text,.md,.markdown,.docx,.doc,.odt,.rtf,.pdf,.html,.htm,.epub" required>
      <p>Word, PDF, RTF, OpenDocument, HTML, EPUB, Markdown, or plain text. The file is turned into text for the editing workflow.</p>
      <p><button>Submit</button></p>
    </form>
    """
    return layout("New manuscript", _nav(user), body, practice_appearance(conn, author["organization_id"]))


def _author_jobs(conn, author):
    return conn.execute(
        "SELECT * FROM jobs WHERE author_id = ? ORDER BY created_at DESC",
        (author["id"],),
    ).fetchall()


def _in_hand(jobs):
    for job in jobs:
        if job["human_requested"]:
            return job
    return jobs[0]


def _persona_label(conn, job) -> str:
    row = conn.execute("SELECT name FROM personas WHERE id = ?", (job["persona_id"],)).fetchone()
    if row is None:
        return ""
    return row["name"]


def _shelf(conn, editor, jobs, current_id, *, author) -> str:
    items = []
    for job in jobs:
        current = " aria-current='page'" if job["id"] == current_id else ""
        extra = ""
        if author is None and "author_email" in job.keys():
            extra = f" <span>{esc(job['author_email'])}</span>"
        items.append(
            "<li>"
            f"<a href='/jobs/{esc(job['id'])}'{current}>{esc(job['title'])}</a>"
            f"{extra} {esc(job['status'])}"
            "</li>"
        )
    if author is None:
        links = []
        if allowed(editor, "pipeline"):
            asked = search_manuscripts(conn, editor, human_requested=1)
            if asked:
                links.append(f"<p><a href='/pipeline'>{len(asked)} asked for a human editor</a></p>")
            links.append("<p><a href='/pipeline'>Search manuscripts</a></p>")
        if allowed(editor, "manuscripts"):
            links.append("<p><a href='/manuscripts'>All manuscripts</a></p>")
        return (
            f"<p class='practice'>{esc(editor['org_name'])}</p>"
            "<h2>Work in motion</h2>"
            f"<ul class='shelf-list'>{''.join(items)}</ul>"
            f"{''.join(links)}"
        )
    return (
        "<h2>Your manuscripts</h2>"
        f"<ul class='shelf-list'>{''.join(items)}</ul>"
        '<p><a class="action" href="/submit">New manuscript</a></p>'
    )


def _reading(conn, user, editor, job, *, author, query) -> str:
    job_id = job["id"]
    arts = conn.execute("SELECT * FROM artifacts WHERE job_id = ?", (job_id,)).fetchall()
    documents = [
        f"<h2>{esc(art['kind'])}</h2><pre class='manuscript'>{esc(read_object(art['object_key']))}</pre>"
        for art in arts
    ]
    messages = conn.execute(
        "SELECT * FROM messages WHERE job_id = ? ORDER BY created_at",
        (job_id,),
    ).fetchall()
    thread = "".join(f"<p><strong>{esc(m['sender_role'])}</strong> {esc(m['body'])}</p>" for m in messages)
    sentence = f"{esc(job['status'])}. {job['word_count']} words."
    if job["genre"]:
        sentence += f" {esc(job['genre'])}."
    persona = _persona_label(conn, job)
    if persona:
        sentence += f" {esc(persona)}."
    if author is None and editor is not None and allowed(editor, "payouts"):
        sentence += f" Model cost {esc(_model_cost(job['cost_usd_micros']))}."
    facts = f"<p>{sentence}</p>"
    if author is None and job["error_code"]:
        facts += f"<p class='error' role='alert'>{esc(job['error_code'])}</p>"
    notes = []
    if job["comps"]:
        notes.append(f"<p>Comps: {esc(job['comps'])}</p>")
    if job["do_not_change"]:
        notes.append(f"<p>Do not change: {esc(job['do_not_change'])}</p>")
    if job["focus"]:
        notes.append(f"<p>Focus: {esc(job['focus'])}</p>")
    if author is None and job["human_requested"]:
        notes.append("<p>Author asked for a human editor.</p>")
    if author is not None and job["human_requested"]:
        notes.append("<p>You asked this editor to work on it.</p>")
    asked = ((query or {}).get("asked") or [""])[0] == "1"
    sent = ((query or {}).get("sent") or [""])[0] == "1"
    if sent:
        notice = "<p class='note' role='status'>Message sent.</p>"
    elif asked:
        notice = "<p class='note' role='status'>The editor can see that you asked.</p>"
    else:
        notice = ""
    human = ""
    if author is not None and not job["human_requested"]:
        human = f"""
        <form method="post" action="/jobs/{esc(job_id)}/human-request">
          <input type="hidden" name="csrf" value="{esc(user['csrf'])}">
          <button>Ask this editor to work on it</button>
        </form>
        """
    can_reply = author is not None or (
        editor is not None
        and allowed(editor, "messages")
        and editor["organization_id"] == job["organization_id"]
    )
    if can_reply:
        reply = f"""
        <form method="post" action="/jobs/{esc(job_id)}/thread">
          <input type="hidden" name="csrf" value="{esc(user['csrf'])}">
          <label>Message</label>
          <textarea name="body" required></textarea>
          <p><button>Send</button></p>
        </form>
        """
    else:
        reply = "<p>Messages are not assigned to this login.</p>"
    return f"""
    <h1>{esc(job['title'])}</h1>
    {notice}
    {facts}
    {''.join(notes)}
    {human}
    {"<p><a href='#messages'>Messages</a></p>" if documents else ""}
    {''.join(documents)}
    <h2 id="messages">Messages</h2>
    {thread or "<p>No messages yet.</p>"}
    {reply}
    """


def _work_place(conn, user, editor, job, jobs, *, author, query) -> str:
    return (
        "<div class='place'>"
        f"<article class='pass'>{_reading(conn, user, editor, job, author=author, query=query)}</article>"
        f"<aside class='shelf'>{_shelf(conn, editor, jobs, job['id'], author=author)}</aside>"
        "</div>"
    )


def _job_page(conn, user, editor, job_id: str, author=None, query=None) -> bytes:
    job = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if job is None:
        raise ValueError("job not found")
    if user["role"] == "author" and (author is None or author["id"] != job["author_id"]):
        raise ValueError("job not found")
    if user["role"] in ("editor", "owner"):
        visible = {row["id"] for row in jobs_visible(conn, editor)}
        if job["id"] not in visible:
            raise ValueError("job not found")
    if editor is not None and allowed(editor, "manuscripts"):
        here = "/manuscripts"
        shelf_jobs = jobs_visible(conn, editor)
    elif editor is not None and allowed(editor, "pipeline"):
        here = "/pipeline"
        shelf_jobs = [job]
    elif editor is not None:
        here = "/"
        shelf_jobs = [job]
    else:
        here = ""
        shelf_jobs = _author_jobs(conn, author)
    body = _work_place(conn, user, editor, job, shelf_jobs, author=author, query=query)
    if editor is not None:
        return _desk_layout(conn, user, editor, job["title"], body, here)
    return layout(job["title"], _nav(user), body, practice_appearance(conn, job["organization_id"]))


def _next_path(form) -> str:
    target = form.get("next")
    if target in ("/", "/desk", "/local-agent"):
        return target
    return "/"


def _create_agent_token(conn, user, form):
    raw = issue_agent_token(conn, user["id"], form.get("label", ""))
    body = f"""
    <h1>Agent token</h1>
    <p>Copy this token into the local agent's Authorization header. The desk will not show it again.</p>
    <pre>{esc(raw)}</pre>
    <p><a href="{esc(_next_path(form))}">Back</a></p>
    """
    return 200, layout("Agent token", _nav(user), body), [("Cache-Control", "no-store")]


def _local_agent_card(conn, user, next_path: str, mcp_url: str) -> str:
    tokens = "".join(
        "<li>"
        f"{esc(row['label'] or 'Agent token')} "
        f"<form style='display:inline' method='post' action='/agent-tokens/{esc(row['id'])}/revoke'{_guard('Revoke this token?')}>"
        f"<input type='hidden' name='csrf' value='{esc(user['csrf'])}'>"
        f"<input type='hidden' name='next' value='{esc(next_path)}'>"
        "<button>Revoke</button></form>"
        "</li>"
        for row in list_agent_tokens(conn, user["id"])
    )
    listed = f"<ul>{tokens}</ul>" if tokens else ""
    return f"""
    <div class="card">
      <h2>Local agent</h2>
      <p>Connect a local agent to this login. It can transfer a workflow into the persona, see workloads, run a queued workload locally, and do the desk actions this login is assigned. For the practice editor it can also report what this process still needs before other people use it. The MCP address is <code>{esc(mcp_url)}</code>. Send the token as <code>Authorization: Bearer</code>.</p>
      <p><a href="/skills/desk-persona">Download the desk persona skill</a> and install it as <code>desk-persona/SKILL.md</code> where that agent loads skills.</p>
      <form method="post" action="/agent-tokens">
        <input type="hidden" name="csrf" value="{esc(user['csrf'])}">
        <input type="hidden" name="next" value="{esc(next_path)}">
        <label>Token label</label>
        <input name="label" placeholder="laptop…" autocomplete="off" spellcheck="false">
        <p><button>Create token</button></p>
      </form>
      {listed}
    </div>
    """


def _mcp_url(host: str, scheme: str = "http") -> str:
    scheme = "https" if scheme == "https" else "http"
    if host:
        return f"{scheme}://{host}/mcp"
    shown = config.HOST
    if shown in ("0.0.0.0", "::"):
        shown = "127.0.0.1"
    return f"{scheme}://{shown}:{config.PORT}/mcp"


def _serve_mcp(handler, method: str) -> None:
    origin = handler.headers.get("Origin")
    scheme, host = public_origin(handler)
    if origin and host and origin != f"{scheme}://{host}":
        _send_bytes(
            handler,
            403,
            _rpc_error(None, -32600, "origin is not allowed"),
            "application/json",
        )
        return
    if method != "POST":
        _send_bytes(
            handler,
            405,
            _rpc_error(None, -32601, "use POST"),
            "application/json",
            [("Allow", "POST")],
        )
        return
    raw_length = handler.headers.get("Content-Length", "0") or "0"
    try:
        length = int(raw_length)
    except ValueError:
        length = -1
    if length < 0 or length > 1_000_000:
        _send_bytes(handler, 400, _rpc_error(None, -32600, "the request is too large"), "application/json")
        return
    raw = handler.rfile.read(length) if length else b""
    try:
        message = json.loads(raw.decode("utf-8")) if raw else None
    except (UnicodeDecodeError, json.JSONDecodeError):
        _send_bytes(handler, 400, _rpc_error(None, -32700, "the request could not be read"), "application/json")
        return
    if isinstance(message, list):
        _send_bytes(handler, 400, _rpc_error(None, -32600, "send one message"), "application/json")
        return
    token = _bearer(handler.headers.get("Authorization", ""))
    conn = open_db()
    try:
        user = user_for_agent_token(conn, token) if token else None
        if user is None:
            msg_id = message.get("id") if isinstance(message, dict) else None
            text = "a token is required" if not token else "token is not valid"
            _send_bytes(
                handler,
                401,
                _rpc_error(msg_id, -32001, text),
                "application/json",
                [("WWW-Authenticate", "Bearer")],
            )
            return
        response = handle_message(conn, user, message, mcp_url=_mcp_url(host, scheme))
    finally:
        conn.close()
    if response is None:
        handler.send_response(202)
        _hsts(handler)
        handler.send_header("Content-Length", "0")
        handler.end_headers()
        return
    _send_bytes(handler, 200, json.dumps(response).encode(), "application/json")


_WEBHOOK_MAX = 1_000_000


def _serve_patreon_webhook(handler) -> None:
    """Patreon posts the raw body. The signature is over those exact bytes."""
    raw_length = handler.headers.get("Content-Length")
    try:
        length = int(raw_length) if raw_length is not None else -1
    except ValueError:
        length = -1
    if length < 0 or length > _WEBHOOK_MAX:
        handler.close_connection = True
        _send_bytes(handler, 400, b"Patreon webhook could not be read", "text/plain; charset=utf-8")
        return
    raw = handler.rfile.read(length) if length else b""
    conn = open_db()
    try:
        apply_patreon_webhook(
            conn,
            raw,
            handler.headers.get("X-Patreon-Signature", ""),
            handler.headers.get("X-Patreon-Event", ""),
        )
    except ValueError as exc:
        _send_bytes(handler, 400, str(exc).encode(), "text/plain; charset=utf-8")
        return
    finally:
        conn.close()
    _send_bytes(handler, 200, b"ok", "text/plain; charset=utf-8")


def _serve_skill(handler) -> None:
    path = config.DESK_PERSONA_SKILL
    if not path.is_file():
        _send_bytes(handler, 404, b"missing skill", "text/plain; charset=utf-8")
        return
    _send_bytes(
        handler,
        200,
        path.read_bytes(),
        "text/markdown; charset=utf-8",
        [("Content-Disposition", 'attachment; filename="SKILL.md"')],
    )


def _send_bytes(handler, status: int, body: bytes, content_type: str, extra=None) -> None:
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    _hsts(handler)
    handler.send_header("Content-Length", str(len(body)))
    for key, value in extra or []:
        handler.send_header(key, _header_value(value))
    handler.end_headers()
    if body:
        handler.wfile.write(body)


def _rpc_error(msg_id, code: int, message: str) -> bytes:
    return json.dumps(
        {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}
    ).encode()


def _bearer(header: str) -> str:
    parts = (header or "").split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip()
    return ""


def _read_form(handler) -> dict:
    raw_length = handler.headers.get("Content-Length", "0") or "0"
    try:
        length = int(raw_length)
    except ValueError as exc:
        raise ValueError("the form could not be read") from exc
    if length < 0 or length > _MAX_BODY:
        handler.close_connection = True
        if 0 < length <= 32 * 1024 * 1024:
            remaining = length
            while remaining:
                chunk = handler.rfile.read(min(remaining, 65536))
                if not chunk:
                    break
                remaining -= len(chunk)
        raise ValueError("manuscript file is over 8 MB")
    raw = handler.rfile.read(length) if length else b""
    ctype = handler.headers.get("Content-Type", "")
    if "multipart/form-data" in ctype:
        return _parse_multipart(raw, ctype)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("the form could not be read") from exc
    parsed = parse_qs(text)
    return {key: values[-1] for key, values in parsed.items()}


def _parse_multipart(raw: bytes, ctype: str) -> dict:
    boundary = ""
    for piece in ctype.split(";"):
        piece = piece.strip()
        if piece.lower().startswith("boundary="):
            boundary = piece.split("=", 1)[1].strip().strip('"')
    if not boundary:
        raise ValueError("the form could not be read")
    try:
        marker = b"--" + boundary.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ValueError("the form could not be read") from exc
    form = {}
    for chunk in raw.split(marker):
        if chunk.startswith(b"\r\n"):
            chunk = chunk[2:]
        elif chunk.startswith(b"\n"):
            chunk = chunk[1:]
        if chunk.startswith(b"--"):
            continue
        if chunk.endswith(b"\r\n"):
            chunk = chunk[:-2]
        elif chunk.endswith(b"\n"):
            chunk = chunk[:-1]
        head, sep, content = chunk.partition(b"\r\n\r\n")
        if not sep:
            head, sep, content = chunk.partition(b"\n\n")
        if not sep:
            continue
        disposition = ""
        for line in head.decode("utf-8", "replace").splitlines():
            if line.lower().startswith("content-disposition"):
                disposition = line
                break
        if not disposition:
            continue
        name = ""
        filename = None
        for bit in disposition.split(";"):
            bit = bit.strip()
            lower = bit.lower()
            if lower.startswith("name="):
                name = bit.split("=", 1)[1].strip().strip('"')
            elif lower.startswith("filename="):
                filename = bit.split("=", 1)[1].strip().strip('"')
        if not name:
            continue
        if filename is None:
            try:
                form[name] = content.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ValueError("the form could not be read") from exc
        else:
            form[name] = (filename, content)
    return form


def serve() -> None:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = open_db()
    try:
        tls = _server_tls()
        if tls is None:
            tls = acme.server_context()
    except acme.AcmeError as exc:
        raise SystemExit(str(exc)) from exc
    scheme = "https" if tls is not None else "http"
    try:
        if config.DEV_LOGIN or config.DEMO_MODE:
            seeded = seed_local_accounts(conn)
            host = "127.0.0.1" if config.HOST in ("0.0.0.0", "::") else config.HOST
            print("owner: owner@example.com")
            print("author: author@example.com")
            print(f"author page: {scheme}://{host}:{config.PORT}/e/{seeded['editor_id']}")
    finally:
        conn.close()
    server = ThreadingHTTPServer((config.HOST, config.PORT), App)
    if tls is not None:
        server.socket = tls.wrap_socket(server.socket, server_side=True)
    print(f"{scheme}://{config.HOST}:{config.PORT}")
    server.serve_forever()


def public_origin(handler) -> tuple[str, str]:
    """Scheme and host the browser used. A proxy's forwarded host wins when trusted."""
    address = getattr(handler, "client_address", None)
    peer = address[0] if address else ""
    headers = handler.headers
    return resolve_public_origin(
        host=headers.get("Host", "") or "",
        tls=isinstance(getattr(handler, "connection", None), ssl.SSLSocket),
        peer=peer,
        trust_proxy=config.TRUST_PROXY,
        forwarded=headers.get("Forwarded", "") or "",
        forwarded_proto=headers.get("X-Forwarded-Proto", "") or "",
        forwarded_host=headers.get("X-Forwarded-Host", "") or "",
        forwarded_port=headers.get("X-Forwarded-Port", "") or "",
    )


def _hsts(handler) -> None:
    if public_origin(handler)[0] == "https":
        handler.send_header("Strict-Transport-Security", "max-age=31536000")


def _session_cookie(sid: str, *, secure: bool) -> str:
    cookie = f"sid={sid}; HttpOnly; Path=/; SameSite=Lax"
    if secure:
        cookie += "; Secure"
    return cookie


def _clear_cookie(*, secure: bool) -> str:
    cookie = "sid=; HttpOnly; Path=/; SameSite=Lax; Max-Age=0"
    if secure:
        cookie += "; Secure"
    return cookie


def _server_tls() -> ssl.SSLContext | None:
    """TLS 1.2 and 1.3 when both certificate files are set. Otherwise plain HTTP."""
    cert = (config.TLS_CERT or "").strip()
    key = (config.TLS_KEY or "").strip()
    if not cert and not key:
        return None
    if not cert or not key:
        raise SystemExit("TLS_CERT and TLS_KEY are both required")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    try:
        context.load_cert_chain(cert, key)
    except (OSError, ssl.SSLError) as exc:
        raise SystemExit("TLS certificate could not be loaded") from exc
    return context


if __name__ == "__main__":
    serve()
