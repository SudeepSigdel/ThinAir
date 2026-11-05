# Good first issues

Self-contained tasks for a first contribution. Each one names the files involved and how to know
it's done. Comment on (or open) an issue before starting, so two people don't pick the same one.

1. **Show elapsed time on running jobs.** The job page shows "started 5 min ago" but not how long
   a finished job took. Add a duration to the meta line for finished jobs.
   *Files:* `src/thinair/web/static/app.js` (`renderJob`). *Done when:* finished jobs show e.g.
   "took 3 min 12 s", and running jobs don't.

2. **`thinair token list` shows when a token was last used.** The data is stored
   (`ApiToken.last_used_at`) but not printed. *Files:* `src/thinair/cli/main.py`, `tests/test_auth.py`.
   *Done when:* the list shows "never" or a relative time, with a test.

3. **Detect `pnpm` and `yarn` projects.** Today a Node project always installs with `npm`. Use
   `pnpm install --frozen-lockfile` when `pnpm-lock.yaml` exists and `yarn install --frozen-lockfile`
   for `yarn.lock` (check the current flags in their docs). *Files:* `src/thinair/project/detect.py`,
   `tests/test_project_unit.py`.

4. **Detect `go.mod` projects.** Add a Go stack: a sandbox Dockerfile based on the official
   `golang` image (owning `/workspace` as uid 10001, see `CLAUDE.md`), `go test ./...` as the
   default test. *Files:* `src/thinair/project/detect.py`, `src/thinair/sandbox/images/`, tests.

5. **Copy button for the diff.** Add a "Copy diff" button on agent job pages using
   `navigator.clipboard.writeText`. *Files:* `app.js`, `index.html`, `style.css`.
   *Done when:* it works on a phone and shows "Copied".

6. **Job list filters in the web app.** The API supports `?status=`. Add "All / Running / Failed"
   chips above the job list. *Files:* `app.js`, `index.html`, `style.css`.

7. **Delete old jobs.** Add `DELETE /api/jobs/{id}` for finished jobs, removing their events,
   runs and results. *Files:* `src/thinair/api/routes.py`, `tests/test_api.py`.
   *Done when:* running jobs can't be deleted (409), with tests for both cases.

8. **Better error for a missing `test` script in Node projects.** If `package.json` has no
   `scripts.test`, the baseline fails with npm's generic message. Detect it before running and say
   "Add a `test` script to package.json or set `test:` in thinair.yaml".
   *Files:* `src/thinair/project/prepare.py` (read `package.json` like `thinair.yaml`), tests.

9. **Verify and add the Gemini CLI agent config.** Read the Gemini CLI docs for headless mode,
   approvals and auth, write `src/thinair/agents/builtin/gemini.yaml`, record what you checked in
   `docs/agents.md`, and add a unit test pinning the flags. (Codex CLI is the same task.)

10. **Dark-mode screenshot in the README.** Take phone-sized screenshots of the home and report
    pages and add them under `docs/images/`, linked from the README.
