"""Deal With It!

The warning filter has to be installed before anything imports
face_recognition. face_recognition_models is unmaintained and still calls
pkg_resources at import time; the advice in the warning it triggers ("pin to
Setuptools<81") does not help, and the pin that does live in pyproject.toml.
"""

import warnings

warnings.filterwarnings('ignore', message='pkg_resources is deprecated')
