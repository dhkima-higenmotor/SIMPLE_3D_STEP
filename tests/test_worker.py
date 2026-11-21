"""Tests for the non-blocking multiprocessing CAD worker and GUI responsiveness."""

from __future__ import annotations

import time
from pathlib import Path

from simple_3d_step.core.worker import CadWorkerClient
from simple_3d_step.models import SimplifyConfig, VisibilityConfig
from tests.generate_sample_step import generate_closed_enclosure_step


def _wait_for_done(client: CadWorkerClient, cmd_id: int, timeout: float = 45.0):
    deadline = time.perf_counter() + timeout
    progress_seen = 0
    tick_gaps: list[float] = []
    last_t = time.perf_counter()

    while time.perf_counter() < deadline:
        now = time.perf_counter()
        tick_gaps.append(now - last_t)
        last_t = now
        for ev in client.poll_events():
            if ev.kind == "progress" and ev.payload[0] == cmd_id:
                progress_seen += 1
            elif ev.kind == "done" and ev.payload[0] == cmd_id:
                return ev.payload[2], progress_seen, max(tick_gaps)
            elif ev.kind in ("error", "crashed") and ev.payload[0] == cmd_id:
                raise AssertionError(f"Worker failed: {ev.payload}")
        time.sleep(0.02)
    raise TimeoutError(f"Timed out waiting for worker cmd_id={cmd_id}")


def test_worker_full_pipeline_nonblocking(tmp_path: Path) -> None:
    """Verify full load -> analyze -> simplify -> save runs in subprocess with zero main-thread freeze."""
    src = generate_closed_enclosure_step(tmp_path / "worker_in.step")
    out = tmp_path / "worker_out.step"

    client = CadWorkerClient()
    try:
        # 1. Load STEP in worker subprocess
        cid = client.submit("load_step", {"path": str(src)})
        loaded, prog_cnt, max_gap = _wait_for_done(client, cid)
        assert len(loaded["parts"]) == 6
        assert prog_cnt >= 1
        assert max_gap < 0.25  # Main process loop never blocked!

        # 2. Visibility analysis in worker subprocess
        cid = client.submit(
            "analyze_visibility",
            {"config": VisibilityConfig(num_views=26, grid_resolution=80), "overrides": {}},
        )
        vis_data, _, max_gap = _wait_for_done(client, cid)
        assert vis_data["external_count"] == 3
        assert vis_data["internal_count"] == 3
        assert max_gap < 0.25

        # 3. Exact B-Rep simplify in worker subprocess
        cid = client.submit(
            "simplify",
            {"config": SimplifyConfig(), "overrides": {}},
        )
        res, _, max_gap = _wait_for_done(client, cid)
        assert res.validation.is_valid is True
        assert res.validation.solid_count == 1
        assert res.validation.shell_count == 1
        assert max_gap < 0.25

        # 4. Save STEP from worker subprocess
        cid = client.submit("save_result", {"output_path": str(out), "schema": "AP214"})
        saved, _, _ = _wait_for_done(client, cid)
        assert Path(saved["output_path"]).exists()
        assert saved["size_bytes"] > 1000
    finally:
        client.shutdown()
