# Buma — User Acceptance Testing (UAT) Script

**Product:** Buma — Intelligent Bug Triaging & Assignment System
**Repository:** https://github.com/Zeeldesai12345/buma

**Audience:** Reviewers and acceptance testers.
No familiarity with Buma's codebase is required. Follow each step exactly as written.

---

## Tester Information

| Field | Value |
|---|---|
| Tester name | |
| Date | |
| Buma version / commit | |
| Browser used | |
| Operating system | |

---

## Before You Begin

Make sure the following are already done before starting the test scenarios below. These are covered in the [User Guide](user-guide.md).

- [ ] Buma stack is running (`docker compose up` completed without errors)
- [ ] Web dashboard is running (`npm start` in the `web-dashboard/` folder)
- [ ] ngrok tunnel is active and the GitHub App webhook URL has been updated
- [ ] At least one GitHub repository has been enrolled in Buma
- [ ] At least two developer profiles have been added to that repository

If any of the above are not ready, stop and complete the setup before proceeding.

---

## How to Use This Document

- Work through each scenario in order.
- Follow the **Steps** exactly as written.
- After each step, check whether the **Expected Result** matches what you see.
- Record your **Actual Result** and mark **Pass** or **Fail**.
- Leave **Notes** for anything unexpected, confusing, or worth flagging — even if the test passed.

A test **passes** only when the actual result fully matches the expected result.

---

## Scenario 1 — System Health Check

**Purpose:** Confirm the backend is running and reachable before testing anything else.

**Preconditions:** Docker stack is running.

| # | Step | Expected Result | Actual Result | Pass / Fail |
|---|---|---|---|---|
| 1 | Open a browser and go to `http://localhost:8000/health` | Page displays: `{"status":"ok","service":"buma"}` | | |
| 2 | Open a browser and go to `http://localhost:3000` | The Buma dashboard loads without a blank screen or error | | |

**Notes:**

---

## Scenario 2 — Dashboard Login with GitHub

**Purpose:** Confirm that a user can sign in to the dashboard using their GitHub account.

**Preconditions:** Scenario 1 passed. You have a GitHub account.

| # | Step | Expected Result | Actual Result | Pass / Fail |
|---|---|---|---|---|
| 1 | On the dashboard (`http://localhost:3000`), click **Sign in with GitHub** | You are redirected to GitHub's authorization page | | |
| 2 | Review the permissions requested and click **Authorize** | You are redirected back to `http://localhost:3000` | | |
| 3 | Confirm you are now logged in | Your GitHub username or avatar is visible somewhere on the dashboard | | |
| 4 | Refresh the page | You remain logged in — you are not sent back to the login screen | | |

**Notes:**

---

## Scenario 3 — View Repository Configuration

**Purpose:** Confirm that an enrolled repository and its developer team are visible in the dashboard.

**Preconditions:** Scenario 2 passed. A repository has been enrolled and at least two developers have been added.

| # | Step | Expected Result | Actual Result | Pass / Fail |
|---|---|---|---|---|
| 1 | In the dashboard sidebar, click **Repositories** | The repositories page loads | | |
| 2 | Locate the enrolled repository in the list | The repository name (e.g. `myorg/myrepo`) is displayed | | |
| 3 | Click **People** and select the repository | A list of developer profiles is shown, each with a GitHub username and skills | | |
| 4 | Confirm at least two developers are listed | At least two developer entries are visible | | |

**Notes:**

---

## Scenario 4 — Automatic Triage of a Bug Issue

**Purpose:** This is the core feature. Confirm that when a bug issue is opened on GitHub, Buma automatically triages it — assigning a developer, setting a priority label, and posting a comment.

**Preconditions:** Scenarios 1–3 passed. ngrok is running. The GitHub App is installed on the test repository.

| # | Step | Expected Result | Actual Result | Pass / Fail |
|---|---|---|---|---|
| 1 | Go to the enrolled GitHub repository in your browser | The repository page loads | | |
| 2 | Click **Issues** → **New issue** | The new issue form opens | | |
| 3 | Enter a title that clearly describes a bug, e.g. *"Login button crashes on mobile"*. Leave the body blank. Click **Submit new issue** | The issue is created on GitHub | | |
| 4 | Wait up to 30 seconds, then refresh the issue page | The category and priority **labels** (e.g. `bug` and `P1`) have been applied to the issue | | |
| 5 | On the same issue page, check the **Assignees** section | A developer has been automatically assigned | | |
| 6 | Scroll down to the comments section | Buma has posted a comment explaining why this developer was chosen and what priority was assigned | | |
| 7 | Go back to the Buma dashboard **Home** page | The issue you just created appears in the **Recent Activity** list | | |

**Notes:**

---

## Scenario 5 — Non-Bug Issue is Skipped

**Purpose:** Confirm that Buma only triages bug reports and ignores feature requests or questions.

**Preconditions:** Scenarios 1–3 passed. ngrok is running.

