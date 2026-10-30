import json
from pathlib import Path
import sys
import threading

ROOT = Path('/home/ubuntu/douyin-live-recorder')
sys.path.insert(0, str(ROOT / 'src'))
import douyin_media_downloader as media

profiles = json.loads((ROOT / 'src/profiles.json').read_text())
settings = json.loads((ROOT / 'src/settings.json').read_text())
profile = next(p for p in profiles if p['id'] == 'zhuni-zhea')
cancel = threading.Event()
timer = threading.Timer(100, cancel.set)
timer.start()
try:
    result = media.download_profile(profile, settings, videos=False, stories=True, cancel_event=cancel)
    report = {'profile_id': profile['id'], 'videos_status': result['videos']['status'], 'stories': {key: result['stories'].get(key) for key in ('status', 'downloaded', 'failed', 'skipped')}, 'has_mobile_session': bool(media.load_mobile_session_cookie_header())}
except InterruptedError:
    report = {'profile_id': profile['id'], 'status': 'timed_out_after_100_seconds'}
except Exception as exc:
    report = {'profile_id': profile['id'], 'status': 'probe_error', 'error_type': type(exc).__name__}
finally:
    timer.cancel()
(ROOT / 'story-probe.json').write_text(json.dumps(report, indent=2))
print(json.dumps(report))
