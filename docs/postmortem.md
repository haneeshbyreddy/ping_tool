# Incident Post-mortem — template

Use one file per significant outage. The dashboard captures the structured
fields (root cause + resolution notes) on the *Pending post-mortem* card; this
doc is the longer human write-up for repeat offenders or multi-site events.

---

## Summary

- **Incident ID:** INC-____
- **Date / time (UTC):** ____
- **Duration:** ____  (down → restored)
- **Site(s):** ____  (region · node · IP)
- **Severity:** critical / warning
- **Acknowledged by:** ____

## What happened

A short narrative: what the operator saw, when the alert fired, who responded.

## Detection

- How did we find out? (dashboard / ntfy)
- Did the state machine classify it correctly?
  - Inferred cause (engine guess): power / link-equipment
  - **Confirmed cause** (filled at resolution): ____

## Timeline (UTC)

| Time | Event |
|------|-------|
| __:__ | First 100%-loss poll |
| __:__ | DOWN confirmed (3 consecutive) / alert sent |
| __:__ | Acknowledged by ____ |
| __:__ | Root cause found |
| __:__ | Service restored (recovery hysteresis cleared) |

## Root cause

What actually broke (power / fiber-backhaul / hardware / weather-RF / other) and
why. Note if the topology suppression (parent down → children UNREACHABLE) or the
power-vs-link heuristic helped or misled.

## Resolution

What the technician did to restore service; parts/gear used.

## Follow-ups

- [ ] Preventive action
- [ ] Threshold/topology tuning, if the FSM mis-classified
- [ ] Inventory fix in the dashboard (Nodes page), if metadata was wrong
