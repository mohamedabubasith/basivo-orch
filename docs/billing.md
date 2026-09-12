# Billing

## The switch

`BILLING_MODE` in `deploy/.env` decides whether money is real.

| Value | What happens |
|---|---|
| `demo` (default) | The plans on the billing page are a preview. No limit is enforced anywhere, the payment provider is never called, checkout and the customer portal answer 400, and `POST /billing/webhook` answers 404. |
| `production` | Every workspace starts on Free and its limits apply. Paying moves the workspace onto a bigger plan. |

Production mode requires `DODO_API_KEY`, `DODO_WEBHOOK_SECRET`,
`DODO_PRODUCT_PRO` and `DODO_PRODUCT_TEAM`. Without them the service refuses to
start, rather than looking healthy until the first customer tries to pay.

The switch is deliberately separate from `ENVIRONMENT`: a staging box runs
`ENVIRONMENT=production` for cookie and TLS strictness while having no payment
provider behind it.

## The provider

[Dodo Payments](https://dodopayments.com) is a merchant of record. It is the
legal seller, so it charges GST in India and VAT elsewhere, issues the invoice,
and pays out to an Indian bank account. None of that tax handling is in this
repository, which is the reason it was chosen over a gateway. Stripe is not an
option: Stripe India has been invite only since May 2024.

Two calls go out (`billing/provider.py`):

- `POST /checkouts` starts a hosted checkout. The workspace id travels in
  `metadata`, and that is what every later event is matched on. Never the
  payer's email: people pay with a different address than they sign up with.
- `POST /customers/{id}/customer-portal/session` returns the page where a
  customer changes a card, downloads invoices, or cancels.

Deliveries arrive at `POST /billing/webhook`, signed with the Standard Webhooks
scheme (`webhook-id`, `webhook-timestamp`, `webhook-signature`). The signature
is checked over the RAW body, and a delivery more than five minutes old is
refused even when its signature is valid.

## What a plan controls

| Limit | Where it is enforced |
|---|---|
| Runs per month | `flows.service.create_run`, so all four routes, the webhook path and the schedule ticker share one check |
| Apps | `appbuilder.service.create_project`, before the flow and the address are written |
| Storage | `FlowEngine._save_artifact` and the app builder's upload route, the two places bytes are written. Counted over every artifact and every uploaded image the workspace holds |
| Flows | `flows.service.create_flow`, so the template installer cannot get past it |
| Members | `auth/routers/orgs.py::invite_member` |
| Run history | `flows.service.list_runs` filters the window. Nothing is deleted |

Going over answers `402` with a plain sentence, handled once in `main.py`.

The plan in force is COMPUTED, never stored: a grace period that runs out or a
cancelled period that ends changes the answer on the next request. There is no
job to run, so no job whose failure hands out a free upgrade.

## Lifecycle

| Event | Result |
|---|---|
| `subscription.active`, `.renewed`, `.plan_changed` | active on that plan, grace cleared |
| `payment.failed`, `subscription.on_hold`, `.failed` | past due, plan kept for `BILLING_GRACE_DAYS` |
| `subscription.cancelled` | cancelled, plan runs to the end of the paid period |
| `subscription.expired` | back to Free |

Every delivery is recorded in `billing_event` by the provider's own delivery id,
so a retry inserts nothing and changes nothing. An event older than the last one
applied is ignored, because providers do reorder.

## Prices

Prices ship in `billing/plans.py` and can be edited from the admin screen, which
writes a `plan_override` row. The merged catalogue is cached in each process for
30 seconds.

A price edited here is what the console SHOWS. What a card is charged lives with
the provider, so a price change means creating the new product there and putting
its id in the same edit.

## Platform staff

`is_superuser` on the user row. Granted locally, never from the product:

```bash
cd apps/api && uv run python -m basivo_orch.manage staff you@example.com
```

Staff get `/api/v1/admin/*`: the plan editor, product-wide counts, grouped
errors, and the workspace list. A caller who is not staff gets 404, not 403.
