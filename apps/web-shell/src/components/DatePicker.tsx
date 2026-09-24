import { useEffect, useRef, useState } from "react";
import { Calendar } from "lucide-react";

/**
 * D04（2026-09-19 用户要求）：日期一律走日历，且配色跟随项目主题。
 *
 * 浏览器原生日历弹层只认 `color-scheme`，选中色/表头色由 UA 决定，与深色金融终端的
 * 令牌（`--mint` / `--line` / `--panel`）对不上，所以这里自带一份月历；输入框保留，
 * 手填与粘贴照旧可用（历史上多处依赖 `YYYYMMDD` 文本框，测试也按 placeholder 取它）。
 *
 * 值格式沿用各处既有约定：`compact` = `YYYYMMDD`（游资雷达），否则 = `YYYY-MM-DD`。
 */

const WEEKDAYS = ["一", "二", "三", "四", "五", "六", "日"];

function pad(value: number): string {
  return String(value).padStart(2, "0");
}

function isoOf(date: Date): string {
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
}

function todayIso(): string {
  return isoOf(new Date());
}

/** `YYYYMMDD` → `YYYY-MM-DD`；不合格式返回空串。 */
export function compactToIso(value: string): string {
  return /^\d{8}$/.test(value) ? `${value.slice(0, 4)}-${value.slice(4, 6)}-${value.slice(6, 8)}` : "";
}

/** `YYYY-MM-DD` → `YYYYMMDD`。 */
export function isoToCompact(iso: string): string {
  return iso.replaceAll("-", "");
}

interface Props {
  /** 受控值，格式由 `compact` 决定；允许处于输入中的不完整状态。 */
  value: string;
  onChange: (next: string) => void;
  /** true → `YYYYMMDD`；false → `YYYY-MM-DD`。 */
  compact?: boolean;
  ariaLabel: string;
  placeholder?: string;
}

interface Cell {
  iso: string;
  day: number;
  /** 属于上/下月（仍可选，标灰）。 */
  other: boolean;
}

function monthCells(cursor: string): Cell[] {
  const parts = cursor.split("-").map(Number);
  const year = parts[0] ?? new Date().getFullYear();
  const month = parts[1] ?? 1;
  const first = new Date(year, month - 1, 1);
  // 周一为一周起点：把 1 日往前推到上一个周一。
  const shift = (first.getDay() + 6) % 7;
  const start = new Date(first);
  start.setDate(first.getDate() - shift);
  return Array.from({ length: 42 }, (_, index) => {
    const date = new Date(start);
    date.setDate(start.getDate() + index);
    return { iso: isoOf(date), day: date.getDate(), other: date.getMonth() !== month - 1 };
  });
}

export function DatePicker({ value, onChange, compact = false, ariaLabel, placeholder }: Props) {
  const rootRef = useRef<HTMLLabelElement>(null);
  const [open, setOpen] = useState(false);
  const iso = compact ? compactToIso(value) : value;
  const validIso = /^\d{4}-\d{2}-\d{2}$/.test(iso) ? iso : "";
  const [cursor, setCursor] = useState(() => (validIso ? validIso.slice(0, 7) : todayIso().slice(0, 7)));

  useEffect(() => {
    if (!open) return;
    setCursor((validIso || todayIso()).slice(0, 7));
    const onPointerDown = (event: PointerEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) setOpen(false);
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    document.addEventListener("pointerdown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("pointerdown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [open, validIso]);

  function shiftMonth(delta: number): void {
    const parts = cursor.split("-").map(Number);
    const next = new Date(parts[0] ?? new Date().getFullYear(), (parts[1] ?? 1) - 1 + delta, 1);
    setCursor(`${next.getFullYear()}-${pad(next.getMonth() + 1)}`);
  }

  function commit(nextIso: string): void {
    onChange(compact ? isoToCompact(nextIso) : nextIso);
    setCursor(nextIso.slice(0, 7));
    setOpen(false);
  }

  const [cursorYear, cursorMonth] = cursor.split("-");
  return (
    <label className="date-pick" ref={rootRef}>
      <input
        className="mono"
        value={value}
        inputMode="numeric"
        placeholder={placeholder ?? (compact ? "YYYYMMDD" : "YYYY-MM-DD")}
        maxLength={compact ? 8 : 10}
        aria-label={ariaLabel}
        onChange={(event) => onChange(event.target.value)}
      />
      <button
        type="button"
        className="date-pick-btn"
        aria-label={`${ariaLabel}：打开日历`}
        aria-expanded={open}
        onClick={() => setOpen((current) => !current)}
      >
        <Calendar size={14} aria-hidden="true" />
      </button>
      {open && (
        <div className="date-pick-pop" role="dialog" aria-label={`${ariaLabel} 日历`}>
          <div className="date-pick-head">
            <button type="button" className="tag-button" aria-label="上一个月" onClick={() => shiftMonth(-1)}>‹</button>
            <b>{cursorYear} 年 {cursorMonth} 月</b>
            <button type="button" className="tag-button" aria-label="下一个月" onClick={() => shiftMonth(1)}>›</button>
          </div>
          <div className="date-pick-week" aria-hidden="true">{WEEKDAYS.map((day) => <span key={day}>{day}</span>)}</div>
          <div className="date-pick-grid">
            {monthCells(cursor).map((cell) => (
              <button
                key={cell.iso}
                type="button"
                className={`date-pick-day ${cell.iso === validIso ? "is-selected" : ""} ${cell.other ? "is-other" : ""} ${cell.iso === todayIso() ? "is-today" : ""}`}
                aria-label={cell.iso}
                aria-pressed={cell.iso === validIso}
                onClick={() => commit(cell.iso)}
              >
                {cell.day}
              </button>
            ))}
          </div>
          <div className="date-pick-foot">
            <button type="button" className="text-button" onClick={() => commit(todayIso())}>今天</button>
            <button type="button" className="text-button" onClick={() => { onChange(""); setOpen(false); }}>清除</button>
          </div>
        </div>
      )}
    </label>
  );
}
