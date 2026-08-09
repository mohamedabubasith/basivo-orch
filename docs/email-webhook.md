# Sending email through a webhook

This project does not send email itself. It POSTs each rendered message to a URL
you own, and something on the other end — n8n, Make, Zapier, an internal
service — does the sending.

The reason to want this: the sending mailbox can be an account this service
holds no credentials for. Connect Gmail to n8n over OAuth once, and all this
service ever knows is a URL.

## The one thing to understand first

**The payload contains password-reset and email-verification links.** Those are
credentials — anyone holding a reset link can take over the account it belongs
to, until it expires or is used.

So whatever receives this webhook is as security-critical as this service is.
Your automation platform, its execution history, and anyone who can read that
history can all see live reset links. n8n stores execution data by default;
consider turning that off for this workflow, or setting a short retention.

Two consequences are enforced rather than suggested. In staging or production
the service **will not start** unless:

- `EMAIL_WEBHOOK_URL` is `https://` — over plain HTTP the links are readable by
  anything on the path, which is account takeover for every message sent
- `EMAIL_WEBHOOK_SECRET` or `EMAIL_WEBHOOK_AUTH_HEADER` is set — see below

## Quickest path: import the workflow

`docs/n8n-workflow.json` in this project is ready to import — n8n → Workflows →
Import from File. It contains the webhook, the signature check, a Gmail node
and the response.

Three things to do after importing:

1. **Connect your Google account** on the Gmail node. This is the only place a
   mail credential exists; this service never holds one.
2. **Set `BASIVO_WEBHOOK_SECRET`** in n8n's environment to the
   `EMAIL_WEBHOOK_SECRET` from your `.env`. Do not paste it into the node — a
   secret in a workflow travels with every export of that workflow.
3. **Copy the production webhook URL** from the Webhook node into
   `EMAIL_WEBHOOK_URL`, then activate the workflow. The test URL only accepts
   one request per click of "Listen".

The imported workflow sets execution saving to `none` on success, on purpose:
n8n stores execution data by default, and that data would contain live
password-reset links.

## Configuration

```bash
EMAIL_WEBHOOK_URL=https://your-n8n.example.com/webhook/send-mail
EMAIL_WEBHOOK_SECRET=<generated for you in .env>
EMAIL_WEBHOOK_AUTH_HEADER=            # optional, sent as the Authorization header
EMAIL_WEBHOOK_TIMEOUT_SECONDS=10
```

## What gets sent

`POST` with `Content-Type: application/json`:

```json
{
  "from": {"email": "no-reply@example.com", "name": "Docchk"},
  "html": "<html>…</html>",
  "project": "Docchk",
  "subject": "Reset your password",
  "text": "…",
  "to": "person@example.com"
}
```

Headers:

| Header | |
| --- | --- |
| `X-Basivo-Timestamp` | Unix seconds, so you can reject an old capture |
| `X-Basivo-Signature` | `sha256=<hmac>` over `timestamp + "." + body` |
| `Authorization` | Only if `EMAIL_WEBHOOK_AUTH_HEADER` is set |

Redirects are **not** followed. A 302 would forward the body — including a live
reset link — to a host you never configured.

## Why the signature is not optional

An n8n webhook is a URL, and URLs leak: into browser history, a screenshot, a
workflow someone exported and shared. An unauthenticated one lets whoever finds
it send email **from your domain, with content of their choosing**. That is a
phishing kit with your branding on it, and your domain's reputation behind it.

The signature is what lets your workflow tell this service apart from whoever
found the link.

### Verifying it

The Code node in `n8n-workflow.json` already does this; the shape is below if
you are building it elsewhere.

Note the two ways to get the bytes. Hashing the **raw body** is the correct
approach and what the standard implementations do — but n8n only exposes it
when "Raw Body" is enabled on the Webhook node, and if it is not, n8n parses
the JSON and the original bytes are gone. So the sender writes a *canonical*
body — sorted keys, no spaces, real UTF-8 — which a receiver can rebuild from
the parsed object when the raw form is unavailable. The bundled workflow tries
the raw body first and falls back.

```javascript
const crypto = require('crypto');

const secret    = $env.BASIVO_WEBHOOK_SECRET;   // set in n8n, never inline
const item      = $input.first();
const headers   = item.json.headers || {};
const signature = headers['x-basivo-signature'] || '';
const timestamp = headers['x-basivo-timestamp'] || '';

// Reject anything older than five minutes, so a captured request cannot be
// replayed indefinitely.
if (Math.abs(Date.now() / 1000 - Number(timestamp)) > 300) {
  throw new Error('Stale request');
}

// Raw body if available, canonical rebuild otherwise.
const canonical = (v) => {
  if (Array.isArray(v)) return '[' + v.map(canonical).join(',') + ']';
  if (v && typeof v === 'object') {
    return '{' + Object.keys(v).sort()
      .map((k) => JSON.stringify(k) + ':' + canonical(v[k])).join(',') + '}';
  }
  return JSON.stringify(v);
};
const raw = typeof item.json.body === 'string'
  ? Buffer.from(item.json.body, 'utf8')
  : Buffer.from(canonical(item.json.body), 'utf8');

const expected = 'sha256=' + crypto
  .createHmac('sha256', secret)
  .update(Buffer.concat([Buffer.from(timestamp + '.', 'utf8'), raw]))
  .digest('hex');

// timingSafeEqual, not ===. A plain comparison exits at the first wrong byte,
// and the timing difference is enough to recover a signature one byte at a
// time given enough attempts.
const a = Buffer.from(signature);
const b = Buffer.from(expected);
if (a.length !== b.length || !crypto.timingSafeEqual(a, b)) {
  throw new Error('Bad signature');
}

return $input.all();
```

A thrown error returns a non-2xx, which this service logs as a delivery
failure — that is how a broken signature check becomes visible instead of
silently dropping every email.

If you would rather not write this at all, set `EMAIL_WEBHOOK_AUTH_HEADER` to a
random value and use n8n's built-in **Header Auth** credential. That is weaker
— a static token has no replay protection — but it takes a minute and is
enormously better than an open webhook.

## Sending the mail

A Gmail node after the check, with the message type set to HTML.

The bundled workflow's Code node returns the parsed email as the item, so the
fields are `{{ $json.to }}`, `{{ $json.subject }}` and
`{{ $json.html }}`. If your Code node passes the request through
untouched instead, they are under `{{ $json.body.to }}` and so on.

Use `{{ $json.text }}` as the plaintext alternative where the node
supports one — most spam filters prefer a multipart message.

## When delivery fails

A failed send is logged and returns `false`; it never raises into the request
that triggered it. A user who registers successfully but whose email bounces
still has an account, and the error is not surfaced to an unauthenticated
caller — a delivery error is infrastructure detail.

The practical consequence: **watch for `email_send_failed` in your logs.** If
the webhook is down, registration keeps succeeding and nobody can confirm their
address, and nothing else will tell you.

The failure log includes the exception, which for an HTTP error contains the
webhook URL. That is acceptable only because production requires the endpoint
to be authenticated — the URL alone is not a credential. It is another reason
not to run this with an open webhook.
