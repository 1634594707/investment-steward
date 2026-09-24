import { useEffect, useState } from "react";
import type { CoreConnection } from "../state/coreClient";
import { BrandMark, IconMenu, IconWinClose, IconWinMax, IconWinMin } from "./icons";
import { ExitDialog } from "../components/ExitDialog";

interface Props {
  connection: CoreConnection;
  coreVersion?: string;
  railCollapsed: boolean;
  onToggleRail: () => void;
  context: string;
}

/** 窗口最大化状态（F3-3）：Electron 主进程在 maximize/unmaximize 时推送；浏览器环境恒为 false。 */
function useMaximized(): boolean {
  const [maximized, setMaximized] = useState(false);
  useEffect(() => {
    const bridge = window.stewardWin;
    if (!bridge?.onWindowState) return;
    bridge.onWindowState((value) => setMaximized(Boolean(value)));
  }, []);
  return maximized;
}

/** 桌面标题栏：Electron 中为 frameless 窗口拖拽区（双击切换最大化）；浏览器中窗口三键为占位。 */
export function TitleBar({ connection, coreVersion = "—", railCollapsed, onToggleRail, context }: Props) {
  const maximized = useMaximized();
  const [exitDialogOpen, setExitDialogOpen] = useState(false);

  async function handleCloseClick() {
    const bridge = window.stewardWin;
    if (!bridge) return;
    // 如果主进程提供了自定义关闭检查
    if (bridge.getRememberedCloseAction && bridge.performCloseAction) {
      try {
        const remembered = await bridge.getRememberedCloseAction();
        if (remembered === "tray" || remembered === "quit") {
          await bridge.performCloseAction(remembered, false);
          return;
        }
      } catch {
        /* 读取失败则打开弹窗 */
      }
      setExitDialogOpen(true);
    } else {
      void bridge.close();
    }
  }

  async function handleConfirmExit(action: "tray" | "quit", remember: boolean) {
    setExitDialogOpen(false);
    const bridge = window.stewardWin;
    if (bridge?.performCloseAction) {
      await bridge.performCloseAction(action, remember);
    } else {
      void bridge?.close();
    }
  }

  function winControl(action: "minimize" | "maximize") {
    void window.stewardWin?.[action]();
  }

  const chip = { text: `Core ${coreVersion} · ${connection === "ready" ? "已连接" : connection === "demo" ? "演示模式" : connection === "connecting" ? "连接中" : "未连接"}`, on: connection === "ready" };

  return (
    <>
      <header className="titlebar">
        <button className={`tb-btn hamb ${railCollapsed ? "active" : ""}`} aria-label={railCollapsed ? "展开侧栏" : "折叠侧栏"} aria-expanded={!railCollapsed} title={railCollapsed ? "展开侧栏" : "折叠侧栏"} onClick={onToggleRail}>
          <IconMenu />
        </button>
        <BrandMark />
        <span className="tb-name">Investment Steward</span>
        <span className="tb-sub">{context}</span>
        <span className={`tb-core ${chip.on ? "on" : ""}`}><span className="dot" />{chip.text}</span>
        <div className="tb-win">
          <button className="tb-btn" aria-label="最小化" onClick={() => winControl("minimize")}><IconWinMin /></button>
          <button className="tb-btn" aria-label={maximized ? "还原窗口" : "最大化窗口"} title={maximized ? "还原" : "最大化"} aria-pressed={maximized} onClick={() => winControl("maximize")}>
            <span className={`tb-max-ico ${maximized ? "restore" : ""}`}><IconWinMax /></span>
          </button>
          <button className="tb-btn close" aria-label="关闭" onClick={handleCloseClick}><IconWinClose /></button>
        </div>
      </header>
      <ExitDialog
        open={exitDialogOpen}
        onClose={() => setExitDialogOpen(false)}
        onConfirm={handleConfirmExit}
      />
    </>
  );
}
