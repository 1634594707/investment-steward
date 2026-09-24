import { useEffect, useMemo, useRef, useState } from "react";
import { useFocusTrap } from "../components/useFocusTrap";

/** 命令面板命令（F1-1）：静态导航/动作 + 动态项（持仓/研究/插件）统一由此描述。 */
export interface PaletteCommand {
  id: string;
  label: string;
  hint?: string;
  /** 分组名（命令面板内按组呈现：导航 / 动作 / 持仓 / 研究 / 插件）。 */
  group: string;
  keywords?: string;
  run: () => void;
}

/** 朴素模糊匹配：完整包含 > 子序列；得分高者在前，同分保持注册顺序。 */
function score(command: PaletteCommand, query: string): number {
  if (!query) return 1;
  const haystack = `${command.label} ${command.hint ?? ""} ${command.keywords ?? ""}`.toLowerCase();
  const needle = query.toLowerCase();
  const includeAt = haystack.indexOf(needle);
  if (includeAt === 0) return 100;
  if (includeAt > 0) return 60;
  let cursor = 0;
  let hits = 0;
  for (const char of needle) {
    const found = haystack.indexOf(char, cursor);
    if (found === -1) return 0;
    hits += cursor === found ? 2 : 1;
    cursor = found + 1;
  }
  return hits;
}

interface Props {
  open: boolean;
  commands: PaletteCommand[];
  onClose: () => void;
}

/** Ctrl+K 命令面板（对标 VS Code / Linear / Raycast）：模糊搜索命令与对象，键盘上下选择、Enter 执行、Esc 关闭。 */
export function CommandPalette({ open, commands, onClose }: Props) {
  const [query, setQuery] = useState("");
  const [index, setIndex] = useState(0);
  const inputRef = useRef<HTMLInputElement | null>(null);
  const listRef = useFocusTrap<HTMLDivElement>(open);

  useEffect(() => {
    if (open) {
      setQuery("");
      setIndex(0);
      requestAnimationFrame(() => inputRef.current?.focus());
    }
  }, [open]);

  const filtered = useMemo(() => {
    return commands
      .map((command) => ({ command, score: score(command, query.trim()) }))
      .filter((entry) => entry.score > 0)
      .sort((a, b) => b.score - a.score)
      .map((entry) => entry.command);
  }, [commands, query]);

  useEffect(() => {
    setIndex(0);
  }, [query]);

  if (!open) return null;

  function execute(command: PaletteCommand | undefined) {
    if (!command) return;
    onClose();
    command.run();
  }

  function onKeyDown(event: React.KeyboardEvent) {
    if (event.key === "ArrowDown") {
      event.preventDefault();
      setIndex((current) => Math.min(current + 1, filtered.length - 1));
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      setIndex((current) => Math.max(current - 1, 0));
    } else if (event.key === "Enter") {
      event.preventDefault();
      execute(filtered[index]);
    }
  }

  let lastGroup = "";
  return (
    <div className="palette-overlay" onClick={onClose}>
      <div className="palette" role="dialog" aria-modal="true" aria-label="命令面板" onClick={(event) => event.stopPropagation()} ref={listRef} onKeyDown={onKeyDown}>
        <div className="palette-input">
          <input
            ref={inputRef}
            value={query}
            placeholder="搜索命令、页面、持仓、研究……"
            aria-label="搜索命令"
            onChange={(event) => setQuery(event.target.value)}
          />
          <span className="palette-count">{filtered.length} 项</span>
        </div>
        <div className="palette-list" role="listbox" aria-label="命令列表">
          {filtered.length === 0 && <div className="palette-empty">没有匹配的命令。输入以搜索，Esc 关闭。</div>}
          {filtered.map((command, itemIndex) => {
            const groupHeader = command.group !== lastGroup ? (lastGroup = command.group) : null;
            return (
              <div key={command.id}>
                {groupHeader && <div className="palette-group">{groupHeader}</div>}
                <button
                  className={`palette-item ${itemIndex === index ? "selected" : ""}`}
                  role="option"
                  aria-selected={itemIndex === index}
                  onMouseEnter={() => setIndex(itemIndex)}
                  onClick={() => execute(command)}
                >
                  <span>
                    <b>{command.label}</b>
                    {command.hint && <small>{command.hint}</small>}
                  </span>
                  {command.keywords && <span className="mono">{command.keywords}</span>}
                </button>
              </div>
            );
          })}
        </div>
        <div className="palette-foot">
          <span>↑↓ 选择</span>
          <span>Enter 执行</span>
          <span>Esc 关闭</span>
        </div>
      </div>
    </div>
  );
}
