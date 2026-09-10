# Chat

The Chat trigger publishes a chat window. Drag it on, connect an agent,
publish, and the node shows a link: open it, send it to someone, or paste the
embed code into a site. There is nothing to build and nothing to host.

It exists because the other way of talking to a flow is an API key and a
stream of run events. That is a good way to integrate and a poor way to have a
conversation, and "put a chat box on this" was turning into a week of somebody
else's frontend work.

## What the flow sees

```
{{ input.text }}         what the person typed
{{ input.session_id }}   one id per browser, stable across reloads
{{ input.visitor }}      { ip }, so two visitors can be told apart in the log
```

Key the agent's memory on the session:

| Agent setting | Value |
|---|---|
| Memory | Conversation |
| Memory key | `{{ input.session_id }}` |

Without that, every message in one conversation arrives as a stranger.

## Making it a conversation

A chat that answers each message from that message alone is a form with a
cursor. "Make it blue" then means a blue picture of nothing in particular.

What makes it a conversation is one node with memory, keyed on the session:

```
Chat  ->  AI Agent (Memory: conversation, key {{ input.session_id }})  ->  AI Image
```

The agent writes the brief and remembers the last one it wrote. Tell it that
every message after the first is a change to what it last briefed, and that it
must reply with the WHOLE brief rather than only the change, because the node
that renders has not seen the conversation. Then:

> A quote card that says: ship small, ship often. Deep purple.
>
> make it deep green instead, and add "Basivo" small at the bottom

produces the same card in green with the name added. The same shape works for
video, with the agent briefing AI Video.

Worth being plain about what this is: each message renders a NEW file from an
updated brief. Nothing edits the previous picture pixel by pixel, so a change
can move other things too. For a poster or a short intro that is usually what
people want; for "keep everything and nudge this one word", say so in the
message and the brief carries it.

## What comes back

The reply is whatever the last node returns: `text`, `reply`, `message` or
`answer` if it is an object, the string itself if it is one. An AI Agent and
Generate with LLM both return `text`, so a flow of Chat to Agent needs no wiring
at all.

A run that fails answers "Something went wrong answering that." The flow's own
error is not shown to the visitor: it names repositories, models and
credentials, and it is written for the person who built the flow. The real
error is on the run page as usual.

## What the visitor sees while it thinks

An agent can take a minute. A blank window for a minute reads as broken, so
the window shows the steps as they happen — which node is running, which model
is thinking, which tools it called, how long each took — and keeps them behind
a disclosure under the finished answer.

Only the shape of the work is shown, never its content: node names, node
types, status, duration, the model's name, the names of tools called. No
prompts, no outputs, no credentials, no repository names. Turn the whole thing
off with **Show what the flow is doing** on the Chat node when the machinery is
nobody else's business.

## The link

```
https://console.basivo.in/chat/{flow_id}/{token}
```

The token is an HMAC of the flow id under the deployment's `SECRET_KEY`, the
same way the Telegram webhook secret works: no column, no migration, nothing
in an exported graph. Two consequences worth knowing.

**The link is the credential.** Anyone holding it can talk to the flow, and
every message is a run you pay for. Send it the way you would send an
unlisted URL.

**Rotating `SECRET_KEY` invalidates every chat link at once**, which is the
correct blast radius for a compromised deployment key and a nuisance
otherwise.

## Why it polls

The page sends a message, then asks for the answer every 700ms, backing off to
three seconds as a run gets long. These endpoints are the only ones a stranger
can reach without an API key, and a stream held open per visitor is a cheap
way to occupy the server. A reply takes seconds; a poll costs a row read.

## Limits

- 20 messages a minute per address, and 240 polls. Generous for a person,
  useless for a script.
- A run has three minutes to answer before the page gives up on it. The run
  itself carries on and is visible on the run page.
- The window answers from the PUBLISHED version. Saving is not publishing:
  the editor says "Saved. v2 is live" when they differ, and so does the Chat
  node's panel.
- The window keeps no transcript. A reload starts an empty window; the agent
  still remembers, because its memory is on the server and keyed by session.
