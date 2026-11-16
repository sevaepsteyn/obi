"""T007: chat degrades fast when no key is available, and never pulls anthropic into the core."""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

from obi.chat.session import build_runtime
from obi.core.llm import ChatUnavailable
from tests.fakes import chat_config


def test_missing_key_raises_chat_unavailable_fast(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    config = chat_config(tmp_path)  # no [chat].api_key

    start = time.monotonic()
    with pytest.raises(ChatUnavailable) as exc_info:
        build_runtime(config)
    elapsed = time.monotonic() - start

    assert elapsed < 5.0  # SC-004: fast fail
    assert "discover" in str(exc_info.value)  # actionable: the offline commands still work


def test_importing_chat_does_not_import_anthropic() -> None:
    # In a clean interpreter, importing the chat stack (and the CLI) must NOT pull in anthropic
    # (it is imported lazily), so the offline core never depends on the cloud SDK at import time.
    code = (
        "import obi.chat.session, obi.cli.main, sys; "
        "assert 'anthropic' not in sys.modules, [m for m in sys.modules if 'anthropic' in m]"
    )
    subprocess.run([sys.executable, "-c", code], check=True)
