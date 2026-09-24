import { resolveUiCardRenderer, UI_CARD_RENDERERS } from "./index";

/**
 * 渲染器契约测试。本仓 `typecheck`/`test` 均以 tsc 作为运行器（noEmit，编译期断言）。
 * 覆盖：白名单四值冻结、resolve 返回类型稳定（始终落在渲染器联合类型内）。
 */

// 白名单冻结为四值：新增渲染器必须先补内核实现（apps/web-shell/src/cards/renderers.tsx）再扩此处。
const _rendererArity: readonly ["card", "summary_row", "inline", "silent"] = UI_CARD_RENDERERS;

// resolve 的返回类型必须始终落在渲染器联合类型内；若签名改变，以下两行在编译期报错。
const fromNull: "card" | "summary_row" | "inline" | "silent" = resolveUiCardRenderer(null);
const fromUndefined: "card" | "summary_row" | "inline" | "silent" = resolveUiCardRenderer(undefined);

// 即便输入被类型系统绕过（as never），返回类型也必须仍是合法渲染器。
const escaped: "card" | "summary_row" | "inline" | "silent" = resolveUiCardRenderer("bogus" as never);

void fromNull;
void fromUndefined;
void escaped;