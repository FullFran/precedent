/**
 * precedent — OpenCode adapter
 *
 * Surfaces the same failure-class pattern pages Claude Code's PreToolUse
 * hook does, but through OpenCode's plugin hooks instead. This is weaker
 * than the Claude Code hook, on purpose documented, not glossed over: see
 * the "Agent support" section in ../../README.md and ../../DOCS.md.
 *
 * Why "tool.execute.after" and not "tool.execute.before":
 *
 *   "tool.execute.before"(input: {tool, sessionID, callID}, output: {args})
 *     => Promise<void>
 *
 *   The only mutable thing here is `output.args`. There is no field to
 *   inject context for the model to read, and throwing aborts the tool
 *   call outright rather than informing it. A hook that can only mutate
 *   arguments or block cannot do what this tool exists to do: surface a
 *   page, not gate an edit.
 *
 *   "tool.execute.after"(input: {tool, sessionID, callID, args}, output:
 *     {title, output, metadata}) => Promise<void>
 *
 *   `output.output` is the tool result string the model actually reads,
 *   and it is mutable. Appending to it is the only place in OpenCode's
 *   hook surface where this adapter can put a pattern page in front of the
 *   model at all -- which is exactly why it arrives after the edit has
 *   already landed, not before it, unlike Claude Code's PreToolUse hook.
 *
 * Tool names: OpenCode's built-in tools are registered under lowercase
 * ids -- "edit", "write", "bash" -- not the capitalized Edit/Write/Bash
 * names Claude Code uses. Verified directly against the installed
 * @opencode-ai/plugin type definitions (Hooks["tool.execute.after"]'s
 * `input.tool: string`) and against the actual tool registrations inside
 * the opencode binary itself (`ID="bash"`, `rP="edit"`, `DN="write"` next
 * to their `H.register({[ID]: ...})` / `H.register({[rP]: ...})` /
 * `H.register({[DN]: ...})` calls), not guessed from Claude Code's naming.
 *
 * Safety contract: this code runs inside the OpenCode agent process, not
 * as a subprocess the way the Claude Code hook does. Nothing here may
 * throw. Every path is wrapped, and a failure of any kind -- tripwire.py
 * missing, python3 missing, a bad match, anything -- must leave
 * `output.output` exactly as OpenCode produced it.
 */

import type { Plugin } from "@opencode-ai/plugin"
import { execFile } from "node:child_process"
import { existsSync } from "node:fs"
import path from "node:path"
import { fileURLToPath } from "node:url"

// Verified tool ids (see module docstring above) -- lowercase, not the
// capitalized Edit/Write/Bash names Claude Code's hook matches against.
const EDIT_TOOLS = new Set(["edit", "write"])
const BASH_TOOL = "bash"

const AGENT_NAME = "opencode"
const MATCH_TIMEOUT_MS = 5000

const HEADER =
  "\n\n---\n" +
  "precedent match (plugin/opencode/precedent.ts) -- this page matched " +
  "AFTER the tool call already ran: OpenCode's tool.execute.after fires " +
  "post-hoc, unlike Claude Code's PreToolUse. It is not part of the " +
  "tool's own output. Read it before your next action.\n\n"
const FOOTER = "\n---\n"

function resolveTripwirePath(): string {
  const override = process.env.PRECEDENT_TRIPWIRE
  if (override) return override
  // This file lives at plugin/opencode/precedent.ts. tripwire.py lives at
  // plugin/claude-code/scripts/tripwire.py -- a sibling directory one
  // level up, under plugin/.
  const here = path.dirname(fileURLToPath(import.meta.url))
  return path.resolve(here, "..", "claude-code", "scripts", "tripwire.py")
}

/**
 * Shell out to `tripwire.py --match --agent opencode ...`. Never throws:
 * any failure (missing script, missing python3, non-zero exit, timeout)
 * resolves to "", the same way a miss does.
 */
function runMatch(args: string[]): Promise<string> {
  return new Promise((resolve) => {
    let settled = false
    const finish = (value: string) => {
      if (settled) return
      settled = true
      resolve(value)
    }
    try {
      const tripwire = resolveTripwirePath()
      if (!existsSync(tripwire)) {
        finish("")
        return
      }
      execFile(
        "python3",
        [tripwire, "--match", "--agent", AGENT_NAME, ...args],
        { timeout: MATCH_TIMEOUT_MS },
        (error, stdout) => {
          if (error) {
            finish("")
            return
          }
          finish(stdout)
        },
      )
    } catch {
      finish("")
    }
  })
}

const PrecedentPlugin: Plugin = async () => {
  return {
    "tool.execute.after": async (input, output) => {
      try {
        let matchArgs: string[] | undefined

        if (EDIT_TOOLS.has(input.tool)) {
          const filePath = input.args?.path
          if (typeof filePath === "string" && filePath) {
            matchArgs = ["--path", filePath]
          }
        } else if (input.tool === BASH_TOOL) {
          const command = input.args?.command
          if (typeof command === "string" && command) {
            matchArgs = ["--command", command]
          }
        }

        if (!matchArgs) return
        if (typeof output.output !== "string") return

        const matched = await runMatch(matchArgs)
        if (!matched) return

        output.output = `${output.output}${HEADER}${matched}${FOOTER}`
      } catch {
        // A tripwire failure must never touch the tool's own output. See
        // the safety contract in the module docstring above.
      }
    },
  }
}

export default PrecedentPlugin
