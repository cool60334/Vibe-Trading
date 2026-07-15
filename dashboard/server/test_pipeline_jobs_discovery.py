import pipeline_jobs as pj


def test_foundry_mine_job_has_a_single_foundry_step(tmp_path):
    job = pj.create_job(tmp_path, kind="foundry_mine", symbol="eth",
                        config={"image": "talos:test"})
    assert [s["stage"] for s in job["steps"]] == ["foundry"]
    assert job["config"]["image"] == "talos:test"


def test_discovery_pipeline_job_prepends_bridge_to_the_pipeline(tmp_path):
    job = pj.create_job(tmp_path, kind="discovery_pipeline", symbol="eth", config={})
    assert job["steps"][0]["stage"] == "bridge"
    assert [s["stage"] for s in job["steps"][1:]] == pj.pipeline_sequence()
    assert job["overlay_dir"].endswith(job["job_id"])


def test_step_command_builds_foundry_and_bridge_argv():
    foundry = pj.step_command("foundry", "eth", overlay_dir="/ov", runs_dir="/rd",
                              manifests_dir="/md", zoo_dir="/zd", image="img",
                              llm="openai", model="gpt-4o-mini", daily_max=6,
                              pause_file="/p.pause", oos_start="2025-01-01")
    # must use `mine` (enqueue-then-run), not `run` (which no-ops on an empty queue)
    assert "mine" in foundry
    assert "--runs-dir" in foundry and "/rd" in foundry
    assert "--pause-file" in foundry and "/p.pause" in foundry
    assert "--oos-start" in foundry and "2025-01-01" in foundry
    # ohlcv path is derived from manifests_dir + symbol
    assert "--ohlcv-path" in foundry
    assert any("ohlcv_eth.parquet" in a for a in foundry)
    bridge = pj.step_command("bridge", "eth", overlay_dir="/ov", runs_dir="/rd",
                             manifests_dir="/md", zoo_dir="/zd", image="img",
                             llm="openai", model="gpt-4o-mini", daily_max=6,
                             pause_file="/p.pause", oos_start="2025-01-01")
    assert "foundry_bridge" in " ".join(bridge)
    assert "--overlay-dir" in bridge and "/ov" in bridge
