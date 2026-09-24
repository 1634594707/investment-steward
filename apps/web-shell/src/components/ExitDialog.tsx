import { useState } from "react";
import { useFocusTrap } from "./useFocusTrap";

export interface ExitDialogProps {
  open: boolean;
  onClose: () => void; // 取消
  onConfirm: (action: "tray" | "quit", remember: boolean) => void;
}

export function ExitDialog({ open, onClose, onConfirm }: ExitDialogProps) {
  const [selectedAction, setSelectedAction] = useState<"tray" | "quit">("tray");
  const [remember, setRemember] = useState(false);
  const trapRef = useFocusTrap<HTMLDivElement>(open);

  if (!open) return null;

  return (
    <div className="confirm-overlay" onClick={onClose}>
      <aside
        className="confirm-dialog exit-dialog"
        role="dialog"
        aria-modal="true"
        aria-label="关闭应用确认"
        onClick={(e) => e.stopPropagation()}
        ref={trapRef}
      >
        <header className="confirm-head">
          <span className="section-kicker">APPLICATION LIFECYCLE</span>
          <h3>关闭 Investment Steward</h3>
          <p>请选择关闭窗口后的运行模式：</p>
        </header>

        <div className="exit-options-grid">
          <div
            className={`exit-option-card ${selectedAction === "tray" ? "selected" : ""}`}
            onClick={() => setSelectedAction("tray")}
          >
            <div className="exit-option-header">
              <div className="exit-radio-circle">
                {selectedAction === "tray" && <div className="exit-radio-inner" />}
              </div>
              <span className="exit-option-title">保留到系统托盘（推荐）</span>
            </div>
            <p className="exit-option-desc">
              Core 与插件后台持续运行，保持宏观预警与资产监控，可从托盘图标秒级唤醒。
            </p>
          </div>

          <div
            className={`exit-option-card ${selectedAction === "quit" ? "selected" : ""}`}
            onClick={() => setSelectedAction("quit")}
          >
            <div className="exit-option-header">
              <div className="exit-radio-circle">
                {selectedAction === "quit" && <div className="exit-radio-inner" />}
              </div>
              <span className="exit-option-title">完全退出程序</span>
            </div>
            <p className="exit-option-desc">
              安全保存全部状态并停止 Core 及所有后台 Sidecar 进程，释放全部系统资源。
            </p>
          </div>
        </div>

        <label className="exit-remember-checkbox">
          <input
            type="checkbox"
            checked={remember}
            onChange={(e) => setRemember(e.target.checked)}
          />
          <span>记住我的选择，以后不再询问（可随时在设置中修改）</span>
        </label>

        <footer className="confirm-foot exit-dialog-foot">
          <button type="button" className="btn btn-ghost" onClick={onClose}>
            取消
          </button>
          <button
            type="button"
            className="btn btn-primary"
            onClick={() => onConfirm(selectedAction, remember)}
          >
            确认执行
          </button>
        </footer>
      </aside>
    </div>
  );
}
