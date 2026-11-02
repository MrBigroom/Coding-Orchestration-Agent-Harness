// Loaded into Pi by the orchestrator's chat sessions. Pi has no permission system of its
// own, so this asks over RPC before tool calls. It reads the mode from a file on every
// call, which lets the GUI change it mid-session. It prompts; it is not a sandbox.
import { readFileSync } from "node:fs";
import { relative, resolve, isAbsolute } from "node:path";

const READ_TOOLS = new Set(["read", "grep", "find", "ls"]);
const EDIT_TOOLS = new Set(["edit", "write"]);
const SAFE_BASH =
	/^\s*(cat|head|tail|wc|ls|tree|pwd|rg|grep|fd|find|file|stat|du|git\s+(status|log|diff|show|branch))(\s|$)/;
const UNSAFE_BASH = /[;&|<>`$\n]|\s-(exec|execdir|ok|okdir|delete|fprint\w*)\b|--output\b/;

function mode(): string {
	try {
		return readFileSync(process.env.ORCHESTRATOR_PI_MODE_FILE ?? "", "utf8").trim();
	} catch {
		return "ask";
	}
}

// Standalone runs: Plan, but the agent may write inside its output folder.
function inOutputFolder(input: Record<string, unknown>, cwd: string): boolean {
	const folder = process.env.ORCHESTRATOR_OUTPUT_DIR;
	const target = input.path ?? input.file_path;
	if (!folder || typeof target !== "string") return false;
	const rel = relative(resolve(folder), resolve(cwd, target));
	return rel !== "" && !rel.startsWith("..") && !isAbsolute(rel);
}

function describe(toolName: string, input: Record<string, unknown>): string {
	if (toolName === "bash") return String(input.command ?? "");
	return String(input.path ?? input.file_path ?? JSON.stringify(input));
}

export default function (pi: any) {
	const alwaysAllowed = new Set<string>();

	pi.on("tool_call", async (event: any, ctx: any) => {
		const tool: string = event.toolName;
		const current = mode();
		if (current === "yolo" || READ_TOOLS.has(tool)) return undefined;
		if (current === "report" && EDIT_TOOLS.has(tool) && inOutputFolder(event.input, ctx.cwd)) return undefined;
		if (current === "plan" || current === "report") {
			const command = String(event.input.command ?? "");
			if (tool === "bash" && SAFE_BASH.test(command) && !UNSAFE_BASH.test(command)) return undefined;
			const hint = current === "report" ? "Write only inside the output folder." : "Describe the change instead of making it.";
			return { block: true, reason: `This session is read-only. ${hint}` };
		}
		if (current === "auto-edit" && EDIT_TOOLS.has(tool)) return undefined;
		if (alwaysAllowed.has(tool)) return undefined;

		const always = `Always allow ${tool}`;
		const choice = await ctx.ui.select(`Allow ${tool}?\n${describe(tool, event.input)}`, ["Allow", always, "Deny"]);
		if (choice === always) alwaysAllowed.add(tool);
		if (choice === "Allow" || choice === always) return undefined;
		return { block: true, reason: "Denied by the user." };
	});
}
