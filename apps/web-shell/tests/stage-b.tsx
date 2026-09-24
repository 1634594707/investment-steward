import { useState } from "react";
import { createRoot } from "react-dom/client";
import { InvestmentPage } from "../src/routes/InvestmentPage";
import { ReviewPage } from "../src/routes/ReviewPage";
import { LibraryPage } from "../src/routes/LibraryPage";
import { EvidenceDrawer } from "../src/shell/EvidenceDrawer";
import { createMockCandles, mockDecisions, mockEvidence, mockHoldings, mockPlans, mockTheses, mockWeeklyReview } from "../src/state/mockData";
import type { Book, LearningActivity, Plan } from "@investment-steward/domain-contracts";
import type { CandlePeriod } from "../src/shell/AppShell";
import "../src/styles.css";

const now = new Date().toISOString();
const initialPlans: Plan[] = [
  { ...mockPlans[0]!, plan_id: "active-one", title: "执行计划甲", status: "active" },
  { ...mockPlans[0]!, plan_id: "active-two", title: "执行计划乙", status: "active" },
  { ...mockPlans[0]!, plan_id: "completed-one", title: "已结束计划", status: "completed" },
];
const initialActivity: LearningActivity = {
  activity_id: "fixture-learning", user_id: "local", unit_id: "fixture-unit", unit_type: "reflection",
  objective: "回顾证据条件", user_answer: "原判断记录", reflection: "下次先核验来源",
  completed_at: now, created_at: now, schema_version: "1.0",
};

function Fixture() {
  const [books, setBooks] = useState<Book[]>([
    { book_id: "book-one", title: "聪明的投资者", author: "本杰明·格雷厄姆", status: "reading", source: "user", progress: 0.62, notes: ["安全边际如何验证？"], created_at: now, updated_at: now, data_leaves_device: false },
    { book_id: "book-two", title: "置身事内", author: "兰小欢", status: "finished", source: "manual", progress: 1, notes: [], created_at: now, updated_at: now, data_leaves_device: false },
  ]);
  const [view, setView] = useState("investment");
  const [fail, setFail] = useState(false);
  const [slow, setSlow] = useState(false);
  const [evidenceId, setEvidenceId] = useState<string | null>(null);
  const [holdings, setHoldings] = useState(mockHoldings);
  const [theses, setTheses] = useState(mockTheses);
  const [period, setPeriod] = useState<CandlePeriod>("day");
  const [plans, setPlans] = useState(initialPlans);
  const [activities, setActivities] = useState([initialActivity]);
  const [decisions, setDecisions] = useState([
    { ...mockDecisions[0]!, made_at: now },
    { ...mockDecisions[1]!, made_at: new Date(Date.now() - 45 * 86400000).toISOString() },
  ]);
  const [calls, setCalls] = useState(0);
  async function request() {
    setCalls((value) => value + 1);
    await new Promise((resolve) => setTimeout(resolve, slow ? 20000 : 100));
    if (fail) throw new Error("Fixture request failure");
  }
  return <div style={{ height: "100vh", display: "flex", flexDirection: "column" }}>
    <header style={{ display: "flex", flexWrap: "wrap", gap: 16, padding: 12 }}>
      <button onClick={() => setView("investment")}>投资测试</button><button onClick={() => setView("review")}>复盘测试</button>
      <button onClick={() => setView("library")}>图书馆测试</button>
      <label><input type="checkbox" checked={fail} onChange={(event) => setFail(event.target.checked)} />模拟失败</label>
      <label><input type="checkbox" checked={slow} onChange={(event) => setSlow(event.target.checked)} />延迟 20 秒</label>
      <output aria-label="请求次数">{calls}</output>
    </header>
    <main style={{ overflow: "auto", padding: 24, flex: 1, minHeight: 0 }}>
      {view === "investment" ? <InvestmentPage policy={null} evidence={mockEvidence} holdings={holdings} theses={theses} candles={createMockCandles(period)} candlesLoading={false} candlePeriod={period} marketPluginEnabled onNavigate={setView} onSelectInstrument={() => undefined} onSelectCandlePeriod={setPeriod} onOpenEvidence={setEvidenceId}
        onSaveThesis={async (thesis) => { await request(); setTheses((items) => items.map((item) => item.thesis_id === thesis.thesis_id ? thesis : item)); return true; }}
        onCreateHolding={async (input) => { await request(); setHoldings((items) => [...items, { ...mockHoldings[0]!, ...input, holding_id: crypto.randomUUID() }]); return true; }}
        onDeleteHolding={async (id) => { await request(); setHoldings((items) => items.filter((item) => item.holding_id !== id)); return true; }}
        onCreateThesis={async (input) => { await request(); setTheses((items) => [...items, { ...mockTheses[0]!, ...input, thesis_id: crypto.randomUUID() }]); return true; }}
        onCreatePolicyDraft={async () => { await request(); return null; }} onConfirmPolicy={async () => { await request(); return true; }}
        onPullEvidence={async () => { await request(); return mockEvidence; }} /> :
      view === "library" ? <LibraryPage books={books} readingPlan={null} isDemo={false} onNavigate={setView}
        onCreateBook={async (input) => { await request(); const book: Book = { ...input, source: input.source ?? "user", book_id: crypto.randomUUID(), progress: 0, notes: [], created_at: now, updated_at: now, data_leaves_device: false }; setBooks(items => [...items, book]); return book; }}
        onUpdateBook={async (id, input) => { await request(); const book = { ...books.find(item => item.book_id === id)!, ...input }; setBooks(items => items.map(item => item.book_id === id ? book : item)); return book; }}
        onDeleteBook={async (id) => { await request(); setBooks(items => items.filter(item => item.book_id !== id)); return true; }}
        onLookupIsbn={async () => { await request(); return { available: true, title: "查询返回书名" }; }}
        onGeneratePlan={async () => { await request(); return null; }}
        onGenerateInsights={async () => { await request(); return { ok: true, insights: "样例知识结构", citations: ["聪明的投资者"] }; }}
        onAnnotationToResearch={async () => { await request(); return null; }} /> :
      <ReviewPage decisions={decisions} plans={plans} activities={activities} review={mockWeeklyReview} onNavigate={setView}
        onAddDecision={async (input) => { await request(); setDecisions((items) => [...items, { ...mockDecisions[0]!, ...input, decision_id: crypto.randomUUID(), made_at: now }]); return true; }}
        onTransitionPlan={async (id, status) => { await request(); setPlans((items) => items.map((item) => item.plan_id === id ? { ...item, status } : item)); return true; }}
        onDeleteActivity={async (id) => { await request(); setActivities((items) => items.filter((item) => item.activity_id !== id)); return true; }}
        onExportReflections={async () => { await request(); return activities; }}
        onProposePolicyChange={async () => { await request(); return null; }} onConfirmPolicy={async () => { await request(); return true; }} />}
    </main><EvidenceDrawer items={mockEvidence} evidenceId={evidenceId} onClose={() => setEvidenceId(null)} />
  </div>;
}

createRoot(document.getElementById("root")!).render(<Fixture />);
