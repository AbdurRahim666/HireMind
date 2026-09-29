# HireMind feature flags. Default is OFF so existing behavior is untouched.
# Enable via an env var (HIREMIND_ENABLED=1) or a `HIREMIND_ENABLED` attr in
# the gitignored config.py (the same knob pattern the rest of the app uses).
import os

_FEATURE_ENV_TRUE = {"1", "true", "yes", "on"}


def hiremind_enabled() -> bool:
    try:
        from config import HIREMIND_ENABLED as flag
    except Exception:
        flag = None
    if isinstance(flag, bool):
        return flag
    if flag:
        return True
    return os.environ.get("HIREMIND_ENABLED", "").strip().lower() in _FEATURE_ENV_TRUE