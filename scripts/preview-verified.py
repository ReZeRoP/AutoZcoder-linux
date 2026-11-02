"""Exercise candidate verified SSE with slow synthetic upstream, never real accounts."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'build/fedora/python'), str(ROOT), str(ROOT / 'tests')]
HOME = Path(tempfile.mkdtemp(prefix='autozcoder-verified-preview-'))
os.environ['HOME'] = str(HOME)
for key, folder in [('XDG_CONFIG_HOME', 'config'), ('XDG_DATA_HOME', 'data'),
                    ('XDG_CACHE_HOME', 'cache'), ('XDG_STATE_HOME', 'state')]:
    os.environ[key] = str(HOME / folder)
os.environ['AUTOZCODER_PACKAGE_MANAGED'] = '1'
os.environ['ZAI_CAPTCHA_MINT'] = '0'

import uvicorn
from zcode_gateway.app import create_app
from zcode_gateway.config import Config
from zcode_gateway.pool import Account
from tests_fixtures import FakeClient, FakeResponse, frame, sample_events


class Slow(FakeResponse):
    async def aiter_bytes(self):
        for event in sample_events(tools=True):
            await asyncio.sleep(.3)
            yield frame(event)


async def parked(*args, **kwargs):
    await asyncio.Event().wait()


# Avoid all model-catalog/token/balance/relay network work in the synthetic demo.
from zcode_gateway.tokens import TokenManager
from zcode_gateway.relay_manager import RelayManager
TokenManager.refresh_loop = parked
TokenManager.balance_loop = parked
RelayManager.start_health_loop = lambda *args, **kwargs: None

cfg = Config(accounts={'dir': str(HOME / 'accounts')},
             server={'host': '127.0.0.1', 'port': 18791,
                     'admin_key': 'preview-only', 'client_keys': [{'key': 'preview-only'}]},
             store={'sqlite_path': str(HOME / 'gateway.db')},
             logging={'capture': False, 'capture_dir': str(HOME / 'logs')},
             update={'enabled': False}, generator={'enabled': False},
             upstream={'base_url': 'http://127.0.0.1:1', 'stream_delivery': 'verified',
                       'stream_verify_timeout_seconds': 30, 'stream_heartbeat_seconds': .5})
cfg.pool.balance_poll_seconds = 0
app = create_app(cfg)
original = app.router.lifespan_context


class RepeatSlow(FakeClient):
    async def send(self, headers, body, stream, control=None):
        self.pending = [Slow(b'')]
        return await super().send(headers, body, stream, control=control)


@asynccontextmanager
async def synthetic(app):
    async with original(app):
        state = app.state.app_state
        await state.pool.upsert(Account(id='synthetic-preview', access_token='not-real'))
        state.service.client = RepeatSlow()
        yield


app.router.lifespan_context = synthetic


@app.get('/verification-preview')
async def preview_page():
    from fastapi.responses import HTMLResponse
    return HTMLResponse('''<!doctype html><title>Verified SSE integration</title>
    <h1>Verified SSE integration</h1><button id="run">Run synthetic slow stream</button>
    <pre id="result">Ready. Synthetic upstream only.</pre><script>
    document.querySelector('#run').onclick=async()=>{
      const output=document.querySelector('#result'); output.textContent='Waiting for headers';
      const start=performance.now(); const response=await fetch('/v1/chat/completions',{
        method:'POST',headers:{'Content-Type':'application/json','Authorization':'Bearer preview-only'},
        body:JSON.stringify({model:'GLM-5.3-Flash',stream:true,messages:[{role:'user',content:'synthetic'}]})});
      output.textContent='HTTP '+response.status+'; headers after '+Math.round(performance.now()-start)+' ms\\n';
      const reader=response.body.getReader(),decoder=new TextDecoder();let text='',count=0;
      while(true){const part=await reader.read();if(part.done)break;text+=decoder.decode(part.value,{stream:true});
        count++;output.textContent='HTTP '+response.status+'; received '+count+' chunks; elapsed '+Math.round(performance.now()-start)+' ms\\n'+text;}
      output.textContent+='\\nComplete; successful DONE count: '+(text.match(/data: \\[DONE\\]/g)||[]).length;
    };</script>''')


if __name__ == '__main__':
    uvicorn.run(app, host='127.0.0.1', port=18791, access_log=False)