| # | Step | Expected Result | Actual Result | Pass / Fail |
|---|---|---|---|---|
| 1 | In the enrolled GitHub repository, click **Issues** → **New issue** | The new issue form opens | | |
| 2 | Enter a title that is clearly a feature request, e.g. *"Add dark mode to the settings page"*. Click **Submit new issue** | The issue is created on GitHub | | |
| 3 | Wait 30 seconds, then refresh the issue page | No label has been applied, no developer has been assigned, and no Buma comment appears | | |

**Notes:**

---

## Scenario 6 — Triage History View

**Purpose:** Confirm that the triage history log is accurate and readable.

**Preconditions:** Scenario 4 passed (at least one triage decision exists).

| # | Step | Expected Result | Actual Result | Pass / Fail |
|---|---|---|---|---|
| 1 | In the dashboard, open the **Home** page | The **Recent Activity** list of triage decisions loads | | |
| 2 | Locate the entry for the bug issue created in Scenario 4 | The entry shows the issue title (or ID), assigned developer, and priority | | |
| 3 | Confirm the decision matches what appeared on the GitHub issue | The assignee and priority in the dashboard match the label and assignee set on the GitHub issue | | |

**Notes:**

---

## Scenario 7 — Developer Workload View

**Purpose:** Confirm that the workload view reflects current open assignments per developer.

**Preconditions:** Scenario 4 passed.

| # | Step | Expected Result | Actual Result | Pass / Fail |
|---|---|---|---|---|
| 1 | In the dashboard, click **People** | A page loads showing each developer's open assignments and capacity | | |
| 2 | Find the developer who was assigned in Scenario 4 | Their open assignment count is at least 1 | | |
| 3 | Confirm other developers are also listed | The full team is shown, not just the assigned developer | | |

**Notes:**

---

## Scenario 8 — Add a New Developer Profile

**Purpose:** Confirm that a new developer can be added to the team via the dashboard.

**Preconditions:** Scenario 2 passed (logged in).

| # | Step | Expected Result | Actual Result | Pass / Fail |
|---|---|---|---|---|
| 1 | Go to **People** and select the repository | The developer list is shown | | |
| 2 | Click **Add Developer** | A form appears asking for GitHub login, skills, and capacity | | |
| 3 | Enter a valid GitHub username, at least one skill (e.g. `bug`), and a capacity value (e.g. `3`). Click **Save** | The new developer appears in the list without a page error | | |
| 4 | Refresh the page | The new developer is still listed — the save persisted | | |

**Notes:**

---

## Scenario 9 — Update a Developer Profile

**Purpose:** Confirm that an existing developer's skills or capacity can be changed.

**Preconditions:** Scenario 8 passed (at least one developer exists in the list).

| # | Step | Expected Result | Actual Result | Pass / Fail |
|---|---|---|---|---|
| 1 | In the developer list, click **Edit** next to any developer | An editable form opens pre-filled with their current values | | |
| 2 | Change the capacity value (e.g. from `3` to `1`) and click **Save** | The updated value is reflected in the developer list | | |
| 3 | Refresh the page | The updated value persists after refresh | | |

**Notes:**

---

## Scenario 10 — Interactive API Docs

**Purpose:** Confirm that the API is self-documented and explorable — useful for integration by other teams.

**Preconditions:** Docker stack is running.

| # | Step | Expected Result | Actual Result | Pass / Fail |
|---|---|---|---|---|
| 1 | Open `http://localhost:8000/docs` in your browser | The Swagger UI page loads, listing all available API routes | | |
| 2 | Expand the `GET /health` route and click **Try it out** → **Execute** | A response of `{"status":"ok","service":"buma"}` appears in the response body | | |

**Notes:**

---

## Overall Assessment

| Scenario | Title | Result |
|---|---|---|
| 1 | System Health Check | Pass / Fail / Blocked |
| 2 | Dashboard Login with GitHub | Pass / Fail / Blocked |
| 3 | View Repository Configuration | Pass / Fail / Blocked |
| 4 | Automatic Triage of a Bug Issue | Pass / Fail / Blocked |
| 5 | Non-Bug Issue is Skipped | Pass / Fail / Blocked |
| 6 | Triage History View | Pass / Fail / Blocked |
| 7 | Developer Workload View | Pass / Fail / Blocked |
| 8 | Add a New Developer Profile | Pass / Fail / Blocked |
| 9 | Update a Developer Profile | Pass / Fail / Blocked |
| 10 | Interactive API Docs | Pass / Fail / Blocked |

**Total passed:** &nbsp;&nbsp;&nbsp;/ 10

---

## Defect Log

Use this section to record anything that did not work as expected. One row per defect.

| # | Scenario | Step | What happened | Severity (High / Medium / Low) |
|---|---|---|---|---|
| 1 | | | | |
| 2 | | | | |
| 3 | | | | |

**Severity guide:**
- **High** — core feature broken, no workaround (e.g. triage never fires)
- **Medium** — feature partially works or requires extra steps (e.g. wrong priority assigned)
- **Low** — cosmetic or minor usability issue (e.g. unclear button label)

---

## General Feedback

*Use this space for any open-ended comments — things that were confusing, missing, or that worked particularly well.*

---

## Sign-off

| Field | Value |
|---|---|
| Tester signature / name | |
| Date completed | |
| Overall verdict | Accepted / Accepted with conditions / Not accepted |
| Conditions (if any) | |
