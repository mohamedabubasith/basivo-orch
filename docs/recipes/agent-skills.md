# Recipe: skills — teach an agent a procedure once

A skill is what you would otherwise paste into an agent's prompt: the refund
policy, the code review checklist, the way your team writes release notes.

Pasting works for one procedure and falls apart at five. The text gets
duplicated across flows and drifts, every run pays for all of it whether or not
it is relevant, and the actual request ends up buried behind several thousand
words the agent was not asked to follow this time.

So skills are separate objects, and the agent is given **only their names and
descriptions**. It reads a body by calling a tool, and only when it decides one
applies.

## What the agent sees

With three skills selected, the system prompt gains three lines:

```
## Skills available to you

- "refund-policy" — Use when a customer asks for money back, including chargebacks.
- "shipping-delays" — Use when a customer asks where their parcel is.
- "escalation" — Use when the customer has asked for a manager.
```

Then, mid-run, this happens in the run log:

```
skill.offered   refund-policy, shipping-delays, escalation
skill.loaded    refund-policy · 233 chars · budget left 59767
```

One skill's instructions entered the conversation. The other two cost three
lines between them. That is the whole idea: **cost scales with the skills used,
not the skills available.**

## Writing a good one

The **description** is the only part read before choosing, so it is the whole
basis of the choice. Describe the *situation*, not the document:

| Weak | Strong |
|---|---|
| `Refund policy` | `Use when a customer asks for money back, including partial refunds and chargebacks` |
| `Deployment docs` | `Use when asked to deploy, roll back, or check what is currently released` |

The **instructions** are loaded whole, so keep them a procedure. A 40-page
policy belongs in a bundled file, which the agent reads with `read_skill_file`
only if it needs to.

## Importing what you already have

The format is Anthropic's `SKILL.md` — frontmatter `name` and `description`,
then markdown — so a skill written for Claude imports here unchanged. Paste the
file into **Skills → Import SKILL.md**. Export gives you the file back, so a
skill can live in the repository it describes.

```markdown
---
name: pdf-forms
description: Use when the user needs to fill in a PDF form or extract its fields.
---

# PDF forms
…
```

## The guards

- **Skill budget** (default 60,000 characters per run) caps how much skill text
  one run may pull in. When a load would exceed it, the agent is *told* it hit
  the limit rather than silently getting less — so it can say so in its answer
  instead of quietly working blind.
- **Loading the same skill twice** is refused; it is a loop, not new
  information.
- **A wrong name** gets an error listing the real ones, so the agent recovers
  in the next turn rather than failing the run.
- **A deleted skill** does not break flows that list it. The agent is simply
  not offered it, and the run log records `skill.missing` — because a library
  edit should not take down every workflow that referenced it, and it should
  not silently make an agent worse either.

## Skills and memory together

They answer different questions. Memory is *what happened with this
counterparty*; a skill is *how this company does this thing*. A support agent
usually wants both: memory keyed to the customer, skills for the policies.
See [agent-memory.md](agent-memory.md).
