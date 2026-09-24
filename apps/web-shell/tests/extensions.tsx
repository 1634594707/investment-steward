import { useState } from "react";
import { createRoot } from "react-dom/client";
import { ExtensionsPage } from "../src/routes/ExtensionsPage";
import { mockPluginCatalog } from "../src/state/mockData";
import "../src/styles.css";

function Fixture() {
  const [fail, setFail] = useState(true);
  const [calls, setCalls] = useState(0);
  const [catalog, setCatalog] = useState(mockPluginCatalog);
  async function request() { setCalls(value => value + 1); await new Promise(resolve => setTimeout(resolve, 100)); return !fail; }
  return <><label><input type="checkbox" checked={fail} onChange={event => setFail(event.target.checked)} />模拟失败</label><output aria-label="请求次数">{calls}</output><main style={{ padding: 24 }}>
    <ExtensionsPage catalog={catalog} channels={[]} audit={[]} capabilityCount={8} busy={null} message={null} isDemo
      onChange={async (id, action) => { if (!await request()) return false; setCatalog(items => items.map(item => item.manifest.plugin_id === id && item.installation ? { ...item, installation: { ...item.installation, state: action === "disable" ? "disabled" : "enabled" } } : item)); return true; }}
      onUpdate={async () => request()} onRevoke={async () => request()} />
  </main></>;
}
createRoot(document.getElementById("root")!).render(<Fixture />);
