#!/bin/bash

set -ex -o pipefail

git rev-parse HEAD

python --version
python -c "import struct; print('bits:', struct.calcsize('P') * 8)"

python -m pip install -U pip setuptools wheel
pip --version

python setup.py sdist --formats=zip
pip install dist/*.zip

if [ "$CHECK_DOCS" = "1" ]; then
    pip install -r ci/rtd-requirements.txt
    towncrier --yes  # catch errors in newsfragments
    cd docs
    # -n (nit-picky): warn on missing references
    # -W: turn warnings into errors
    sphinx-build -nW  -b html source build
else
    # Actual tests
    pip install -r test-requirements.txt

    if [ "$CHECK_FORMATTING" = "1" ]; then
        source check.sh
    fi

    mkdir empty
    cd empty

    INSTALLDIR=$(python -c "import os, trio; print(os.path.dirname(trio.__file__))")
    pytest -W error -ra --junitxml=../test-results.xml --run-slow --faulthandler-timeout=60 ${INSTALLDIR} --cov="$INSTALLDIR" --cov-config=../.coveragerc --verbose

    # Disable coverage on 3.8-dev, at least until it's fixed (or a1 comes out):
    #   https://github.com/python-trio/trio/issues/711
    #   https://github.com/nedbat/coveragepy/issues/707#issuecomment-426455490
    if [ "$(python -V)" != "Python 3.8.0a0" ]; then
        bash <(curl -s https://codecov.io/bash)
    fi
fi
