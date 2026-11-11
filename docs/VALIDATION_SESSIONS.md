# Human validation sessions

Status: **prepared, not executed**. This packet enables local review without purchasing hosting. It is not evidence of an independent security assessment, observed accessibility, customer adoption or measured time savings. Automated checks and internal code reviews remain separate evidence.

Use the exact reviewed commit and the complete local Docker application described in the [README](../README.md#run-the-complete-platform). Use disposable accounts and synthetic fixtures. Do not import employer, customer or personal datasets. Keep any participant names, contact details, recordings, tokens and raw notes outside the public repository.

## Before the session

Record the operator, commit, OS, browser, hardware and start time. Verify that the complete application starts and that the participant can access it locally. A shared screen is sufficient for a guided technical review; for an unassisted usability task, the participant must control the application. A public deployment is not required.

Read this consent statement before collecting observations:

> This is a voluntary review of a personal software project using synthetic data. You may stop at any time. I would like to record task outcomes, difficulties and elapsed time. Recording your voice or screen, quoting you, or publishing your name requires separate permission. Declining publication does not prevent participation.

Record consent for observations, recording, attribution and quotations separately. No response means no permission. Agree on a retention period for private notes and remove identifiers from any public findings.

## Independent security review

Choose a reviewer who did not implement the change. Give them the commit, [security model](SECURITY.md), [limitations](LIMITATIONS.md), local startup instructions and two disposable accounts with distinct projects. Agree on scope and stop conditions before testing. No public targets, third-party services, production credentials, unbounded load or destructive tests outside the disposable environment are authorized by this document.

| Area | Review task | Expected observation |
| --- | --- | --- |
| Tenant isolation | Substitute the other account's project, dataset, chart, report and job identifiers in read, write and deletion requests. | No foreign content, state change or successful deletion. |
| Account access | Exercise logout, refresh-token replay, password reset and account deletion with two sessions. | Session/reset behavior matches the documented revocation rules; deleted accounts cannot authenticate. |
| Deletion | Try wrong confirmation/password, another account's resources, an active job and repeated requests. Then delete an eligible dataset/project. | Confirmation and authorization enforced; in-flight work is handled explicitly; removed content cannot be reopened; cleanup and retention claims are accurate. |
| Recovery mail | Interrupt delivery, reject SMTP temporarily, restart the dispatcher and replay a reset link. Inspect persisted delivery records and logs. | Pending delivery survives restart, retry is bounded, reset links expire and are single-use, and secrets do not appear in clear text. |
| Uploads | Use bounded malformed, oversize and expansion-heavy fixtures within agreed limits. | Controlled errors, bounded resource use, no path escape or foreign access. |
| Operational boundary | Inspect secrets, runtime database grants, backup access and container configuration. | Record locally verified controls separately from untested external configuration. |

For each finding record an ID, severity, exact steps, expected/actual results, affected commit, sanitized evidence, fix commit and retest result. An automated scan is not the reviewer's sign-off. Keep the review pending until the reviewer supplies actual findings and confirms their retests.

## Usability pilot

Start with one analyst and one engineer who have not been coached through the tasks. These two sessions are formative feedback, not a representative study. Ask them to think aloud; do not explain which controls to use. Record help as assistance rather than an independent success.

1. Open the sample, explain what a row represents and identify the limits of the demonstration.
2. In the complete local application, import [sales-replay.csv](sample-data/sales-replay.csv), choose the highest-value channel, preview duplicate removal, apply it and reassess the decision.
3. Explain why the two distinct monitor orders remain and how the missing amount affects the total and mean. Use the [case study](SALES_REPLAY_CASE.md) as the observer's answer key, not a participant script.
4. Undo the change, generate a report and reopen the project after signing in again. Compare exported values with the source.
5. Delete an eligible test dataset or project using the confirmation flow. Explain what remains in audit history or backups. Use another disposable account to review account deletion; do not delete the observer's account.
6. Ask which instruction, state or error was unclear and what information was needed to trust the result.

For each task record: participant pseudonym, task ID, start/end, independent completion, assistance, incorrect decisions, errors, questions, accessibility setup and finding IDs. Measure elapsed time from task presentation to the correct result. A failed task remains a failure even if the observer later demonstrates the solution.

## Accessibility and device checks

The chart value tables and automated keyboard/reflow tests already exist. Complete these observations with actual equipment; browser emulation is not a substitute:

| Setup | Required task |
| --- | --- |
| Keyboard only | Navigate the workspace, upload, open and cancel a destructive confirmation, and verify focus returns to a usable control. |
| NVDA with Firefox or VoiceOver with Safari | Read chart titles, column headings and exact values; identify grouped or omitted values; complete form/error and confirmation flows. Record assistive-technology versions. |
| Native browser zoom | Check 200% and 400% zoom, reflow, visible focus and usable controls. Distinguish native zoom from CSS simulation. |
| Physical phone | Test portrait/landscape, menu, chart tables, forms and dialogs with touch and the phone's accessibility settings. Record model, OS and browser. |

Record pass, fail or blocked for each task. Record the actual announcement or interaction when reporting a problem; an accessible name in the DOM alone does not prove a screen reader announces it correctly.

## Publish only observed results

After consent and remediation, publish an anonymized case-study update containing the commit, participant roles/count, fixture provenance, tasks, measured outcomes, problems fixed and remaining limitations. If a time-saving claim is needed, measure a comparable baseline task with the same success criteria; do not infer savings from a demo duration. Label synthetic data as synthetic. With permission, a later public or suitably anonymized real dataset can extend the case; do not relabel this fixture as customer data.

| Gate | Current status | Completion evidence |
| --- | --- | --- |
| Independent human security review | Pending | Reviewer scope, findings and retests |
| Unassisted analyst and engineer pilot | Pending | Consented task records and follow-up observations |
| Screen reader and physical mobile validation | Pending | Actual device/technology records and resolved findings |
| Measured case-study results | Pending | Anonymized outcomes linked to the reviewed commit |

No gate becomes complete merely because this packet, an invitation or a test script exists.
