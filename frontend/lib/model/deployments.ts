import type {
  DeploymentCondition,
  DeploymentLink,
  DeploymentLinkFields,
  DeploymentResolution,
  DeploymentTarget,
} from "@/lib/api/generated";

/**
 * Deployment targets as the explorer shows them. Four things stay apart: what is
 * configured (a target's pattern), when it applies (conditions), what a context
 * resolves it to (each resolution, with every variable's basis), and whether
 * anyone confirmed it (verification). Nothing here checks that a URL answers.
 */

export const KIND_LABEL: Record<DeploymentTarget["kind"], string> = {
  ingress: "Ingress",
  "openapi-server": "OpenAPI server",
  link: "Recorded link",
};

export const STATE_MEANING: Record<DeploymentResolution["state"], string> = {
  resolved: "Every part of the URL is known in this context.",
  partial: "Some variables are still open; they are named, not guessed.",
  unresolved: "The host itself is not known in this context.",
};

export const CONDITIONS_MEANING: Record<NonNullable<DeploymentResolution["conditions"]>, string> = {
  none: "No conditions: it applies everywhere.",
  match: "Its conditions hold for the context asked about.",
  no_match: "Its conditions do not hold for the context asked about.",
  unknown: "The context does not say whether its conditions hold.",
  conflict: "Its conditions contradict each other.",
};

export const VERIFICATION_LABEL: Record<NonNullable<DeploymentResolution["verification"]>, string> = {
  unverified: "unverified",
  user_confirmed: "confirmed by you",
};

/** The variables a resolution leaves open: what a link could still supply. */
export function holesOf(resolution: DeploymentResolution): string[] {
  return (resolution.variables ?? []).filter((v) => v.value == null && !v.withheld).map((v) => v.name);
}

/** Where a target is stated, as `path:line`; null for a link of its own. */
export function statedAt(target: DeploymentTarget): string | null {
  if (!target.path) return null;
  return target.line ? `${target.path}:${target.line}` : target.path;
}

/** The form's own text fields: what a person types, before it is a request. */
export interface LinkForm {
  label: string;
  environment: string;
  url: string;
  targetId: string;
  /** One `name=value` per line. */
  variables: string;
  /** One condition per line: `branch=main`, `branch~release-*`, `pull_request=false`, `var:NAME=value`. */
  conditions: string;
  /** One caller repository slug per line; empty serves every caller. A loopback URL needs them. */
  callers: string;
  confirmed: boolean;
}

export const EMPTY_FORM: LinkForm = {
  label: "",
  environment: "",
  url: "",
  targetId: "",
  variables: "",
  conditions: "",
  callers: "",
  confirmed: false,
};

export function formOf(link: DeploymentLink): LinkForm {
  return {
    label: link.label,
    environment: link.environment ?? "",
    url: link.url ?? "",
    targetId: link.target_id ?? "",
    variables: Object.entries(link.variables ?? {})
      .map(([name, value]) => `${name}=${value}`)
      .join("\n"),
    conditions: (link.conditions ?? []).map(conditionText).join("\n"),
    callers: (link.callers ?? []).join("\n"),
    confirmed: link.verification === "user_confirmed",
  };
}

export function conditionText(condition: DeploymentCondition): string {
  const subject = condition.kind === "variable" ? `var:${condition.name ?? ""}` : condition.kind;
  return `${subject}${condition.op === "glob" ? "~" : "="}${condition.value}`;
}

const CONDITION_KINDS = new Set(["branch", "environment", "pull_request"]);

/** One typed condition line, or the reason it is not one. */
export function parseCondition(line: string): DeploymentCondition | string {
  const match = /^([A-Za-z_:][\w:.-]*)\s*([=~])\s*(.+)$/.exec(line.trim());
  if (!match) return `"${line.trim()}" is not "subject=value" or "subject~pattern"`;
  const subject = match[1] ?? "";
  const operator = match[2];
  const value = (match[3] ?? "").trim();
  const op = operator === "~" ? "glob" : "equals";
  if (subject.startsWith("var:")) {
    const name = subject.slice(4);
    if (!name) return `"${line.trim()}" names no variable`;
    return { kind: "variable", name, op, value };
  }
  if (!CONDITION_KINDS.has(subject)) return `"${subject}" is not branch, environment, pull_request or var:NAME`;
  return { kind: subject as DeploymentCondition["kind"], op, value };
}

/** The request fields for a form, or the problems that stop it being sent. */
export function fieldsOf(form: LinkForm): { fields: DeploymentLinkFields } | { problems: string[] } {
  const problems: string[] = [];
  const variables: Record<string, string> = {};
  for (const line of lines(form.variables)) {
    const at = line.indexOf("=");
    if (at <= 0) {
      problems.push(`"${line}" is not "name=value"`);
      continue;
    }
    variables[line.slice(0, at).trim()] = line.slice(at + 1).trim();
  }
  const conditions: DeploymentCondition[] = [];
  for (const line of lines(form.conditions)) {
    const parsed = parseCondition(line);
    if (typeof parsed === "string") problems.push(parsed);
    else conditions.push(parsed);
  }
  if (!form.label.trim()) problems.push("A link needs a label.");
  if (!form.url.trim() && !form.targetId) problems.push("A link needs a URL, a target, or both.");
  if (problems.length > 0) return { problems };
  return {
    fields: {
      label: form.label.trim(),
      environment: form.environment.trim() || null,
      url: form.url.trim() || null,
      target_id: form.targetId || null,
      variables,
      conditions,
      callers: lines(form.callers),
      verification: form.confirmed ? "user_confirmed" : "unverified",
    },
  };
}

function lines(text: string): string[] {
  return text
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean);
}
