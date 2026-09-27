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
// 注：实为对完整 process.env 按名称标记的**排除**（denylist），不是白名单——
// 名字不含这 8 个标记的凭据仍会透传。属已知边界，见路线图 S 系列。
function sanitizeEnv(env: NodeJS.ProcessEnv): NodeJS.ProcessEnv {
  const clean: NodeJS.ProcessEnv = {};
  for (const [key, value] of Object.entries(env)) {
    if (key && SECRET_ENV_MARKERS.some((marker) => key.toUpperCase().includes(marker))) continue;
    if (value !== undefined) clean[key] = value;
  }
  return clean;
}

/**
 * S6（用户视角路线图 2026-09-26）：单次 JSON-RPC 调用的上限。
 *
 * 改造前 `sendJsonRpc` 构造的 Promise **没有 setTimeout、没有 race、没有 deadline**，
 * 只在「stdout 出一行」「子进程退出」「stdin.write 抛错」三种情况下 settle。一个活着
 * 却永不回行的 runner 会让该 Promise 永久 pending；又因为 `requestJsonRpc` 用
 * `rpcQueue` 把调用**串行化**，一次未 settle 的调用会把其后所有调用一起堵死——而
 * 渲染层那边就是一个永不落地的 `await`，没有报错也没有超时。
 *
 * 超过该时限即以可读错误结束本次调用，保证队列能继续推进。
 */
const RPC_CALL_TIMEOUT_MS = 30_000;

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
      // S6：给本次调用装一个上限。定时器与 waiter 同生共死——settle  whichever 先到
      // 都必须清掉另一方，否则会留下悬挂定时器或在已完成后误拒。
      let settled = false;
      let timer: ReturnType<typeof setTimeout> | undefined;
      const finish = (fn: () => void) => {
        if (settled) return;
        settled = true;
        if (timer) clearTimeout(timer);
        fn();
      };
      const waiter = {
        resolve: (line: string) => {
          finish(() => {
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
          });
        },
        reject: (error: Error) => finish(() => reject(error)),
      };
      timer = setTimeout(() => {
        // 只摘掉**自己**这个 waiter：后面的调用还排在队列里，不能连带拒掉。
        const index = this.stdoutWaiters.indexOf(waiter);
        if (index >= 0) this.stdoutWaiters.splice(index, 1);
        this.lastError = `${this.spec.name} JSON-RPC 超时（>${RPC_CALL_TIMEOUT_MS / 1000}s 无应答）`;
        finish(() =>
          reject(
            new Error(
              `${this.spec.name} JSON-RPC 调用超时（${RPC_CALL_TIMEOUT_MS / 1000}s 无应答）：` +
                "插件可能已卡死。请重启应用后再试。",
            ),
          ),
        );
      }, RPC_CALL_TIMEOUT_MS);
      this.stdoutWaiters.push(waiter);
      try {
        stdin.write(`${JSON.stringify(request)}\n`, "utf8");
      } catch (error) {
        const index = this.stdoutWaiters.indexOf(waiter);
        if (index >= 0) this.stdoutWaiters.splice(index, 1);
        finish(() => reject(error instanceof Error ? error : new Error(String(error))));
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
