"""
archiver.config
───────────────
Unified configuration. Two storage backends, with a clear split:

  .env (secrets):
      All credentials (API tokens, session paths, telegram chat IDs).
      Loaded by python-dotenv on import.

  config.toml (behavior):
      User lists per platform, plus behavior policies (delete-after-
      upload, dedup-after-download, ...) in a hierarchical structure
      that supports per-user overrides without env-var encoding hazards.
      Loaded by PolicyStore.

The split is deliberate:
  - Secrets benefit from .env's tight ecosystem (.gitignore, shell tools).
  - Behavior + user lists benefit from TOML's structured + unicode-safe
    representation. Usernames like "user.name!" or "正常用户" work natively.

Per-platform CONFIG SECTIONS are loaded only when both:
  (a) the platform is in ENABLED_PLATFORMS, and
  (b) at least one user is configured for it in config.toml.

This makes enable/disable a one-line toggle without scrubbing TOML.

Why frozen dataclasses?
  - Immutable: config can't be accidentally mutated mid-run.
  - Hashable for the value parts.
  - dataclasses.asdict() for diagnostics.

NOTE on frozen + PolicyStore:
  Config.policy_store IS a mutable object (the CLI mutates it via
  store.set/unset). frozen=True freezes the dataclass's REFERENCES,
  not the referents. So Config.policy_store always points at the
  same store instance, but that store's internal state can change.
  This is exactly what we want.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from dotenv import load_dotenv

from core import PolicyStore, DownloadPolicy, db_path as _core_db_path
from core import env
from core.platform import paths as _osp

log = logging.getLogger(__name__)

load_dotenv(_osp.config_dir(_osp.SUITE) / ".env")


# ── env-var primitives (secrets only; shared parsing lives in core.env) ───────

_req = env.req
_opt = env.opt


def _optf(key: str, default: float) -> float:
    """Optional float env var with a numeric fallback. A blank or malformed
    value falls back to `default` rather than crashing the run — pacing knobs
    are safety rails, not hard requirements."""
    raw = _opt(key, "")
    if not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        log.warning("config: %s=%r is not a number — using default %s",
                    key, raw, default)
        return default


def _opti(key: str, default: int) -> int:
    raw = _opt(key, "")
    if not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        log.warning("config: %s=%r is not an integer — using default %s",
                    key, raw, default)
        return default


# gallery-dl's `browser` option accepts "<name>" or "<name>:<platform>" and sets
# a matching User-Agent + TLS cipher order. Only these base names carry a cipher
# profile in gallery-dl; an unknown one silently disables the fingerprint, so we
# validate and fall back rather than ship a mismatched/absent fingerprint.
_KNOWN_BROWSERS = frozenset({"firefox", "chrome"})


def _opt_browser(key: str, default: str) -> str:
    """Read a gallery-dl `browser` fingerprint value (e.g. 'firefox' or
    'firefox:windows'). Unknown base name → warn and fall back to `default`,
    since a typo'd fingerprint is worse than the intended one."""
    raw = _opt(key, "").strip()
    if not raw:
        return default
    base = raw.split(":", 1)[0].lower()
    if base not in _KNOWN_BROWSERS:
        log.warning("config: %s=%r has unknown browser %r (known: %s) — using "
                    "default %r", key, raw, base, sorted(_KNOWN_BROWSERS),
                    default)
        return default
    return raw


# ── Per-platform request pacing ───────────────────────────────────────────────

@dataclass(frozen=True)
class Pacing:
    """Per-platform anti-throttle pacing, decoupled from the global SLEEP_MIN/
    SLEEP_MAX (which only X still uses). Instagram and TikTok each carry their
    own so IG can crawl without dragging the others down.

    The knobs, in order of ban-relevance:
      - sleep_request_[min|max]: random gap BETWEEN API calls. The single most
        important number — this is what turns a burst into human-looking traffic.
      - sleep_[min|max]: extra gap before each file download.
      - sleep_429: hard back-off (seconds) when the site returns HTTP 429
        (rate-limited). Retrying into a 429 is what escalates a soft flag into a
        disabled account, so we STOP and wait instead.
      - retries: per-request retry ceiling. Kept LOW on purpose — see sleep_429.
      - user_gap_[min|max]: random pause between successive accounts in one run,
        so a multi-account cycle reads like a person opening profiles, not a
        scanner sweeping a list.
    """
    sleep_request_min: float
    sleep_request_max: float
    sleep_min:         float
    sleep_max:         float
    sleep_429:         float
    retries:           int
    user_gap_min:      float
    user_gap_max:      float

    @classmethod
    def from_env(cls, prefix: str, *, defaults: "Pacing") -> "Pacing":
        """Load `<PREFIX>_SLEEP_REQUEST_MIN` … overrides, falling back to
        `defaults` for anything unset. `prefix` is the platform env stem, e.g.
        'INSTAGRAM' or 'TIKTOK'."""
        p = prefix.upper()
        return cls(
            sleep_request_min = _optf(f"{p}_SLEEP_REQUEST_MIN", defaults.sleep_request_min),
            sleep_request_max = _optf(f"{p}_SLEEP_REQUEST_MAX", defaults.sleep_request_max),
            sleep_min         = _optf(f"{p}_SLEEP_MIN",         defaults.sleep_min),
            sleep_max         = _optf(f"{p}_SLEEP_MAX",         defaults.sleep_max),
            sleep_429         = _optf(f"{p}_SLEEP_429",         defaults.sleep_429),
            retries           = _opti(f"{p}_RETRIES",           defaults.retries),
            user_gap_min      = _optf(f"{p}_USER_GAP_MIN",      defaults.user_gap_min),
            user_gap_max      = _optf(f"{p}_USER_GAP_MAX",      defaults.user_gap_max),
        )


