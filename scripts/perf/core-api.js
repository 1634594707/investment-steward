/**
 * Core API 压测脚本（k6 v1.x）
 *
 * 端点子集来源：apps/web-shell/src 里前端实际引用且经 scripts/perf_probe.py
 * 探测健康（200）的只读端点，权重按前端引用次数与轮询频率设定。
 *
 * 用法：
 *   k6 run scripts/perf/core-api.js --env SCENARIO=smoke
 *   k6 run scripts/perf/core-api.js --env SCENARIO=baseline --env OUT=.runtime/perf-baseline.json
 *   k6 run scripts/perf/core-api.js --env SCENARIO=ramp
 *
 * 注意：只压只读 GET。写操作（POST/PUT/DELETE）会真落库，需先切副本库，不在本脚本范围。
 */
import http from 'k6/http';
import { check, sleep } from 'k6';
import { Rate } from 'k6/metrics';

const BASE = __ENV.BASE || 'http://127.0.0.1:18765';
const TOKEN = __ENV.TOKEN || 'manual-test-token';
const SCENARIO = __ENV.SCENARIO || 'baseline';
const OUT = __ENV.OUT || '';
// 单端点诊断模式：ONLY=/health 时只打这一个端点，用来测该端点的极限吞吐。
const ONLY = __ENV.ONLY || '';
// 排除某些端点做对照实验（逗号分隔），用于验证单个端点对整体吞吐的影响。
const EXCLUDE = (__ENV.EXCLUDE || '').split(',').filter(Boolean);

// think time（秒）。测单端点极限吞吐时设 THINK=0，避免 25 VU × 10 req/s 的人为天花板。
const THINK = __ENV.THINK === undefined ? 0.1 : parseFloat(__ENV.THINK);

const PARAMS = {
  headers: {
    'X-Core-Session-Token': TOKEN,
    Accept: 'application/json',
  },
};

// [路径, 权重]。权重 = 前端引用次数 + 轮询频率折算。
const ENDPOINTS = [
  ['/holdings', 4],
  ['/notifications/pending', 3],
  ['/overview', 3],
  ['/brief/today', 2],
  ['/evidence', 2],
  ['/decisions', 2],
  ['/judgments', 2],
  ['/personal/settings', 2],
  ['/learning/activities', 2],
  ['/watch-items', 2],
  ['/health', 1],
  ['/session', 1],
  ['/portfolio/risk', 1],
  ['/slots', 1],
];

const ACTIVE = ENDPOINTS.filter(([p]) => !EXCLUDE.includes(p));
const TOTAL_WEIGHT = ACTIVE.reduce((sum, e) => sum + e[1], 0);

function pickPath() {
  if (ONLY) return ONLY;
  let r = Math.random() * TOTAL_WEIGHT;
  for (const [path, weight] of ACTIVE) {
    r -= weight;
    if (r <= 0) return path;
  }
  return ACTIVE[0][0];
}

const SCENARIOS = {
  smoke: { executor: 'constant-vus', vus: 1, duration: '20s' },
  baseline: { executor: 'constant-vus', vus: 25, duration: '60s' },
  soak: { executor: 'constant-vus', vus: 10, duration: '120s' },
  // 容量探测：按「每秒请求数」加压而不是按 VU 加压。VU 模式下服务端一旦排队，
  // 客户端只会看到延迟暴涨而吞吐不涨，无法读出真实容量上限。
  capacity: {
    executor: 'ramping-arrival-rate',
    startRate: 100,
    timeUnit: '1s',
    preAllocatedVUs: 60,
    maxVUs: 500,
    stages: [
      { duration: '30s', target: 100 },
      { duration: '30s', target: 200 },
      { duration: '30s', target: 300 },
      { duration: '30s', target: 400 },
      { duration: '30s', target: 600 },
      { duration: '30s', target: 800 },
      { duration: '20s', target: 0 },
    ],
  },
  ramp: {
    executor: 'ramping-vus',
    startVUs: 5,
    stages: [
      { duration: '30s', target: 10 },
      { duration: '30s', target: 25 },
      { duration: '30s', target: 50 },
      { duration: '30s', target: 100 },
      { duration: '30s', target: 0 },
    ],
  },
};

export const options = {
  scenarios: { main: SCENARIOS[SCENARIO] || SCENARIOS.baseline },
  thresholds: {
    http_req_duration: ['p(95)<500'],
    http_req_failed: ['rate<0.01'],
  },
  summaryTrendStats: ['avg', 'min', 'med', 'p(95)', 'p(99)', 'max'],
};

const errorRate = new Rate('core_errors');

export default function () {
  const path = pickPath();
  const res = http.get(BASE + path, { ...PARAMS, tags: { endpoint: path } });
  const ok = check(res, {
    'status 200': (r) => r.status === 200,
  });
  errorRate.add(!ok);
  if (THINK > 0) sleep(THINK);
}

export function handleSummary(data) {
  // k6 v1.x 的 handleSummary 里，每个指标是 { type, contains, values, thresholds }，
  // 真实数值挂在 values 下，直接读 data.metrics.http_reqs.rate 会全是 null。
  const v = (name, key) => {
    const m = data.metrics[name];
    if (!m || !m.values) return null;
    const x = m.values[key];
    return typeof x === 'number' ? Math.round(x * 1000) / 1000 : x;
  };

  const payload = {
    scenario: SCENARIO,
    base: BASE,
    measuredAt: new Date().toISOString(),
    endpoints: ACTIVE.map(([p, w]) => ({ path: p, weight: w })),
    excluded: EXCLUDE,
    metrics: {
      rps: v('http_reqs', 'rate'),
      totalRequests: v('http_reqs', 'count'),
      durationAvg: v('http_req_duration', 'avg'),
      durationP50: v('http_req_duration', 'med'),
      durationP95: v('http_req_duration', 'p(95)'),
      durationP99: v('http_req_duration', 'p(99)'),
      durationMax: v('http_req_duration', 'max'),
      failedRate: v('http_req_failed', 'rate'),
      checkPassRate: v('checks', 'rate'),
      vusMax: v('vus_max', 'value'),
      dataReceivedBytes: v('data_received', 'count'),
    },
  };

  const m = payload.metrics;
  const text = [
    `scenario=${SCENARIO}  vusMax=${m.vusMax}  reqs=${m.totalRequests}`,
    `rps=${m.rps}  failedRate=${m.failedRate}  checkRate=${m.checkPassRate}`,
    `latency ms  avg=${m.durationAvg}  p50=${m.durationP50}  p95=${m.durationP95}  p99=${m.durationP99}  max=${m.durationMax}`,
    `received=${m.dataReceivedBytes} bytes`,
  ].join('\n');

  const out = {};
  out['stdout'] = text;
  if (OUT) out[OUT] = JSON.stringify(payload, null, 2);
  return out;
}
