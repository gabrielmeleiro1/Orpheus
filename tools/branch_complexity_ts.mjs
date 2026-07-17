// Branch counter for TypeScript/TSX/JS sources, used by tools/branch_complexity.py.
// Reads newline-separated file paths on stdin, prints {path: branchCount} JSON on stdout.
// Uses the TypeScript compiler bundled with the desktop app so counts match real syntax,
// not regex guesses.

import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import fs from "node:fs";
import path from "node:path";

const here = path.dirname(fileURLToPath(import.meta.url));
const require = createRequire(path.join(here, "..", "desktop", "package.json"));
const ts = require("typescript");

const BRANCH_KINDS = new Set([
  ts.SyntaxKind.IfStatement,
  ts.SyntaxKind.ConditionalExpression,
  ts.SyntaxKind.CaseClause,
  ts.SyntaxKind.CatchClause,
  ts.SyntaxKind.WhileStatement,
  ts.SyntaxKind.DoStatement,
  ts.SyntaxKind.ForStatement,
  ts.SyntaxKind.ForInStatement,
  ts.SyntaxKind.ForOfStatement,
]);

const SHORT_CIRCUIT_OPERATORS = new Set([
  ts.SyntaxKind.AmpersandAmpersandToken,
  ts.SyntaxKind.BarBarToken,
  ts.SyntaxKind.QuestionQuestionToken,
  ts.SyntaxKind.AmpersandAmpersandEqualsToken,
  ts.SyntaxKind.BarBarEqualsToken,
  ts.SyntaxKind.QuestionQuestionEqualsToken,
]);

function scriptKindFor(filePath) {
  if (filePath.endsWith(".tsx")) return ts.ScriptKind.TSX;
  if (filePath.endsWith(".ts")) return ts.ScriptKind.TS;
  return ts.ScriptKind.JS;
}

function countBranches(source, filePath) {
  const sourceFile = ts.createSourceFile(
    filePath,
    source,
    ts.ScriptTarget.Latest,
    /* setParentNodes */ false,
    scriptKindFor(filePath),
  );
  let branches = 0;
  const visit = (node) => {
    if (BRANCH_KINDS.has(node.kind)) {
      branches += 1;
    } else if (
      ts.isBinaryExpression(node) &&
      SHORT_CIRCUIT_OPERATORS.has(node.operatorToken.kind)
    ) {
      branches += 1;
    }
    ts.forEachChild(node, visit);
  };
  visit(sourceFile);
  return branches;
}

const paths = fs
  .readFileSync(0, "utf8")
  .split("\n")
  .map((line) => line.trim())
  .filter(Boolean);

const result = {};
for (const filePath of paths) {
  result[filePath] = countBranches(fs.readFileSync(filePath, "utf8"), filePath);
}
process.stdout.write(JSON.stringify(result));
