# Security policy

## Scope

This policy covers the code in this repository and the release files built
from it. Problems in Home Assistant, its recorder or HACS belong to those
projects.

Sediment deletes rows from your history database, so we care most about these:

- Data loss: Sediment must never delete or change a row inside the raw window,
  and never hand thinned history to the recorder's own purge without your
  confirmation.
- The takeover: the recorder's settings change only after the compatibility
  guard passes, and come back only when you untick the takeover and confirm,
  or remove the integration.
- Imports: an import file must not be able to write outside `states`,
  `states_meta` and `state_attributes`, or make the service read a path
  outside the allowed folders.

## Reporting

Report security problems in private. Open a
[confidential issue on GitLab](https://gitlab.com/Chaos02-oss/sediment/-/issues/new?issue[confidential]=true),
not on the GitHub mirror. Do not post details in a public issue.

Include the Sediment and Home Assistant versions, your database engine, what
you did, what happened and what you expected. We aim to answer within 7 days.
