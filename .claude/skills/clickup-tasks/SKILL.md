---
name: clickup-tasks
description: Add, start, check off and list the build server's progress tasks in ClickUp (list FW_build_server). Use when the user asks to add or create a task, mark a task as started or done, check something off, or see current tasks, and when a session hook says to list or update ClickUp tasks.
argument-hint: "list | add <task> | start <task> | done <task>"
---

# ClickUp progress tasks

Progress on this project is tracked in one ClickUp list. Use the ClickUp
connector tools (`mcp__claude_ai_ClickUp__*`); load them with ToolSearch first
if they are deferred.

- Workspace ID: `9014948373`
- List: **FW_build_server**, ID `901422322774`
  (https://app.clickup.com/9014948373/v/li/901422322774)
- Statuses, in order: `to do` → `in progress` → `complete`

Always pass `workspace_id` and the list ID above; never ask which list to use.
When you mention a task, write it as a markdown link with the task name as the
text, e.g. [Add API tokens](https://app.clickup.com/t/abc123).

Requested: `$ARGUMENTS`. With no arguments, do `list`.

## list

Call `clickup_filter_tasks` with `list_ids: ["901422322774"]` (open tasks only;
add `include_closed: true` only if the user asks for finished ones too). Follow
`next_page` while `has_more` is true. Show the tasks grouped by status, in
progress first, then to do, one line each as a link. If there are none, say so.

## add <task>

Call `clickup_create_task` with `list_id: "901422322774"` and a short
imperative name (e.g. "Add per-user API tokens"). Put detail the user gave,
or context from the conversation, in `markdown_description`. Add several tasks
if the user listed several. Report each new task as a link.

## start <task> / done <task>

1. Find the task: list the open tasks as above and match the name, ignoring
   case and small wording differences. A ClickUp task ID or URL can be used
   directly.
2. If nothing matches, or several tasks match about equally, show the
   candidates and ask; never guess which task to change.
3. Call `clickup_update_task` with `status: "in progress"` (start) or
   `status: "complete"` (done).
4. Report the change as a link.

## When a hook reminds you

- **Session start**: run `list` once, before or alongside the user's first
  request, and keep the open tasks in mind while working.
- **After a reply that changed files**: compare what you just did with the
  open tasks. Set a task to `in progress` when you started on it and to
  `complete` when it is fully done (code written and tests passing). Only
  change tasks that clearly match the work; do not create tasks from a
  reminder, only when the user asks. If nothing applies, finish without
  mentioning ClickUp.
