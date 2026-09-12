/**
 * The pages a payment provider, and a careful customer, look for before they
 * trust you with money: what it costs, what you promise, what you do with
 * their data, how they get their money back, and how they reach a person.
 *
 * They are deliberately plain. Nothing here is padded to look legal, because
 * the version somebody actually reads is the one that helps them, and the
 * version nobody reads is the one that hides a surprise.
 *
 * **Every fact on these pages is checked against the code.** The plan limits
 * come from `billing/plans.py`, the storage numbers from the quota checks, and
 * the sentence about the free coding agent from what its provider publishes.
 * A pricing page that disagrees with what the service enforces is worse than
 * no pricing page: it is a promise the software will break.
 */

import { Link } from "react-router-dom";
import type { ReactNode } from "react";

import { Button } from "../components/ui";
import { Footer, Nav } from "../components/landing/Sections";
import { consoleOrigin } from "../lib/consoleOrigin";

/**
 * Who is selling, and where a customer writes.
 *
 * One place, because it appears on five pages and a business address that is
 * right on four of them is wrong everywhere. Fill `legalName` and `address`
 * once the business is registered: until then the pages say what is true,
 * which is that support is by email.
 */
const BUSINESS = {
  brand: "Basivo",
  legalName: "Basivo",
  email: "support@basivo.in",
  /** Postal address. Leave empty until there is a registered one to give. */
  address: "",
  country: "India",
  site: "orch.basivo.in",
  console: "console.basivo.in",
};

/** When the wording last changed. Update it when you change the words. */
const UPDATED = "12 September 2026";

function Page({
  title,
  lead,
  children,
}: {
  title: string;
  lead?: string;
  children: ReactNode;
}) {
  return (
    <>
      <Nav />
      <main className="mx-auto max-w-3xl px-5 pb-24 pt-28">
        <h1 className="text-3xl font-semibold tracking-tight text-ink-50 sm:text-4xl">
          {title}
        </h1>
        {lead && <p className="mt-3 text-lg text-ink-300">{lead}</p>}
        <p className="mt-2 text-sm text-ink-500">Last updated {UPDATED}</p>
        <div className="mt-10 space-y-8">{children}</div>
      </main>
      <Footer />
    </>
  );
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="space-y-3">
      <h2 className="text-lg font-medium text-ink-100">{title}</h2>
      <div className="space-y-3 text-ink-300 [&_a]:text-brand-300 [&_a:hover]:text-brand-200 [&_li]:ml-5 [&_li]:list-disc">
        {children}
      </div>
    </section>
  );
}

function Mail() {
  return <a href={`mailto:${BUSINESS.email}`}>{BUSINESS.email}</a>;
}

/* ------------------------------------------------------------- pricing --- */

/**
 * The plans, exactly as `billing/plans.py` enforces them.
 *
 * Kept in step by hand and checked by a test, rather than fetched: the plans
 * endpoint needs a session, and a pricing page that asks a visitor to sign in
 * before it will say the price is not a pricing page.
 */
const PLANS = [
  {
    code: "free",
    name: "Free",
    tagline: "Build a pipeline and see it work.",
    inr: "₹0",
    usd: "$0",
    features: [
      "100 runs a month",
      "3 flows and 2 apps",
      "GitHub and Jira triggers",
      "The free coding agent, no key needed",
      "Bring your own model keys",
      "100 MB of files and apps",
      "7 days of run history",
    ],
  },
  {
    code: "pro",
    name: "Pro",
    tagline: "For one person shipping real work.",
    inr: "₹2,499",
    usd: "$29",
    featured: true,
    features: [
      "3,000 runs a month",
      "Unlimited flows, 20 apps",
      "3 members",
      "1 GB of files and apps",
      "30 days of run history",
      "Email support",
    ],
  },
  {
    code: "team",
    name: "Team",
    tagline: "For a team running pipelines in production.",
    inr: "₹8,999",
    usd: "$99",
    features: [
      "15,000 runs a month",
      "Unlimited flows, 100 apps",
      "10 members",
      "5 GB of files and apps",
      "90 days of run history",
      "Priority support",
    ],
  },
];

