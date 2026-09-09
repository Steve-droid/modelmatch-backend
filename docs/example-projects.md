# New-account example projects (P38p)

New password and Google accounts receive two private, populated dashboards:

- **Example: Pull Request Review** — 30 sample runs.
- **Example: Security Scan** — 20 sample runs.

`SEED_NEW_USER_EXAMPLES=true` is the default and is explicit in the deployment configuration.
The current benchmark catalog is required. Recommendations and sample costs use the existing
deterministic services/fixtures; no provider calls, Jenkins connections or CI tokens are created.
Sample findings and feedback belong to the new user. Public paid-feature permissions are unchanged.

User creation, both projects, sample runs/findings/feedback and (for Google) nonce consumption
commit together. Recommendation/project/run helpers accept `commit=False` only for this outer
transaction. Provisioning failure rolls everything back, leaving no half-created account or pair.
The existing registration lock/cap applies; a partial unique index permits only one example per
user/task. Returning login never reseeds, and deleted examples stay deleted.

Migration `a4b5c6d7e8f9` adds `project.is_example`, default false for existing projects. No backfill
or rename touches existing accounts. `ProjectOut.isExample` drives the sample-data notice and
suppresses incomplete-setup prompts. CI setup/token creation, Jenkins connection and project
editing reject example projects; owners can inspect findings, change their own feedback and delete
examples. Create a separate real CI agent to connect a repository. The example flag is not an
accepted create/update input and cannot be used to convert sample data into real CI history.

The operator CLI's existing named demo seeding explicitly opts out of automatic signup examples;
it still provisions its own fixtures. It is not a public API bypass. API contract tests disable
automatic examples when supplying their own scenario data; dedicated onboarding tests enable it
with the real catalog, including mixed concurrent signup and second-project failure rollback.

Release migration first, with global catalog/demo seeding disabled, then update backend/frontend.
On the public stack, verify a new account immediately lists both examples and can browse their
dashboards without setup or chat. Preserve old accounts and existing CI runs. Rollback can disable
automatic provisioning for new accounts, but must preserve sample flags and CI guards for any
examples already created; don't downgrade away the flag while sample data remains.
