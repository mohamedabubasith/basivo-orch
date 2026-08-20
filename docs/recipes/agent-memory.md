# Recipe: an agent that remembers

By default every run starts blank. That is right for one-shot work — classify
this ticket, summarise this payload — and wrong the moment a person expects
continuity: *"that fix didn't work"*, *"same customer as yesterday"*, *"what
did I tell you to remember?"*. An agent with no memory answers those by asking
what you are talking about.

Turn on **Memory → Remember the conversation** on the AI Agent node, and past
requests and replies are sent again ahead of the new one.

## The one field that matters: Memory Key

Memory Key decides **whose** conversation this is. It supports `{{ … }}`, and
in almost every real flow it should use one:

| Flow | Memory Key | Result |
|---|---|---|
| GitHub issue triage | `{{ input.body.issue.number }}` | one thread per issue |
| Support chat | `{{ input.body.chat_id }}` | one thread per customer |
| Daily standup summariser | *(empty)* | one shared thread |

Leaving it empty is a real choice, not a default to accept: **every trigger
then shares one thread**, so two customers would read each other's history. A
key that renders empty — a webhook that arrived without the field — fails the
node deliberately rather than quietly filing the conversation under the shared
thread.

## What is stored, and where

In your own Postgres, in the `agent_memory` table, beside the runs and
credentials it relates to. Nothing is sent to a third-party memory service.

Only **the request and the final reply** are kept. Tool calls and their results
are not: they are where credentials, file contents and third-party payloads
live, and a table that accumulates conversation history is the wrong place for
those. What people mean by "remember the conversation" is what was said.

Each row is scoped to the flow *and* the node, so two agents in one flow keep
separate memories, and publishing a new version does not wipe what an agent
knows.

## Memory Window

How many past turns are replayed, newest kept (default 20). This is a cost
control as much as a relevance one: history is resent on **every** model call,
so an unbounded memory makes each run more expensive than the last until it
hits the model's context limit. Narrowing the window takes effect on the next
run.

## Reading it back

Two steps appear in the run log:

- `memory.loaded` — the subject and how many turns were recalled.
- `memory.saved` — what the thread holds now.

When an agent gives a surprising answer, `memory.loaded` is the first place to
look: it says exactly which thread it read and how much of it, which is the
difference between a debuggable agent and a mysterious one.
