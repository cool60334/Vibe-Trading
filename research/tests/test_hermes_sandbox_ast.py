import pytest
from research.hermes.sandbox_ast import check_source, UnsafeCodeError

SAFE = "import pandas as pd\nimport numpy as np\n\ndef compute(df):\n    return df['close'].pct_change(5)\n"


def test_safe_source_passes():
    assert check_source(SAFE) is None


@pytest.mark.parametrize("bad", [
    "import os\n",
    "import socket\n",
    "from subprocess import run\n",
    "import requests\n",
    "open('/etc/passwd')\n",
    "eval('1+1')\n",
    "exec('x=1')\n",
    "__import__('os')\n",
    "compile('x=1', '<string>', 'exec')\n",
    "getattr(pd, 'eval')\n",
    "setattr(pd, 'x', 1)\n",
    "df['close'].bfill()\n",
    "df['close'].fillna(method='bfill')\n",
    "df['close'].backfill()\n",
    "pd.eval('1+1')\n",
])
def test_unsafe_source_raises(bad):
    with pytest.raises(UnsafeCodeError):
        check_source("import pandas as pd\n" + bad)


@pytest.mark.parametrize("safe", [
    "df['close'].fillna(0)\n",
    "df['close'].fillna(method='ffill')\n",
])
def test_safe_calls_do_not_raise(safe):
    assert check_source("import pandas as pd\n" + safe) is None
