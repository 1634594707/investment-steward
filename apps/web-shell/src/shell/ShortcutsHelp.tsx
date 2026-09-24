import { useEffect } from "react";
import { SHORTCUTS } from "../state/shortcuts";
import { useFocusTrap } from "../components/useFocusTrap";

interface Props {
  open: boolean;
  onClose: () => void;
}

/**
 * 快捷键速查（F1-2）：`?` 或 Electron 帮助菜单呼出。
 * Electron 菜单经 webContents.send → preload 派发 `steward:show-shortcuts` 事件，
 * AppShell 监听该事件后置 open=true。
 */
export function ShortcutsHelp({ open, onClose }: Props) {
  const dialogRef = useFocusTrap<HTMLDivElement>(open);

  useEffect(() => {
    if (!open) return;
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") onClose();
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [open, onClose]);

  if (!open) return null;
  return (
    <div className="confirm-overlay" onClick={onClose}>
      <div className="shortcuts-help" role="dialog" aria-modal="true" aria-label="快捷键速查" onClick={(event) => event.stopPropagation()} ref={dialogRef}>
        <header className="confirm-head">
          <span className="section-kicker">KEYBOARD FIRST</span>
          <h3>快捷键速查</h3>
          <p>键盘优先是这套桌面的默认操作方式：跳页、切持仓、开面板都不必碰鼠标。</p>
        </header>
        <div className="mt-8">
          {SHORTCUTS.map((item) => (
            <div className="shortcuts-row" key={item.keys}>
              <span className="keys">
                {item.keys.split(" ").map((key, index) => (
                  <kbd className="kbd" key={`${key}-${index}`}>{key}</kbd>
                ))}
              </span>
              <span className="desc">
                {item.action}
                <small>{item.scope}</small>
              </span>
            </div>
          ))}
        </div>
        <footer className="confirm-foot">
          <button className="secondary-button" onClick={onClose}>
            关闭（Esc）
          </button>
        </footer>
      </div>
    </div>
  );
}
