import { useState } from "react";
import { createRoot } from "react-dom/client";
import { QuantPage } from "../src/routes/QuantPage";
import "../src/styles.css";

function Fixture() {
  const [fail, setFail] = useState(true);
  const [calls, setCalls] = useState(0);
  async function request() {
    setCalls(value => value + 1);
    await new Promise(resolve => setTimeout(resolve, 100));
    if (fail) throw new Error("Fixture failure");
  }
  return <>
    <label><input type="checkbox" checked={fail} onChange={event => setFail(event.target.checked)} />模拟失败</label><output aria-label="请求次数">{calls}</output>
    <main><QuantPage pool={null} isDemo={false} marketPluginEnabled onNavigate={() => undefined}
      onListParameterSets={async () => []} onFetchStages={async () => []} onFetchStrategyPacks={async () => []}
      onFactorMine={async symbol => { await request(); return { available: true, symbol, top: [{ formula: "ret_5", formula_tokens: ["ret_5"], train_ic: 0.2, valid_ic: 0.1, samples: 250 }], as_of: "2026-09-04", dataset_version: "fixture" }; }}
      onPublishParameterSet={async () => { await request(); return { ok: true }; }}
      onForkParameterSet={async () => { await request(); return null; }}
      onReplayParameterSet={async () => { await request(); return null; }}
      onLineageParameterSet={async () => []}
      onTrainModel={async () => { await request(); return { ok: true }; }}
      onImportStrategyPack={async () => { await request(); return { ok: true }; }}
      onRunStrategyPack={async () => { await request(); return null; }}
      onAddTrackRecord={async () => { await request(); return { ok: true }; }}
      onVerifyTrackRecord={async () => { await request(); return { ok: true }; }}
    /></main>
  </>;
}
createRoot(document.getElementById("root")!).render(<Fixture />);
