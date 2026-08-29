# Vendor-agnostic backlog (seeds the Paperclip board — 2026-08-29)

Swept 2026-08-29 with the full-tree host-literal grep (broader than the QA suite's
section-2 check, which covers `tooling/ config/ stacks/ ansible/` only). The tree is
already grep-clean except deliberate meta-references; what remains is semantic work.

## From the surviving Paperclip board (re-dispatch on restore)

- **BUC-7 — provisioning / IaC** (Role 2): finish `bootstrap.sh` → Ansible path against a
  clean target, not the reference host.
- **BUC-8 — adapters / tooling** (Role 3): canonical secrets manifest ownership, adapter
  completeness (notifier beyond telegram/slack/stdout, firewall beyond none/fortigate).
- **BUC-9 — QA hardening**: preflight fail-closed matrix, `none`-fallback matrix,
  CI secret scan (CI half **done 2026-08-29** — `.github/workflows/ci.yml`).
- **BUC-6 — deploy runbook**: written for a stranger with a fresh VM, validated by them.

## De-hostify items found in this sweep

- `docs/PROVISIONING.md` §11 "buckserver dogfood" — reference-host section; fine in a
  private repo, must be generalized or moved out before any public flip.
- `scripts/secret-scan.py` allows example configs to carry the reference host's
  non-secret facts as documentation — revisit that exemption before going public.
- ~~`PIHOLE_WEBPASSWORD` has no `optional_when`~~ — **fixed 2026-08-29 (BUC-16)**: the key is
  now `optional_when: "dns.adapter != pihole"`, and the core stack renders the pihole service
  + its secret mount only for `dns.adapter == pihole`, so the waiver matches what is deployed.
- Socket-proxy behavior **verified 2026-08-29**: on `lscr.io/linuxserver/socket-proxy`,
  `ALLOW_*` flags are additive allows evaluated before the `POST` rule —
  `POST=0 + ALLOW_RESTARTS=1` yields restart-only (204 restart / 403 create, tested
  live). `stacks/core/images.yaml`'s claim is correct as shipped.

## Definition of done (unchanged from the ship plan)

The bundle stands up on a machine that is not the reference host from nothing but
`git clone` + a filled `site.yaml`. Until that passes, it is not deployable elsewhere,
whatever the code says.
