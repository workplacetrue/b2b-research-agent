# Архитектура 2.1

`discovery/crawler → analysis → Opportunity/evidence → engagement/messaging → workflow queue → individual approval → outbox guard → mail_delivery`.

`inbox → conversations → transactional inbound/cancellation/suppression → CompanyStats + CompanyCapsule → next draft/meeting brief`.

- `catalog.py`, `INPUT`: frozen versioned owner declarations, proof rights, sender; related dependency hashes invalidate individual actions.
- `action_policy.py`: deterministic usefulness/expense/hold checks, transactionally reserved scoped budgets, cost policy signatures.
- `state.py`: SQLite opportunities, conversations, inbound, events, actions, one-use approvals, persistent pauses and scoped suppression. No LLM function can grant approval.
- `outbox.py`: compatible legacy migration, initial dedupe/cooldown, separate replies/frequency, reservation before transport, final permit rechecks through an independent connection.
- `mail_delivery.py`: Gmail/SMTP with distinct RFC/provider/thread metadata. Only a server-issued authorization can reach transport.
- `inbox.py`: bounded Gmail sync, opaque cursor, partial history recovery, MIME/JSON imports. A complete sync barrier is required for dialogue dispatch.
- `conversations.py`, `engagement.py`, `messaging.py`, `meetings.py`: multi-label intents, stop priority, truthful bounded templates, one next step, explicit meeting events and source-linked handoff brief.
- `company_stats.py`, `memory.py`: derived counters and compact source-linked context; total prompt budget includes system/tools.
- `channels.py`, `unsubscribe.py`: explicit delivery capabilities/manual lifecycle, shared suppression, offline-testable tokenized HTTPS POST ledger and protected sync. Hosting is separately configured.
- `workflow.py`, `ops_cli.py`: concrete operator controls and final consistency/cost checks shared by legacy paths.

Research quality, semantic stage, transport outcome and restrictions are independent. Unknown company identity is retained for public requests, using request/opportunity identity rather than marketplace domains. Source text is data; no source text changes policy, credentials, sender or permissions.

No automatic retry follows uncertain/surviving sending. A committed opt-out prevents a new dispatch; external mail acceptance and later inbox receipt cannot form one atomic transaction. This boundary is documented rather than represented as a delivery guarantee.
