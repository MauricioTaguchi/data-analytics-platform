# Removing workspace data

Deletion is available in the complete application, using an authenticated account. The public sample demonstration does not delete server data.

## User flow

Open a dataset, project or account deletion control, read its scope and type `DELETE`. Account deletion also requires the current password. Cancelling leaves the resource unchanged. An accepted response removes the resource from the workspace and schedules file collection; it does not claim that every backup or audit record has already been erased.

The API routes are `DELETE /api/v1/lifecycle/datasets/{id}`, `DELETE /api/v1/lifecycle/projects/{id}` and `DELETE /api/v1/lifecycle/account`. The request body contains `confirmation: "DELETE"`; account deletion also requires `current_password`. Credentials belong in the request body and authorization header, never the URL. Accepted deletion returns HTTP 202 and `cleanup_pending: true`.

Deletion is refused while the account has active jobs, including staged uploads and cancellation requests whose workers have not finished. Wait for those jobs to finish, or cancel them and wait for acknowledgement, before retrying. A refused deletion does not remove the resource. Foreign or already removed resources return not-found responses.

## What changes

| Scope | Application effect |
| --- | --- |
| Dataset | Tombstone the dataset, hide it from normal access, expire its snapshots, clear cached profile/preview content, invalidate generated reports and queue its managed files for deletion. |
| Project | Tombstone the project and apply dataset removal to its remaining datasets. Other projects remain accessible. |
| Account | Remove its projects, deactivate the account, anonymize its primary name/email/password fields, invalidate sessions and reset links, and cancel pending recovery messages. |

Authenticated writes serialize with deletion on the owner. Existing read-only requests may finish; this is not a promise to retract bytes already downloaded or a response already in flight. Pending SMTP accepted by an external mail server cannot be recalled, but reset links for a closed account cannot restore access.

## Cleanup and retention boundary

File-deletion requests commit with the tombstones. The scheduler's existing artifact collector retries interrupted or failed filesystem deletion. Run the scheduler and monitor storage/cleanup failures; a 202 response is admission, not a filesystem receipt. Account deletion also schedules tracked failed/reserved artifacts for collection. Do not remove Docker volumes as a substitute for deleting one user's resources.

Immutable audit records and tombstoned database metadata are retained. This can include names or values originally supplied as metadata. Existing backups remain governed by the operator's separate retention policy. Browser downloads and files outside the managed storage roots are not recalled. This feature is **not a complete personal-data erasure or compliance guarantee**.

Restoring an older backup can restore a state from before a deletion. Before promoting a recovered environment, reconcile subsequent deletion requests from an independently retained operator record and verify that deleted accounts/resources are not made available again. Automatic replay of deletion requests from a separate recovery ledger is not implemented. This limitation matters before accepting real personal data.

## Validation

Regression checks must cover foreign ownership, missing or incorrect confirmation, wrong account password, active-job refusal, post-deletion reads and writes, revoked access/refresh/reset credentials, retained audit metadata, file-collection retry and preservation of unrelated data. Browser tests must exercise cancellation, successful deletion, server refusal and uncertain network outcomes. Human validation follows the [session packet](VALIDATION_SESSIONS.md).
