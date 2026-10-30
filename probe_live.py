import asyncio
import json
from pathlib import Path

from streamget.platforms.douyin.live_stream import DouyinLiveStream

ROOT = Path('/home/ubuntu/douyin-live-recorder')

async def main():
    profiles = json.loads((ROOT / 'src/profiles.json').read_text())
    semaphore = asyncio.Semaphore(2)

    async def check(profile):
        if not profile.get('enabled', True):
            return {'id': profile['id'], 'name': profile['name'], 'state': 'disabled'}
        async with semaphore:
            try:
                live = DouyinLiveStream(cookies=None)
                url = profile['url']
                fallback = profile.get('fallback_live_url')
                if 'live.douyin.com' in url:
                    room = await asyncio.wait_for(live.fetch_web_stream_data(url), 25)
                elif fallback and profile.get('fallback_source_url') == url:
                    room = await asyncio.wait_for(live.fetch_web_stream_data(fallback), 25)
                else:
                    room = await asyncio.wait_for(live.fetch_app_stream_data(url), 25)
                stream = await live.fetch_stream_url(room, profile.get('quality', 'OD'))
                return {'id': profile['id'], 'name': profile['name'], 'state': 'live' if stream.is_live else 'offline', 'has_stream_url': bool(stream.flv_url or stream.record_url)}
            except Exception as exc:
                return {'id': profile['id'], 'name': profile['name'], 'state': 'probe_failed', 'error_type': type(exc).__name__}
    results = await asyncio.gather(*(check(p) for p in profiles))
    (ROOT / 'live-probe.json').write_text(json.dumps(results, ensure_ascii=False, indent=2))
    print(json.dumps({'checked': len(results), 'live': sum(r['state'] == 'live' for r in results), 'offline': sum(r['state'] == 'offline' for r in results), 'probe_failed': sum(r['state'] == 'probe_failed' for r in results), 'disabled': sum(r['state'] == 'disabled' for r in results)}))

asyncio.run(main())
