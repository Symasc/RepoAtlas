import type { HighlighterCore, ThemedToken } from "shiki/core";

/**
 * Syntax colour for the code reader. Shiki, loaded on first use: its core, the
 * JavaScript regex engine (no WebAssembly) and only the grammars a file needs.
 * The one theme paints with `--ra-code-*` variables, so both colour schemes
 * come from the app's own tokens.
 */

const GRAMMARS: Record<string, () => Promise<unknown>> = {
  typescript: () => import("shiki/langs/typescript.mjs"),
  tsx: () => import("shiki/langs/tsx.mjs"),
  javascript: () => import("shiki/langs/javascript.mjs"),
  jsx: () => import("shiki/langs/jsx.mjs"),
  python: () => import("shiki/langs/python.mjs"),
  ruby: () => import("shiki/langs/ruby.mjs"),
  shellscript: () => import("shiki/langs/shellscript.mjs"),
  yaml: () => import("shiki/langs/yaml.mjs"),
  json: () => import("shiki/langs/json.mjs"),
  jinja: () => import("shiki/langs/jinja.mjs"),
  dockerfile: () => import("shiki/langs/dockerfile.mjs"),
  markdown: () => import("shiki/langs/markdown.mjs"),
  toml: () => import("shiki/langs/toml.mjs"),
  css: () => import("shiki/langs/css.mjs"),
  sql: () => import("shiki/langs/sql.mjs"),
};

const BY_EXTENSION: Record<string, string> = {
  ts: "typescript",
  mts: "typescript",
  cts: "typescript",
  tsx: "tsx",
  js: "javascript",
  mjs: "javascript",
  cjs: "javascript",
  jsx: "jsx",
  py: "python",
  pyi: "python",
  rb: "ruby",
  rake: "ruby",
  gemspec: "ruby",
  sh: "shellscript",
  bash: "shellscript",
  zsh: "shellscript",
  yaml: "yaml",
  yml: "yaml",
  sls: "yaml",
  json: "json",
  j2: "jinja",
  jinja: "jinja",
  jinja2: "jinja",
  md: "markdown",
  toml: "toml",
  css: "css",
  sql: "sql",
};

const BY_NAME: Record<string, string> = {
  Gemfile: "ruby",
  Rakefile: "ruby",
  Dockerfile: "dockerfile",
  Containerfile: "dockerfile",
};

/** The grammar for a file, by its name; `null` reads it as plain text. */
export function grammarFor(path: string): string | null {
  const name = path.slice(path.lastIndexOf("/") + 1);
  if (BY_NAME[name]) return BY_NAME[name];
  if (name.startsWith("Dockerfile")) return "dockerfile";
  const dot = name.lastIndexOf(".");
  return dot > 0 ? (BY_EXTENSION[name.slice(dot + 1).toLowerCase()] ?? null) : null;
}

let highlighter: Promise<HighlighterCore> | null = null;

function load(): Promise<HighlighterCore> {
  highlighter ??= (async () => {
    const [{ createHighlighterCore, createCssVariablesTheme }, { createJavaScriptRegexEngine }] = await Promise.all([
      import("shiki/core"),
      import("shiki/engine/javascript"),
    ]);
    return createHighlighterCore({
      themes: [createCssVariablesTheme({ name: "atlas", variablePrefix: "--ra-code-", fontStyle: true })],
      langs: [],
      // a grammar pattern the engine cannot translate is skipped, not fatal
      engine: createJavaScriptRegexEngine({ forgiving: true }),
    });
  })();
  return highlighter;
}

/** One line's tokens: text, and the variable that colours it. */
export type CodeToken = Pick<ThemedToken, "content" | "color" | "fontStyle">;

/** Lines of `code` as coloured tokens, or `null` for a file read as plain text. */
export async function tokenize(code: string, grammar: string | null): Promise<CodeToken[][] | null> {
  if (grammar === null || !GRAMMARS[grammar]) return null;
  const shiki = await load();
  if (!shiki.getLoadedLanguages().includes(grammar)) {
    await shiki.loadLanguage((await GRAMMARS[grammar]()) as Parameters<HighlighterCore["loadLanguage"]>[0]);
  }
  // a cold grammar compiles as it reads the first lines, which can outlast
  // Shiki's 500ms a line on a busy machine and leave them plain; a minified
  // line is the one worth giving up on, so it is left plain instead
  const options = { lang: grammar, theme: "atlas", tokenizeTimeLimit: 1500, tokenizeMaxLineLength: 2000 };
  return shiki.codeToTokens(code, options).tokens.map((line) =>
    line.map(({ content, color, fontStyle }) => ({ content, color, fontStyle })),
  );
}

/** Tokens a reference may sit in: never inside a comment, and inside a string only as an import's specifier. */
export function isComment(token: CodeToken): boolean {
  return token.color?.includes("token-comment") ?? false;
}

/** A bare name, as a tag or a plain YAML word arrives: never the inside of a string. */
const BARE = /^[A-Za-z_$][\w$.:-]*$/;

/**
 * The theme colours quoted, template and regular-expression strings as
 * `string-expression`, but tag names too; a tag name is a bare word, which a
 * string token on one line almost never is.
 */
export function isString(token: CodeToken): boolean {
  if (token.color?.includes("token-string-expression")) return !BARE.test(token.content.trim());
  return token.color?.includes("token-string") ?? false;
}
