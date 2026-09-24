import { ChildProcess, spawn } from "node:child_process";
import { once } from "node:events";

export interface SidecarSpec {
  name: string;
  command: string;
  args: string[];
  cwd: string;
  env?: NodeJS.ProcessEnv;
  /** Runner 使用 stdin/stdout JSON-RPC；普通 sidecar 默认不暴露输入。 */
  stdio?: "pipe" | "ignore";
}

const SECRET_ENV_MARKERS = [
  "TOKEN",
  "SECRET",
  "APIKEY",
  "API_KEY",
  "PASSWORD",
  "PASSWD",
  "PRIVATE_KEY",
  "CREDENTIAL",
];

// G3-5 零密钥：sidecar 子进程不继承父进程环境里的疑似凭据变量。
// 子进程（尤其 plugin-runner）应只拿到显式注入的非敏感白名单，而不是整份宿主 env。
function sanitizeEnv(env: NodeJS.ProcessEnv): NodeJS.ProcessEnv {
  const clean: NodeJS.ProcessEnv = {};
  for (const [key, value] of Object.entries(env)) {
    if (key && SECRET_ENV_MARKERS.some((marker) => key.toUpperCase().includes(marker))) continue;
    if (value !== undefined) clean[key] = value;
  }
  return clean;
}

export class SidecarProcess {
  private child: ChildProcess | undefined;
  private lastError: string | null = null;
  private stdoutBuffer = "";
  private stdoutWaiters: Array<{ resolve: (line: string) => void; reject: (error: Error) => void }> = [];
  private rpcQueue: Promise<unknown> = Promise.resolve();

  constructor(private readonly spec: SidecarSpec) {}

  get pid(): number | undefined {
    return this.running ? this.child?.pid : undefined;
  }

  get running(): boolean {
    return !!this.child && this.child.exitCode === null && this.child.signalCode === null && !this.child.killed;
  }

  get error(): string | null {
    return this.lastError;
  }

  start(): void {
    if (this.running) return;
    this.lastError = null;
    this.stdoutBuffer = "";
    this.stdoutWaiters = [];
    const child = spawn(this.spec.command, this.spec.args, {
      cwd: this.spec.cwd,
      env: sanitizeEnv({ ...process.env, ...this.spec.env }),
      stdio: [this.spec.stdio ?? "ignore", "pipe", "pipe"],
      windowsHide: true,
    });
    this.child = child;
    child.stdout?.setEncoding("utf8");
    child.stdout?.on("data", (chunk: string) => {
      this.stdoutBuffer += chunk;
      let newline = this.stdoutBuffer.indexOf("\n");
      while (newline >= 0) {
        const line = this.stdoutBuffer.slice(0, newline).replace(/\r$/, "");
        this.stdoutBuffer = this.stdoutBuffer.slice(newline + 1);
        this.stdoutWaiters.shift()?.resolve(line);
        newline = this.stdoutBuffer.indexOf("\n");
      }
    });
    child.stderr?.on("data", (chunk: Buffer) => {
      const line = chunk.toString("utf8").trim();
      if (line) this.lastError = `${this.spec.name}: ${line.slice(-500)}`;
    });
    child.on("error", (error) => {
      this.lastError = `${this.spec.name}: ${error.message}`;
    });
    child.on("exit", (code, signal) => {
      if (code !== 0 && signal !== "SIGTERM") {
        this.lastError = `${this.spec.name} exited (${code ?? signal ?? "unknown"})`;
      }
      if (this.child === child) this.child = undefined;
      const error = new Error(`${this.spec.name} exited before completing JSON-RPC response`);
      for (const waiter of this.stdoutWaiters.splice(0)) waiter.reject(error);
    });
  }

  /** Send one line-delimited JSON-RPC request. Calls are serialized to preserve response order. */
  requestJsonRpc(request: object): Promise<Record<string, unknown>> {
    const operation = this.rpcQueue.then(() => this.sendJsonRpc(request));
    this.rpcQueue = operation.catch(() => undefined);
    return operation;
  }

  private sendJsonRpc(request: object): Promise<Record<string, unknown>> {
    const stdin = this.child?.stdin;
    if (!this.running || !stdin || !this.child?.stdout) {
      return Promise.reject(new Error(`${this.spec.name} is not running with an RPC channel`));
    }
    return new Promise((resolve, reject) => {
      this.stdoutWaiters.push({
        resolve: (line) => {
          try {
            const parsed: unknown = JSON.parse(line);
            if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
              reject(new Error(`${this.spec.name} returned a non-object JSON-RPC response`));
              return;
            }
            resolve(parsed as Record<string, unknown>);
          } catch (error) {
            reject(error instanceof Error ? error : new Error(String(error)));
          }
        },
        reject,
      });
      try {
        stdin.write(`${JSON.stringify(request)}\n`, "utf8");
      } catch (error) {
        this.stdoutWaiters.pop();
        reject(error instanceof Error ? error : new Error(String(error)));
      }
    });
  }

  async stop(): Promise<void> {
    const child = this.child;
    if (!child || !this.running) {
      this.child = undefined;
      return;
    }
    child.kill();
    await once(child, "exit").catch(() => undefined);
    if (this.child === child) this.child = undefined;
  }
}
