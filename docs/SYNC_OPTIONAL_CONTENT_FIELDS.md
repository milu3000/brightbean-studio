# Optional Meta message fields

The canonical adapter requests complete message content first. Only Graph400
code100 with an explicit unsupported optional-field diagnostic may switch a
message checkpoint to basic fields. Permission failures, quota failures, unknown
400 responses and missing participant/recipient identity fields do not trigger
this downgrade. The raw error response is never persisted or logged.

The failed page consumes its already reserved GETs and commits no message data.
The checkpoint retains the same cursor and records its field mode plus a probe
time. A later scheduled page reserves its usual two GETs before retrying. There
is no immediate unreserved fallback request.

Basic responses can update observed text, but cannot clear already captured
attachments. Messages expose `fields_unavailable` and remain marked for repair.
An exhausted basic page is still partial coverage in bootstrap cutover review.
After six hours, a later normally budgeted page tries complete fields again. Only
a successful complete snapshot may clear prior media or its partial-content
marker. This interval is an engineering retry default, not a content-retention
rule or freshness guarantee.

Migration0018 adds two inert checkpoint fields. Empty tables and all existing
rollout settings stay disabled; no account is enrolled, no provider request is
made, and no retention processing is activated by applying this schema.