export function Pricing() {
  const origin = consoleOrigin();
  const register = origin ? `${origin}/register` : "/register";

  return (
    <Page
      title="Pricing"
      lead="Start free. Upgrade when a limit is in your way, not before."
    >
      <div className="grid gap-4 sm:grid-cols-3">
        {PLANS.map((plan) => (
          <div
            key={plan.code}
            className={`flex flex-col rounded-2xl border p-5 ${
              plan.featured
                ? "border-brand-400/60 bg-brand-500/5"
                : "border-ink-700/70 bg-ink-900/40"
            }`}
          >
            <p className="text-sm font-medium text-ink-100">{plan.name}</p>
            <p className="mt-1 text-sm text-ink-400">{plan.tagline}</p>
            <p className="mt-4 text-2xl font-semibold text-ink-50">
              {plan.inr}
              <span className="text-sm font-normal text-ink-500">
                {" "}
                a month
              </span>
            </p>
            <p className="text-sm text-ink-500">{plan.usd} outside India</p>
            <ul className="mt-4 flex-1 space-y-1.5 text-sm text-ink-300">
              {plan.features.map((line) => (
                <li key={line}>{line}</li>
              ))}
            </ul>
            <a href={register} className="mt-5">
              <Button
                variant={plan.featured ? "primary" : "ghost"}
                className="w-full"
              >
                {plan.code === "free" ? "Start free" : `Choose ${plan.name}`}
              </Button>
            </a>
          </div>
        ))}
      </div>

      <Section title="What a run is">
        <p>
          One execution of one flow, from its trigger to its last node, however
          many nodes it passes through. A flow that runs on a schedule every
          hour uses 24 runs a day. One message to an app in the App Builder is
          also one run.
        </p>
      </Section>

      <Section title="What models cost">
        <p>
          Nothing extra from us. The free coding agent is included on every
          plan, including Free, and needs no key of your own. For the agent
          nodes you connect your own Anthropic, OpenAI or other provider key and
          pay that provider directly, at their price. We never add a margin to a
          model call, and we never see your key after you save it: it is
          encrypted, and it is sent to the provider you chose and to nobody
          else.
        </p>
      </Section>

      <Section title="Taxes, currency and billing">
        <p>
          Prices are shown without tax. Payments are processed by Dodo Payments,
          which is the merchant of record and therefore the seller: they add GST
          or VAT at the rate for your country, issue the invoice, and handle the
          payment. You can pay in Indian rupees or in US dollars, by card or by
          UPI, and the subscription renews monthly until you cancel it.
        </p>
        <p>
          Cancelling, and getting your money back, are described in the{" "}
          <Link to="/refunds">refund and cancellation policy</Link>.
        </p>
      </Section>

      <Section title="If you go over a limit">
        <p>
          Nothing is deleted and nothing is charged without you asking. A run
          over the monthly count is refused with a message saying so, a flow or
          app over the count cannot be created, and a file that would take the
          workspace over its storage is refused before it is written. Everything
          you already have keeps working, and upgrading lifts the limit at once.
        </p>
      </Section>
    </Page>
  );
}

/* --------------------------------------------------------------- terms --- */

