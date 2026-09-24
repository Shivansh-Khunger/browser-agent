# Issue tracker: GitHub

Issues and specs for this project live in the `shreverr/browser-agent` GitHub repository. Use the `gh` CLI for all operations and pass `-R shreverr/browser-agent`; the local clone's `origin` points at a different fork.

## Conventions

- **Create an issue**: `gh issue create -R shreverr/browser-agent --title "..." --body "..."`.
- **Read an issue**: `gh issue view <number> -R shreverr/browser-agent --comments`, filtering comments and labels as needed.
- **List issues**: `gh issue list -R shreverr/browser-agent --state open --json number,title,body,labels,comments` with appropriate label and state filters.
- **Comment on an issue**: `gh issue comment <number> -R shreverr/browser-agent --body "..."`.
- **Apply or remove labels**: `gh issue edit <number> -R shreverr/browser-agent --add-label "..."` or `--remove-label "..."`.
- **Close an issue**: `gh issue close <number> -R shreverr/browser-agent --comment "..."`.

Do not infer the issue repository from the local Git remote. Always target `shreverr/browser-agent` explicitly.

## Pull requests as a triage surface

**PRs as a request surface: no.**

GitHub shares one number space across issues and pull requests. If a bare number is ambiguous, resolve it with `gh pr view <number> -R shreverr/browser-agent`, then fall back to `gh issue view <number> -R shreverr/browser-agent`.

## Skill operations

When a skill says **publish to the issue tracker**, create an issue in `shreverr/browser-agent`.

When a skill says **fetch the relevant ticket**, run `gh issue view <number> -R shreverr/browser-agent --comments`.

## Wayfinding operations

The map is one issue with native GitHub sub-issues as decision tickets.

- **Map**: create one issue labelled `wayfinder:map`. Its body contains Destination, Notes, Decisions so far, Not yet specified, and Out of scope.
- **Child ticket**: create an issue labelled `wayfinder:research`, `wayfinder:prototype`, `wayfinder:grilling`, or `wayfinder:task`, then link it to the map as a native sub-issue.
- **Blocking**: use GitHub's native issue dependencies. A ticket is unblocked when every blocking issue is closed.
- **Frontier**: inspect the map's open children in map order; skip tickets with open blockers or any assignee.
- **Claim**: assign the ticket to the driving developer before any work: `gh issue edit <number> -R shreverr/browser-agent --add-assignee @me`.
- **Resolve**: post the decision as a resolution comment, close the ticket, then append a gist and named link to the map's Decisions-so-far section.

Use GitHub GraphQL `addSubIssue` and `addBlockedBy` mutations when native relationship commands are unavailable in the installed `gh` version. Only fall back to body conventions when the repository lacks native relationships.