# Conservative "flagged account, safety over speed" defaults. Time is cheap on a
# 24/7 box; a disabled account is not. Loosen per-platform via the env overrides
# once the account is healthy again.
_IG_PACING_DEFAULTS = Pacing(
    sleep_request_min = 30.0, sleep_request_max = 60.0,
    sleep_min         = 8.0,  sleep_max         = 15.0,
    sleep_429         = 900.0,          # 15-min back-off on rate-limit
    retries           = 3,
    user_gap_min      = 180.0, user_gap_max    = 420.0,   # 3–7 min between users
)
_TIKTOK_PACING_DEFAULTS = Pacing(
    sleep_request_min = 10.0, sleep_request_max = 20.0,
    sleep_min         = 5.0,  sleep_max         = 10.0,
    sleep_429         = 600.0,          # 10-min back-off
    retries           = 4,
    user_gap_min      = 60.0, user_gap_max     = 150.0,   # 1–2.5 min between users
)


def _load_local_platforms(store: PolicyStore) -> tuple[str, ...]:
    """Read + VALIDATE the global `local_platforms` list from config.toml.

    config.toml is hand-editable, and the value is consumed as
    `for name in local_platforms`. If someone writes a bare string
    (`local_platforms = "foo"`), Python would iterate its CHARACTERS into
    phantom platforms — so coerce a lone string to a single-element list,
    reject non-lists, and drop non-string / blank entries with a warning.
    Returns a clean, lowercased, de-duplicated, ordered tuple."""
    raw = store.get("local_platforms", default=[])
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        log.warning("config: local_platforms must be a list of strings, got "
                    "%r — ignoring", raw)
        return ()
    names: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            log.warning("config: local_platforms entry %r is not a non-empty "
                        "string — skipping", item)
            continue
        name = item.strip().lower()
        if name not in names:
            names.append(name)
    return tuple(names)


# NOTE: The archiver no longer holds any Telegram configuration. Post-cutover
# it sends nothing — it only writes pending rows into the shared items table.
# The dispatcher is the sole Telegram session owner and resolves the
# destination peer (TELEGRAM_CHAT_ID_*) at send time from (platform, username).
# Keeping creds here would be dead weight that the archiver would needlessly
# require at startup.


# ── X / Twitter ───────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class XConfig:
    users:      tuple[str, ...]
    auth_token: str
    ct0:        str
    twid:       str

    @classmethod
    def from_store(cls, store: PolicyStore, *, require_auth: bool = True) -> "XConfig":
        # When download is disabled for this platform the orchestrator skips
        # fetch + health-check, so auth is never used — don't demand it at load
        # (lets you run a download-off X as reconcile/upload-only, no creds).
        get = _req if require_auth else (lambda k: _opt(k, ""))
        return cls(
            users      = store.list_users("x"),
            auth_token = get("X_AUTH_TOKEN"),
            ct0        = get("X_CT0"),
            twid       = get("X_TWID"),
        )


# ── TikTok ────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class TikTokConfig:
    users:               tuple[str, ...]
    cookies_file:        str
    firefox_profile:     str
    cookie_refresh_days: float
    pacing:              Pacing
    # gallery-dl fingerprint for the photo-carousel pass. Defaults to the cookie
    # origin (Firefox); the yt-dlp video pass keeps its own curl_cffi chrome
    # impersonation (works; TikTok is tolerant), so this only aligns the photo
    # pass to where the cookies were minted.
    browser:             str

    @classmethod
    def from_store(cls, store: PolicyStore, *, require_auth: bool = True) -> "TikTokConfig":
        # TikTok reads cookies lazily (via _opt), so there's nothing to require
        # at load; require_auth is accepted for a uniform call site.
        return cls(
            users               = store.list_users("tiktok"),
            cookies_file        = _opt("TIKTOK_COOKIES_FILE", "./cookies/tiktok.txt"),
            firefox_profile     = _opt("FIREFOX_PROFILE", ""),
            cookie_refresh_days = float(_opt("COOKIE_REFRESH_DAYS", "3")),
            pacing              = Pacing.from_env("TIKTOK", defaults=_TIKTOK_PACING_DEFAULTS),
            browser             = _opt_browser("TIKTOK_BROWSER", "firefox"),
        )