export function Terms() {
  return (
    <Page
      title="Terms of service"
      lead={`The agreement between you and ${BUSINESS.brand} when you use the service at ${BUSINESS.console}.`}
    >
      <Section title="1. Who we are and what this is">
        <p>
          {BUSINESS.legalName === BUSINESS.brand
            ? BUSINESS.brand
            : `${BUSINESS.legalName}, which operates ${BUSINESS.brand},`}{" "}
          is a workflow automation service: you draw a pipeline, and the service
          runs it. By creating an account you accept these terms. If you do not
          accept them, do not use the service.
        </p>
      </Section>

      <Section title="2. Your account">
        <p>
          You must be 18 or older. You are responsible for what happens in your
          workspace, for the people you invite into it, and for the credentials
          you store in it. Tell us at once, at <Mail />, if you think somebody
          else has access to your account.
        </p>
      </Section>

      <Section title="3. Plans, payment and renewal">
        <p>
          Paid plans are billed monthly in advance through Dodo Payments, which
          is the merchant of record and the seller of record for every
          transaction. A subscription renews automatically until you cancel it,
          and cancelling stops the next renewal rather than the current period.
          If we change a price, you will be told at least 30 days before it
          applies to you.
        </p>
      </Section>

      <Section title="4. Fair use">
        <p>
          Each plan carries limits on runs, flows, apps, members and storage,
          published on the <Link to="/pricing">pricing page</Link> and enforced
          by the service. We may throttle or suspend a workspace that is using
          the service in a way that damages it for other people, such as mining
          cryptocurrency, running an open proxy, or automating abuse of a third
          party. Where the situation allows, we will write to you first.
        </p>
      </Section>

      <Section title="5. Your content stays yours">
        <p>
          Your flows, your credentials, your run history and the applications
          you build belong to you. You give us only the permission needed to run
          the service for you: to store that content, to process it, and to send
          the parts you direct us to send to the providers you chose. You can
          download an app's full source at any time from the version rail, and
          you can ask us for a copy of everything else.
        </p>
        <p>
          An application you deploy is published at a public address, which
          means anyone holding the link can open it. Unpublishing takes it down
          at once.
        </p>
      </Section>

      <Section title="6. What an AI agent produces">
        <p>
          Agents write code, text and files from your instructions, and they are
          sometimes wrong. Review anything they produce before you rely on it,
          and especially before you merge it, deploy it, or send it to a
          customer. The service is a tool you direct, not professional advice.
        </p>
        <p>
          The free coding agent runs on a model that its provider offers at no
          charge, and that provider states that data collected during its free
          period may be used to improve the model. Do not send confidential or
          personal information through the free agent. Connect your own model
          key for work that must stay private, and the request goes to that
          provider under your agreement with them instead.
        </p>
      </Section>

      <Section title="7. What you may not do">
        <p>You may not use the service to</p>
        <ul className="space-y-1.5">
          <li>break the law, or help somebody else break it,</li>
          <li>
            build or distribute malware, phishing pages, or anything designed to
            impersonate a real person or organisation,
          </li>
          <li>
            publish adult content, gambling, or anything a payment provider
            would refuse,
          </li>
          <li>infringe somebody else's copyright, trademark or privacy,</li>
          <li>
            attack the service itself, including attempts to escape the sandbox
            an agent runs in, to reach another workspace's data, or to defeat a
            plan limit.
          </li>
        </ul>
        <p>
          We may suspend or close an account that does any of these, and we will
          say why.
        </p>
      </Section>

      <Section title="8. Availability">
        <p>
          This is beta software on a small deployment. We do not offer an uptime
          guarantee, we may change or remove a feature, and maintenance can make
          the service briefly unavailable. We will not change something you
          depend on without notice where we can avoid it.
        </p>
      </Section>

      <Section title="9. Ending the agreement">
        <p>
          You may close your account at any time. We may close yours for a
          serious or repeated breach of these terms, or if we stop offering the
          service, in which case a paid period is refunded in proportion. When
          an account is closed its data is deleted within 30 days, so export
          what you want to keep first.
        </p>
      </Section>

      <Section title="10. Liability">
        <p>
          Nothing here limits liability that cannot be limited by law. Subject
          to that, neither side is liable for indirect or consequential loss,
          including lost profit or lost data, and our total liability for any
          claim is limited to the fees you paid in the three months before it
          arose. The service is provided as it is, without warranties beyond
          those the law requires.
        </p>
      </Section>

      <Section title="11. Law">
        <p>
          These terms are governed by the laws of {BUSINESS.country}, and the
          courts there have jurisdiction over any dispute. We will always try to
          settle one by email first.
        </p>
      </Section>

      <Section title="12. Changes">
        <p>
          We may update these terms. A material change is announced by email to
          workspace owners at least 30 days before it takes effect, and the date
          at the top of this page always says when the wording last changed.
        </p>
      </Section>

      <Section title="Contact">
        <p>
          Questions about these terms go to <Mail />.
        </p>
      </Section>
    </Page>
  );
}

/* ------------------------------------------------------------- privacy --- */

export function Privacy() {
  return (
    <Page
      title="Privacy policy"
      lead="What we hold, why we hold it, and who else ever sees it."
    >
      <Section title="What we collect">
        <ul className="space-y-1.5">
          <li>
            <strong className="text-ink-200">Your account.</strong> Name, email
            address, and a password we store only as a hash. If you sign in with
            Google or GitHub we receive your email and name from them.
          </li>
          <li>
            <strong className="text-ink-200">Your workspace.</strong> The flows
            you draw, the runs they produce, the applications you build, the
            images you upload to them, and the credentials you save, which are
            encrypted at rest.
          </li>
          <li>
            <strong className="text-ink-200">Technical records.</strong> IP
            address, browser, and timestamps for sign in attempts and API
            requests, kept so that we can investigate abuse and keep accounts
            safe.
          </li>
          <li>
            <strong className="text-ink-200">Payment records.</strong> Your
            plan, your subscription status, and the invoice identifiers Dodo
            Payments sends us. Card numbers never reach our servers.
          </li>
        </ul>
      </Section>

      <Section title="Why we hold it">
        <p>
          To run the service you asked for, to bill you, to answer your support
          messages, and to keep the platform safe. We do not sell personal data,
          we do not advertise, and we do not profile you.
        </p>
      </Section>

      <Section title="Who else sees it">
        <ul className="space-y-1.5">
          <li>
            <strong className="text-ink-200">Dodo Payments</strong> processes
            payments as the merchant of record and receives what a payment
            needs.
          </li>
          <li>
            <strong className="text-ink-200">The model provider you chose</strong>{" "}
            receives the content of the prompts and files your agents send it.
            With your own key that is your agreement with them. With the free
            coding agent it is the provider of that free model, who states that
            data collected during its free period may be used to improve the
            model, which is why the free agent should not carry confidential
            work.
          </li>
          <li>
            <strong className="text-ink-200">
              The services you connect yourself,
            </strong>{" "}
            such as GitHub, Jira, Slack or an email webhook, receive what your
            flows send them.
          </li>
          <li>
            <strong className="text-ink-200">Our hosting provider</strong> holds
            the encrypted disk the service runs on.
          </li>
        </ul>
        <p>
          That is the whole list. We will add to it only with notice on this
          page.
        </p>
      </Section>

      <Section title="Cookies">
        <p>
          Session and security cookies only: one that keeps you signed in, one
          that protects forms against cross site requests. No advertising
          cookies and no third party analytics.
        </p>
      </Section>

      <Section title="How long we keep it">
        <p>
          For as long as your account exists. Run history older than your plan's
          window stops being listed but is not deleted. When you close an
          account, everything in it is deleted within 30 days, except records we
          are required to keep for tax and accounting.
        </p>
      </Section>

      <Section title="Your rights">
        <p>
          Write to <Mail /> and you can have a copy of your data, a correction,
          or its deletion. We answer within 30 days. If we ever hold something
          you did not expect, tell us and we will remove it.
        </p>
      </Section>

      <Section title="Children">
        <p>The service is not for anyone under 18.</p>
      </Section>
    </Page>
  );
}

