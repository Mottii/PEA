#!/usr/bin/env node
/**
 * Zero-dependency Claude Desktop (.mcpb) stdio launcher for PEA v2 (`pea-mcp`).
 *
 * Resolves the user's configured local PEA project directory from PEA_PROJECT_ROOT
 * (populated via `${user_config.project_folder}`) and execs `.venv/bin/pea-mcp`
 * (or `.venv\Scripts\pea-mcp.exe` on Windows) with stdin/stdout/stderr inherited
 * and zero shell interpolation.
 *
 * IMPORTANT: Never write diagnostics to stdout — stdout is exclusively the MCP
 * JSON-RPC stdio transport.
 */

import { spawn } from "node:child_process";
import { existsSync, statSync } from "node:fs";
import path from "node:path";
import process from "node:process";

function isUnexpandedTemplate(value) {
  return typeof value === "string" && /^\$\{[^}]+\}$/.test(value.trim());
}

function cleanOptionalEnv(value) {
  if (typeof value !== "string") {
    return "";
  }
  const trimmed = value.trim();
  if (!trimmed || isUnexpandedTemplate(trimmed)) {
    return "";
  }
  return trimmed;
}

function resolveProjectRoot(argv, env) {
  for (let i = 0; i < argv.length; i += 1) {
    if (argv[i] === "--project-root" && i + 1 < argv.length) {
      return argv[i + 1].trim();
    }
  }
  return cleanOptionalEnv(env.PEA_PROJECT_ROOT);
}

function findPeaCommand(projectRoot, platform) {
  const candidates =
    platform === "win32"
      ? [
          {
            command: path.join(projectRoot, ".venv", "Scripts", "pea-mcp.exe"),
            args: [],
          },
          {
            command: path.join(projectRoot, ".venv", "Scripts", "python.exe"),
            args: ["-m", "pea.mcp_server"],
          },
        ]
      : [
          {
            command: path.join(projectRoot, ".venv", "bin", "pea-mcp"),
            args: [],
          },
          {
            command: path.join(projectRoot, ".venv", "bin", "python"),
            args: ["-m", "pea.mcp_server"],
          },
          {
            command: path.join(projectRoot, ".venv", "bin", "python3"),
            args: ["-m", "pea.mcp_server"],
          },
        ];

  for (const candidate of candidates) {
    if (existsSync(candidate.command)) {
      return candidate;
    }
  }
  return null;
}

function fail(message, code = 1) {
  process.stderr.write(`[pea-mcp launcher] ${message}\n`);
  process.exit(code);
}

const projectRootRaw = resolveProjectRoot(process.argv.slice(2), process.env);
if (!projectRootRaw) {
  fail(
    "PEA_PROJECT_ROOT is not configured. Set 'PEA Project Folder' in the extension settings " +
      "to your local PEA v2 workspace containing .venv/.",
  );
}

const projectRoot = path.resolve(projectRootRaw);
if (!existsSync(projectRoot) || !statSync(projectRoot).isDirectory()) {
  fail(`Configured PEA project folder does not exist or is not a directory: ${projectRoot}`);
}

const target = findPeaCommand(projectRoot, process.platform);
if (!target) {
  const expected =
    process.platform === "win32"
      ? ".venv\\Scripts\\pea-mcp.exe"
      : ".venv/bin/pea-mcp";
  fail(
    `Could not find ${expected} inside ${projectRoot}. ` +
      "Create the Python 3.13 .venv and install PEA (`uv pip install --python .venv/bin/python -e .`).",
  );
}

const childEnv = { ...process.env, PEA_PROJECT_ROOT: projectRoot };

const allowedOrigins = cleanOptionalEnv(process.env.PEA_ALLOWED_ORIGINS);
if (allowedOrigins) {
  childEnv.PEA_ALLOWED_ORIGINS = allowedOrigins;
} else {
  delete childEnv.PEA_ALLOWED_ORIGINS;
}

const ocrBackend = cleanOptionalEnv(process.env.PEA_OCR_BACKEND);
if (ocrBackend) {
  childEnv.PEA_OCR_BACKEND = ocrBackend;
} else {
  delete childEnv.PEA_OCR_BACKEND;
}

const child = spawn(target.command, target.args, {
  cwd: projectRoot,
  env: childEnv,
  stdio: ["inherit", "inherit", "inherit"],
  shell: false,
  windowsHide: true,
});

child.on("error", (err) => {
  fail(`Failed to spawn ${target.command}: ${err.message}`);
});

for (const signal of ["SIGINT", "SIGTERM"]) {
  process.on(signal, () => {
    if (!child.killed) {
      child.kill(signal);
    }
  });
}

child.on("exit", (code, signal) => {
  if (signal) {
    process.stderr.write(`[pea-mcp launcher] child terminated by signal ${signal}\n`);
    process.exit(1);
  }
  process.exit(code ?? 0);
});