def _split_stories_from_include(raw_include: str, stories_interval: float) -> str:
    """When the stories fast lane is active (`stories_interval > 0`), remove
    'stories' from the HEAVY posts/reels include so the two passes never double-
    fetch the same content at two different paces. If that leaves the heavy
    include empty (someone set INSTAGRAM_INCLUDE=stories only), fall back to
    'posts,reels' — the heavy pass must still have something to walk, and the
    lane owns stories. When the lane is off, the include is returned untouched
    (legacy behavior: stories, if listed, ride the slow pass)."""
    if stories_interval <= 0:
        return raw_include
    subs = [s.strip() for s in raw_include.split(",") if s.strip()]
    kept = [s for s in subs if s.lower() != "stories"]
    if len(kept) != len(subs):
        log.info("Instagram: 'stories' handled by the dedicated fast lane "
                 "(every %.0fs); heavy include=%s", stories_interval,
                 ",".join(kept) or "posts,reels")
    return ",".join(kept) if kept else "posts,reels"


# ── Instagram ─────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class InstagramConfig:
    """
    Posts + reels only by default. Stories/highlights are opt-in via
    INSTAGRAM_INCLUDE — higher ban risk + they disappear, which
    complicates incremental-checkpoint logic.
    """
    users:               tuple[str, ...]
    cookies_file:        str
    firefox_profile:     str
    cookie_refresh_days: float
    include:             str
    pacing:              Pacing
    # gallery-dl fingerprint (User-Agent + TLS ciphers). Defaults to the cookie
    # origin (Firefox), so the session we replay presents as the browser that
    # minted it — a UA/TLS mismatch is a primary bot signal on Instagram.
    browser:             str
    # Stories fast lane. Stories vanish in 24h, so they get a SEPARATE pass on a
    # tighter cadence than the slow posts/reels crawl (which is not time-
    # sensitive). `stories_interval` = seconds between story sweeps (0 = lane
    # off); the sweep is stories-only and does NOT touch the posts/reels
    # incremental checkpoints. `stories_user_gap_*` = the (shorter) randomized
    # pause between users in a story sweep — smaller than the heavy pass's gap so
    # a many-friend sweep still finishes well inside the 24h window; per-REQUEST
    # pacing stays the cautious `pacing.sleep_request_*`.
    stories_interval:     float
    stories_user_gap_min: float
    stories_user_gap_max: float

    _ALLOWED_INCLUDE = frozenset({
        "posts", "reels", "stories", "highlights", "tagged", "channel",
    })

    def __post_init__(self):
        for sub in (s.strip().lower() for s in self.include.split(",")):
            if sub and sub not in self._ALLOWED_INCLUDE:
                raise RuntimeError(
                    f"INSTAGRAM_INCLUDE: unknown subcategory '{sub}'. "
                    f"Allowed: {sorted(self._ALLOWED_INCLUDE)}"
                )

    @classmethod
    def from_store(cls, store: PolicyStore, *, require_auth: bool = True) -> "InstagramConfig":
        # Instagram reads cookies lazily (via _opt), so there's nothing to
        # require at load; require_auth is accepted for a uniform call site.
        stories_interval = _optf("INSTAGRAM_STORIES_INTERVAL", 10800.0)  # 3h; 0=off
        include = _split_stories_from_include(
            _opt("INSTAGRAM_INCLUDE", "posts,reels"), stories_interval)
        return cls(
            users               = store.list_users("instagram"),
            cookies_file        = _opt("INSTAGRAM_COOKIES_FILE", "./cookies/instagram.txt"),
            firefox_profile     = _opt("FIREFOX_PROFILE", ""),
            cookie_refresh_days = float(_opt("COOKIE_REFRESH_DAYS", "3")),
            include             = include,
            pacing              = Pacing.from_env("INSTAGRAM", defaults=_IG_PACING_DEFAULTS),
            browser             = _opt_browser("INSTAGRAM_BROWSER", "firefox"),
            stories_interval     = stories_interval,
            stories_user_gap_min = _optf("INSTAGRAM_STORIES_USER_GAP_MIN", 20.0),
            stories_user_gap_max = _optf("INSTAGRAM_STORIES_USER_GAP_MAX", 60.0),
        )


