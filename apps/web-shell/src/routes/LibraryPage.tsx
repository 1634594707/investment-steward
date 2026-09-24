import { useMemo, useState } from "react";
import { Search, Plus, Pencil, Trash2, BookOpen } from "lucide-react";
import { createCoreClient } from "../state/coreClient";
import "./library.css";
import type { Book, LibraryPlan, ResearchRun } from "@investment-steward/domain-contracts";
import type { CoreConnection } from "../state/coreClient";
import { useLibrary, type LibraryInsightsResult } from "../hooks/useLibrary";
import { ConfirmDialog } from "../components/ConfirmDialog";

interface LookupResult {
  available: boolean;
  title?: string;
  publishers?: string[];
  pages?: number;
  source?: string;
  degraded_reason?: string;
}

interface Props {
  onNavigate: (view: "review" | "research") => void;
  isDemo: boolean;
  disconnected?: boolean;
  /** B2：Core 连接态（书架在其变化时重载）。 */
  connection: CoreConnection;
  /** B2：当前视图是否为图书馆页（KeepAlive 常驻挂载）。A03：传给 useLibrary 做页面级停表。 */
  active: boolean;
  /** M2-03：AI 前置就绪 = 存在「使用中」模型方案；未就绪时认知档案入口禁用。 */
  aiReady?: boolean;
  /** 批注转研究问题（POST /library/annotations/{annotation_id}/to-research，annotation_id = "{book_id}:{index}"）。 */
  onAnnotationToResearch: (annotationId: string, question: string) => Promise<ResearchRun | null>;
}

const STATUS_LABEL: Record<Book["status"], string> = { reading: "在读", finished: "已读", wishlist: "想读" };
const SOURCE_LABEL: Record<Book["source"], string> = { ai: "模型推荐", user: "用户上传", manual: "手录" };

/** 研读图书馆（独立应用插件页）：书架接 GET/POST/DELETE /library/books 真实数据；
 *  ISBN 录入走 /library/books/lookup/{isbn}（Open Library 白名单），查不到降级为手录，不编造元数据。 */
