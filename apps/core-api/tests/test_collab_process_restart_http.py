"""真正终止执行中的Core后，同库新进程只能重试中断阶段，不能重跑已完成阶段。"""
import json
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest

SERVER = r'''
import json, socket, sys, threading
from pathlib import Path
from uuid import uuid4
import uvicorn
from investment_steward_core import collab, model_client
from investment_steward_core.api import create_app
from investment_steward_core.config import CoreSettings
from investment_steward_core.domain import ModelProfile
folder, mode = Path(sys.argv[1]), sys.argv[2]
interrupt_stage = sys.argv[3] if len(sys.argv)>3 else "draft"
app = create_app(CoreSettings(data_dir=folder,session_token="crash-test",model_access_enabled=True))
db = app.state.core.database
identity = folder / "run-id.txt"
if not identity.exists():
 profile=ModelProfile(profile_id=uuid4(),name="synthetic",base_url="https://example.invalid",model="synthetic",credential_ref="unused",status="使用中")
 db.upsert_model_profile(profile)
 run=collab.create_run("600001","synthetic crash recovery","synthetic tech",{"S1":"synthetic price"},{},str(profile.profile_id),profile.model,
  profile_fingerprints={stage:collab.profile_fingerprint(profile) for stage in collab.STAGES})
 db.save_collab_run(run)
 identity.write_text(run["run_id"])
run_id=identity.read_text()
def answer(*args,**kwargs):
 saved=db.get_collab_run(run_id)
 stage=saved["stages"][saved["next_index"]]["stage"]
 with (folder/"calls.jsonl").open("a",encoding="utf-8") as log:
  log.write(json.dumps({"mode":mode,"stage":stage})+"\n")
 if mode=="crash" and stage==interrupt_stage:
  (folder/"waiting").write_text(stage)
  threading.Event().wait()
 if stage=="check": payload={"observations":["synthetic completed check"],"gaps":[],"readiness":"high"}
 elif stage=="risk": payload={"risks":["synthetic limited evidence"],"verdict":"partial"}
 else: payload={"title":"synthetic report","executive_summary":"synthetic limited observation",
  "report":"Synthetic technical observation [S1]. No fundamental data or recommendation.","citations":["S1"],
  "limitations":["synthetic only"],"confidence":"low","revision_notes":"synthetic revision"}
 return model_client.ModelReply(content=json.dumps(payload),model="synthetic",provider="synthetic",latency_ms=1)
model_client.call_active_model=answer
sock=socket.socket(); sock.bind(("127.0.0.1",0))
server=uvicorn.Server(uvicorn.Config(app,host="127.0.0.1",log_level="error"))
def stop():
 sys.stdin.readline(); server.should_exit=True
threading.Thread(target=stop,daemon=True).start()
print(json.dumps({"port":sock.getsockname()[1],"run_id":run_id}),flush=True)
try: server.run(sockets=[sock])
finally: db.close()
'''


def _start(folder, mode, interrupt_stage="draft"):
    process = subprocess.Popen([sys.executable, "-u", "-c", SERVER, str(folder), mode, interrupt_stage],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    with ThreadPoolExecutor() as pool:
        try:
            ready = json.loads(pool.submit(process.stdout.readline).result(timeout=15))
        except Exception:
            process.kill()
            process.wait(timeout=10)
            raise
    client = httpx.Client(base_url=f"http://127.0.0.1:{ready['port']}",
                         headers={"X-Core-Session-Token": "crash-test"}, timeout=5)
    for _ in range(100):
        try:
            if client.get("/health").status_code == 200:
                return process, client, ready["run_id"]
        except httpx.TransportError:
            pass
        time.sleep(0.02)
    client.close()
    process.kill()
    process.communicate(timeout=10)
    raise AssertionError("isolated Core did not become ready")


@pytest.mark.parametrize("interrupt_stage", ["draft", "risk"])
def test_real_core_process_crash_preserves_completed_stages_and_resumes_remaining(tmp_path, interrupt_stage):
    stages = ["check", "draft", "risk", "revise"]
    index = stages.index(interrupt_stage)
    first, http, run_id = _start(tmp_path, "crash", interrupt_stage)
    try:
        for stage in stages[:index]:
            checked = http.post("/evidence/collab-next", json={"run_id": run_id})
            assert checked.status_code == 200, checked.text
            assert checked.json()["executed_stage"] == stage
            assert checked.json()["executed_status"] == "done"
        completed = checked.json()["run"]["stages"][:index]
        with ThreadPoolExecutor() as pool:
            request = pool.submit(http.post, "/evidence/collab-next", json={"run_id": run_id})
            for _ in range(200):
                if (tmp_path / "waiting").exists():
                    break
                time.sleep(0.02)
            assert (tmp_path / "waiting").read_text() == interrupt_stage
            first.kill()
            first.communicate(timeout=10)
            try:
                request.result(timeout=10)
            except httpx.TransportError:
                pass
    finally:
        http.close()
        if first.poll() is None:
            first.kill()
            first.communicate(timeout=10)
    second, http, restored_id = _start(tmp_path, "resume", interrupt_stage)
    try:
        assert restored_id == run_id and second.pid != first.pid
        restored = http.get("/evidence/collab-state", params={"run_id": run_id}).json()["run"]
        assert restored["next_index"] == index and restored["stages"][:index] == completed
        assert restored["stages"][index]["status"] == "failed"
        assert "远端" in restored["stages"][index]["error"]
        for stage in stages[index:]:
            answer = http.post("/evidence/collab-next", json={"run_id": run_id})
            assert answer.status_code == 200, answer.text
            assert answer.json()["executed_stage"] == stage
            assert answer.json()["executed_status"] == "done", answer.text
        assert answer.json()["done"] is True and answer.json()["persisted"] is True
        assert answer.json()["run"]["stages"][:index] == completed
        repeated = http.post("/evidence/collab-next", json={"run_id": run_id})
        assert repeated.json()["collab_report_id"] == answer.json()["collab_report_id"]
        calls = [json.loads(line) for line in Path(tmp_path / "calls.jsonl").read_text().splitlines()]
        assert calls == [*[{"mode": "crash", "stage": stage} for stage in stages[:index + 1]],
                         *[{"mode": "resume", "stage": stage} for stage in stages[index:]]]
    finally:
        http.close()
        second.stdin.write("stop\n")
        second.stdin.flush()
        _, stderr = second.communicate(timeout=10)
        assert second.returncode == 0, stderr