/* ------------------------------------------------------------- refunds --- */

export function Refunds() {
  const origin = consoleOrigin();
  const billing = origin ? `${origin}/app/billing` : "/app/billing";

  return (
    <Page
      title="Refund and cancellation policy"
      lead="Cancel in two clicks. If the service did not work for you, ask and you get your money back."
    >
      <Section title="Cancelling">
        <p>
          Open <a href={billing}>Billing</a> in the console and choose Manage
          subscription. Cancelling stops the next renewal and leaves your
          workspace on its paid plan until the end of the period you have
          already paid for. Nothing is deleted: the workspace moves to the Free
          plan and its limits, and everything over those limits stays readable
          and downloadable.
        </p>
        <p>The Free plan needs no cancellation and never charges anything.</p>
      </Section>

      <Section title="Refunds">
        <ul className="space-y-1.5">
          <li>
            <strong className="text-ink-200">
              Within 14 days of your first payment
            </strong>{" "}
            on a new subscription, for any reason, we refund it in full.
          </li>
          <li>
            <strong className="text-ink-200">A duplicate or accidental
            charge</strong> is refunded in full, whenever you notice it.
          </li>
          <li>
            <strong className="text-ink-200">A failure on our side</strong> that
            left you unable to use the service for a meaningful part of a
            billing period is refunded in proportion, and we would rather you
            asked than went without.
          </li>
          <li>
            Beyond that, a period already started is not refunded in part, since
            the plan's runs and storage are available to you throughout it.
          </li>
        </ul>
      </Section>

      <Section title="How to ask">
        <p>
          Email <Mail /> from the address on the account, with the invoice
          number if you have it. We reply within 2 business days. An approved
          refund is issued by Dodo Payments to the original payment method and
          usually appears within 5 to 10 business days, depending on your bank.
        </p>
        <p>
          Please write to us before opening a dispute with your bank. A dispute
          takes weeks and we can usually settle it the same day.
        </p>
      </Section>
    </Page>
  );
}

/* ------------------------------------------------------------- contact --- */

export function Contact() {
  return (
    <Page
      title="Contact"
      lead="One address, answered by the person who builds it."
    >
      <Section title="Support and everything else">
        <p>
          <Mail />. Support, billing questions, refunds, privacy requests and
          security reports all arrive here, and we reply within 2 business days.
          For a security issue please write first and give us a chance to fix it
          before it is public.
        </p>
      </Section>

      <Section title="Bugs and the source">
        <p>
          The code is open:{" "}
          <a
            href="https://github.com/mohamedabubasith/basivo-orch"
            target="_blank"
            rel="noreferrer"
          >
            github.com/mohamedabubasith/basivo-orch
          </a>
          . A reproducible bug is most useful as an issue there.
        </p>
      </Section>

      <Section title="Business details">
        <p>
          {BUSINESS.legalName}, {BUSINESS.country}. The service runs at{" "}
          {BUSINESS.console} and this site is {BUSINESS.site}.
        </p>
        {BUSINESS.address ? (
          <p className="whitespace-pre-line">{BUSINESS.address}</p>
        ) : (
          <p>A postal address is available on request by email.</p>
        )}
        <p>
          Payments are processed by Dodo Payments as merchant of record, and
          your card statement will show their name.
        </p>
      </Section>
    </Page>
  );
}
