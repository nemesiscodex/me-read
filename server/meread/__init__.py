import os
from pathlib import Path

HOME = Path(os.environ.get('ME_READ_HOME', Path.home() / '.local/share/me-read'))
# Set before anything imports huggingface_hub, which reads it once.
os.environ.setdefault('HF_HUB_CACHE', str(HOME / 'models/hub'))
