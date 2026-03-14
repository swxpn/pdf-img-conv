import path from "node:path";
import { spawnSync } from "node:child_process";

function resolvePythonExecutable() {
  const venvPython = path.join(process.cwd(), ".venv", "bin", "python");
  return venvPython;
}

export function runConversionWorker(command, payload) {
  const pythonExec = resolvePythonExecutable();
  const workerPath = path.join(process.cwd(), "scripts", "conversion_worker.py");

  const proc = spawnSync(pythonExec, [workerPath], {
    input: JSON.stringify({ command, payload }),
    encoding: "utf8",
    maxBuffer: 10 * 1024 * 1024,
  });

  if (proc.error) {
    throw new Error(`Failed to run conversion worker: ${proc.error.message}`);
  }

  const stdout = (proc.stdout || "").trim();
  const stderr = (proc.stderr || "").trim();

  if (!stdout) {
    throw new Error(stderr || "Conversion worker returned no output.");
  }

  let parsed;
  try {
    parsed = JSON.parse(stdout);
  } catch (e) {
    throw new Error(`Invalid worker output: ${stdout}`);
  }

  if (!parsed.ok) {
    throw new Error(parsed.error || "Worker failed.");
  }

  return parsed.result;
}
