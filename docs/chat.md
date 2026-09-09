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

## What comes back

The reply is whatever the last node returns: `text`, `reply`, `message` or
`answer` if it is an object, the string itself if it is one. An AI Agent and
Write with AI both return `text`, so a flow of Chat to Agent needs no wiring
at all.

A run that fails answers "Something went wrong answering that." The flow's own
error is not shown to the visitor: it names repositories, models and
credentials, and it is written for the person who built the flow. The real
error is on the run page as usual.

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
- The window keeps no transcript. A reload starts an empty window; the agent
  still remembers, because its memory is on the server and keyed by session.
