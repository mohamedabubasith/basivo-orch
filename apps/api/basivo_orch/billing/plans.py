"""The plan catalogue.

One table, read by the API, the UI and the webhook handler alike. A limit that
lived in two places would eventually disagree with itself, and the version a
customer sees is not the one that would be enforced.

``None`` means unlimited. Prices are display strings: the provider holds the
real price, and a number typed here would be the one that is wrong after the
first price change.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Plan:
    code: str
    name: str
    tagline: str
    price_inr: str
    price_usd: str
    #: Runs a workspace may start per calendar month. None is unlimited.
    runs_per_month: int | None
    #: Flows a workspace may keep. None is unlimited.
    flows: int | None
    #: Apps under the App Builder. Counted apart from flows: an app is worth
    #: more to run than a flow (a coding agent and a compiler per message) and
    #: is worth more to keep (every version is a stored build).
    apps: int | None
    #: Members, including the owner.
    seats: int | None
    #: How far back the run history is listed. Nothing is deleted.
    history_days: int
    #: Everything a workspace has stored: rendered files, app builds and the
    #: images uploaded to them. None is unlimited. Disk is the one resource a
    #: deployment cannot overcommit, so this limit is not about revenue: a
    #: workspace that fills the disk takes every other workspace down with it.
    storage_mb: int | None
    features: tuple[str, ...]

    @property
    def is_free(self) -> bool:
        return self.code == FREE


FREE = "free"
PRO = "pro"
TEAM = "team"

#: The plan a workspace has until it subscribes, and the one it falls back to
#: when a subscription lapses. Never zero runs: an account that cannot run
#: anything at all cannot evaluate the product either.
PLANS: dict[str, Plan] = {
    FREE: Plan(
        code=FREE,
        name="Free",
        tagline="Build a pipeline and see it work.",
        price_inr="₹0",
        price_usd="$0",
        runs_per_month=100,
        flows=3,
        apps=2,
        seats=1,
        history_days=7,
        storage_mb=100,
        features=(
            "100 runs a month",
            "3 flows and 2 apps",
            "GitHub and Jira triggers",
            "Bring your own model keys",
            "100 MB of files and apps",
            "7 days of run history",
        ),
    ),
    PRO: Plan(
        code=PRO,
        name="Pro",
        tagline="For one person shipping real work.",
        price_inr="₹2,499",
        price_usd="$29",
        runs_per_month=3_000,
        flows=None,
        apps=20,
        seats=3,
        history_days=30,
        storage_mb=1_000,
        features=(
            "3,000 runs a month",
            "Unlimited flows, 20 apps",
            "3 members",
            "1 GB of files and apps",
            "30 days of run history",
            "Email support",
        ),
    ),
    TEAM: Plan(
        code=TEAM,
        name="Team",
        tagline="For a team running pipelines in production.",
        price_inr="₹8,999",
        price_usd="$99",
        runs_per_month=15_000,
        flows=None,
        apps=100,
        seats=10,
        history_days=90,
        storage_mb=5_000,
        features=(
            "15,000 runs a month",
            "Unlimited flows, 100 apps",
            "10 members",
            "5 GB of files and apps",
            "90 days of run history",
            "Priority support",
        ),
    ),
}

#: Order shown on the billing page, cheapest first.
PLAN_ORDER: tuple[str, ...] = (FREE, PRO, TEAM)

#: The plans that need a payment. Everything here must have a provider product
#: id configured before the deployment may run in production mode.
PAID_PLANS: tuple[str, ...] = (PRO, TEAM)

#: What a workspace gets while billing is switched off, and what the demo
#: deployment shows. Deliberately not one of the sellable plans: nothing here
#: should ever look like something that was paid for.
UNLIMITED = Plan(
    code="unlimited",
    name="Unlimited",
    tagline="Billing is turned off in this deployment.",
    price_inr="₹0",
    price_usd="$0",
    runs_per_month=None,
    flows=None,
    apps=None,
    seats=None,
    history_days=3_650,
    storage_mb=None,
    features=("Every feature, no limits, nothing charged",),
)


def plan_or_free(code: str | None) -> Plan:
    """The named plan, or Free when it is unknown.

    A plan code that no longer exists (a tier we retired, a row written by an
    older version) must not lock a workspace out of its own data. It falls back
    to the free limits, which is the same place a lapsed subscription lands.
    """
    if not code:
        return PLANS[FREE]
    return PLANS.get(code, PLANS[FREE])
