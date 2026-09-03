"""Deal With It!

face_recognition_models calls pkg_resources at import time, which warns on
every modern setuptools; the pin in pyproject.toml is what keeps it working.
"""

import warnings

warnings.filterwarnings('ignore', message='pkg_resources is deprecated')
