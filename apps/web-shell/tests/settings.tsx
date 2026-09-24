import { useState } from "react";
import { createRoot } from "react-dom/client";
import type { ModelProfile } from "@investment-steward/domain-contracts";
import { SettingsPage } from "../src/routes/SettingsPage";
import "../src/styles.css";

const now = new Date().toISOString();
function Fixture() {
  const [fail, setFail] = useState(true);
  const [models, setModels] = useState<ModelProfile[]>([{ profile_id: "fixture", name: "隔离模型方案", base_url: "https://example.invalid", model: "fixture", credential_ref: "model_api_key", status: "使用中", created_at: now, updated_at: now }]);
  async function request() { if (fail) throw new Error("隔离请求失败"); }
  return <><label><input type="checkbox" checked={fail} onChange={event => setFail(event.target.checked)} />模拟失败</label><main>
    <SettingsPage plugins={[]} capabilities={[]} capabilityCount={0} modelProfiles={models} credentials={[]} notifyChannels={[]} investorProfile={null} connection="ready" coreVersion="fixture" onNavigate={() => undefined} onEditProfile={() => undefined}
      onExportAll={async () => null} onCreateModelProfile={async input => { await request(); const model: ModelProfile = { ...input, profile_id: "new", status: "使用中", created_at: now, updated_at: now }; setModels(items => [...items, model]); return model; }}
      onUpdateModelProfile={async (id, input) => { await request(); const model = { ...models.find(item => item.profile_id === id)!, ...input }; setModels(items => items.map(item => item.profile_id === id ? model : item)); return model; }}
      onActivateModelProfile={async () => { await request(); return true; }} onTestModelProfile={async () => { await request(); return null; }} onDeleteModelProfile={async () => { await request(); return true; }}
      onUpsertCredential={async () => { await request(); return null; }} onTestCredential={async () => { await request(); return null; }} onDeleteCredential={async () => { await request(); return true; }} />
  </main></>;
}
createRoot(document.getElementById("root")!).render(<Fixture />);
