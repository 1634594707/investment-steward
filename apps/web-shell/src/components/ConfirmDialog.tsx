import { useEffect, useState } from "react";
import { useFocusTrap } from "./useFocusTrap";

interface DiffItem {
  label: string;
  before: string;
  after: string;
}

interface Props {
  title: string;
  summary: string;
  diffs: DiffItem[];
  confirmLabel?: string;
  cancelLabel?: string;
  requireNote?: boolean;
  /**
   * M5-F02：显示说明框但**不强制**填写（自选删除 `requireNote={false}` 直接隐藏输入框；
   * 持仓删除要「保留说明框、改为选填」，两种语义必须分开，不能用一个布尔混用）。
   * 生效优先级：`requireNote=true` → 显示且必填；否则 `noteOptional=true` → 显示且选填；否则不显示。
   */
  noteOptional?: boolean;
  busy?: boolean;
  error?: string | null;
  onConfirm: (summaryText: string) => void;
  onCancel: () => void;
}

/**
 * 需确认动作的统一入口。原则草案/动作等未经显式确认（explicit_ui）不能生效；
 * 确认时要求用户手打一句确认说明，连同 diff 一起走审计。
 * F1-3：打开即聚焦、Tab 焦点圈定在对话框内、关闭后焦点还原。
 */
export function ConfirmDialog({
  title,
  summary,
  diffs,
  confirmLabel = "确认并启用",
  cancelLabel = "取消",
  requireNote = true,
  noteOptional = false,
  busy = false,
  error = null,
  onConfirm,
  onCancel,
}: Props) {
  const [confirmText, setConfirmText] = useState("");
  const dialogRef = useFocusTrap<HTMLDivElement>(true);
  const canConfirm = !busy && (!requireNote || confirmText.trim().length >= 4);
  const showNote = requireNote || noteOptional;

  // F1-2：路由局部实例（复盘/投资/设置页内部弹窗）不经 AppShell 弹层栈，自行响应 Esc。
  useEffect(() => {
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape" && !busy) onCancel();
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [busy, onCancel]);

  return (
    <div className="confirm-overlay" onClick={() => { if (!busy) onCancel(); }}>
      <aside
        className="confirm-dialog"
        role="dialog"
        aria-modal="true"
        aria-label={title}
        onClick={(event) => event.stopPropagation()}
        ref={dialogRef}
      >
        <header className="confirm-head">
          <span className="section-kicker">EXPLICIT CONFIRMATION</span>
          <h3>{title}</h3>
          <p>{summary}</p>
        </header>

        <div className="confirm-diffs">
          {diffs.map((item) => (
            <div className="confirm-diff" key={item.label}>
              <div className="confirm-diff-label">{item.label}</div>
              <div className="confirm-diff-columns">
                <div className="confirm-diff-before">
                  <span>当前</span>
                  <pre>{item.before}</pre>
                </div>
                <div className="confirm-diff-after">
                  <span>变更后</span>
                  <pre>{item.after}</pre>
                </div>
              </div>
            </div>
          ))}
        </div>

        {showNote && (
          <label className="confirm-note">
            <span>{requireNote ? "确认说明（必填，将写入审计记录）" : "确认说明（选填，留空则审计记为固定说明）"}</span>
            <textarea
              value={confirmText}
              onChange={(event) => setConfirmText(event.target.value)}
              placeholder={requireNote ? "用一句话说明这次确认的原因…" : "可以留空；填了会写进审计记录…"}
              rows={2}
            />
          </label>
        )}
        {error && <p className="form-error" role="alert">{error}</p>}

        <footer className="confirm-foot">
          <button className="secondary-button" disabled={busy} onClick={onCancel}>
            {cancelLabel}
          </button>
          <button
            className="primary-button confirm-primary"
            disabled={!canConfirm}
            onClick={() => canConfirm && onConfirm(confirmText.trim())}
          >
            {busy ? "处理中…" : confirmLabel}
          </button>
        </footer>
      </aside>
    </div>
  );
}