# ── Master config ─────────────────────────────────────────────────────────────

# eq=False on the master config to avoid the frozen-dataclass requirement
# that every field be hashable. PolicyStore wraps an RLock and isn't
# hashable. We don't use Config as a dict key anywhere — equality and
# hashing aren't useful here.
@dataclass(frozen=True, eq=False)
class Config:
    x:            XConfig         | None
    tiktok:       TikTokConfig    | None
    instagram:    InstagramConfig | None
    policy_store: PolicyStore

    output_dir: str = "./downloads"
    # Where the top-level chat_id route folders live. Defaults to output_dir
    # (the historical single-tree layout) so every install is byte-identical
    # until ROUTES_DIR is set; the two-root split (downloads/records internal,
    # routes on another volume) sets it explicitly. ONLY the chat_id ingest
    # scan reads this — platform downloads, archives and quarantine stay under
    # output_dir.
    routes_dir: str = ""
    db_path:    str = ""   # resolved in load() via core.db_path()
    log_file:   str = "./.archiver/archiver.log"
    state_dir:  str = "./.archiver"

    sleep_min: float = 3.0
    sleep_max: float = 8.0

    # How many fetching platforms download concurrently, each on its own DB
    # connection and pace. 0 = all at once (default); 1 = fully sequential (the
    # pre-concurrency behavior, a rollback switch). See orchestrator._run_platforms.
    max_concurrent_platforms: int = 0

    auth_failure_threshold: int = 3

    enabled_platforms: frozenset[str] = field(default_factory=frozenset)
    reconcile_after_run: bool = False
    # User-managed folders treated as platforms (no download). Names only;
    # users are auto-discovered from {output_dir}/{name}/* subfolders.
    local_platforms: tuple[str, ...] = ()

    @classmethod
    def load(cls, *, load_platform_configs: bool = True,
             require_platforms: bool = True) -> "Config":
        # 1. Build the store first — it sources user lists for the rest.
        store = PolicyStore()

        # 2. Resolve enabled platforms (env var; secrets-adjacent setting).
        enabled = frozenset(
            p.strip().lower()
            for p in _opt("ENABLED_PLATFORMS", "x,tiktok,instagram").split(",")
            if p.strip()
        )

        # 3. Build per-platform config blocks only for enabled platforms
        #    with at least one configured user in config.toml. A platform whose
        #    download is disabled (DownloadPolicy) is built WITHOUT requiring
        #    auth — it'll be reconcile/upload-only, no credentials needed.
        dlp = DownloadPolicy(store)
        x_cfg = tt_cfg = ig_cfg = None
        if load_platform_configs:
            if "x" in enabled and store.list_users("x"):
                x_cfg = XConfig.from_store(store, require_auth=dlp.enabled_for("x"))

            if "tiktok" in enabled and store.list_users("tiktok"):
                tt_cfg = TikTokConfig.from_store(store, require_auth=dlp.enabled_for("tiktok"))

            if "instagram" in enabled and store.list_users("instagram"):
                ig_cfg = InstagramConfig.from_store(store, require_auth=dlp.enabled_for("instagram"))

        local_platforms = _load_local_platforms(store)

        if require_platforms and not (x_cfg or tt_cfg or ig_cfg or local_platforms):
            raise RuntimeError(
                "No platforms configured. Add users via "
                "`archiver config add --platform <x|tiktok|instagram> "
                "--user <name>`, or add a user-managed folder via "
                "`archiver local add <name>`, and ensure ENABLED_PLATFORMS "
                f"includes that platform in .env. (config.toml: {store.path})"
            )

        output_dir = _opt("OUTPUT_DIR", "./downloads")
        return cls(
            x                 = x_cfg,
            tiktok            = tt_cfg,
            instagram         = ig_cfg,
            policy_store      = store,
            output_dir        = output_dir,
            routes_dir        = _opt("ROUTES_DIR", "") or output_dir,
            db_path           = str(_core_db_path()),
            log_file          = _opt("LOG_FILE",   "./.archiver/archiver.log"),
            state_dir         = _opt("STATE_DIR",  "./.archiver"),
            sleep_min         = float(_opt("SLEEP_MIN", "3")),
            sleep_max         = float(_opt("SLEEP_MAX", "8")),
            max_concurrent_platforms = _opti("ARCHIVER_MAX_CONCURRENT_PLATFORMS", 0),
            enabled_platforms = enabled,
            reconcile_after_run = _opt("RECONCILE_AFTER_RUN", "false").lower()
                                  in {"1", "true", "yes", "on"},
            local_platforms   = local_platforms,
        )
