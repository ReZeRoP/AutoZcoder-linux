"""Preview a foreground gateway against disposable data, never production state.

The preview host owns this process. An independently launched custom dashboard
can attach to the printed configuration; it never manages the default user unit.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

root = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(root / 'build/fedora/python'), str(root)]
home = Path(tempfile.mkdtemp(prefix='autozcoder-preview-'))
os.environ['HOME'] = str(home)
for name, folder in [('XDG_CONFIG_HOME', 'config'), ('XDG_DATA_HOME', 'data'),
                     ('XDG_CACHE_HOME', 'cache')]:
    os.environ[name] = str(home / folder)
os.environ['AUTOZCODER_PACKAGE_MANAGED'] = '1'
os.environ['ZAI_CAPTCHA_MINT'] = '0'

from zcode_gateway.linux_service import bootstrap_config, serve
import yaml

path = bootstrap_config()
data = yaml.safe_load(path.read_text())
data['server']['port'] = 18789
data['upstream']['base_url'] = 'http://127.0.0.1:1'
data['generator']['enabled'] = False
path.write_text(yaml.safe_dump(data, sort_keys=False))
print(f'Isolated preview configuration: {path}', flush=True)
sys.exit(serve(config_path=path))
