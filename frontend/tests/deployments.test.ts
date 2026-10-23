import { describe, expect, it } from "vitest";

import type { DeploymentLink, DeploymentResolution, DeploymentTarget } from "@/lib/api/generated";
import {
  conditionText,
  EMPTY_FORM,
  fieldsOf,
  formOf,
  holesOf,
  parseCondition,
  statedAt,
} from "@/lib/model/deployments";

describe("conditions", () => {
  it.each([
    ["branch=main", { kind: "branch", op: "equals", value: "main" }],
    ["branch~release-*", { kind: "branch", op: "glob", value: "release-*" }],
    ["environment = staging", { kind: "environment", op: "equals", value: "staging" }],
    ["pull_request=false", { kind: "pull_request", op: "equals", value: "false" }],
    ["var:REGION=eu", { kind: "variable", name: "REGION", op: "equals", value: "eu" }],
  ])("reads %s", (line, expected) => {
    expect(parseCondition(line)).toEqual(expected);
  });

  it.each(["main", "colour=blue", "var:=x"])("refuses %s with a reason", (line) => {
    expect(typeof parseCondition(line)).toBe("string");
  });

  it("writes a condition back as it is typed", () => {
    for (const line of ["branch=main", "branch~release-*", "var:REGION=eu"]) {
      const parsed = parseCondition(line);
      if (typeof parsed === "string") throw new Error(parsed);
      expect(conditionText(parsed)).toBe(line);
    }
  });
});

describe("the link form", () => {
  it("becomes request fields, unverified unless confirmed", () => {
    const result = fieldsOf({
      ...EMPTY_FORM,
      label: " staging ",
      url: "https://web-staging.example.test/",
      variables: "stage=staging\n\n",
      conditions: "branch=develop",
    });
    expect(result).toEqual({
      fields: {
        label: "staging",
        environment: null,
        url: "https://web-staging.example.test/",
        target_id: null,
        variables: { stage: "staging" },
        conditions: [{ kind: "branch", op: "equals", value: "develop" }],
        callers: [],
        verification: "unverified",
      },
    });
  });

  it("names every problem rather than sending a half-read link", () => {
    const result = fieldsOf({ ...EMPTY_FORM, variables: "stage", conditions: "colour=blue" });
    expect("problems" in result && result.problems).toHaveLength(4);
  });

  it("round-trips a stored link", () => {
    const link: DeploymentLink = {
      id: 1,
      repository: "web",
      created_at: "2026-01-01T00:00:00Z",
      updated_at: "2026-01-01T00:00:00Z",
      label: "prod",
      environment: "production",
      url: null,
      target_id: "dt_0123456789abcdef",
      variables: { stage: "prod" },
      conditions: [{ kind: "branch", op: "equals", value: "main" }],
      verification: "user_confirmed",
    };
    const form = formOf(link);
    expect(form.confirmed).toBe(true);
    const fields = fieldsOf(form);
    expect("fields" in fields && fields.fields.conditions).toEqual(link.conditions);
    expect("fields" in fields && fields.fields.verification).toBe("user_confirmed");
  });
});

describe("callers", () => {
  it("survive an edit: a form built from a link sends the callers it was made with", () => {
    const link: DeploymentLink = {
      id: 3,
      repository: "api",
      created_at: "2026-01-01T00:00:00Z",
      updated_at: "2026-01-01T00:00:00Z",
      label: "dev",
      environment: "dev",
      url: "http://localhost:8000",
      callers: ["web", "tools"],
    };
    const form = formOf(link);
    expect(form.callers).toBe("web\ntools");
    const result = fieldsOf(form);
    expect("fields" in result && result.fields.callers).toEqual(["web", "tools"]);
  });

  it("are empty, not missing, for a link that names none", () => {
    const result = fieldsOf({ ...EMPTY_FORM, label: "prod", url: "https://app.example.test/" });
    expect("fields" in result && result.fields.callers).toEqual([]);
  });
});

describe("targets", () => {
  it("lists only the open variables as holes; a withheld one is not a hole", () => {
    const resolution: DeploymentResolution = {
      basis: "chart-defaults",
      label: "chart defaults",
      rendered: "https://{stage}.example.test/",
      state: "partial",
      variables: [
        { name: "stage", source: "value", value: null },
        { name: "host", source: "value", value: "example.test", basis: "chart-defaults" },
        { name: "token", source: "value", value: null, withheld: true },
      ],
    };
    expect(holesOf(resolution)).toEqual(["stage"]);
  });

  it("says where a target is stated", () => {
    const base: DeploymentTarget = { kind: "ingress", pattern: "x", repository: "web", target_id: "dt_0" };
    expect(statedAt({ ...base, path: "charts/web/templates/ingress.yaml", line: 7 })).toBe(
      "charts/web/templates/ingress.yaml:7",
    );
    expect(statedAt({ ...base, kind: "link" })).toBeNull();
  });
});
