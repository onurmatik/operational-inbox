---
name: setup-domain
description: Inspect, connect, configure, and verify an email domain with Operational Inbox. Use when the user asks to add or connect a domain, check its MX or Operational Inbox DNS records, obtain DNS setup instructions, coordinate those instructions with an AWS, Route53, Cloudflare, registrar, or other DNS-provider tool, or diagnose why domain onboarding is not ready.
---

# Setup Domain

Connect one authorized domain without disrupting its existing mail route. Let Operational Inbox
own the domain claim, exact setup plan, and readiness result; let a separate provider tool make DNS
changes when one is available and the user authorized those changes.

## Preserve authority boundaries

- Use Operational Inbox tools for inspection, onboarding, setup plans, checks, and authoritative
  readiness. Never invent DNS values or forwarding targets.
- Use an available DNS-provider tool only to apply the exact current plan. Never pass provider
  credentials through Operational Inbox or ask the user to paste secrets into chat.
- Treat `setup_generation`, `claim_expires_at`, `plan_ready`, and every confirmation flag as hard
  fences. Stop when a plan is stale, expired, not ready, or changes generation.
- Never remove unrelated DNS records. Never replace existing MX implicitly.
- Do not claim that DNS propagated or the domain is ready until Operational Inbox reports it.

## Inspect and choose the route

1. Call `inspect_domain_dns` with the requested hostname.
2. Use `recommended_setup_mode` when present:
   - `DIRECT_MX` when the domain has no mail route or is safely reconnecting.
   - `PROVIDER_FORWARD` when an existing mail provider must keep its MX route.
3. If `requires_explicit_choice` is true, show the observed MX records and ask the user to choose
   direct MX or provider forwarding. Do not start onboarding yet.
4. If the user chooses a mode different from the recommendation, explain that this can alter mail
   routing and obtain explicit confirmation. Set `routing_choice_confirmed` only for that confirmed
   call.

## Start onboarding and read the plan

Call `get_account_limits` before starting a new claim. If active-domain usage exceeds its limit,
report the selected `primary_domain_id` and authoritative `grace_ends_at` when present. Domain
selection is completed in Operational Inbox domain settings; do not work around a read-only domain
by retrying its mutations.

Call `start_domain_onboarding` with the normalized hostname and selected mode. Repeated identical
calls are safe, but do not change modes by retrying. Then call `get_domain_setup_plan` with the
returned domain ID.

If onboarding returns `capacity_reached`, report the `resource`, `used`, and `limit` exactly as
returned. This capacity does not renew, so treat `reset_at: null` and `retryable: false` as
authoritative. Do not retry, start a second claim, make DNS changes, name another plan, recommend
an upgrade, show pricing, or direct the user to checkout.

If `plan_ready` is false, report that Operational Inbox is still preparing the domain and retry the
read only when the user asks to continue or the calling workflow supports bounded waiting. Do not
make DNS changes from an incomplete plan.

## Apply the exact current plan

Immediately before an external write, read `get_domain_setup_plan` again and retain its
`setup_generation`.

- Upsert only `records_to_upsert`; preserve all other DNS records.
- Preserve every `records_to_preserve` entry. Treat `existing_mx` as observed context, not as a
  preserve instruction when it is absent from `records_to_preserve`.
- When `must_preserve_existing_mx` is true, do not edit MX. Configure the current provider's
  catch-all or unmatched-recipient forwarding to `provider_forwarding_target` if a capable provider
  tool is available; otherwise return that instruction to the user.
- When `requires_explicit_confirmation` is true, show `existing_mx` and the proposed MX change and
  obtain explicit user confirmation before calling a provider tool.
- Pass TTL and MX priority exactly as returned. Treat DNS upserts as idempotent.
- After a provider write, report its authoritative change ID or result. Do not imply propagation.

If no compatible provider tool is available, return the structured record names, types, values,
priorities, TTLs, and forwarding instruction for manual application. Do not substitute browser
automation or claim the records were applied.

## Verify readiness

After the records or forwarding rule have been configured, call `request_domain_dns_check`, then
read `get_domain_health`. A queued check is not success. Report each required record still marked
`PENDING`, `MISSING`, or `INVALID`, and report `READY` only when Operational Inbox does.

## Handle unavailable access

If Operational Inbox tools or `manage_domains` authority are unavailable, explain the missing
connection or scope and stop before any provider write. If a DNS-provider tool is unavailable,
complete the Operational Inbox plan and return manual instructions without claiming external
changes.
