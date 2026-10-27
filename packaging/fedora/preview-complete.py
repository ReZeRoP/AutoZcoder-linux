"""Preview the staged complete package without touching real accounts or port8789."""
import os
import sys
from pathlib import Path

from version import stage_path

root = Path(__file__).resolve().parents[2]
stage = stage_path(root)
if not (stage / 'RUNTIME-MANIFEST.json').is_file():
    raise SystemExit(f'No staged runtime at {stage}; build the RPM first.')
os.environ['HOME'] = str(root / 'build/complete-preview/home')
Path(os.environ['HOME']).mkdir(parents=True, exist_ok=True)
sys.path[:0] = [str(stage / 'python'), str(stage)]
os.environ['AUTOZCODER_PACKAGE_MANAGED'] = '1'
os.environ['AUTOZCODER_RUNTIME_ROOT'] = str(stage)
for name, folder in [('XDG_CONFIG_HOME','config'),('XDG_DATA_HOME','data'),('XDG_CACHE_HOME','cache')]:
    os.environ[name] = str(root / 'build/complete-preview' / folder)
from zcode_gateway.linux_desktop import bootstrap_config, main
import yaml
path = bootstrap_config()
data = yaml.safe_load(path.read_text())
data['server']['port'] = 8790
path.write_text(yaml.safe_dump(data, sort_keys=False))
sys.exit(main(['--config', str(path)]))