export function LibraryPage({ onNavigate, isDemo, connection, disconnected = false, active, onAnnotationToResearch, aiReady = true }: Props) {
  // B2：图书馆域数据自取（回调 props 已清零；别名对齐既有局部命名，页面主体零改动）。
  const client = useMemo(() => createCoreClient(), []);
  const {
    books, booksLoading: loading, booksError: error, readingPlan,
    bumpRevision: onRetry, createBook: onCreateBook, updateBook: onUpdateBook, deleteBook: onDeleteBook,
    lookupIsbn: onLookupIsbn, generatePlan: onGeneratePlan, generateLibraryInsights: onGenerateInsights,
  } = useLibrary(client, connection, aiReady, active);
  const [search, setSearch] = useState("");
  const [shelfStatus, setShelfStatus] = useState<"all" | Book["status"]>("all");
  const [density, setDensity] = useState("comfortable");
  const [deleteBusy, setDeleteBusy] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const visibleBooks = useMemo(() => books.filter(book =>
    (shelfStatus === "all" || book.status === shelfStatus) &&
    [book.title, book.author, book.isbn ?? ""].join(" ").toLocaleLowerCase().includes(search.trim().toLocaleLowerCase())
  ).sort((a, b) => b.updated_at.localeCompare(a.updated_at)), [books, search, shelfStatus]);
  const latestUpdate = books.map(book => book.updated_at).sort().at(-1);
  const averageProgress = books.length ? Math.round(books.reduce((sum, book) => sum + book.progress, 0) / books.length * 100) : 0;
  const [formOpen, setFormOpen] = useState(false);
  const [planBusy, setPlanBusy] = useState(false);
  const [planError, setPlanError] = useState<string | null>(null);
  const [isbnInput, setIsbnInput] = useState("");
  const [titleInput, setTitleInput] = useState("");
  const [authorInput, setAuthorInput] = useState("");
  const [statusInput, setStatusInput] = useState<Book["status"]>("reading");
  const [progressInput, setProgressInput] = useState("0");
  const [notesInput, setNotesInput] = useState("");
  const [editingBookId, setEditingBookId] = useState<string | null>(null);
  const [lookupNote, setLookupNote] = useState<string | null>(null);
  const [formError, setFormError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  // —— 认知档案（模型生成，挂引用） ——
  const [insights, setInsights] = useState<LibraryInsightsResult | null>(null);
  const [insightsBusy, setInsightsBusy] = useState(false);
  const [deleteBookId, setDeleteBookId] = useState<string | null>(null);
  // —— 批注转研究：annotation_id = "{book_id}:{index}"，与后端 create_research_from_annotation 的解析规则一致 ——
  const [convertId, setConvertId] = useState<string | null>(null);
  const [questionDraft, setQuestionDraft] = useState("");
  const [convertBusy, setConvertBusy] = useState(false);
  const [convertMsg, setConvertMsg] = useState<string | null>(null);

  const annotationRows = useMemo(
    () => books.flatMap((book) => book.notes.map((note, index) => ({ annotationId: `${book.book_id}:${index}`, bookTitle: book.title, note }))),
    [books],
  );

  async function handleConvertToResearch() {
    if (!convertId || convertBusy) return;
    const question = questionDraft.trim();
    if (!question) {
      setConvertMsg("请先写下要研究的具体问题");
      return;
    }
    setConvertBusy(true);
    setConvertMsg(null);
    try {
      const run = await onAnnotationToResearch(convertId, question);
      if (run) {
        setConvertId(null);
        setQuestionDraft("");
        setConvertMsg(`已创建研究记录（${run.run_id.slice(0, 8)}…）`);
      } else setConvertMsg("转研究失败，请重试。");
    } catch { setConvertMsg("转研究失败，请重试。"); }
    finally { setConvertBusy(false); }
  }

  const reading = books.filter((book) => book.status === "reading");
  const finished = books.filter((book) => book.status === "finished");
  const wishlist = books.filter((book) => book.status === "wishlist");

  function resetForm() {
    setIsbnInput("");
    setTitleInput("");
    setAuthorInput("");
    setStatusInput("reading");
    setProgressInput("0");
    setNotesInput("");
    setEditingBookId(null);
    setLookupNote(null);
    setFormError(null);
  }

  function beginCreate() {
    resetForm();
    setFormOpen(true);
  }

  function beginEdit(book: Book) {
    setEditingBookId(book.book_id);
    setIsbnInput(book.isbn ?? "");
    setTitleInput(book.title);
    setAuthorInput(book.author);
    setStatusInput(book.status);
    setProgressInput(String(Math.round(book.progress * 100)));
    setNotesInput(book.notes.join("\n"));
    setLookupNote(null);
    setFormError(null);
    setFormOpen(true);
  }

  async function handleLookup() {
    if (busy) return;
    const clean = isbnInput.replace(/[-\s]/g, "");
    if (!clean) {
      setLookupNote("请先填 ISBN（10 或 13 位数字）");
      return;
    }
    setBusy(true);
    setLookupNote(null);
    try {
      const result = await onLookupIsbn(clean);
      if (result === null) {
        setLookupNote("查询失败，可以重试或手动填写。");
        return;
      }
      if (result.available && result.title) {
        setTitleInput(result.title);
        setLookupNote(`已补全：${result.title}${result.publishers?.length ? ` · ${result.publishers.join(" / ")}` : ""}${result.pages ? ` · ${result.pages} 页` : ""}`);
      } else {
        setLookupNote(result.degraded_reason ?? "未查到书籍信息。");
      }
    } catch { setLookupNote("查询失败，可以重试或手动填写。"); }
    finally { setBusy(false); }
  }

  async function handleSave() {
    if (busy) return;
    if (!titleInput.trim()) {
      setFormError("书名必填");
      return;
    }
    if (!Number.isFinite(Number(progressInput)) || Number(progressInput) < 0 || Number(progressInput) > 100) {
      setFormError("阅读进度必须在 0 到 100 之间。");
      return;
    }
    setBusy(true);
    setFormError(null);
    const clean = isbnInput.replace(/[-\s]/g, "") || null;
    const payload = {
      title: titleInput.trim(),
      author: authorInput.trim(),
      isbn: clean,
      progress: Math.min(1, Math.max(0, Number(progressInput) / 100)),
      notes: notesInput.split("\n").map((item) => item.trim()).filter(Boolean),
      status: statusInput,
      source: "user" as const,
    };
    try {
      const created = editingBookId ? await onUpdateBook(editingBookId, payload) : await onCreateBook(payload);
      if (created === null) { setFormError("保存失败，请重试。"); return; }
      resetForm();
      setFormOpen(false);
    } catch { setFormError("保存失败，请重试。"); }
    finally { setBusy(false); }
  }

  return (
    <section className="page-view library-page">
      <header className="library-heading">
        <div><span className="kicker">阅读与研究</span><h2>研读工作台</h2></div>
        <button className="primary-button" disabled={busy} onClick={beginCreate}><Plus size={16} />添加书目</button>
      </header>
      <dl className="library-summary">
        <div><dt>阅读进度</dt><dd>{averageProgress}% <small>{books.length} 本书</small></dd></div>
        <div><dt>当前计划</dt><dd>{readingPlan?.source_chapter || "尚无计划"}</dd></div>
        <div><dt>最近更新</dt><dd>{latestUpdate ? new Date(latestUpdate).toLocaleDateString("zh-CN") : "暂无记录"}</dd></div>
      </dl>
      {(error || disconnected) && <div className="library-load-error" role="alert"><span>{disconnected ? "Core 未连接。" : error}{books.length > 0 ? " 当前显示上次读取的书架。" : ""}</span><button className="text-btn" disabled={loading} onClick={onRetry}>重新读取</button></div>}
      {loading && <p role="status">正在读取书架…</p>}

      <div className="library-workspace">
        <div className="library-shelf">
          <div className="sec-head">
            <div>
              <span className="kicker">{isDemo ? "书架（演示模式未接 Core）" : "书架 · 本机真实存储"}</span>
              <h3>{`在读 ${reading.length} · 已读 ${finished.length} · 想读 ${wishlist.length}`}</h3>
            </div>
            <button className="text-btn" disabled={busy} onClick={() => formOpen ? (resetForm(), setFormOpen(false)) : beginCreate()}>{formOpen ? "收起" : "添加书目"} <span>→</span></button>
          </div>

          <div className="library-filters">
            <label className="library-search"><Search size={16} /><input type="search" aria-label="搜索书架" value={search} onChange={event => setSearch(event.target.value)} placeholder="书名、作者或 ISBN" /></label>
            <select aria-label="书架状态筛选" value={shelfStatus} onChange={event => setShelfStatus(event.target.value as typeof shelfStatus)}>
              <option value="all">全部书目</option><option value="reading">在读</option><option value="finished">已读</option><option value="wishlist">想读</option>
            </select>
            <select aria-label="书架密度" value={density} onChange={event => setDensity(event.target.value)}>
              <option value="comfortable">舒适</option><option value="compact">紧凑</option>
            </select>
          </div>
          {formOpen && (
            <div className="panel mb-14">
              <div className="form-row">
                <label className="form-field">
                  <span>ISBN（可选 · 10/13 位，填了可自动补全书名）</span>
                  <input type="text" disabled={busy} value={isbnInput} onChange={(event) => setIsbnInput(event.target.value)} placeholder="9787111111111" />
                </label>
                <label className="form-field">
                  <span>&nbsp;</span>
                  <button className="text-btn" aria-label="查询 ISBN" onClick={handleLookup} disabled={busy}>{busy ? "查询中…" : "查 Open Library 补全"}</button>
                </label>
              </div>
              <div className="form-row mt-10">
                <label className="form-field">
                  <span>书名（必填）</span>
                  <input type="text" disabled={busy} value={titleInput} onChange={(event) => setTitleInput(event.target.value)} placeholder="聪明的投资者" />
                </label>
                <label className="form-field">
                  <span>作者</span>
                  <input type="text" disabled={busy} value={authorInput} onChange={(event) => setAuthorInput(event.target.value)} placeholder="格雷厄姆" />
                </label>
              </div>
              <div className="form-row mt-10">
                <label className="form-field">
                  <span>书架状态</span>
                  <select value={statusInput} onChange={(event) => setStatusInput(event.target.value as Book["status"])}>
                    <option value="reading">在读</option>
                    <option value="finished">已读</option>
                    <option value="wishlist">想读</option>
                  </select>
                </label>
                <label className="form-field">
                  <span>阅读进度（0-100%）</span>
                  <input value={progressInput} onChange={(event) => setProgressInput(event.target.value)} inputMode="numeric" type="number" min="0" max="100" step="1" />
                </label>
              </div>
              <div className="form-row mt-10">
                <label className="form-field span-2">
                  <span>阅读笔记（每行一条，可选）</span>
                  <textarea value={notesInput} onChange={(event) => setNotesInput(event.target.value)} rows={3} placeholder="记录关键章节、问题或批注" />
                </label>
              </div>
              <div className="form-row mt-10">
                <label className="form-field">
                  <span>&nbsp;</span>
                  <button className="text-btn" aria-label={editingBookId ? "保存修改" : "保存到书架"} onClick={handleSave} disabled={busy}>{busy ? "保存中…" : editingBookId ? "保存修改" : "保存到书架"}</button>
                </label>
              </div>
              {lookupNote && <p className="form-error muted">{lookupNote}</p>}
              {formError && <p className="form-error">{formError}</p>}
            </div>
          )}

            <div className="panel-grad">
            {isDemo ? (
                <div className="shelf-demo">
                <div className="sd-row">
                  <span className="sd-name">《聪明的投资者》</span>
                  <span className="mono-note">演示数据 · 读到 62%</span>
                </div>
                <div className="sd-row">
                  <span className="sd-name">《置身事内》</span>
                  <span className="mono-note">演示数据 · 读到 34%</span>
                </div>
                <div className="sd-row">
                  <span className="sd-name">模型推荐 · 《周期》</span>
                  <span className="mono-note violet">演示数据</span>
                </div>
              </div>
            ) : books.length === 0 && !loading && !error && !disconnected ? (
              <p className="sub-lead">
                暂无书目。
              </p>
            ) : (
              <div className={`book-list ${density}`}>
                {books.length > 0 && visibleBooks.length === 0 && <div className="library-no-results"><p>没有匹配的书目。</p><button className="text-btn" onClick={() => { setSearch(""); setShelfStatus("all"); }}>清除筛选</button></div>}
                {visibleBooks.map((book) => (
                  <div key={book.book_id} className="book-card">
                    <div className="book-cover" data-status={book.status} aria-hidden="true"><BookOpen size={18} /><strong>{book.title}</strong><span>{book.author || "作者未记录"}</span></div>
                    <span className="book-title">
                      {book.title}
                      {book.author ? <span className="bt-sub"> · {book.author}</span> : null}
                      {book.isbn ? <span className="bt-sub"> · {book.isbn}</span> : null}
                    </span>
                    <span className="book-meta">
                      <span className={`soft-tag ${book.status === "reading" ? "blue" : book.status === "finished" ? "mint" : "amber"}`}>
                        {STATUS_LABEL[book.status]}{book.source !== "user" ? ` · ${SOURCE_LABEL[book.source]}` : ""}
                      </span>
                      <progress max={1} value={book.progress} aria-label={`${book.title}阅读进度`} /><span className="mono-note">{Math.round(book.progress * 100)}%</span>
                      <button className="text-btn" disabled={busy} title={`编辑${book.title}`} aria-label={`编辑${book.title}`} onClick={() => beginEdit(book)}><Pencil size={16} /></button>
                      <button className="text-btn coral" disabled={busy} title={`删除${book.title}`} aria-label={`删除${book.title}`} onClick={() => { setDeleteError(null); setDeleteBookId(book.book_id); }}><Trash2 size={16} /></button>
                    </span>
                  </div>
                ))}
              </div>
            )}
          </div>

        </div>

        <aside className="library-plan">
          <div className="sec-head">
            <div>
              <span className="kicker">今日荐读</span>
              <h3>{isDemo ? "今天的 20 分钟（演示数据）" : "当前研读计划"}</h3>
            </div>
            <button className="text-btn" onClick={() => onNavigate("review")}>去复盘 <span>→</span></button>
          </div>
          {isDemo ? (
            <div className="action-list tight">
              <button className="action-item">
                <span className="item-num">01</span>
                <span className="item-copy">
                  <b>《聪明的投资者》第 8 章 · 市场先生（演示数据）</b>
                  <small>配合今天「波动收窄」的观察做对照笔记，约 20 分钟。</small>
                </span>
                <span className="mode research">研读</span>
              </button>
              <button className="action-item" onClick={() => onNavigate("review")}>
                <span className="item-num">02</span>
                <span className="item-copy">
                  <b>昨日批注回顾 · 2 条（演示数据）</b>
                  <small>把「安全边际」的批注转成一条研究问题，写入研究页。</small>
                </span>
                <span className="mode review_plan">转研究</span>
              </button>
            </div>
          ) : (
            <div className="panel mt-12">
              {readingPlan ? (
                <div className="action-item static">
                  <span className="item-num">01</span>
                  <span className="item-copy">
                    <b>{`${readingPlan.source_chapter} · ${new Date(readingPlan.created_at).toLocaleDateString("zh-CN")}`}</b>
                    {readingPlan.book_ref && <small>来源书目：{books.find(book => book.book_id === readingPlan.book_ref)?.title ?? "书目已移除"}</small>}
                    <small>{readingPlan.daily_task}</small>
                    <small className="faint">{`依据：${readingPlan.rationale}`}</small>
                  </span>
                  <span className="mode research">研读</span>
                </div>
              ) : (
                <p className="sub-lead">
                  暂无研读计划。
                </p>
              )}
              <div className="card-actions wide">
                <button className="text-btn" disabled={planBusy} onClick={async () => { setPlanBusy(true); setPlanError(null); try { const plan = await onGeneratePlan(readingPlan?.book_ref ?? null); if (plan === null) setPlanError("生成失败，请重试。"); } catch { setPlanError("生成失败，请重试。"); } finally { setPlanBusy(false); } }}>
                  {planBusy ? "生成中…" : readingPlan ? "换一份荐读" : "生成今日荐读"} <span>→</span>
                </button>
                {planError && <span className="form-error flat">{planError}</span>}
              </div>

            </div>
          )}

          {!isDemo && (
            <div className="panel mt-14">
              <div className="sec-head">
                <div>
                  <span className="kicker">认知档案</span>
                  <h3>知识结构与引用</h3>
                </div>
                <button
                  className="text-btn"
                  disabled={insightsBusy || books.length === 0 || !aiReady}
                  title={aiReady ? undefined : "AI 未接通：先在 设置 → 模型配置 添加方案并设为「使用中」"}
                  onClick={async () => {
                    setInsightsBusy(true);
                    setInsights(null);
                    try {
                      const result = await onGenerateInsights();
                      setInsights(result ?? { ok: false, detail: "认知档案生成失败，请重试。" });
                    } catch { setInsights({ ok: false, detail: "认知档案生成失败，请重试。" }); }
                    finally { setInsightsBusy(false); }
                  }}
                >
                  {insightsBusy ? "生成中…" : insights ? "重新生成" : "生成认知档案"} <span>→</span>
                </button>
              </div>
              {books.length === 0 && (
                <p className="sub-lead tight">暂无书目，无法生成认知档案。</p>
              )}
              {insights && !insights.ok && (
                <div className="note-rule warn">
                  <b>未生成（{insights.stage ?? "unknown"}）</b>{insights.detail}
                </div>
              )}
              {insights?.ok && (
                <div className="insights-body">
                  {insights.insights}
                  <div className="insights-cite">
                    引用书目：{insights.citations?.join("、")} · {insights.model}（{insights.latency_ms}ms）· 不构成投资建议
                  </div>
                </div>
              )}
            </div>
          )}

          {!isDemo && (
            <div className="panel mt-14">
              <div className="sec-head">
                <div>
                  <span className="kicker">批注回顾</span>
                  <h3>把书里的批注变成研究问题</h3>
                </div>
              </div>
              {annotationRows.length === 0 ? (
                <p className="sub-lead">
                  暂无阅读批注。
                </p>
              ) : (
                <div className="annotation-list">
                  {annotationRows.map(({ annotationId, bookTitle, note }) => (
                    <div key={annotationId} className="annotation-card">
                      <div className="ac-head">
                        <span className="ac-note">{note}</span>
                        <span className="ac-book">《{bookTitle}》</span>
                      </div>
                      {convertId === annotationId ? (
                        <div className="form-row mt-6">
                          <input value={questionDraft} onChange={(event) => setQuestionDraft(event.target.value)} placeholder="要研究的具体问题（如：安全边际在当下的 A 股如何量化？）" aria-label="研究问题" />
                          <button className="text-btn" disabled={convertBusy} onClick={() => void handleConvertToResearch()}>{convertBusy ? "提交中…" : "提交"} <span>→</span></button>
                          <button className="text-btn" onClick={() => { setConvertId(null); setQuestionDraft(""); }} disabled={convertBusy}>取消</button>
                        </div>
                      ) : (
                        <button className="text-btn self-start" onClick={() => { setConvertId(annotationId); setQuestionDraft(""); setConvertMsg(null); }}>转研究问题 <span>→</span></button>
                      )}
                    </div>
                  ))}
                </div>
              )}
              {convertMsg && <span className="probe-note">{convertMsg}</span>}
            </div>
          )}
          {/* 「双挂载」架构说明已收纳至顶部「设计与数据边界说明」折叠区（v6 UI 优化） */}
        </aside>

      </div>
      {deleteBookId && books.find((book) => book.book_id === deleteBookId) && (
        <ConfirmDialog
          title="从书架删除这本书"
          summary="书目、进度和笔记会从本机书架移除，已生成的研究记录不受影响。"
          diffs={[{ label: "书目", before: books.find((book) => book.book_id === deleteBookId)?.title ?? "", after: "删除" }]}
          confirmLabel="确认删除"
          busy={deleteBusy}
          error={deleteError}
          onConfirm={async () => {
            if (deleteBusy) return;
            setDeleteBusy(true); setDeleteError(null);
            try { const ok = await onDeleteBook(deleteBookId); if (ok) setDeleteBookId(null); else setDeleteError("删除失败，请重试。"); }
            catch { setDeleteError("删除失败，请重试。"); }
            finally { setDeleteBusy(false); }
          }}
          onCancel={() => setDeleteBookId(null)}
        />
      )}
    </section>
  );
}
