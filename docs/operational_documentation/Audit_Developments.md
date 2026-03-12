# Audit developments

## Current state

The audit log is now demonstrating the intended governance loop end-to-end:

- Assessment author creates an item
- Assessment author submits it for review
- Reviewer returns for changes with a required note
- Assessment author revises via immutable versioning (new_version)
  - change_note can explicitly reference the return note
- Assessment author re-submits
- Reviewer confirms
- Confirmed versions are durable; earlier confirmed versions are deprecated when a new one is confirmed

This provides a defensible chain of custody for authored artifacts.

## Planned (QOL) improvements (not urgent)

### Filtering

- Preset filters (e.g. "Assessments only", "Tasks only", "Workflows only")
- Time-window filters (e.g. last 24h / 7d)
- Actor filters (e.g. only reviewer actions)
- Action filters (submit/return/confirm/new_version)

### Reporting

- Review throughput metrics (submitted → confirmed, returned → resubmitted latency)
- "Items returned" report for triage
- "Items pending review" report by domain
- Export (CSV) of filtered audit results

### UX

- Saved searches / filter presets
- Links from audit rows directly to the entity/version view pages
