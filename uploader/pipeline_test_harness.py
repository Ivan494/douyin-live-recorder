#!/usr/bin/env python3
"""End-to-end live test: run the production HeadlessService code on ONE
temporary in-memory profile (never written to profiles.json/settings.json).

Usage: pipeline_test_harness.py <live.douyin.com room id> <record seconds> [segment seconds]
Output: downloads/_pipeline_test/ ; status: src/status.pipeline_test.json
"""
import copy, json, logging, os, sys
os.environ["DOUYIN_HEADLESS"] = "1"
REPO = "/home/ubuntu/douyin-live-recorder"
sys.path.insert(0, REPO + "/src")
import douyin_recorder_app as app
import douyin_headless as h

room, seconds = sys.argv[1], float(sys.argv[2])
segment = int(sys.argv[3]) if len(sys.argv) > 3 else 120
store = app.RecorderStore(read_only=True)
store.save = lambda *a, **k: None              # never persist test state
store.save_profiles_only = lambda *a, **k: None
store.settings = copy.deepcopy(store.settings)
store.settings["recording_segment_max_seconds"] = segment
base = copy.deepcopy(next(p for p in store.profiles if p.get("enabled", True)))
store.profiles = [dict(base, id="pipeline-test", name="_pipeline_test", enabled=True,
                       url=f"https://live.douyin.com/{room}", fallback_live_url="", fallback_source_url="",
                       original_profile_url="", output_dir="", record_live=True,
                       auto_download_stories=False, auto_download_videos=False,
                       poll_interval_seconds=30, priority=True)]
h.setup_logging()
svc = h.HeadlessService(store, status_path=REPO + "/src/status.pipeline_test.json")
logging.info("PIPELINE-TEST start room=%s seconds=%s segment=%s output=%s",
             room, seconds, segment, store.profiles[0]["output_dir"])
svc.run(seconds)
logging.info("PIPELINE-TEST stopped")
