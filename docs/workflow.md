# workflow.json

A persona is a `workflow.json` file. The worker reads that file and runs one model call for each step. Checkout copies the file onto the manuscript. A later edit does not change a manuscript that was already submitted.

The desk serves this document at [/docs/workflow](/docs/workflow).

Create the file on the desk in three ways at [/personas/new](/personas/new). Upload a `workflow.json` file, edit the JSON in the text editor, or open the wizard. The wizard only creates a new file. The text editor checks the JSON before it saves. A local agent can also write the file: install the [desk persona skill](/skills/desk-persona), and the agent writes `workflow.json` through MCP.

## The file

`workflow.json` is an object with a title, an optional model, and a list of steps.

```json
{
  "title": "Developmental edit",
  "model": "grok-4.3",
  "steps": [
    {
      "name": "compendium",
      "skill": "author-compendium",
      "ask": "Compile what the manuscript establishes.",
      "produces": "compendium"
    },
    {
      "name": "promise",
      "skill": "author-dev",
      "ask": "Diagnose promise and genre fit.",
      "into": "letter",
      "heading": "Promise and genre fit",
      "attach": "compendium"
    }
  ]
}
```

`manifest.json` sits next to it and only names this file:

```json
{"harness": "workflow", "entry": "workflow.json"}
```

## The model

`model` is the id this file asks for. Letters, digits, and `. _ : / - +`. It is not a provider and it is not a key. The key stays on Settings.

A run uses that id on any provider the practice has connected that lists it. The cheapest published price is tried first. An exact id is tried before an id that ends with `/{model}` when the price is the same, so `grok-4.3` matches a provider that lists `x-ai/grok-4.3`. A provider that did not list a price is used after the ones that did. If a call fails, the next provider that lists that same model is tried. The run does not switch to another model when this one was listed. If none list it, the practice's default models are used, on any provider that lists them. Put the model in this file when the persona should run on a specific one. A file with no `model` uses those defaults. With no default saved, a file with no `model` fails once a provider is saved. With no provider saved, the desk uses the process setup and local mode still writes the letter on this machine.

## A step

Each step is one model call. The model cost estimate counts one call per step, including a step that runs only when the author filled in a focus. That estimate is an operating expense for the editor. Authors do not see it.

| Field | Required | What it does |
| --- | --- | --- |
| `name` | yes | The step id. Lowercase letters, numbers, and hyphens. Unique in the file. |
| `skill` | yes | Which instructions the call uses. A platform skill name, or a custom skill in this package. |
| `ask` | yes | The question for this manuscript. |
| `produces` | one of these | Stores the answer as its own document. The value is the document name, such as `compendium`. |
| `into` | one of these | Adds the answer as a section of a document. The value is that document's name, such as `letter`. |
| `heading` | with `into` | The section heading inside that document. |
| `attach` | no | The name of an earlier step. That step's text is included in this call. |
| `when` | no | Omit it and the step always runs. `focus` runs only when the author filled in a focus. |

A step has `produces` or `into`, not both. A readback step needs a compendium step somewhere in the file.

## Skills

Three built-in skills can be named on a step. The ask on the step is the question. Do not add files for those names.

- `author-compendium` compiles what the manuscript establishes.
- `author-dev` diagnoses one question.
- `author-readback` previews the compendium.

A custom skill is a folder in the package, `skills/{name}/SKILL.md`, plus any notes beside it. The folder shape is the [Agent Skills](https://agentskills.io/specification) format. The worker reads `SKILL.md` for that step and does not run scripts from the package.

## Where the answer goes

`produces` is a separate document. The compendium and the readback are separate documents.

`into` collects sections into one document. The developmental letter is one document, `letter`, with a heading for each diagnosis.

`attach` passes an earlier document into the call. A diagnosis attaches the compendium. It does not attach the manuscript again. The manuscript and the author's form are already on every call.

## The usual way to write it

Build the process with your local agent until the steps are the ones you want. Then ask that agent to put the file on the desk.

1. Open [Local agent](/local-agent) and create a token.
2. Install [desk-persona/SKILL.md](/skills/desk-persona) where that agent loads skills.
3. Tell it which persona to change. It writes `manifest.json`, `workflow.json`, and any custom skill files with `install_agent_files`.
4. It publishes the persona when you ask. Publishing does not set a price. Author access is an active membership of that editor's Patreon campaign. The editor connects their own Patreon client on Settings, and the desk asks Patreon with that client.

The wizard starts a new developmental file. It does not edit a file already saved on a persona. To change a saved file, use the text editor or upload a replacement. Both check the JSON before saving.
