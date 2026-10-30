import fs from "node:fs/promises";
import path from "node:path";
import { execFileSync } from "node:child_process";

function readGit(args, cwd) {
  return execFileSync("git", args, { cwd, encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] }).trim();
}

/** Identify selected application bytes independently of the harness checkout. */
export async function performanceSourceProvenance({ sourceRepo, revision = null, git = readGit }) {
  const directory = await fs.realpath(path.resolve(sourceRepo));
  const exported = {
    sourceRepo: directory, sourceRevision: null, selectedSourceMode: "export",
    selectedSourceCommit: null, selectedSourceDirty: null,
  };
  if (revision !== null) {
    // Do not substitute HEAD or working-tree dirtiness for an immutable revision.
    const commit = (await git(["rev-parse", "--verify", `${revision}^{commit}`], directory)).trim();
    return { ...exported, sourceRevision: commit, selectedSourceMode: "revision",
      selectedSourceCommit: commit, selectedSourceDirty: false };
  }

  let topLevel;
  try {
    topLevel = (await git(["rev-parse", "--show-toplevel"], directory)).trim();
  } catch (error) {
    // Plain source exports need no Git metadata. A checkout with its own .git
    // must not become an apparent export merely because its Git command failed.
    try {
      await fs.lstat(path.join(directory, ".git"));
    } catch (metadataError) {
      if (metadataError.code === "ENOENT") return exported;
      throw metadataError;
    }
    throw error;
  }
  if (path.relative(directory, await fs.realpath(topLevel)) !== "") return exported;
  const commit = (await git(["rev-parse", "HEAD"], directory)).trim();
  const dirty = Boolean((await git(["status", "--porcelain"], directory)).trim());
  return { ...exported, selectedSourceMode: "worktree", selectedSourceCommit: commit, selectedSourceDirty: dirty };
}

/** Reject bundled packages from a different dependency tree before timing. */
export async function validatePerformanceDependencies({ repo, metafile, absWorkingDir = repo }) {
  if (!metafile?.inputs || typeof metafile.inputs !== "object") {
    throw new Error("Frontend performance dependency validation requires an esbuild metafile");
  }
  const root = await fs.realpath(path.join(path.resolve(repo), "desktop", "node_modules"));
  const inputs = [];
  for (const input of Object.keys(metafile.inputs)) {
    if (!input.split(/[\\/]/).some(part => part.toLowerCase() === "node_modules")) continue;
    const resolved = await fs.realpath(path.resolve(absWorkingDir, input));
    const relative = path.relative(root, resolved);
    if (relative === ".." || relative.startsWith(`..${path.sep}`) || path.isAbsolute(relative)) {
      throw new Error(`Frontend performance dependency ${input} resolves outside the shared desktop dependency root ${root}. Select a source tree that uses the main checkout's installed dependencies.`);
    }
    inputs.push(resolved);
  }
  return { root, inputCount: inputs.length, inputs: inputs.sort() };
}

/** Use the current workload and dependencies with an explicitly selected app tree. */
export async function performanceSourceOptions({ repo, sourceRepo, fixturePath }) {
  const sourceDirectory = path.resolve(sourceRepo);
  const desktopSource = path.join(sourceDirectory, "desktop", "src");
  if (!(await fs.stat(desktopSource)).isDirectory()) {
    throw new Error("Source repository must contain desktop/src");
  }
  return {
    stdin: {
      contents: await fs.readFile(fixturePath, "utf8"),
      loader: "tsx",
      sourcefile: fixturePath,
      resolveDir: path.join(desktopSource, "test"),
    },
    // A detached baseline needs no node_modules. This is a resolution fallback;
    // validatePerformanceDependencies rejects a conflicting selected-tree package.
    nodePaths: [path.join(path.resolve(repo), "desktop", "node_modules")],
  };
}
