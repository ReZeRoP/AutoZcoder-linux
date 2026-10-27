"""Launch the real GTK desktop with isolated preview data."""
import os
import sys
from pathlib import Path

from version import read_version

root = Path(__file__).resolve().parents[2]
print(f'Previewing AutoZcoder Linux {read_version(root)} from source')
sys.path[:0] = [str(root / 'build/fedora/python'), str(root)]
os.environ['HOME'] = str(root / 'build/preview/home')
Path(os.environ['HOME']).mkdir(parents=True, exist_ok=True)
for name, folder in [("XDG_CONFIG_HOME", "config"), ("XDG_DATA_HOME", "data"),
                     ("XDG_CACHE_HOME", "cache")]:
    os.environ[name] = str(root / "build/preview" / folder)
from zcode_gateway.linux_desktop import bootstrap_config, main
import yaml

path = bootstrap_config()
data = yaml.safe_load(path.read_text())
data['server']['port'] = 8790
path.write_text(yaml.safe_dump(data, sort_keys=False))
sys.exit(main(['--config', str(path)]))
