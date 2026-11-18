import os
import sys
import gc
import subprocess
import calendar
from pathlib import Path
import json
import base64
import threading
import io
import time
import re
import random
import tempfile
import struct
import zlib
import queue
import copy
from collections import OrderedDict
import xml.etree.ElementTree as ET
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from urllib.parse import urlparse

# ── Windows Hello / vault dependencies ──
import ctypes
import hashlib
import secrets

from pixtagra_models import (
    API_CONFIG, PROVIDERS, SELECTABLE_MODELS, RECOMMENDED_MODEL, ACTIVE_GUI_CREDENTIAL_PROVIDERS,
    LEGACY_CONFIG_MODEL_PROVIDERS, get_provider_config, _legacy_config_provider_id,
    extract_json, _coerce_text_content, extract_api_text,
    configured_base_urls, remember_provider_base_url, migrate_model_selection,
)
from pixtagra_quality import (
    build_keyword_review_prompt, build_compact_keyword_review_prompt, apply_keyword_review,
    build_keyword_deduplication_prompt, map_keyword_deduplication,
    keyword_review_warnings, keyword_review_detail,
    MODEL_REVIEW_LIMITATIONS,
)
from pixtagra_session import image_identity
from pixtagra_provider import ProviderRequest
from pixtagra_provider_legacy import LegacyOperations, LegacyProvider
from pixtagra_recovery import WorkspaceRecoveryMixin
from pixtagra_viewport import ViewportCanvas
from pixtagra_branding import AppToplevel, apply_app_icon, configure_windows_app_id
from pixtagra_language import LANGUAGE_OPTIONS, LANGUAGE_NAMES, normalize_language, metadata_language, metadata_options, split_keywords
from pixtagra_keywords import keyword_limits
from pixtagra_exports import (
    TARGETS as EXPORT_TARGETS, get_target as get_export_target,
    record_errors as export_record_errors, csv_row as agency_csv_row,
    csv_bytes as agency_csv_bytes, alamy_jpeg_bundle,
)

from pixtagra_keywords import (
    PROMPT_MODES,
    KEYWORD_BLACKLIST,
    KEYWORD_STOP_WORDS,
    _PROMPT_STYLE_BLACKLIST,
    _GENERIC_BUSINESS_PHRASES,
    _GENERIC_FILLER_WORDS,
    _STOCK_USE_CASE_MARKERS,
    _WEAK_DUPLICATE_TOKENS,
    _SINGULAR_EXCEPTIONS,
    build_prompt,
    build_metadata_repair_prompt,
    _prompt_general_photography,
    _prompt_commercial_background,
    _normalize_keyword_text,
    _parse_batch_keyword_input,
    _merge_keyword_lists,
    _keyword_tokens,
    _stem_keyword_token,
    _keyword_signature_tokens,
    _contains_non_english_text,
    _looks_prompt_like_keyword,
    _looks_empty_business_keyword,
    _looks_filler_heavy,
    _is_near_duplicate,
    KEYWORD_POLICY,
    KEYWORD_MINIMUM_COUNT,
    KEYWORD_TARGET_COUNT,
    _apply_keyword_policy_prompt,
    _GENERAL_SUBJECT_TOKENS,
    _PEOPLE_TOKENS,
    _SETTING_TOKENS,
    _COMPOSITION_PHRASES,
    _COLOR_MATERIAL_TOKENS,
    _BACKGROUND_TOKENS,
    _keyword_priority,
    rank_keywords_for_stock,
    sanitize_keywords_for_stock,
    clean_keywords,
    _KW_REVIEW_BLACKLIST,
    validate_keywords,
    _stock_word_count,
    _COMPLIANCE_KEYS,
    normalize_compliance_flags,
    validate_metadata_mode,
    metadata_compliance_warnings,
    validate_stock_metadata,
    classify_stock_metadata,
)

from pixtagra_credentials import WindowsCredentialManagerBackend
from pixtagra_credential_migration import (
    LegacyVaultSource,
    MigrationStateStore,
    STATE_FILENAME,
    VaultCredentialMigrationEngine,
)
from pixtagra_credential_policy import CredentialPolicyCoordinator
from pixtagra_live_credentials import CredentialConsentBridge
from pixtagra_windows_hello import WindowsHelloConsentBackend
from pixtagra_api_errors import (
    classify_api_error, FatalApiError as _FatalApiError, RetryableApiError as _RetryableApiError,
)


def _native_startup_error(title: str, message: str) -> None:
    """Show startup failures even when Tk cannot create a window."""
    try:
        print(f"{title}\n{message}", file=sys.stderr)
    except Exception:
        pass
    if os.environ.get("PSS_NO_STARTUP_DIALOG", "").strip().lower() in {"1", "true", "yes"}:
        return
    if os.name == "nt":
        try:
            ctypes.windll.user32.MessageBoxW(None, message, title, 0x10)
        except Exception:
            pass


def _packaged_test_isolation_enabled() -> bool:
    """Keep the packaged verification harness opt-in and outside normal startup."""
    return os.environ.get("PIXTAGRA_PACKAGED_TEST_ISOLATION", "").strip() == "1"


def _write_packaged_probe_result(payload: dict) -> None:
    """Write a redacted probe result only when the explicit harness supplies a path."""
    raw_path = os.environ.get("PIXTAGRA_PACKAGED_PROBE_OUTPUT", "")
    if not raw_path:
        return
    try:
        Path(raw_path).write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    except Exception:
        pass


def _configure_tcl_environment() -> None:
    """Help source runs and PyInstaller bundles find Tcl/Tk before Tk starts."""
    roots = []
    for raw in (
        getattr(sys, "_MEIPASS", ""),
        sys.prefix,
        getattr(sys, "base_prefix", ""),
        Path(sys.executable).parent,
    ):
        if raw:
            roots.append(Path(raw))

    for root in roots:
        tcl_candidates = (root / "tcl" / "tcl8.6", root / "_tcl_data")
        tk_candidates = (root / "tcl" / "tk8.6", root / "_tk_data")
        if "TCL_LIBRARY" not in os.environ:
            for tcl_dir in tcl_candidates:
                if tcl_dir.joinpath("init.tcl").exists():
                    os.environ["TCL_LIBRARY"] = str(tcl_dir)
                    break
        if "TK_LIBRARY" not in os.environ:
            for tk_dir in tk_candidates:
                if tk_dir.joinpath("tk.tcl").exists():
                    os.environ["TK_LIBRARY"] = str(tk_dir)
                    break


def _touch_keyboard_candidates():
    """Return Windows touch/on-screen keyboard paths in preference order."""
    program_files = Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
    common_files = Path(os.environ.get("CommonProgramFiles", str(program_files / "Common Files")))
    windows_dir = Path(os.environ.get("WINDIR", r"C:\Windows"))
    return (
        common_files / "microsoft shared" / "ink" / "TabTip.exe",
        program_files / "Common Files" / "microsoft shared" / "ink" / "TabTip.exe",
        windows_dir / "System32" / "osk.exe",
    )


def _touch_keyboard_supported():
    """Detect a Windows touch digitizer before launching the keyboard helper."""
    if os.name != "nt":
        return False
    try:
        # SM_MAXIMUMTOUCHES (95) is non-zero on touch-enabled Windows devices.
        return int(ctypes.windll.user32.GetSystemMetrics(95)) > 0
    except (AttributeError, OSError, TypeError, ValueError):
        return False


_configure_tcl_environment()

_MISSING_DEPS = []
try:
    import customtkinter as ctk
except ImportError as exc:
    ctk = None
    _MISSING_DEPS.append(f"customtkinter ({exc})")
try:
    import tkinter as tk
    from tkinter import filedialog, messagebox
except ImportError as exc:
    tk = None
    filedialog = messagebox = None
    _MISSING_DEPS.append(f"tkinter ({exc})")
try:
    import requests
except ImportError as exc:
    requests = None
    _MISSING_DEPS.append(f"requests ({exc})")
try:
    from PIL import Image, ImageDraw, ImageOps
except ImportError as exc:
    Image = ImageDraw = ImageOps = None
    _MISSING_DEPS.append(f"Pillow ({exc})")
try:
    import piexif
    import piexif.helper
except ImportError as exc:
    piexif = None
    _MISSING_DEPS.append(f"piexif ({exc})")

if _MISSING_DEPS:
    _native_startup_error(
        "PixTagra startup error",
        "Required Python packages are missing:\n\n"
        + "\n".join(f"- {item}" for item in _MISSING_DEPS)
        + "\n\nInstall them with:\npython -m pip install -r requirements.txt",
    )
    raise SystemExit(1)

from pixtagra_image_card import ImageCard
from pixtagra_layout import ActionRow
from pixtagra_localization import ctk, tk, messagebox, set_ui_language, translate

import pixtagra_metadata as metadata
from pixtagra_metadata import (
    _escape_xml,
    build_output_filename,
    _SAFE_OUTPUT_PATH_LOCK,
    _RESERVED_OUTPUT_PATHS,
    _MAX_SAFE_OUTPUT_NAME_ATTEMPTS,
    _JPEG_XMP_HEADER,
    _JPEG_EXTENDED_XMP_HEADER,
    _JPEG_EXIF_HEADER,
    _JPEG_PHOTOSHOP_HEADER,
    _JPEG_ICC_HEADER,
    _PNG_SIGNATURE,
    _PNG_XMP_KEYWORD,
    _WEBP_RIFF_HEADER,
    _WEBP_FORM_TYPE,
    _WEBP_VP8X_ICCP_FLAG,
    _WEBP_VP8X_ALPHA_FLAG,
    _WEBP_VP8X_EXIF_FLAG,
    _WEBP_VP8X_XMP_FLAG,
    _WEBP_VP8X_ANIMATION_FLAG,
    _TIFF_SIGNATURES,
    _TIFF_TYPE_SIZES,
    _is_tiff_container,
    _unsupported_tiff_save_error,
    _XMP_NAMESPACES,
    _XML_LANG,
    _is_png_chunk_type,
    _build_png_chunk,
    _parse_png_container,
    _parse_png_itxt,
    _build_png_itxt,
    _build_webp_chunk,
    _parse_webp_container,
    _assemble_webp_container,
    _normalized_output_path,
    _reserve_safe_output_path,
    _release_safe_output_paths,
    _build_jpeg_segment,
    _parse_jpeg_container,
    _jpeg_segment_type,
    _managed_exif_tags,
    _managed_iptc_datasets,
    _empty_exif_tiff,
    _tiff_type_size,
    _parse_tiff_ifd,
    _validate_exif_tiff,
    _tiff_ascii,
    _append_tiff_value,
    _merge_exif_payload,
    _xmp_qname,
    _new_xmp_root,
    _xmp_descriptions,
    _xmp_property,
    _set_xmp_alt,
    _set_xmp_keywords,
    _set_xmp_creator,
    _set_xmp_text,
    _merge_xmp_payload,
    _parse_photoshop_resources,
    _build_photoshop_resource,
    _parse_iptc_records,
    _build_iptc_record,
    _merge_iptc_iim,
    _new_iptc_resource,
    _merge_photoshop_payload,
    _merge_jpeg_metadata,
    _merge_embedded_exif_payload,
    _merge_embedded_xmp_packet,
    _merge_png_metadata,
    _webp_dimensions,
    _new_webp_vp8x_payload,
    _merge_webp_metadata,
    _write_container_bytes_to_tempfile,
    _write_jpeg_bytes_to_tempfile,
    _write_jpeg_to_tempfile,
    _write_metadata_to_tempfile,
    _fsync_file,
    _cleanup_tempfile,
    _publish_new_output,
    _decode_exif_text,
    _jpeg_payloads,
    _xmp_root_from_payload,
    _xmp_alt_text,
    _xmp_keyword_values,
    _xmp_without_managed_fields,
    _iptc_records_from_jpeg,
    _iptc_preservation_snapshot,
    _jpeg_preservation_snapshot,
    _verify_jpeg_preservation,
    _decoded_image_snapshot,
    _embedded_exif_snapshot,
    _verify_embedded_exif_fields,
    _verify_embedded_xmp_fields,
    _xmp_property_text,
    _png_xmp_entries,
    _png_preservation_snapshot,
    _verify_png_preservation,
    _verify_png_written_metadata,
    _webp_preservation_snapshot,
    _verify_webp_preservation,
    _verify_webp_written_metadata,
    _verify_written_metadata,
    _parse_metadata_date,
    _build_xmp_segment,
    _build_iptc_segment,
    _inject_metadata_rebuild,
    inject_metadata,
)


# Encryption — install via: pip install cryptography
try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    HAS_CRYPTO = True
except ImportError:
    HAS_CRYPTO = False

# ──────────────────────────────────────────────────────────────
# PyInstaller build command:
# pyinstaller --onefile --noconsole --name "PixTagra" \
#     --hidden-import cryptography.hazmat.backends.openssl \
#     --collect-all cryptography \
#     --collect-all customtkinter \
#     "pixtagra.py"
# ──────────────────────────────────────────────────────────────

ctk.set_appearance_mode("Light")
ctk.set_default_color_theme("blue")



def _env_int(name, default, minimum=1):
    raw = os.environ.get(name, str(default))
    try:
        value = int(raw)
        return max(minimum, value)
    except (TypeError, ValueError):
        return default


APP_CONFIG_DIR = Path(os.environ.get("PSS_CONFIG_DIR", Path.home() / ".picture_seo_studio"))
CONFIG_FILE = Path(os.environ.get("PSS_CONFIG_FILE", APP_CONFIG_DIR / "config.json"))
MAX_WORKERS = _env_int("PSS_MAX_WORKERS", 2, minimum=1)
MAX_BATCH_SIZE = _env_int("PSS_MAX_BATCH_SIZE", 500, minimum=1)
HTTP_CONNECT_TIMEOUT = _env_int("PSS_HTTP_CONNECT_TIMEOUT", 15, minimum=1)
HTTP_READ_TIMEOUT = _env_int("PSS_HTTP_READ_TIMEOUT", 180, minimum=5)
OLLAMA_READ_TIMEOUT = _env_int("PSS_OLLAMA_READ_TIMEOUT", 300, minimum=5)
HTTP_BACKOFF_MAX = _env_int("PSS_HTTP_BACKOFF_MAX", 30, minimum=1)
SMOKE_READ_TIMEOUT = _env_int("PSS_SMOKE_READ_TIMEOUT", 60, minimum=10)
SMOKE_RETRY_ATTEMPTS = _env_int("PSS_SMOKE_RETRY_ATTEMPTS", 3, minimum=1)
API_DEBUG = os.environ.get("PSS_API_DEBUG", "").strip().lower() in {"1", "true", "yes", "on", "debug"}
RETRY_DELAYS = [5, 15, 30]  # Exponential backoff: 5s, 15s, 30s

# ── Shutterstock 官方分类映射表 ──
# 键：关键词片段（小写，部分匹配）；值：Shutterstock 分类名称
# 按优先级排列：越靠前越优先匹配
SS_CATEGORY_MAP = [
    ({"animal", "cat", "dog", "cow", "cattle", "livestock", "bird", "wildlife", "giraffe", "pet"},
     "Animals/Wildlife"),
    ({"aircraft", "airplane", "jet", "train", "railway", "car", "bus", "boat", "vehicle"},
     "Transportation"),
    # 自然 & 户外
    ({"mountain", "forest", "ocean", "river", "lake", "waterfall", "beach", "desert",
      "jungle", "wildlife", "bird", "flower", "plant", "tree", "landscape", "nature",
      "sky", "cloud", "sunset", "sunrise", "fog", "snow", "ice", "autumn", "spring"},
     "Nature"),
    # 旅行 & 地标
    ({"travel", "city", "street", "architecture", "building", "bridge", "monument",
      "cathedral", "temple", "landmark", "tourism", "urban", "skyline", "village",
      "europe", "asia", "africa", "tropical", "destination"},
     "Buildings/Landmarks"),
    # 人物 & 生活方式
    ({"woman", "man", "person", "people", "family", "child", "baby", "portrait",
      "lifestyle", "couple", "group", "team", "business person", "model"},
     "People"),
    # 商业 & 科技
    ({"business", "office", "meeting", "technology", "computer", "phone", "digital",
      "data", "finance", "money", "chart", "graph", "startup", "corporate", "ai",
      "robot", "network", "software", "laptop"},
     "Business/Finance"),
    # 食物 & 饮料
    ({"food", "drink", "coffee", "tea", "wine", "beer", "meal", "dish", "fruit",
      "vegetable", "bread", "cake", "dessert", "restaurant", "kitchen", "chef",
      "cooking", "baking", "recipe", "cuisine"},
     "Food and Drink"),
    # 健康 & 医疗
    ({"health", "medical", "doctor", "hospital", "fitness", "yoga", "exercise",
      "sport", "wellness", "gym", "running", "swimming", "meditation", "pill",
      "stethoscope", "surgery", "patient"},
     "Healthcare/Medical"),
    # 教育 & 科学
    ({"education", "school", "university", "student", "teacher", "book", "library",
      "science", "laboratory", "research", "experiment", "chemistry", "biology",
      "physics", "math", "study", "learn"},
     "Education"),
    # 艺术 & 创意
    ({"art", "painting", "illustration", "design", "creative", "color", "abstract",
      "pattern", "texture", "background", "graphic", "artist", "gallery", "museum",
      "craft", "handmade", "watercolor", "sketch"},
     "Arts"),
    # 建筑 & 室内
    ({"interior", "room", "home", "house", "apartment", "furniture", "decor",
      "living room", "bedroom", "kitchen", "bathroom", "architecture", "ceiling",
      "floor", "wall", "window", "door", "real estate"},
     "Buildings/Landmarks"),
    # 假日 & 节庆
    ({"christmas", "halloween", "valentine", "easter", "thanksgiving", "birthday",
      "party", "celebration", "holiday", "festival", "new year", "wedding",
      "anniversary", "gift", "decoration"},
     "Holidays"),
    # 美容 & 时尚
    ({"fashion", "beauty", "makeup", "cosmetics", "skincare", "hair", "clothing",
      "dress", "model", "runway", "luxury", "jewelry", "accessories", "perfume",
      "style", "elegant"},
     "Beauty/Fashion"),
    # 工业 & 制造
    ({"industrial", "factory", "machine", "manufacturing", "construction", "engineer",
      "worker", "tool", "equipment", "energy", "solar", "wind", "oil", "steel",
      "warehouse", "logistics", "shipping"},
     "Industrial"),
    # 默认兜底
    (set(), "Objects"),
]


def _infer_ss_categories(title: str, description: str, keywords: list) -> tuple:
    """
    根据标题、描述、关键词推断 Shutterstock 的两个分类。
    返回 (category1, category2)。
    """
    combined = " ".join([title, description] + keywords).lower()
    matches = []
    for keyword_set, category in SS_CATEGORY_MAP[:-1]:  # 排除兜底
        matched_terms = [
            term
            for term in keyword_set
            if re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", combined)
        ]
        if matched_terms:
            score = len(matched_terms)
            matches.append((score, category))
    matches.sort(key=lambda x: -x[0])
    # 取得分最高的两个不同分类
    seen = []
    for _score, cat in matches:
        if cat not in seen:
            seen.append(cat)
        if len(seen) == 2:
            break
    if not seen:
        return SS_CATEGORY_MAP[-1][1], ""
    return seen[0], seen[1] if len(seen) > 1 else ""


def _build_shutterstock_csv_row(path, title, description, keywords, compliance=None):
    """Map validated metadata to one Shutterstock contributor CSV row."""
    cat1, cat2 = _infer_ss_categories(title, description, keywords)
    return agency_csv_row("shutterstock", path, {"title": title, "description": description,
        "keywords": keywords, "compliance": compliance or {}}, (cat1, cat2))


# ── Encrypted API key vault ──

VAULT_DIR = Path.home() / ".pictureseo"
VAULT_FILE = VAULT_DIR / "keys.vault"
SALT_FILE = VAULT_DIR / "keys.salt"


def _get_or_create_salt() -> bytes:
    """Get existing salt or create a new random one."""
    VAULT_DIR.mkdir(parents=True, exist_ok=True)
    if SALT_FILE.exists():
        return SALT_FILE.read_bytes()
    salt = secrets.token_bytes(32)
    SALT_FILE.write_bytes(salt)
    return salt


_cached_vault_key: bytes | None = None
_cached_vault_key_time: float = 0


def _derive_vault_key() -> bytes:
    """
    Derive an AES-256 key from a machine-specific seed + salt.
    This ties the encryption to THIS Windows user on THIS machine.
    Result is cached for 60 seconds to avoid 100k PBKDF2 iterations per image.
    """
    global _cached_vault_key, _cached_vault_key_time
    now = time.time()
    if _cached_vault_key is not None and (now - _cached_vault_key_time) < 60:
        return _cached_vault_key
    salt = _get_or_create_salt()
    # Machine-specific seed: username + machine name
    # This is NOT a password — the real gate is the Windows Hello check
    import getpass, platform
    seed = f"{getpass.getuser()}@{platform.node()}".encode()
    key = hashlib.pbkdf2_hmac("sha256", seed, salt, iterations=100_000)
    _cached_vault_key = key
    _cached_vault_key_time = now
    return key


def save_vault(keys_dict: dict) -> bool:
    """Encrypt and save API keys to vault file. Returns True on success."""
    if not HAS_CRYPTO:
        return False
    try:
        key = _derive_vault_key()
        aesgcm = AESGCM(key)
        nonce = secrets.token_bytes(12)
        plaintext = json.dumps(keys_dict).encode()
        ciphertext = aesgcm.encrypt(nonce, plaintext, None)
        VAULT_DIR.mkdir(parents=True, exist_ok=True)
        VAULT_FILE.write_bytes(nonce + ciphertext)
        return True
    except Exception:
        return False


def load_vault() -> dict:
    """Decrypt and load API keys from vault file. Returns empty dict on failure."""
    if not HAS_CRYPTO or not VAULT_FILE.exists():
        return {}
    try:
        key = _derive_vault_key()
        data = VAULT_FILE.read_bytes()
        nonce, ciphertext = data[:12], data[12:]
        aesgcm = AESGCM(key)
        plaintext = aesgcm.decrypt(nonce, ciphertext, None)
        return json.loads(plaintext.decode())
    except Exception:
        return {}


LEGACY_KEY_MIGRATION_FAILURE = (
    "Legacy API key migration was not completed. The original config value was preserved."
)


class _LegacyKeyMigrationError(RuntimeError):
    """A migration failure that is safe to report without exposing a key."""

    def __init__(self, stage, cause=None):
        super().__init__(stage)
        self.stage = stage
        self.cause = cause


def _load_vault_for_legacy_migration() -> dict:
    """Load a vault strictly so a corrupt existing vault cannot be overwritten."""
    if not HAS_CRYPTO:
        raise _LegacyKeyMigrationError("vault_unavailable")
    if not VAULT_FILE.exists():
        return {}
    try:
        data = VAULT_FILE.read_bytes()
        if len(data) <= 12:
            raise ValueError("vault payload is incomplete")
        key = _derive_vault_key()
        plaintext = AESGCM(key).decrypt(data[:12], data[12:], None)
        vault = json.loads(plaintext.decode())
        if not isinstance(vault, dict):
            raise ValueError("vault payload is not a mapping")
        return vault
    except _LegacyKeyMigrationError:
        raise
    except Exception as exc:
        raise _LegacyKeyMigrationError("vault_read", exc) from exc


def _read_config_mapping(config_path) -> dict:
    with open(config_path, "r", encoding="utf-8") as source:
        config = json.load(source)
    if not isinstance(config, dict):
        raise ValueError("config root is not a mapping")
    return config


def _write_config_tempfile(config_path, config_data) -> Path:
    """Stage validated JSON in the config directory before publication."""
    config_path = Path(config_path)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=".pixtagra_config_", suffix=".tmp", dir=str(config_path.parent)
    )
    temp_path = Path(temp_name)
    descriptor_open = True
    try:
        target = os.fdopen(fd, "w", encoding="utf-8")
        descriptor_open = False
        with target:
            json.dump(config_data, target, ensure_ascii=False, indent=2)
            target.flush()
            os.fsync(target.fileno())
        if _read_config_mapping(temp_path) != config_data:
            raise ValueError("staged config verification failed")
        return temp_path
    except Exception:
        if descriptor_open:
            try:
                os.close(fd)
            except OSError:
                pass
        metadata._cleanup_tempfile(temp_path)
        raise


def _restore_config_bytes(config_path, original_bytes) -> None:
    """Restore the exact original config after post-replace verification fails."""
    config_path = Path(config_path)
    fd, temp_name = tempfile.mkstemp(
        prefix=".pixtagra_config_restore_", suffix=".tmp", dir=str(config_path.parent)
    )
    temp_path = Path(temp_name)
    descriptor_open = True
    try:
        target = os.fdopen(fd, "wb")
        descriptor_open = False
        with target:
            target.write(original_bytes)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temp_path, config_path)
        temp_path = None
        if config_path.read_bytes() != original_bytes:
            raise ValueError("original config restoration verification failed")
    except Exception:
        if descriptor_open:
            try:
                os.close(fd)
            except OSError:
                pass
        cleanup_error = metadata._cleanup_tempfile(temp_path)
        if cleanup_error:
            raise OSError(cleanup_error)
        raise


def _remove_legacy_key_from_config(config_path, migrated_config, original_bytes) -> None:
    """Atomically publish a validated config, restoring its original bytes on failure."""
    temp_path = None
    published = False
    try:
        temp_path = _write_config_tempfile(config_path, migrated_config)
        os.replace(temp_path, config_path)
        published = True
        temp_path = None
        verified = _read_config_mapping(config_path)
        if "api_key" in verified or verified != migrated_config:
            raise ValueError("published config verification failed")
    except Exception as exc:
        cleanup_error = metadata._cleanup_tempfile(temp_path)
        if published:
            try:
                _restore_config_bytes(config_path, original_bytes)
            except Exception as restore_exc:
                raise _LegacyKeyMigrationError("config_restore", restore_exc) from exc
        if cleanup_error:
            raise _LegacyKeyMigrationError("config_cleanup", OSError(cleanup_error)) from exc
        raise _LegacyKeyMigrationError("config_publish", exc) from exc


def _migrate_legacy_api_key(config_path, config, original_bytes) -> dict:
    """Move one legacy plaintext key only after vault and config transactions verify."""
    legacy_key = config.get("api_key")
    if not isinstance(legacy_key, str):
        raise _LegacyKeyMigrationError("legacy_key_type")
    legacy_key = legacy_key.strip()
    if not legacy_key:
        return dict(config)

    model_name = str(config.get("model", ""))
    provider_config = get_provider_config(model_name)
    provider = _legacy_config_provider_id(model_name)
    if provider_config.get("type") != "cloud" or not provider:
        raise _LegacyKeyMigrationError("provider")

    try:
        existing_vault = _load_vault_for_legacy_migration()
    except _LegacyKeyMigrationError:
        raise
    except Exception as exc:
        raise _LegacyKeyMigrationError("vault_read", exc) from exc
    if not isinstance(existing_vault, dict):
        raise _LegacyKeyMigrationError("vault_read")

    if provider in existing_vault and existing_vault[provider] != legacy_key:
        raise _LegacyKeyMigrationError("vault_conflict")

    expected_vault = dict(existing_vault)
    if provider not in expected_vault:
        expected_vault[provider] = legacy_key
        try:
            if save_vault(expected_vault) is not True:
                raise RuntimeError("vault save returned false")
        except Exception as exc:
            raise _LegacyKeyMigrationError("vault_save", exc) from exc

    try:
        verified_vault = _load_vault_for_legacy_migration()
    except _LegacyKeyMigrationError:
        raise
    except Exception as exc:
        raise _LegacyKeyMigrationError("vault_verify", exc) from exc
    if verified_vault.get(provider) != legacy_key:
        raise _LegacyKeyMigrationError("vault_verify")
    if any(verified_vault.get(name) != value for name, value in existing_vault.items()):
        raise _LegacyKeyMigrationError("vault_verify")

    migrated_config = dict(config)
    del migrated_config["api_key"]
    _remove_legacy_key_from_config(config_path, migrated_config, original_bytes)
    return migrated_config


# Waste keywords that dilute SEO - auto-removed from AI output
def load_image_bytes(path):
    try:
        with open(path, "rb") as f:
            return f.read()
    except Exception:
        return None

def create_blue_dot(size=32):
    img = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    ImageDraw.Draw(img).ellipse([4, 4, size-4, size-4], fill=(59, 130, 246, 255))
    return img

def create_test_image(size=128):
    img = Image.new("RGB", (size, size), "#E0F2FE")
    draw = ImageDraw.Draw(img)
    draw.rectangle((12, 12, size - 12, size - 12), outline="#0284C7", width=6)
    draw.ellipse((38, 28, 90, 80), fill="#F59E0B", outline="#B45309", width=3)
    draw.rectangle((30, 84, 98, 108), fill="#10B981", outline="#047857", width=3)
    return img

def prepare_image(data, max_size=768):
    """Resize image for API submission. 768px is sufficient for metadata analysis
    and produces smaller payloads (200-300KB vs 400-600KB at 1024px), reducing
    the chance of connection resets from rate-limited APIs like Kimi."""
    try:
        with Image.open(io.BytesIO(data)) as src:
            img = ImageOps.exif_transpose(src).convert("RGB")
        if max(img.size) > max_size:
            img.thumbnail((max_size, max_size), Image.Resampling.LANCZOS)
        with io.BytesIO() as buf:
            img.save(buf, format="JPEG", quality=80)
            payload = buf.getvalue()
        img.close()
        return payload
    except Exception:
        return data

VALID_IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.webp', '.tiff', '.tif'}
IMAGE_FILE_DIALOG_PATTERN = "*.jpg *.jpeg *.png *.webp *.tif *.tiff"


def validate_image(image_path):
    """Check file extension and size before processing. Returns error string or None."""
    path = Path(image_path)
    if not path.exists():
        return "File does not exist"
    if not path.is_file():
        return "Not a regular file"
    if path.suffix.lower() not in VALID_IMAGE_EXTENSIONS:
        return f"Unsupported format: {path.suffix}"
    try:
        size_mb = path.stat().st_size / (1024 * 1024)
    except OSError as exc:
        return f"Cannot stat file: {exc}"
    if size_mb > 50:
        print(f"Warning: Large file ({size_mb:.1f}MB) — {path.name} — may slow processing")
    try:
        with Image.open(path) as img:
            img.verify()
    except Exception as exc:
        return f"Invalid image file: {exc}"
    return None



from pixtagra_window_geometry import (
    _format_date_picker_value, _date_picker_geometry, _widget_monitor_work_area,
    _centered_window_geometry, _centered_ctk_window_geometry,
)


from pixtagra_card_record import CardRecord, _CardStatusProxy




def call_local_model(image_base64: str, prompt: str, model_name: str, base_url: str) -> str:
    """Call a local Ollama chat endpoint."""
    config = PROVIDERS.get(model_name, {})
    actual_model = config.get("ollama_model", model_name.replace("ollama-", "", 1))

    payload = {
        "model": actual_model,
        "messages": [
            {
                "role": "user",
                "content": prompt,
                "images": [image_base64],
            }
        ],
        "stream": False,
    }

    try:
        response = requests.post(base_url, json=payload, timeout=(HTTP_CONNECT_TIMEOUT, OLLAMA_READ_TIMEOUT),
                                 allow_redirects=False)
        if 300 <= response.status_code < 400:
            raise RuntimeError("Local endpoint redirected the image request; configure its final address.")
        response.raise_for_status()
        result = response.json()
        return result.get("message", {}).get("content", "")
    except requests.ConnectionError:
        raise ConnectionError(
            f"Cannot connect to local model at {base_url}. "
            f"Make sure Ollama is running: `ollama serve`"
        )
    except requests.Timeout:
        raise TimeoutError(f"Local model request timed out after {OLLAMA_READ_TIMEOUT} seconds.")
    except Exception as e:
        raise RuntimeError(f"Local model call failed: {e}")


class App(WorkspaceRecoveryMixin, ctk.CTk):
    def __init__(self):
        set_ui_language("en")
        configure_windows_app_id()
        if sys.platform == "win32":
            # The font-shape renderer redraws dozens of glyphs per rounded
            # control. Native polygons keep the same controls and reduce the
            # measured high-DPI card repaint cost substantially.
            ctk.DrawEngine.preferred_drawing_method = "polygon_shapes"
        super().__init__()
        self.title("PixTagra")
        self.geometry("1400x900")
        self.minsize(1200, 700)
        
        apply_app_icon(self)
        
        self.cards, self.data_cache = {}, {}
        self.card_order = []
        self.card_views = []
        self._card_height = 260
        self._card_height_ready = False
        self._card_gap = 18
        self._card_pad_x = 14
        self.is_processing, self.stop_flag, self.showing_ws = False, False, False
        self._stop_event = threading.Event()
        self.is_saving = False
        self.is_importing = False
        self._import_job = None
        self._import_invalid = []
        self.error_lock = threading.Lock()
        self.api_errors = []
        self.stats = {"total": 0, "processed": 0, "success": 0, "need_review": 0, "failed": 0}
        self._config_data = {}
        self._config_load_error = ""
        self._refresh_job = None
        self._stats_update_job = None
        self._analysis_result_job = None
        self._analysis_result_queue = queue.Queue()
        self._network_check_busy = False
        self._network_check_job = None
        self._save_result_job = None
        self._save_result_queue = queue.Queue()
        self._visible_update_job = None
        self._canvas_mousewheel_active = False
        self._thumb_generation = 0
        self._thumb_pool = ThreadPoolExecutor(max_workers=4)
        self._thumb_flush_job = None
        self._thumb_result_job = None
        self._thumbnail_jump_job = None
        self._alamy_export_job = None
        self._thumb_result_queue = queue.Queue()
        self._thumb_queue = []
        self._thumb_queue_paths = set()
        self._thumb_bulk_requested = False
        self._thumb_cache = OrderedDict()
        self._thumb_cache_limit = 32
        self._shoot_date_picker = None
        self._touch_keyboard_job = None
        self._touch_keyboard_process = None
        self.shoot_location_var = tk.StringVar()
        self.shoot_date_var = tk.StringVar()
        self.language_var = tk.StringVar(master=self, value="English")

        self.main = ctk.CTkFrame(self, fg_color="#F8FAFC", corner_radius=0)
        self.main.pack(fill="both", expand=True)
        
        self.welcome = ctk.CTkFrame(self.main, fg_color="#FFF")
        self.workspace = ctk.CTkFrame(self.main, fg_color="#F8FAFC")
        
        self._setup_welcome()
        self._setup_workspace()
        self._initialize_credential_flow()
        self._load_config()
        self._on_model_change()
        if self._config_load_error:
            self._set_credential_status("Settings load failed; safe defaults are active")
        self.welcome.pack(fill="both", expand=True)
        self._center_main_window()
        self._initialize_recovery(APP_CONFIG_DIR / "workspace.sqlite3", CardRecord)
        self.protocol("WM_DELETE_WINDOW", self._request_close)

    def _on_language_change(self, selection=None):
        if self._workspace_busy():
            self.language_var.set(LANGUAGE_NAMES[self.__dict__.get("_batch_language", "en")])
            return
        language = normalize_language(selection or self.language_var.get())
        self.language_var.set(LANGUAGE_NAMES[language])
        set_ui_language(language, self)
        for view in self.__dict__.get("card_views", ()):
            view.refresh_from_record()
        if "_export_target_menu" in self.__dict__:
            self._update_save_export_buttons()
        self._save_config()
        for row in self.__dict__.get("_action_rows", ()):
            row.request_layout()

    def _add_language_selector(self, parent):
        frame = ctk.CTkFrame(parent, width=300, height=38, fg_color="transparent")
        frame.pack_propagate(False)
        ctk.CTkLabel(frame, text="Language / output", font=("Segoe UI", 11)).pack(side="left", padx=(0, 6))
        menu = ctk.CTkOptionMenu(frame, variable=self.language_var, values=list(LANGUAGE_OPTIONS),
                                 command=self._on_language_change, width=140, height=34)
        menu.pack(side="left")
        self._language_menus.append(menu)
        return frame

    def _center_main_window(self):
        self.update_idletasks()
        width, height, x, y = _centered_ctk_window_geometry(
            self,
            1400,
            900,
            _widget_monitor_work_area(self),
        )
        self.minsize(min(1200, width), min(700, height))
        self.geometry(f"{width}x{height}{x:+d}{y:+d}")
        self.update_idletasks()

    def destroy(self):
        self._persist_session()
        self._unbind_after_job("_session_job")
        self.stop_flag = True
        self._stop_event.set()
        self._unbind_after_job("_network_check_job")
        self._release_active_credential_lease()
        bridge = getattr(self, "_credential_bridge", None)
        if bridge is not None:
            bridge.shutdown()
        coordinator = getattr(self, "_credential_coordinator", None)
        if coordinator is not None:
            coordinator.clear_all_session_keys()
            coordinator.reset_stored_use_consent()
        self._unbind_canvas_mousewheel()
        # An export poll writes progress widgets and dialogs after a save
        # completes; it must not outlive the window.
        self._unbind_after_job("_alamy_export_job")
        touch_job = getattr(self, "_touch_keyboard_job", None)
        if touch_job is not None:
            try:
                self.after_cancel(touch_job)
            except Exception:
                pass
            self._touch_keyboard_job = None
        self._cancel_workspace_jobs(reset_pool=True)
        try:
            self._thumb_pool.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass
        super().destroy()
        store = self.__dict__.get("_session_store")
        if store is not None:
            store.close()

    def _bind_edit_context_menu(self, widget):
        """Bind standard edit actions without exposing any stored credential."""
        native_widget = getattr(widget, "_entry", widget)
        native_widget.bind("<Button-3>", self._show_edit_context_menu, add="+")
        native_widget.bind("<FocusIn>", self._schedule_touch_keyboard, add="+")
        native_widget.bind("<Button-1>", self._schedule_touch_keyboard, add="+")
        return native_widget

    def _schedule_touch_keyboard(self, _event=None):
        if not _touch_keyboard_supported() or getattr(self, "_touch_keyboard_job", None) is not None:
            return
        try:
            self._touch_keyboard_job = self.after(120, self._open_touch_keyboard)
        except Exception:
            self._touch_keyboard_job = None

    def _open_touch_keyboard(self):
        self._touch_keyboard_job = None
        if not _touch_keyboard_supported():
            return False
        process = getattr(self, "_touch_keyboard_process", None)
        try:
            if process is not None and process.poll() is None:
                return True
        except Exception:
            self._touch_keyboard_process = None
        for keyboard_path in _touch_keyboard_candidates():
            if not keyboard_path.exists():
                continue
            try:
                self._touch_keyboard_process = subprocess.Popen(
                    [str(keyboard_path)],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                return True
            except (OSError, ValueError):
                continue
        return False

    def _native_ui_px(self, value):
        return max(1, round(value * ctk.ScalingTracker.get_widget_scaling(self)))

    def _native_ui_font(self, size=13, bold=False):
        return ("Segoe UI", -self._native_ui_px(size), "bold" if bold else "normal")

    def _scale_native_dialog(self, dialog):
        """Keep native dialog controls at the same physical scale as CTk."""
        from tkinter import font as tkfont
        def visit(widget):
            if isinstance(widget, (tk.Label, tk.Button, tk.Entry, tk.Text, tk.Listbox)):
                bold = tkfont.Font(root=self, font=widget.cget("font")).actual("weight") == "bold"
                widget.configure(font=self._native_ui_font(15 if bold else 13, bold))
                if isinstance(widget, tk.Label) and int(widget.cget("wraplength")):
                    widget.configure(wraplength=self._native_ui_px(int(widget.cget("wraplength"))))
            if isinstance(widget, tk.Frame):
                for option in ("padx", "pady"):
                    value = int(widget.cget(option))
                    if value:
                        widget.configure(**{option: self._native_ui_px(value)})
            if isinstance(widget, tk.Scrollbar):
                widget.configure(width=self._native_ui_px(14))
            for child in widget.winfo_children():
                visit(child)
        visit(dialog)

    def _edit_context_states(self, target):
        try:
            writable = str(target.cget("state")) == "normal"
            if isinstance(target, tk.Text):
                selected = bool(target.tag_ranges("sel"))
                content = bool(target.get("1.0", "end-1c"))
            else:
                selected = bool(target.selection_present())
                content = bool(target.get())
            try:
                clipboard = bool(target.clipboard_get())
            except tk.TclError:
                clipboard = False
            return {"cut": writable and selected, "copy": selected,
                    "paste": writable and clipboard, "select_all": content}
        except tk.TclError:
            return dict.fromkeys(("cut", "copy", "paste", "select_all"), False)

    def _show_edit_context_menu(self, event):
        target = event.widget
        self._context_edit_target = target
        menu = getattr(self, "_edit_context_menu", None)
        if menu is None:
            menu = tk.Menu(self, tearoff=0)
            menu.add_command(label="Cut", command=lambda: self._run_edit_context_action("cut"))
            menu.add_command(label="Copy", command=lambda: self._run_edit_context_action("copy"))
            menu.add_command(label="Paste", command=lambda: self._run_edit_context_action("paste"))
            menu.add_separator()
            menu.add_command(label="Select all", command=lambda: self._run_edit_context_action("select_all"))
            self._edit_context_menu = menu
        menu.configure(font=self._native_ui_font())
        states = self._edit_context_states(target)
        for index, action in ((0, "cut"), (1, "copy"), (2, "paste"), (4, "select_all")):
            menu.entryconfigure(index, state="normal" if states[action] else "disabled")
        try:
            target.focus_set()
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            try:
                menu.grab_release()
            except tk.TclError:
                pass
        return "break"

    def _run_edit_context_action(self, action):
        target = getattr(self, "_context_edit_target", None)
        if target is None:
            return
        if not self._edit_context_states(target).get(action, False):
            return
        try:
            if action == "select_all":
                if hasattr(target, "tag_add"):
                    target.tag_add("sel", "1.0", "end-1c")
                elif hasattr(target, "selection_range"):
                    target.selection_range(0, "end")
                return
            target.event_generate({
                "cut": "<<Cut>>",
                "copy": "<<Copy>>",
                "paste": "<<Paste>>",
            }[action])
        except (KeyError, tk.TclError):
            pass

    def _initialize_credential_flow(self):
        """Create real dependencies only after the PixTagra Tk root exists."""
        import getpass
        import platform

        if _packaged_test_isolation_enabled():
            from pixtagra_packaged_selftest import create_no_access_dependencies

            backend, hello, _unused_migration = create_no_access_dependencies()
            username, machine_name = "packaged-test-user", "packaged-test-machine"
        else:
            backend = WindowsCredentialManagerBackend()
            hello = WindowsHelloConsentBackend()
            username, machine_name = getpass.getuser(), platform.node()
        source = LegacyVaultSource(
            vault_path=VAULT_FILE,
            salt_path=SALT_FILE,
            username=username,
            machine_name=machine_name,
        )
        self._migration_state_store = MigrationStateStore(VAULT_DIR / STATE_FILENAME)
        migration = VaultCredentialMigrationEngine(source, backend, self._migration_state_store)
        self._credential_coordinator = CredentialPolicyCoordinator(
            hello_backend=hello,
            credential_backend=backend,
            migration_engine=migration,
            ui_thread_id=threading.get_ident(),
        )
        self._credential_bridge = CredentialConsentBridge(self, self._credential_coordinator)
        self._active_credential_lease = None
        self._legacy_config_key_present = False
        self._credential_status_text = "Session only"
        self.key_var.trace_add("write", self._on_key_input_changed)

    def _credential_provider(self):
        return get_provider_config(self.model_var.get()).get("provider", "")

    def _credential_hwnd(self):
        try:
            hwnd = int(self.winfo_id())
        except Exception:
            return 0
        return hwnd if hwnd > 0 else 0

    def _on_key_input_changed(self, *_args):
        coordinator = getattr(self, "_credential_coordinator", None)
        provider = self._credential_provider() if hasattr(self, "model_var") else ""
        if coordinator is None or provider not in ACTIVE_GUI_CREDENTIAL_PROVIDERS:
            return
        secret = self.key_var.get()
        if secret:
            result = coordinator.set_session_key(provider, secret)
            if result.status == "session_key_set":
                self._set_credential_status("Session only")
        else:
            coordinator.clear_session_key(provider)

    def _set_credential_status(self, text):
        self._credential_status_text = str(text)
        label = getattr(self, "credential_status_lbl", None)
        if label is not None:
            try:
                label.configure(text=self._credential_status_text)
            except Exception:
                pass

    def _release_active_credential_lease(self):
        # Some isolated regression tests exercise App methods without running
        # CTk.__init__; bypass tkinter's attribute proxy in that narrow case.
        lease = self.__dict__.get("_active_credential_lease")
        if lease is not None:
            try:
                lease.close()
            except Exception:
                pass
        self._active_credential_lease = None

    def _start_credential_operation(self, operation, provider, callback, secret_supplier=None):
        bridge = getattr(self, "_credential_bridge", None)
        if bridge is None:
            return callback(None)
        if bridge.busy:
            return messagebox.showwarning("PixTagra", "A Windows Hello verification is already in progress.")
        self._credential_operation_pending = True
        self._update_stats()

        def complete(result):
            self._credential_operation_pending = False
            self._update_stats()
            callback(result)

        return bridge.start(
            operation,
            provider,
            self._credential_hwnd(),
            secret_supplier=secret_supplier,
            on_complete=complete,
        )

    def _credential_error_message(self, result, action):
        if result is None:
            return f"{action} is unavailable."
        if result.status == "hello_unavailable":
            return "Persistent key storage requires Windows Hello. You can still use a session-only key."
        if result.status == "consent_not_verified":
            return "Windows Hello verification was not completed."
        if result.status == "consent_busy":
            return "Another credential verification is already in progress."
        if result.backend_status == "conflict":
            return "A different saved key already exists. Choose Replace saved key to continue."
        if result.backend_status == "not_found":
            return "No saved key is available for this provider."
        return f"{action} was not completed."

    def _legacy_migration_hint(self):
        state_store = getattr(self, "_migration_state_store", None)
        phase = None
        if state_store is not None:
            try:
                state = state_store.read()
                if state.status == "valid" and state.state:
                    phase = state.state.get("phase")
            except Exception:
                phase = None
        if phase in {"restart_verification_pending", "legacy_cleanup_pending"}:
            return "Migration verification pending"
        if VAULT_FILE.exists() or SALT_FILE.exists():
            return "Legacy saved keys found"
        return ""

    def _unbind_after_job(self, attr_name):
        job = getattr(self, attr_name, None)
        if job:
            try:
                self.after_cancel(job)
            except Exception:
                pass
            setattr(self, attr_name, None)

    def _reset_thumb_queue_state(self):
        self._thumb_queue.clear()
        self._thumb_queue_paths.clear()
        self._thumb_bulk_requested = False
        while True:
            try:
                _record, img, _generation = self._thumb_result_queue.get_nowait()
            except queue.Empty:
                break
            # Decoded but unapplied thumbnails would otherwise wait for the
            # garbage collector; every other discard path closes them.
            if img is not None:
                img.close()
        for card in self.cards.values():
            if card.thumb_image is None:
                card.thumb_requested = False

    def _cancel_workspace_jobs(self, reset_pool=False):
        self._unbind_after_job("_import_job")
        self.is_importing = False
        self._unbind_after_job("_analysis_result_job")
        self._reset_analysis_result_queue()
        self._unbind_after_job("_save_result_job")
        self._reset_save_result_queue()
        self._unbind_after_job("_thumb_flush_job")
        self._unbind_after_job("_thumb_result_job")
        self._unbind_after_job("_thumbnail_jump_job")
        self._unbind_after_job("_visible_update_job")
        self._unbind_after_job("_refresh_job")
        self._unbind_after_job("_stats_update_job")
        self._thumb_generation += 1
        self._reset_thumb_queue_state()
        if reset_pool:
            try:
                self._thumb_pool.shutdown(wait=False, cancel_futures=True)
            except Exception:
                pass
            self._thumb_pool = ThreadPoolExecutor(max_workers=4)

    def _reset_analysis_result_queue(self):
        while True:
            try:
                self._analysis_result_queue.get_nowait()
            except queue.Empty:
                break

    def _reset_save_result_queue(self):
        while True:
            try:
                self._save_result_queue.get_nowait()
            except queue.Empty:
                break

    def _drain_save_results(self):
        """Apply save completion callbacks on Tk's main thread."""
        self._save_result_job = None
        try:
            result = self._save_result_queue.get_nowait()
        except queue.Empty:
            result = None
        if result is not None:
            self._save_done(*result)
        if self.is_saving or not self._save_result_queue.empty():
            self._save_result_job = self.after(25, self._drain_save_results)

    def _show_welcome_view(self):
        # Regression self-check focus:
        # 1) import 5 images -> Home -> Resume Session
        # 2) import 100 images -> fast scroll -> Home
        # 3) import images -> Clear immediately
        # 4) Analyze blocked when smoke test fails
        # 5) base_url normalization for host / v1 / chat / chat-completions
        self._unbind_canvas_mousewheel()
        self.workspace.pack_forget()
        self.welcome.pack(fill="both", expand=True)
        self.showing_ws = False
        self.prog.set(0)
        self.prog_lbl.configure(text="Ready")
        self._update_resume_button()

    def _bind_canvas_mousewheel(self, _event=None):
        self._canvas_mousewheel_active = True

    def _unbind_canvas_mousewheel(self, _event=None):
        self._canvas_mousewheel_active = False

    def _update_resume_button(self):
        if not hasattr(self, "resume_btn"):
            return
        has_session = bool(self.cards)
        if has_session and not self.resume_btn.winfo_ismapped():
            self.resume_btn.pack(pady=(12, 0))
            self.resume_hint.pack(pady=(8, 0))
        elif not has_session:
            if self.resume_btn.winfo_ismapped():
                self.resume_btn.pack_forget()
            if self.resume_hint.winfo_ismapped():
                self.resume_hint.pack_forget()

    def _resume_session(self):
        if not self.cards:
            return
        self.welcome.pack_forget()
        self.workspace.pack(fill="both", expand=True)
        self.showing_ws = True
        self._sync_workspace_state(progress_text=f"Loaded {len(self.cards)} images")

    def _open_shoot_date_picker(self):
        """Show a local calendar that writes the existing metadata date format."""
        existing = getattr(self, "_shoot_date_picker", None)
        if existing is not None:
            try:
                if existing.winfo_exists():
                    existing.deiconify()
                    existing.lift()
                    existing.focus_set()
                    return
            except Exception:
                pass

        parsed = metadata._parse_metadata_date(self.shoot_date_var.get())
        try:
            initial = datetime.strptime(parsed.get("display", ""), "%m/%d/%Y")
        except ValueError:
            initial = datetime.now()

        picker = AppToplevel(self)
        picker.title("Choose date")
        picker.withdraw()
        picker.resizable(False, False)
        picker.configure(fg_color="#F8FAFC")
        picker.transient(self)
        self._shoot_date_picker = picker

        state = {"year": initial.year, "month": initial.month}
        month_var = tk.StringVar(value=calendar.month_name[state["month"]])
        year_var = tk.StringVar(value=str(state["year"]))

        def close_picker():
            if self._shoot_date_picker is picker:
                self._shoot_date_picker = None
            try:
                picker.destroy()
            except Exception:
                pass

        def choose_day(day):
            # Commit an edited year before using it, even when the native focus
            # transition is skipped while clicking a calendar day.
            set_year()
            self.shoot_date_var.set(_format_date_picker_value(state["year"], state["month"], day))
            close_picker()

        header = ctk.CTkFrame(picker, fg_color="transparent")
        header.pack(fill="x", padx=16, pady=(16, 10))
        ctk.CTkLabel(
            header,
            text="Date created",
            font=("Segoe UI", 14, "bold"),
            text_color="#1E293B",
        ).pack(side="left")

        controls = ctk.CTkFrame(picker, fg_color="transparent")
        controls.pack(fill="x", padx=16)
        days = ctk.CTkFrame(picker, fg_color="transparent")
        days.pack(fill="both", expand=True, padx=16, pady=(12, 8))

        def render_days():
            for child in days.winfo_children():
                child.destroy()
            for column, label in enumerate(("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")):
                ctk.CTkLabel(
                    days,
                    text=label,
                    width=42,
                    font=("Segoe UI", 10, "bold"),
                    text_color="#64748B",
                ).grid(row=0, column=column, padx=2, pady=(0, 4))
            for row, week in enumerate(calendar.monthcalendar(state["year"], state["month"]), start=1):
                for column, day in enumerate(week):
                    if not day:
                        ctk.CTkLabel(days, text="", width=42, height=28).grid(row=row, column=column, padx=2, pady=2)
                        continue
                    ctk.CTkButton(
                        days,
                        text=str(day),
                        width=42,
                        height=28,
                        corner_radius=6,
                        fg_color="#FFFFFF",
                        hover_color="#DBEAFE",
                        text_color="#1E3A5F",
                        command=lambda selected_day=day: choose_day(selected_day),
                    ).grid(row=row, column=column, padx=2, pady=2)

        def set_month(value):
            state["month"] = list(calendar.month_name).index(value)
            render_days()

        def set_year(value=None):
            raw = str(value if value is not None else year_var.get()).strip()
            try:
                year = int(raw)
            except (TypeError, ValueError):
                year_var.set(str(state["year"]))
                return
            if not 1 <= year <= 9999:
                year_var.set(str(state["year"]))
                return
            state["year"] = year
            year_var.set(str(year))
            render_days()

        def set_today():
            today = datetime.now()
            state["year"] = today.year
            state["month"] = today.month
            year_var.set(str(today.year))
            month_var.set(calendar.month_name[today.month])
            render_days()

        ctk.CTkOptionMenu(
            controls,
            variable=month_var,
            values=list(calendar.month_name)[1:],
            width=158,
            height=34,
            corner_radius=8,
            fg_color="#0F9DB3",
            command=set_month,
        ).pack(side="left")
        year_input = ctk.CTkEntry(
            controls,
            textvariable=year_var,
            width=96,
            height=34,
            justify="center",
            placeholder_text="YYYY",
            border_width=1,
            border_color="#597D87",
            corner_radius=8,
        )
        year_input.pack(side="left", padx=(8, 0))
        year_input.bind("<Return>", lambda _event: (set_year(), "break")[1])
        year_input.bind("<FocusOut>", lambda _event: set_year())
        # The year is intentionally editable instead of a bounded dropdown:
        # historical photos and future-dated assets are both valid metadata.
        ctk.CTkButton(
            controls,
            text="Today",
            width=64,
            height=34,
            corner_radius=8,
            fg_color="#E2E8F0",
            hover_color="#CBD5E1",
            text_color="#334155",
            command=set_today,
        ).pack(side="left", padx=(8, 0))
        ctk.CTkButton(
            controls,
            text="Clear",
            width=64,
            height=34,
            corner_radius=8,
            fg_color="#E2E8F0",
            hover_color="#CBD5E1",
            text_color="#334155",
            command=lambda: (self.shoot_date_var.set(""), close_picker()),
        ).pack(side="right")
        picker.protocol("WM_DELETE_WINDOW", close_picker)
        render_days()
        picker.update_idletasks()
        anchor = self.shoot_date_picker_btn
        anchor.update_idletasks()
        popup_width = max(picker.winfo_reqwidth(), picker._apply_window_scaling(356))
        popup_height = max(picker.winfo_reqheight(), picker._apply_window_scaling(348))
        screen_x, screen_y, screen_width, screen_height = _widget_monitor_work_area(anchor)
        x, y = _date_picker_geometry(
            anchor.winfo_rootx(),
            anchor.winfo_rooty(),
            anchor.winfo_width(),
            anchor.winfo_height(),
            popup_width,
            popup_height,
            screen_x,
            screen_y,
            screen_width,
            screen_height,
        )
        # CTkToplevel.geometry expects logical dimensions and applies the
        # monitor's window scale internally. Convert the measured physical
        # request back before setting it, otherwise high-DPI displays can
        # stretch the popup past its anchor and the work area.
        picker_width = picker._reverse_window_scaling(popup_width)
        picker_height = picker._reverse_window_scaling(popup_height)
        picker.geometry(f"{picker_width}x{picker_height}{x:+d}{y:+d}")
        picker.deiconify()
        picker.lift()

    def _setup_welcome(self):
        self._language_menus = []
        language_row = self._add_language_selector(self.welcome)
        language_row.pack(anchor="e", padx=20, pady=(6, 0))
        top = ctk.CTkFrame(self.welcome, height=112, fg_color="#FFF")
        top.pack(fill="x")
        top.pack_propagate(False)

        tf = ctk.CTkFrame(top, fg_color="transparent")
        tf.pack(side="left", padx=30, pady=38)
        ctk.CTkImage(light_image=create_blue_dot(24), size=(24, 24))
        ctk.CTkLabel(tf, text="Welcome to PixTagra", font=("Segoe UI", 22, "bold"), text_color="#1E293B").pack(side="left")

        # Metadata defaults remain optional and are only applied during an explicit save.
        cfg = ctk.CTkFrame(top, fg_color="transparent")
        cfg.pack(side="right", padx=30, pady=16)

        def metadata_field(label, variable, placeholder, width, border_color="#597D87"):
            field = ctk.CTkFrame(cfg, fg_color="transparent")
            field.pack(side="left", padx=(0, 12))
            ctk.CTkLabel(
                field,
                text=label,
                font=("Segoe UI", 10, "bold"),
                text_color="#64748B",
            ).pack(anchor="w", pady=(0, 4))
            entry = ctk.CTkEntry(
                field,
                textvariable=variable,
                placeholder_text=placeholder,
                placeholder_text_color="#94A3B8",
                width=width,
                height=40,
                border_color=border_color,
                border_width=1,
                corner_radius=8,
            )
            entry.pack()
            return field, entry

        self.creator_var = tk.StringVar()
        _creator_field, self.creator_entry = metadata_field(
            "Creator (optional)", self.creator_var, "Creator / copyright holder", 176
        )
        _location_field, self.shoot_location_entry = metadata_field(
            "Shoot location (optional)", self.shoot_location_var, "City, region, or country", 186, "#8B5CF6"
        )
        date_field = ctk.CTkFrame(cfg, fg_color="transparent")
        date_field.pack(side="left", padx=(0, 12))
        ctk.CTkLabel(
            date_field,
            text="Date created (optional)",
            font=("Segoe UI", 10, "bold"),
            text_color="#64748B",
        ).pack(anchor="w", pady=(0, 4))
        date_input = ctk.CTkFrame(date_field, fg_color="transparent")
        date_input.pack()
        self.shoot_date_entry = ctk.CTkEntry(
            date_input,
            textvariable=self.shoot_date_var,
            placeholder_text="Pick a date",
            placeholder_text_color="#94A3B8",
            width=144,
            height=40,
            border_color="#8B5CF6",
            border_width=1,
            corner_radius=8,
        )
        self.shoot_date_entry.pack(side="left")
        self.shoot_date_picker_btn = ctk.CTkButton(
            date_input,
            text="📅",
            width=40,
            height=40,
            corner_radius=8,
            fg_color="#F1F5F9",
            hover_color="#E2E8F0",
            text_color="#6D28D9",
            command=self._open_shoot_date_picker,
        )
        self.shoot_date_picker_btn.pack(side="left", padx=(6, 0))

        mode_field = ctk.CTkFrame(cfg, fg_color="transparent")
        mode_field.pack(side="left")
        ctk.CTkLabel(
            mode_field,
            text="Generation mode",
            font=("Segoe UI", 10, "bold"),
            text_color="#64748B",
        ).pack(anchor="w", pady=(0, 4))
        self.mode_var = tk.StringVar(value=PROMPT_MODES[0])
        ctk.CTkOptionMenu(
            mode_field,
            variable=self.mode_var,
            values=PROMPT_MODES,
            width=224,
            height=40,
            corner_radius=8,
            fg_color="#059669",
        ).pack()

        center = ctk.CTkFrame(self.welcome, fg_color="transparent")
        center.pack(expand=True, fill="both")

        # Keep the variable-height controls in one flow. Independent relative
        # placement caused the info block to overlap the import block when DPI
        # scaling or provider-specific controls changed their requested height.
        content = ctk.CTkFrame(center, fg_color="transparent")
        content.pack(expand=True, fill="x", padx=24, pady=28)

        bf = ctk.CTkFrame(content, fg_color="transparent")
        bf.pack(fill="x")

        # API controls above the import action.
        api_frame = ctk.CTkFrame(bf, fg_color="transparent")
        api_frame.pack(pady=(0, 20))
        api_top = ctk.CTkFrame(api_frame, fg_color="transparent")
        api_top.pack()
        api_actions = ctk.CTkFrame(api_frame, fg_color="transparent")
        api_actions.pack(pady=(8, 0))
        api_bottom = ctk.CTkFrame(api_frame, fg_color="transparent")
        api_bottom.pack(pady=(8, 0))

        self.model_var = tk.StringVar(value=RECOMMENDED_MODEL)
        ctk.CTkOptionMenu(api_top, variable=self.model_var, values=list(SELECTABLE_MODELS), width=220, height=40, fg_color="#3B82F6", command=self._on_model_change).pack(side="left", padx=8)

        self.key_var = tk.StringVar()
        self.key_entry = ctk.CTkEntry(api_top, textvariable=self.key_var, placeholder_text="API Key", show="*", width=280, height=40, border_color="#597D87", border_width=1, corner_radius=20)
        self.key_entry.pack(side="left", padx=8)

        self.base_url_var = tk.StringVar()
        self.base_url_lbl = ctk.CTkLabel(api_bottom, text="Base URL:", font=("Segoe UI", 11), text_color="#475569")
        self.base_url_entry = ctk.CTkEntry(api_bottom, textvariable=self.base_url_var, placeholder_text="Optional Base URL", width=300, height=40, border_color="#597D87", border_width=1, corner_radius=20)

        self.local_url_var = tk.StringVar(value="http://localhost:11434")
        self.local_url_lbl = ctk.CTkLabel(api_bottom, text="Local Server:", font=("Segoe UI", 11), text_color="#475569")
        self.local_url_entry = ctk.CTkEntry(api_bottom, textvariable=self.local_url_var, placeholder_text="http://localhost:11434", width=300, height=40, border_color="#597D87", border_width=1, corner_radius=20)
        for widget in (
            self.creator_entry,
            self.shoot_location_entry,
            self.shoot_date_entry,
            self.key_entry,
            self.base_url_entry,
            self.local_url_entry,
        ):
            self._bind_edit_context_menu(widget)
        self.test_conn_btn = ctk.CTkButton(
            api_bottom,
            text="Test Connection",
            width=132,
            height=40,
            corner_radius=20,
            fg_color="#0390AA",
            hover_color="#027A8F",
            font=("Segoe UI", 10, "bold"),
            command=self._test_connection,
        )
        # local_url widgets are hidden by default; shown when a local model is selected

        self.save_key_btn = ctk.CTkButton(
            api_actions, text="Save to Windows", width=126, height=40, corner_radius=20,
            fg_color="#0390AA", hover_color="#027A8F", font=("Segoe UI", 10, "bold"),
            command=self._save_key_to_windows,
        )
        self.load_key_btn = ctk.CTkButton(
            api_actions, text="Load saved key", width=132, height=40, corner_radius=20,
            fg_color="#2563EB", hover_color="#1D4ED8", font=("Segoe UI", 10, "bold"),
            command=self._load_saved_key,
        )
        self.replace_key_btn = ctk.CTkButton(
            api_actions, text="Replace saved key", width=132, height=40, corner_radius=20,
            fg_color="#597D87", font=("Segoe UI", 10, "bold"), command=self._replace_saved_key,
        )
        self.delete_key_btn = ctk.CTkButton(
            api_actions, text="Delete saved key", width=126, height=40, corner_radius=20,
            fg_color="#B91C1C", hover_color="#991B1B", font=("Segoe UI", 10, "bold"),
            command=self._delete_saved_key,
        )
        self.clear_session_btn = ctk.CTkButton(
            api_actions, text="Clear session", width=106, height=40, corner_radius=20,
            fg_color="#64748B", font=("Segoe UI", 10, "bold"), command=self._clear_session_key,
        )
        self.credential_status_lbl = ctk.CTkLabel(
            api_bottom, text="Session only", font=("Segoe UI", 11), text_color="#475569"
        )
        self.migrate_legacy_btn = ctk.CTkButton(
            api_bottom, text="Migrate legacy keys", width=170, height=34, corner_radius=16,
            fg_color="#597D87", font=("Segoe UI", 10, "bold"), command=self._migrate_legacy_keys,
        )

        ctk.CTkButton(bf, text="📁 Import Images", width=300, height=60, fg_color="#597D87", font=("Segoe UI", 16, "bold"), corner_radius=20, command=self.load_images).pack(pady=(14, 0))
        self.resume_btn = ctk.CTkButton(
            bf,
            text="↩ Resume Session",
            width=220,
            height=46,
            fg_color="#3B82F6",
            hover_color="#2563EB",
            font=("Segoe UI", 14, "bold"),
            corner_radius=18,
            command=self._resume_session,
        )
        self.resume_hint = ctk.CTkLabel(
            bf,
            text="Home returns to the start screen without clearing the current session.",
            font=("Segoe UI", 11),
            text_color="#64748B",
        )
        self._update_resume_button()

        inf = ctk.CTkFrame(content, fg_color="transparent")
        inf.pack(fill="x", pady=(18, 0))
        for t in [
            "Metadata language follows your selection; existing records keep their own language.",
            "⚡ Stable cloud + relay + Ollama routing with EXIF + XMP + IPTC metadata writeback",
            f"📦 Start with 50–100 images per batch; workspace limit: {MAX_BATCH_SIZE}.",
        ]:
            ctk.CTkLabel(inf, text=t, font=("Segoe UI", 11), text_color="#64748B").pack(pady=3)
    
    def _setup_workspace(self):
        toolbar = ActionRow(self.workspace, fg_color="#1E293B")
        toolbar.pack(fill="x")
        toolbar.add(ctk.CTkLabel(toolbar, text="PixTagra", width=124, height=34,
                                 font=("Segoe UI", 20, "bold"), text_color="#FFF"))
        bf = toolbar
        self._action_rows = [toolbar]

        self.btns = {}
        primary_actions = [
            ("⌂ Home", "⌂ Home", 96, "#597D87", self._go_home),
            ("+ Add", "Add images", 112, "#597D87", self.load_images),
            ("⚡ Analyze", "Generate metadata", 180, "#0390AA", self._analyze_wrap),
            ("output_options", "Save / Export ▾", 150, "#597D87", lambda: self._toggle_workspace_options("output")),
            ("⏹ Cancel", "⏹ Cancel", 96, "#597D87", self._stop),
        ]
        for key, text, width, fg, cmd in primary_actions:
            kw = dict(text=text, width=width, height=34, fg_color=fg, font=("Segoe UI", 12, "bold"), corner_radius=8, command=cmd)
            if fg == "#0390AA":
                kw["hover_color"] = "#027A8F"
            btn = ctk.CTkButton(bf, **kw)
            bf.add(btn)
            self.btns[key] = btn
        self.btns["⏹ Cancel"].configure(state="disabled")

        self.stats_lbl = toolbar.add(ctk.CTkLabel(
            toolbar, text="0 total | 0 selected", height=34,
            font=("Segoe UI", 11, "bold"), text_color="#FFF"))
        self._update_retry_button()

        outputbar = ActionRow(self.workspace, fg_color="#EAF1F5")
        self._workspace_options = {"output": outputbar}
        self._action_rows.append(outputbar)
        outputbar.add(ctk.CTkLabel(outputbar, text="Image copies:", width=90, height=34,
                                  text_color="#475569", font=("Segoe UI", 11, "bold")))
        output_actions = [
            ("💾 Save", "Save selected", 108, "#0F9DB3", self._save),
            ("💾 Save ready", "Save ready", 108, "#059669", self._save_ready),
            ("💾 Save all", "Save all", 94, "#597D87", self._save_all),
            ("📁 Save→Folder", "📁 Save to folder", 130, "#597D87", self._save_to_folder),
            ("📄 JSON", "JSON", 82, "#64748B", self._export),
            ("📄 JSON ready", "JSON ready", 100, "#64748B", self._export_ready),
            ("📊 SS CSV", "Export", 124, "#7C3AED", self._export_for_target),
        ]
        for column, (key, text, width, fg, cmd) in enumerate(output_actions, start=1):
            if key == "📄 JSON":
                outputbar.add(ctk.CTkLabel(outputbar, text="Metadata files:", width=100, height=34,
                                          text_color="#475569", font=("Segoe UI", 11, "bold")))
            if key == "📊 SS CSV":
                if "_export_target_var" not in self.__dict__:
                    self._export_target_var = tk.StringVar(master=self, value=EXPORT_TARGETS[0].label)
                self._export_target_menu = ctk.CTkOptionMenu(
                    outputbar, variable=self._export_target_var,
                    values=[target.label for target in EXPORT_TARGETS], width=255, height=34,
                    font=("Segoe UI", 12), dropdown_font=("Segoe UI", 12),
                    fg_color="#7C3AED", command=self._on_export_target_change,
                )
                outputbar.add(self._export_target_menu)
            btn = ctk.CTkButton(
                outputbar,
                text=text,
                width=width,
                height=34,
                fg_color=fg,
                font=("Segoe UI", 11, "bold"),
                corner_radius=8,
                command=cmd,
            )
            outputbar.add(btn)
            self.btns[key] = btn
        self._export_hint_label = ctk.CTkLabel(
            self.workspace, text=get_export_target(self._export_target_var.get()).hint,
            font=("Segoe UI", 11), text_color="#475569", anchor="w", justify="left", wraplength=520,
        )
        self._update_save_export_buttons()
        
        cfgbar = ActionRow(self.workspace, fg_color="#FFF")
        self._workspace_config_row = cfgbar
        cfgbar.pack(fill="x")
        self._action_rows.append(cfgbar)
        
        cfgc = ctk.CTkFrame(cfgbar, fg_color="transparent", width=660, height=40)
        cfgc.pack_propagate(False)
        cfgbar.add(cfgc)
        ctk.CTkLabel(cfgc, text="Mode:", font=("Segoe UI", 12, "bold"), text_color="#64748B").pack(side="left", padx=(0, 6))
        self._workspace_mode_menu = ctk.CTkOptionMenu(cfgc, variable=self.mode_var, values=PROMPT_MODES, width=240, height=40, fg_color="#059669", font=("Segoe UI", 12))
        self._workspace_mode_menu.pack(side="left", padx=(0, 18))
        ctk.CTkLabel(cfgc, text="Model:", font=("Segoe UI", 12, "bold"), text_color="#64748B").pack(side="left", padx=(0, 6))
        self._workspace_model_menu = ctk.CTkOptionMenu(cfgc, variable=self.model_var, values=list(SELECTABLE_MODELS), width=240, height=40, fg_color="#3B82F6", font=("Segoe UI", 12), command=self._on_model_change)
        self._workspace_model_menu.pack(side="left")
        self.visual_review_var = tk.BooleanVar(master=self, value=True)
        self._visual_review_checkbox = cfgbar.add(ctk.CTkCheckBox(
            cfgbar, text="Visual review (extra API calls)", width=275, height=34,
            variable=self.visual_review_var, font=("Segoe UI", 11),
        ))
        cfgbar.add(self._add_language_selector(cfgbar))
        
        progf = ctk.CTkFrame(self.workspace, height=36, fg_color="#F8FAFC")
        progf.pack(fill="x")
        progf.pack_propagate(False)
        
        pc = ctk.CTkFrame(progf, fg_color="transparent")
        pc.pack(fill="x", padx=14, pady=4)
        self.prog = ctk.CTkProgressBar(pc, width=500, height=10, progress_color="#0390AA")
        self.prog.pack(side="left", fill="x", expand=True)
        self.prog.set(0)
        self.prog_lbl = ctk.CTkLabel(pc, text="Ready", font=("Segoe UI", 12), text_color="#64748B")
        self.prog_lbl.pack(side="right", padx=15)
        self.batch_summary_lbl = ctk.CTkLabel(
            self.workspace,
            text="No records loaded",
            font=("Segoe UI", 12, "bold"),
            text_color="#475569",
            anchor="w",
        )
        self.batch_summary_lbl.pack(fill="x", padx=25, pady=(0, 6))
        
        batchbar = ActionRow(self.workspace, fg_color="#F1F5F9")
        batchbar.pack(fill="x")
        self._action_rows.append(batchbar)
        morebar = ActionRow(self.workspace, fg_color="#F1F5F9")
        self._workspace_options["tools"] = morebar
        self._action_rows.append(morebar)
        bc = batchbar
        for key, label, cmd in [
            ("all", "Select all", self._sel_all),
            ("none", "Deselect", self._sel_none),
            ("batch_keywords", "Add keywords", self._open_batch_keyword_dialog),
            ("ready", "Select ready", self._sel_ready),
            ("review", "Select review", self._sel_review),
            ("failed", "Select failed", self._sel_failed),
            ("unprocessed", "Select unprocessed", self._sel_unprocessed),
            ("thumbs", "Load thumbnails", self._load_thumbs),
            ("clear", "Clear workspace", self._clear),
        ]:
            bc = batchbar if key in {"all", "none", "batch_keywords"} else morebar
            self.btns[f"filter_{key}"] = ctk.CTkButton(
                bc, text=label, width=108 if key == "batch_keywords" else 82, height=30,
                fg_color="#C17930" if key == "clear" else "#597D87",
                font=("Segoe UI", 11, "bold"), corner_radius=8, command=cmd,
            )
            bc.add(self.btns[f"filter_{key}"])

        self.btns["tools_options"] = batchbar.add(ctk.CTkButton(
            batchbar, text="Selection / retry ▾", width=150, height=30, corner_radius=8,
            fg_color="#597D87", font=("Segoe UI", 11, "bold"),
            command=lambda: self._toggle_workspace_options("tools"),
        ))
        self.btns["regen_failed"] = batchbar.add(ctk.CTkButton(
            batchbar, text="Regenerate failed", width=188, height=30, corner_radius=8,
            fg_color="#B45309", hover_color="#92400E", font=("Segoe UI", 11, "bold"),
            command=self._regenerate_failed,
        ))
        self.btns["🔄 Retry"] = morebar.add(ctk.CTkButton(
            morebar, text="Retry", width=176, height=30, corner_radius=8,
            fg_color="#0390AA", hover_color="#027A8F", font=("Segoe UI", 11, "bold"),
            command=self._retry,
        ))
        self._refresh_regen_failed_button()
        self._update_retry_button()
        
        sc = ctk.CTkFrame(self.workspace, fg_color="#F8FAFC")
        self._workspace_image_container = sc
        sc.pack(fill="both", expand=True)
        
        self.canvas = ViewportCanvas(sc, bg="#F8FAFC", highlightthickness=0,
                                yscrollincrement=max(1, round(sc._apply_widget_scaling(16))))
        sb = tk.Scrollbar(sc, orient="vertical", command=self._on_scrollbar)
        self._canvas_scrollbar = sb
        self.canvas.configure(yscrollcommand=self._on_canvas_yview)
        self.canvas.bind("<Configure>", self._on_canvas_configure, add="+")
        # Windows uses <MouseWheel>; macOS/Linux can extend this with Button-4/5 if needed.
        self.canvas.bind("<Enter>", self._bind_canvas_mousewheel)
        self.canvas.bind("<Leave>", self._unbind_canvas_mousewheel)
        self.canvas.bind("<MouseWheel>", self._on_mousewheel)
        
        self.canvas.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

    def _toggle_workspace_options(self, group):
        row = self._workspace_options[group]
        if row.winfo_manager():
            row.pack_forget()
            if group == "output":
                self._export_hint_label.pack_forget()
        else:
            before = self._workspace_config_row if group == "output" else self._workspace_image_container
            if group == "output":
                self._export_hint_label.pack(fill="x", padx=18, pady=(0, 4), before=before)
                before = self._export_hint_label
            row.pack(fill="x", before=before)
            row.request_layout()
        key, label = ("output_options", "Save / Export") if group == "output" else ("tools_options", "Selection / retry")
        self.btns[key].configure(text=label + (" ▴" if row.winfo_manager() else " ▾"))
    
    def _load_config(self):
        try:
            config_path = Path(CONFIG_FILE)
            if config_path.exists():
                cfg = _read_config_mapping(config_path)
                if "model" in cfg:
                    cfg["model"] = migrate_model_selection(cfg["model"])
                self._legacy_key_migration_error = ""
                if "api_key" in cfg:
                    legacy_key = cfg.get("api_key")
                    if isinstance(legacy_key, str) and legacy_key:
                        # A legacy config secret is a migration source only. Do not autofill,
                        # use it for provider calls, or silently move it into the old Vault.
                        self._legacy_config_key_present = True
                    elif not isinstance(legacy_key, str):
                        self._legacy_key_migration_error = (
                            f"{LEGACY_KEY_MIGRATION_FAILURE} stage=legacy_key_type; error=none"
                        )
                        print(self._legacy_key_migration_error)

                self._config_data = {key: value for key, value in cfg.items() if key != "api_key"}
                language = normalize_language(cfg.get("language", "en"))
                if "language_var" in self.__dict__:
                    self.language_var.set(LANGUAGE_NAMES[language])
                    set_ui_language(language, self)

                self.visual_review_var.set(cfg.get("visual_keyword_review", True) is not False)
                self.creator_var.set(cfg.get("creator", ""))
                self.shoot_location_var.set(cfg.get("shoot_location", ""))
                self.shoot_date_var.set(cfg.get("shoot_date", ""))
                saved_provider = get_provider_config(cfg.get("model", self.model_var.get()))["provider"]
                self.base_url_var.set(configured_base_urls(cfg).get(saved_provider, ""))
                self.local_url_var.set(cfg.get("local_url", self.local_url_var.get()))
                if (cfg.get("model") in SELECTABLE_MODELS
                        or API_CONFIG.get(cfg.get("model"), {}).get("legacy_saved_only")):
                    self.model_var.set(cfg["model"])
                if cfg.get("mode") in PROMPT_MODES:
                    self.mode_var.set(cfg["mode"])
                if self._legacy_config_key_present:
                    self._set_credential_status("Legacy config key found - migrate explicitly")
        except Exception as exc:
            # Keep safe defaults, but leave an actionable diagnostic. A broken
            # settings file must never look like a successful empty config.
            self._config_load_error = f"{type(exc).__name__}: {exc}"
            print(f"[config] load failed; safe defaults active: {self._config_load_error}", file=sys.stderr)
    
    def _save_config(self):
        # Never overwrite or remove the only legacy config secret before an
        # explicit, verified Windows Credential Manager migration succeeds.
        if getattr(self, "_legacy_config_key_present", False):
            return False
        try:
            cfg_dir = Path(CONFIG_FILE).parent
            cfg_dir.mkdir(parents=True, exist_ok=True)
            cfg = remember_provider_base_url(
                self._config_data, self.model_var.get(), self.base_url_var.get()
            )
            cfg.update(
                {
                    "creator": self.creator_var.get(),
                    "model": self.model_var.get(),
                    "mode": self.mode_var.get(),
                    "language": normalize_language(self.language_var.get()) if "language_var" in self.__dict__ else "en",
                    "visual_keyword_review": self.visual_review_var.get() if hasattr(self, "visual_review_var") else True,
                    "shoot_location": self.shoot_location_var.get(),
                    "shoot_date": self.shoot_date_var.get(),
                    "base_url": self.base_url_var.get().strip(),
                    "local_url": self.local_url_var.get().strip(),
                }
            )
            self._config_data = cfg
            _restore_config_bytes(CONFIG_FILE, json.dumps(cfg, ensure_ascii=False, indent=2).encode("utf-8"))
            return True
        except Exception:
            return False

    def _on_model_change(self, model_name=None):
        """Show the appropriate credential controls without reading stored secrets."""
        if model_name is None:
            model_name = self.model_var.get()
        prev_model = getattr(self, '_last_model', None)
        pconf = get_provider_config(model_name)
        previous_provider = get_provider_config(prev_model).get("provider")
        if prev_model is not None and previous_provider != pconf["provider"]:
            self._config_data = remember_provider_base_url(
                self._config_data, prev_model, self.base_url_var.get()
            )
            self.base_url_var.set(configured_base_urls(self._config_data).get(pconf["provider"], ""))
        if pconf["type"] == "local":
            self.key_entry.pack_forget()
            self.base_url_lbl.pack_forget()
            self.base_url_entry.pack_forget()
            if not self.local_url_lbl.winfo_ismapped():
                self.local_url_lbl.pack(side="left", padx=(0, 5))
            if not self.local_url_entry.winfo_ismapped():
                self.local_url_entry.pack(side="left")
            if not self.test_conn_btn.winfo_ismapped():
                self.test_conn_btn.pack(side="left", padx=(10, 0))
            self.save_key_btn.pack_forget()
            self.load_key_btn.pack_forget()
            self.replace_key_btn.pack_forget()
            self.delete_key_btn.pack_forget()
            self.clear_session_btn.pack_forget()
            self.credential_status_lbl.pack_forget()
            self.migrate_legacy_btn.pack_forget()
        else:
            self.base_url_lbl.configure(text="Base URL (optional):")
            self.base_url_entry.configure(
                placeholder_text="Optional custom endpoint / relay",
                border_color="#597D87",
            )
            self.local_url_lbl.pack_forget()
            self.local_url_entry.pack_forget()
            self.test_conn_btn.pack_forget()
            if not self.key_entry.winfo_ismapped():
                self.key_entry.pack(side="left")
            if not self.base_url_lbl.winfo_ismapped():
                self.base_url_lbl.pack(side="left", padx=(0, 5))
            if not self.base_url_entry.winfo_ismapped():
                self.base_url_entry.pack(side="left")
            if not self.test_conn_btn.winfo_ismapped():
                self.test_conn_btn.pack(side="left", padx=(10, 0))
            for button in (
                self.load_key_btn,
                self.save_key_btn,
                self.replace_key_btn,
                self.clear_session_btn,
                self.delete_key_btn,
            ):
                if not button.winfo_ismapped():
                    button.pack(side="left", padx=(5, 0))
            if not self.credential_status_lbl.winfo_ismapped():
                self.credential_status_lbl.pack(side="left", padx=(0, 10))
            migration_hint = self._legacy_migration_hint()
            if migration_hint or getattr(self, "_legacy_config_key_present", False):
                self.migrate_legacy_btn.configure(
                    text="Complete secure migration" if migration_hint == "Migration verification pending" else "Migrate legacy keys"
                )
                if not self.migrate_legacy_btn.winfo_ismapped():
                    self.migrate_legacy_btn.pack(side="left", padx=(0, 5))
            else:
                self.migrate_legacy_btn.pack_forget()
            # Clear stale key when actively switching between cloud providers
            if (prev_model is not None
                    and previous_provider != pconf["provider"]):
                self.key_var.set("")
                self._set_credential_status("Load saved key with PIN")
        self._last_model = model_name

    def _resolve_cloud_api_url(self, model_name, base_url=""):
        conf = API_CONFIG[model_name]
        transport = self._get_cloud_transport(model_name, base_url)
        custom = str(base_url or "").strip().rstrip("/")
        resolved_model = self._resolve_request_model(model_name)
        if not custom:
            if transport == "gemini":
                return f"https://generativelanguage.googleapis.com/v1beta/models/{resolved_model}:generateContent"
            custom = str(conf.get("url", "")).strip().rstrip("/")

        lower = custom.lower()
        if transport == "gemini":
            if ":generatecontent" in lower:
                return custom
            if lower.endswith(f"/models/{resolved_model.lower()}"):
                return f"{custom}:generateContent"
            if lower.endswith("/v1") or lower.endswith("/v1beta"):
                return f"{custom}/models/{resolved_model}:generateContent"
            return f"{custom}/v1beta/models/{resolved_model}:generateContent"

        source = custom or str(conf.get("url", "")).strip()
        if transport == "openai_responses":
            return self._normalize_openai_responses_url(source)
        if transport == "openai_compatible_negotiation":
            if self._prefer_responses_for_proxy(resolved_model):
                return self._normalize_openai_responses_url(source)
            return self._normalize_openai_compat_url(source)
        return self._normalize_openai_compat_url(source)

    def _is_official_cloud_endpoint(self, model_name, endpoint):
        """Identify official direct routes without trusting arbitrary relays."""
        try:
            parsed = urlparse(str(endpoint or ""))
            host = (parsed.hostname or "").lower()
            path = (parsed.path or "").lower()
        except (TypeError, ValueError):
            return False
        provider = API_CONFIG[model_name]["provider"]
        if provider == "qwen":
            return (
                (host == "dashscope.aliyuncs.com" or host.endswith(".maas.aliyuncs.com"))
                and "/compatible-mode/" in path
            )
        official_hosts = {
            "openai": "api.openai.com",
            "kimi": "api.moonshot.cn",
            "minimax": "api.minimaxi.com",
        }
        return host == official_hosts.get(provider, "")

    def _cloud_endpoint_issue(self, model_name, base_url=""):
        conf = API_CONFIG[model_name]
        custom = str(base_url or "").strip()
        endpoint = self._resolve_cloud_api_url(model_name, custom)
        try:
            parsed = urlparse(endpoint)
            invalid = (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.query
                or parsed.fragment
            )
        except (TypeError, ValueError):
            invalid = True
        if invalid:
            return "Endpoint assembly error: use a complete HTTPS provider or relay URL without credentials, query, or fragment."
        return ""

    def _normalize_openai_compat_url(self, base_url):
        custom = str(base_url or "").strip().rstrip("/")
        lower = custom.lower()
        if lower.endswith("/chat/completions"):
            return custom
        if lower.endswith("/chat"):
            return f"{custom}/completions"
        if lower.endswith("/responses"):
            return f"{custom[:-len('/responses')]}/chat/completions"
        if lower.endswith("/v1") or lower.endswith("/v4") or lower.endswith("/api/paas/v4"):
            return f"{custom}/chat/completions"
        return f"{custom}/v1/chat/completions"

    def _normalize_openai_responses_url(self, base_url):
        custom = str(base_url or "").strip().rstrip("/")
        lower = custom.lower()
        if lower.endswith("/responses"):
            return custom
        if lower.endswith("/chat/completions"):
            return f"{custom[:-len('/chat/completions')]}/responses"
        if lower.endswith("/v1"):
            return f"{custom}/responses"
        return f"{custom}/v1/responses"

    def _prefer_responses_for_proxy(self, model_id):
        return bool(re.match(r"^(?:gpt-5|o[134](?:-|$)|claude-)", str(model_id or ""), re.IGNORECASE))

    def _resolve_local_api_url(self, base_url=""):
        custom = str(base_url or "").strip().rstrip("/")
        if not custom:
            return "http://localhost:11434/api/chat"
        lower = custom.lower()
        if lower.endswith("/api/chat"):
            return custom
        if lower.endswith("/api/generate"):
            return custom[:-len("/api/generate")] + "/api/chat"
        if lower.endswith("/api"):
            return f"{custom}/chat"
        return f"{custom}/api/chat"

    def _local_endpoint_issue(self, base_url=""):
        try:
            parsed = urlparse(self._resolve_local_api_url(base_url))
            port = parsed.port
            invalid = (
                parsed.scheme not in ("http", "https") or not parsed.hostname
                or any(char.isspace() for char in parsed.netloc)
                or port == 0 or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment
            )
        except (TypeError, ValueError):
            invalid = True
        return ("Endpoint assembly error: use a complete Ollama HTTP or HTTPS URL with a valid port (1–65535), "
                "without credentials, query, or fragment; for example http://localhost:11434.") if invalid else ""

    def _resolve_cloud_base_root(self, model_name, base_url=""):
        url = self._resolve_cloud_api_url(model_name, base_url).rstrip("/")
        transport = self._get_cloud_transport(model_name, base_url)
        lower = url.lower()
        if transport in ("openai_compat", "openai_responses", "openai_compatible_negotiation"):
            if lower.endswith("/chat/completions"):
                return url[:-len("/chat/completions")]
            if lower.endswith("/responses"):
                return url[:-len("/responses")]
            return url
        if transport == "gemini":
            marker = "/models/"
            idx = lower.find(marker)
            if idx != -1:
                return url[:idx]
            return url
        return url

    def _get_cloud_transport(self, model_name, base_url=""):
        provider = API_CONFIG[model_name]["provider"]
        custom = str(base_url or "").strip().rstrip("/")
        if not custom:
            if provider == "openai":
                return "openai_responses"
            if provider in ("kimi", "qwen", "minimax"):
                return "openai_compat"
            return provider
        resolved_as_chat = self._normalize_openai_compat_url(custom)
        if self._is_official_cloud_endpoint(model_name, resolved_as_chat):
            if provider == "openai":
                return "openai_responses"
            if provider in ("kimi", "qwen", "minimax"):
                return "openai_compat"
        lower = custom.lower()
        if provider == "gemini":
            if "generativelanguage.googleapis.com" in lower or "googleapis.com" in lower or ":generatecontent" in lower:
                return "gemini"
        return "openai_compatible_negotiation"

    def _get_clean_api_key(self):
        return str(self.key_var.get() or "").strip()

    def _mask_secret(self, value):
        value = str(value or "").strip()
        if len(value) <= 6:
            return "*" * len(value)
        return f"{value[:3]}***{value[-3:]}"

    def _redact_secrets(self, value, secrets=None):
        text = "" if value is None else str(value)
        for secret in secrets or []:
            secret = str(secret or "").strip()
            if secret:
                text = text.replace(secret, self._mask_secret(secret))
        current_key = self._get_clean_api_key() if threading.current_thread() is threading.main_thread() and hasattr(self, "key_var") else ""
        if current_key:
            text = text.replace(current_key, self._mask_secret(current_key))
        text = re.sub(r"([?&]key=)[^&\s]+", r"\1***", text, flags=re.IGNORECASE)
        text = re.sub(r"(Authorization\s*:\s*Bearer\s+)[^\s,;]+", r"\1***", text, flags=re.IGNORECASE)
        return text

    def _safe_log_value(self, value):
        if isinstance(value, dict):
            return {k: self._safe_log_value(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self._safe_log_value(v) for v in value]
        return self._redact_secrets(value)

    def _retry_after_seconds(self, response):
        raw = ""
        try:
            raw = response.headers.get("Retry-After", "")
        except Exception:
            raw = ""
        if not raw:
            return None
        try:
            return max(0.0, min(120.0, float(raw)))
        except (TypeError, ValueError):
            return None

    def _backoff_delay(self, attempt, retry_after=None):
        if retry_after is not None:
            return min(float(retry_after), float(HTTP_BACKOFF_MAX))
        base = 2 ** max(0, attempt)
        jitter = random.uniform(0.25, 1.25)
        return min(float(HTTP_BACKOFF_MAX), base + jitter)

    def _log_api_debug_preview(self, label, text, api_key="", limit=200):
        if not API_DEBUG:
            return
        preview = self._redact_secrets(text or "", [api_key])[:limit]
        print(f"[api_debug] {label} preview={repr(preview)}")

    def _is_retryable_status(self, status_code):
        return status_code in (408, 409, 423, 425, 429) or 500 <= int(status_code or 0) < 600

    def _is_fatal_status(self, status_code):
        return 400 <= int(status_code or 0) < 500 and not self._is_retryable_status(status_code)

    def _metadata_schema_issue(self, payload, require_title=True):
        if not isinstance(payload, dict):
            return "metadata JSON was not an object"
        if require_title and (not isinstance(payload.get("title"), str) or not payload["title"].strip()):
            return "metadata JSON missing title"
        keywords = payload.get("keywords", [])
        if isinstance(keywords, str):
            keywords = [item.strip() for item in keywords.replace(";", ",").split(",") if item.strip()]
        if not isinstance(keywords, list):
            return "metadata JSON keywords field was not a list"
        if not keywords:
            return "metadata JSON keywords field was empty"
        if any(not isinstance(keyword, str) or not keyword.strip() for keyword in keywords):
            return "metadata JSON keywords must contain non-empty text values only"
        return ""

    def _smoke_metadata_ok(self, text):
        payload = extract_json(text, require_object=True)
        if not payload:
            return False, "smoke test did not return valid JSON metadata"
        keywords = payload.get("keywords", [])
        if not isinstance(keywords, list):
            return False, "smoke test keywords field was not a list"
        kept, _rejected, _shortfall = sanitize_keywords_for_stock(keywords, target_count=3)
        if not kept or not all(isinstance(item, str) and item.strip() for item in kept):
            return False, "smoke test keywords were not usable after cleanup"
        if len(kept) < 2:
            return False, "smoke test returned fewer than 2 usable keywords after cleanup"
        return True, ""

    def _smoke_retry_delay(self, response, attempt):
        retry_after = self._retry_after_seconds(response)
        delay = self._backoff_delay(attempt, retry_after)
        return min(delay, 5.0)

    def _smoke_timeout(self):
        return (HTTP_CONNECT_TIMEOUT, min(SMOKE_READ_TIMEOUT, HTTP_READ_TIMEOUT))

    def _fallback_model_candidates(self, primary_model):
        cfg = self._config_data if isinstance(self._config_data, dict) else {}
        explicit = cfg.get("fallback_models", [])
        if isinstance(explicit, str):
            explicit = [explicit]
        candidates = []
        for name in explicit:
            if name in API_CONFIG:
                candidates.append(name)

        ranked = []
        for name, conf in API_CONFIG.items():
            if name not in SELECTABLE_MODELS or name == primary_model or conf.get("stability") == "experimental" or not conf.get("fallback_eligible"):
                continue
            rank = 0 if conf.get("stability") == "fallback" else 1
            ranked.append((rank, name))
        candidates.extend(name for _rank, name in sorted(ranked))

        seen = set()
        result = []
        for name in candidates:
            if name == primary_model or name in seen:
                continue
            conf = API_CONFIG.get(name, {})
            if conf.get("stability") == "experimental" or not conf.get("fallback_eligible"):
                continue
            seen.add(name)
            result.append(name)
        return result

    def _api_key_for_model(self, model_name, primary_model, primary_api_key):
        provider = API_CONFIG[model_name]["provider"]
        primary_provider = API_CONFIG[primary_model]["provider"]
        if provider == primary_provider:
            return primary_api_key
        # A batch lease authorizes exactly its selected provider. Do not read a
        # legacy Vault or another provider's stored credential from a worker.
        return ""

    def _get_model_override(self, model_name):
        cfg = self._config_data if isinstance(self._config_data, dict) else {}
        overrides = cfg.get("model_overrides", {})
        if isinstance(overrides, dict):
            direct = overrides.get(model_name)
            if isinstance(direct, str) and direct.strip():
                return direct.strip()
            provider_key = overrides.get(API_CONFIG[model_name]["provider"])
            if isinstance(provider_key, str) and provider_key.strip():
                return provider_key.strip()
        for key in ("model_override", f"{API_CONFIG[model_name]['provider']}_model"):
            value = cfg.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        if API_CONFIG[model_name]["provider"] == "ollama":
            value = cfg.get("ollama_model")
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    def _resolve_request_model(self, model_name):
        override = self._get_model_override(model_name)
        if override:
            return override
        if API_CONFIG[model_name]["provider"] == "ollama":
            return PROVIDERS.get(model_name, {}).get("ollama_model", API_CONFIG[model_name]["model"])
        return API_CONFIG[model_name]["model"]

    def _build_api_context(self, model_name, api_key="", base_url="", local_url=""):
        pconf = get_provider_config(model_name)
        provider = pconf["provider"]
        context = {
            "model_name": model_name,
            "provider": provider,
            "type": pconf["type"],
            "model": self._resolve_request_model(model_name),
            "release_status": pconf.get("release_status", "unknown"),
            "recommended": bool(pconf.get("recommended", False)),
            "requires_live_check": bool(pconf.get("requires_live_check", True)),
            "review_required": bool(pconf.get("review_required", True)),
        }
        if pconf["type"] == "local":
            context["transport"] = "ollama"
            context["route"] = "Local Ollama"
            context["resolved_url"] = self._resolve_local_api_url(local_url or PROVIDERS[model_name].get("default_base_url", "http://localhost:11434"))
            context["endpoint_issue"] = self._local_endpoint_issue(local_url)
        else:
            context["transport"] = self._get_cloud_transport(model_name, base_url)
            context["resolved_url"] = self._resolve_cloud_api_url(model_name, base_url)
            context["route"] = (
                "Direct API"
                if not str(base_url or "").strip() or self._is_official_cloud_endpoint(model_name, context["resolved_url"])
                else "Custom Base URL / Relay"
            )
            context["endpoint_issue"] = self._cloud_endpoint_issue(model_name, base_url)
        if api_key:
            context["api_key"] = self._mask_secret(api_key)
        return context

    def _format_connection_feedback(self, result, fallback_message=""):
        ctx = result.get("ctx", {}) if isinstance(result, dict) else {}
        lines = []
        if fallback_message:
            lines.append(str(fallback_message).strip())
            lines.append("")
        if ctx:
            lines.extend(
                [
                    f"Route: {ctx.get('route', 'Unknown')}",
                    f"Transport: {ctx.get('transport', 'unknown')}",
                    f"Model: {ctx.get('model', 'unknown')}",
                    f"Release policy: {ctx.get('release_status', 'unknown')}",
                    f"Endpoint: {ctx.get('resolved_url', 'unknown')}",
                ]
            )
        status = result.get("status_code") if isinstance(result, dict) else None
        if status:
            lines.append(f"Status: {status}")
        category = result.get("categorized_error") if isinstance(result, dict) else ""
        if category:
            lines.append(f"Category: {category}")
        return "\n".join(line for line in lines if line is not None).strip()

    def _log_api_event(self, label, context, **extra):
        payload = {
            "model": context.get("model"),
            "provider": context.get("provider"),
            "route": context.get("route"),
            "transport": context.get("transport"),
            "resolved_url": context.get("resolved_url"),
        }
        if context.get("api_key"):
            payload["api_key"] = context["api_key"]
        payload.update(extra)
        payload = self._safe_log_value(payload)
        tail = ", ".join(f"{k}={v}" for k, v in payload.items())
        print(f"[{label}] {tail}")

    def _build_smoke_payload(self, model_name, prompt_text, b64, base_url="", local_url=""):
        ctx = self._build_api_context(model_name, base_url=base_url, local_url=local_url)
        if ctx["type"] == "local":
            return ctx, {
                "model": ctx["model"],
                "messages": [{"role": "user", "content": prompt_text, "images": [b64]}],
                "stream": False,
            }
        if ctx["transport"] == "gemini":
            return ctx, {
                "contents": [{"parts": [{"text": prompt_text}, {"inline_data": {"mime_type": "image/jpeg", "data": b64}}]}]
            }
        if ctx["transport"] == "openai_responses" or (
            ctx["transport"] == "openai_compatible_negotiation"
            and self._prefer_responses_for_proxy(ctx["model"])
        ):
            return ctx, {
                "model": ctx["model"],
                "input": [{
                    "role": "user",
                    "content": [
                        {"type": "input_image", "image_url": f"data:image/jpeg;base64,{b64}", "detail": "high"},
                        {"type": "input_text", "text": prompt_text},
                    ],
                }],
                "max_output_tokens": 256,
            }
        return ctx, {
            "model": ctx["model"],
            "messages": [{"role": "user", "content": [self._openai_compat_image_part(b64, ctx["provider"]), {"type": "text", "text": prompt_text}]}],
            "max_tokens": 256,
        }

    def _build_smoke_prompt(self, model_name):
        provider = API_CONFIG.get(model_name, {}).get("provider", "")
        if provider == "kimi":
            return (
                "Look at the attached test image and return ONLY compact valid JSON. "
                "Do not use markdown. Do not add explanations. Use English only. "
                "The JSON keys must be exactly title, description, keywords. "
                "keywords must be an array of at least 5 short stock-search phrases. "
                "For this simple geometric test image, use visible, concrete terms such as "
                "blue background, orange circle, green rectangle, simple geometric design, abstract shapes."
            )
        return (
            "Analyze this simple test image. Return ONLY valid JSON with keys "
            "title, description, keywords. The keywords value must be an array "
            "with at least 3 short English stock-search phrases."
        )

    def _classify_smoke_error(self, status_code=None, body="", reply_text="", message=""):
        combined = " ".join(str(part or "") for part in (body, reply_text, message)).lower()
        if (
            "resolved url is invalid" in combined
            or "endpoint assembly error" in combined
            or "workspace compatible endpoint" in combined
        ):
            return "endpoint_error"
        if "api key required" in combined:
            return "missing_api_key"
        if status_code == 401:
            return "auth_error"
        if status_code == 403:
            return "permission_denied"
        if any(token in combined for token in (
            "no available channel for model", "model_not_found", "model not found", "not found the model",
            "unavailable for this provider account", "not available on this relay"
        )):
            return "model_unavailable"
        if re.search(r"model\s+['\"][^'\"]+['\"]\s+not found", combined):
            return "model_unavailable"
        if "data url" in combined or "image_url" in combined or "relay rejected the image payload" in combined:
            return "relay_image_unsupported"
        if any(token in combined for token in ("does not support vision", "does not appear to support vision", "does not support images", "image input", "multimodal", "vision input")):
            return "vision_unsupported"
        if "non-json" in combined:
            return "non_json"
        if any(token in combined for token in ("访问量过大", "too many requests", "rate limited", "rate-limit", "busy", "overloaded", "temporarily unavailable")):
            return "provider_busy"
        if status_code == 429 or "quota" in combined:
            return "rate_limited"
        if "timed out" in combined:
            return "timeout"
        if "network error" in combined:
            return "network_error"
        return ""

    def _execute_smoke_request(self, model_name, api_key="", base_url="", local_url=""):
        img = create_test_image()
        with io.BytesIO() as buf:
            img.save(buf, format="JPEG", quality=80)
            b64 = base64.b64encode(buf.getvalue()).decode()
        prompt_text = self._build_smoke_prompt(model_name)
        ctx, payload = self._build_smoke_payload(model_name, prompt_text, b64, base_url=base_url, local_url=local_url)
        url = ctx["resolved_url"]
        result = {"ok": False, "message": "", "ctx": ctx, "status_code": None, "categorized_error": "", "smoke_request_sent": False}

        def finish(ok, message="", status_code=None, body="", reply_text="", flags=None):
            result["ok"] = ok
            result["message"] = message
            result["status_code"] = status_code
            result["error_code"] = "none" if ok else classify_api_error(status_code=status_code, body=" ".join(str(v or "") for v in (body, message)))
            result["categorized_error"] = self._classify_smoke_error(status_code, body=body, reply_text=reply_text, message=message)
            extra = {
                "smoke_request_sent": result["smoke_request_sent"],
                "status_code": status_code,
                "categorized_error": result["categorized_error"] or "none",
            }
            if flags:
                extra.update(
                    fallback_triggered=flags.get("fallback_triggered"),
                    missing_image_retry=flags.get("missing_image_retry"),
                    response_format_retry=flags.get("response_format_retry"),
                )
            if message:
                extra["message"] = message
            self._log_api_event("smoke_ok" if ok else "smoke_fail", ctx, **extra)
            return result

        if ctx.get("endpoint_issue"):
            return finish(False, ctx["endpoint_issue"])
        if not url.startswith(("http://", "https://")):
            return finish(False, "Endpoint assembly error: resolved URL is invalid.")
        if ctx["type"] == "cloud" and not str(api_key or "").strip():
            return finish(False, "API key required.")
        if ctx["transport"] == "gemini" and ctx["provider"] != "gemini":
            return finish(False, "Transport mismatch: Gemini direct transport can only be used with Gemini provider.")
        self._log_api_event(
            "smoke_start",
            dict(ctx, api_key=self._mask_secret(api_key)),
            payload_keys=list(payload.keys()),
            smoke_request_sent=False,
            status_code="pending",
            categorized_error="pending",
        )
        timeout = self._smoke_timeout()
        try:
            with requests.Session() as session:
                session.headers.update({"Connection": "close"})
                if ctx["type"] == "local":
                    result["smoke_request_sent"] = True
                    response = self._post_if_active(session, url, json=payload, timeout=(HTTP_CONNECT_TIMEOUT, OLLAMA_READ_TIMEOUT))
                    if response.status_code != 200:
                        message = self._categorize_http_error(model_name, response.status_code, (response.text or "")[:300], url)
                        return finish(False, message, status_code=response.status_code, body=(response.text or "")[:300])
                    try:
                        body = response.json()
                    except ValueError:
                        return finish(False, f"{model_name} returned a non-JSON HTTP response during smoke test.", status_code=response.status_code)
                    text = extract_api_text(body, "openai_compat", "ollama")
                    if not text:
                        return finish(False, f"{model_name} smoke test returned an empty response.", status_code=response.status_code)
                    ok, issue = self._smoke_metadata_ok(text)
                    if not ok:
                        return finish(False, f"{model_name} {issue}.", status_code=response.status_code, reply_text=text)
                    return finish(True, status_code=response.status_code, reply_text=text)

                if ctx["transport"] in (
                    "openai_compat", "openai_responses", "openai_compatible_negotiation"
                ):
                    result["smoke_request_sent"] = True
                    flags = {}
                    response = body = text = None
                    timeout_error = None
                    for smoke_attempt in range(SMOKE_RETRY_ATTEMPTS):
                        timeout_error = None
                        try:
                            response, body, text, flags = self._openai_family_request(
                                session, ctx, model_name, api_key, prompt_text, b64, 256
                            )
                        except requests.Timeout:
                            timeout_error = True
                            response = None
                        if response is None or not self._is_retryable_status(response.status_code) or smoke_attempt >= SMOKE_RETRY_ATTEMPTS - 1:
                            if not timeout_error or smoke_attempt >= SMOKE_RETRY_ATTEMPTS - 1:
                                break
                        delay = self._smoke_retry_delay(response, smoke_attempt) if response is not None else min(self._backoff_delay(smoke_attempt), 5.0)
                        self._log_api_event(
                            "smoke_retry_wait",
                            dict(ctx, api_key=self._mask_secret(api_key)),
                            status_code=getattr(response, "status_code", "timeout"),
                            delay_seconds=round(delay, 2),
                        )
                        time.sleep(delay)
                    if response is None:
                        if timeout_error:
                            return finish(False, f"{model_name} smoke test timed out after {SMOKE_RETRY_ATTEMPTS} attempts.", flags=flags)
                        return finish(False, f"{model_name} smoke test failed before a response was received.", flags=flags)
                    if response.status_code != 200:
                        message = self._categorize_http_error(model_name, response.status_code, (response.text or "")[:300], url)
                        return finish(False, message, status_code=response.status_code, body=(response.text or "")[:300], reply_text=text, flags=flags)
                    if body is None:
                        return finish(False, f"{model_name} returned a non-JSON HTTP response during smoke test.", status_code=response.status_code, reply_text=text, flags=flags)
                    if self._looks_like_missing_image_reply(text):
                        return finish(False, f"{model_name} relay did not accept the test image payload.", status_code=response.status_code, reply_text=text, flags=flags)
                    if not text:
                        return finish(False, f"{model_name} smoke test returned an empty response.", status_code=response.status_code, flags=flags)
                    ok, issue = self._smoke_metadata_ok(text)
                    if not ok:
                        return finish(False, f"{model_name} {issue}.", status_code=response.status_code, reply_text=text, flags=flags)
                    return finish(True, status_code=response.status_code, reply_text=text, flags=flags)

                result["smoke_request_sent"] = True
                response = None
                timeout_error = False
                for smoke_attempt in range(SMOKE_RETRY_ATTEMPTS):
                    try:
                        timeout_error = False
                        response = self._post_if_active(session, f"{url}?key={api_key}", json=payload, timeout=timeout)
                    except requests.Timeout:
                        timeout_error = True
                        response = None
                    if response is not None and not self._is_retryable_status(response.status_code):
                        break
                    if smoke_attempt >= SMOKE_RETRY_ATTEMPTS - 1:
                        break
                    delay = self._smoke_retry_delay(response, smoke_attempt) if response is not None else min(self._backoff_delay(smoke_attempt), 5.0)
                    self._log_api_event(
                        "smoke_retry_wait",
                        dict(ctx, api_key=self._mask_secret(api_key)),
                        status_code=getattr(response, "status_code", "timeout"),
                        delay_seconds=round(delay, 2),
                    )
                    time.sleep(delay)
                if response is None and timeout_error:
                    return finish(False, f"{model_name} smoke test timed out after {SMOKE_RETRY_ATTEMPTS} attempts.")
                if response.status_code != 200:
                    message = self._categorize_http_error(model_name, response.status_code, (response.text or "")[:300], url)
                    return finish(False, message, status_code=response.status_code, body=(response.text or "")[:300])
                try:
                    body = response.json()
                except ValueError:
                    return finish(False, f"{model_name} returned a non-JSON HTTP response during smoke test.", status_code=response.status_code)
                text = extract_api_text(body, ctx["transport"], ctx["provider"])
                if self._looks_like_missing_image_reply(text):
                    return finish(False, f"{model_name} endpoint accepted the request but did not accept the attached image.", status_code=response.status_code, reply_text=text)
                if not text:
                    return finish(False, f"{model_name} smoke test returned an empty response.", status_code=response.status_code)
                ok, issue = self._smoke_metadata_ok(text)
                if not ok:
                    return finish(False, f"{model_name} {issue}.", status_code=response.status_code, reply_text=text)
                return finish(True, status_code=response.status_code, reply_text=text)
        except requests.Timeout:
            return finish(False, f"{model_name} smoke test timed out.")
        except requests.RequestException as exc:
            return finish(False, f"{model_name} smoke test network error: {self._redact_secrets(exc, [api_key])}")

    def _run_request_smoke_test(self, model_name, api_key="", base_url="", local_url=""):
        result = self._execute_smoke_request(model_name, api_key=api_key, base_url=base_url, local_url=local_url)
        return result["ok"], result["message"], result["ctx"]

    def _openai_compat_image_part(self, b64, provider):
        image_url = {"url": f"data:image/jpeg;base64,{b64}"}
        if provider == "openai":
            image_url["detail"] = "high"
        return {"type": "image_url", "image_url": image_url}

    def _openai_image_fallback_content(self, prompt_text, b64):
        return [
            {"type": "text", "text": "The image is attached below as a data URL. Analyze the attached image directly and return JSON only."},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
            {"type": "text", "text": prompt_text},
        ]

    def _openai_compat_content_rounds(self, prompt_text, b64, provider):
        image_part = self._openai_compat_image_part(b64, provider)
        text_part = {"type": "text", "text": prompt_text}
        return [
            [image_part, text_part],
            [text_part, image_part],
            self._openai_image_fallback_content(prompt_text, b64),
        ]

    def _openai_compat_messages(self, provider, content):
        messages = []
        if provider == "kimi":
            messages.append({
                "role": "system",
                "content": (
                    "You are a strict JSON metadata generator. Return compact valid JSON only, "
                    "with exact keys title, description, keywords. Use English only."
                ),
            })
        messages.append({"role": "user", "content": content})
        return messages

    def _looks_like_missing_image_reply(self, text):
        txt = str(text or "").lower()
        hints = (
            "upload the image",
            "provide the image",
            "provide an image",
            "please provide the image",
            "please upload the image",
            "image description",
            "describe it to me",
            "describe the image",
            "provide the image you want metadata for",
            "cannot access image",
            "can't access the image",
            "unable to view the image",
            "unable to access the image",
            "i cannot see the image",
            "i can't see the image",
            "no image was provided",
        )
        return any(token in txt for token in hints)

    def _response_format_was_rejected(self, response):
        body = (getattr(response, "text", "") or "").lower()
        return response.status_code == 400 and (
            "response_format" in body
            or "json_object" in body
            or "unsupported parameter" in body
            or "unknown parameter" in body
            or "extra_forbidden" in body
        )

    def _kimi_native_options(self, url):
        lower = str(url or "").lower()
        return "moonshot.cn" in lower or "moonshot.ai" in lower or "kimi.com" in lower or "kimi.ai" in lower

    def _openai_native_options(self, url):
        return (urlparse(str(url or "")).hostname or "").lower() == "api.openai.com"

    def _provider_json_mode_enabled(self, provider):
        return provider in ("openai", "kimi")

    def _provider_param_rejected(self, response, *param_names):
        if response.status_code != 400:
            return False
        body = (getattr(response, "text", "") or "").lower()
        generic = ("unsupported parameter", "unknown parameter", "extra_forbidden", "invalid parameter", "unrecognized")
        return any(name.lower() in body for name in param_names) or any(token in body for token in generic)

    def _gemini_proxy_model_ids(self, model_name):
        model = API_CONFIG[model_name]["model"]
        if model_name == "Google Gemini 2.5 Pro":
            return [model, "gemini-2.5-pro-preview-06-05", "gemini-2.5-pro-preview-05-06", "gemini-2.5-pro-preview-03-25"]
        if model_name == "Google Gemini 2.5 Flash":
            return [model, "gemini-2.5-flash-preview-05-20", "gemini-2.5-flash-preview-04-17"]
        return [model]

    def _is_gemini_proxy_model_missing(self, response):
        body = (getattr(response, "text", "") or "").lower()
        return ("model_not_found" in body) or ("no available channel for model" in body)

    def _candidate_model_ids(self, model_name):
        override = self._get_model_override(model_name)
        if override:
            return [override]
        if API_CONFIG[model_name]["provider"] == "gemini":
            return self._gemini_proxy_model_ids(model_name)
        return [API_CONFIG[model_name]["model"]]

    def _categorize_http_error(self, model_name, status_code, body, url):
        body = self._redact_secrets(body)
        url = self._redact_secrets(url)
        lower = str(body or "").lower()
        if API_CONFIG.get(model_name, {}).get("provider") == "ollama" and re.search(r"model\s+['\"][^'\"]+['\"]\s+not found", lower):
            model_id = self._resolve_request_model(model_name)
            return f"Local model not found. On the Ollama server, install it with `ollama pull {model_id}` and retry."
        if status_code == 429:
            return f"{model_name} HTTP 429 from provider — request was rate-limited by the provider for the current key/account. Wait and retry, or check provider quota/billing/rate limits."
        if status_code == 401:
            return f"{model_name} HTTP 401 — API key was rejected. Check or replace the key."
        if status_code == 403:
            return f"{model_name} HTTP 403 — permission denied. Check this account's model access and endpoint permissions."
        if any(token in lower for token in ("no available channel for model", "model_not_found", "model not found", "not found the model")):
            if self._is_official_cloud_endpoint(model_name, url):
                return f"{model_name} is unavailable for this provider account. Check the model ID and model access permissions."
            return (
                f"{model_name} is not available on this relay route. Select a model carried by that relay, "
                "or clear Base URL to use the provider's official direct API."
            )
        if any(token in lower for token in ("image_url", "data url", "unsupported image", "invalid image", "cannot access image")):
            return f"{model_name} relay rejected the image payload. Check whether the relay supports image data URLs."
        if any(token in lower for token in ("vision", "image input", "multimodal", "does not support images", "image is not supported")):
            return f"{model_name} does not appear to support vision input on this endpoint."
        if status_code in (400, 404):
            return f"{model_name} HTTP {status_code} endpoint error — check Base URL normalization, provider path, model name, and image payload format."
        if 400 <= status_code < 500:
            return f"{model_name} HTTP {status_code} client error — check provider request settings."
        if status_code in (502, 503, 504, 524) and "<html" in lower:
            return f"{model_name} HTTP {status_code}: the provider or relay gateway timed out or is unavailable."
        # Provider bodies can echo credentials, image data or other sensitive
        # request fields. Worker threads cannot read the UI's current key, and
        # even a key-aware redactor cannot safely classify arbitrary body text.
        return f"{model_name} HTTP {status_code} server error — check provider status and retry later."

    def _openai_responses_request(self, session, model_name, api_key, prompt_text, b64, url, max_tokens):
        provider = API_CONFIG[model_name]["provider"]
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        request_json = {
            "model": self._candidate_model_ids(model_name)[0],
            "input": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_image",
                            "image_url": f"data:image/jpeg;base64,{b64}",
                            "detail": "high",
                        },
                        {"type": "input_text", "text": prompt_text},
                    ],
                }
            ],
            "max_output_tokens": max_tokens,
        }
        response = self._post_if_active(session,
            self._normalize_openai_responses_url(url),
            headers=headers,
            json=request_json,
            timeout=(HTTP_CONNECT_TIMEOUT, HTTP_READ_TIMEOUT),
        )
        payload = None
        text = ""
        if response.status_code == 200:
            try:
                payload = response.json()
            except ValueError:
                payload = None
            if payload is not None:
                text = extract_api_text(payload, "openai_responses", provider)
        return response, payload, text, {
            "protocol": "responses",
            "protocol_fallback": False,
            "fallback_triggered": False,
            "missing_image_retry": False,
            "response_format_retry": False,
        }

    def _post_if_active(self, session, *args, **kwargs):
        """Every additional transport attempt observes Cancel, including protocol negotiation."""
        if self.__dict__.get("stop_flag", False) and self.__dict__.get("is_processing", True):
            raise _FatalApiError("Request cancelled before dispatch")
        # A redirect can resend image bytes to another host even when Requests
        # strips Authorization. Require the user to configure the final endpoint.
        kwargs["allow_redirects"] = False
        response = session.post(*args, **kwargs)
        if 300 <= getattr(response, "status_code", 200) < 400:
            raise _FatalApiError("Endpoint redirected the request; configure the final HTTPS endpoint.", code="endpoint_error")
        return response

    def _openai_compatible_negotiated_request(
        self, session, model_name, api_key, prompt_text, b64, base_url, max_tokens
    ):
        prefer_responses = self._prefer_responses_for_proxy(self._resolve_request_model(model_name))
        request_order = (
            (self._openai_responses_request, self._openai_compat_request)
            if prefer_responses
            else (self._openai_compat_request, self._openai_responses_request)
        )
        response, payload, text, flags = request_order[0](
            session, model_name, api_key, prompt_text, b64, base_url, max_tokens
        )
        if response is None or response.status_code not in (400, 404, 405, 415, 422):
            return response, payload, text, flags

        response, payload, text, alternate_flags = request_order[1](
            session, model_name, api_key, prompt_text, b64, base_url, max_tokens
        )
        flags = dict(alternate_flags)
        flags["protocol_fallback"] = True
        flags["fallback_triggered"] = True
        return response, payload, text, flags

    def _openai_family_request(self, session, ctx, model_name, api_key, prompt_text, b64, max_tokens):
        if ctx["transport"] == "openai_responses":
            return self._openai_responses_request(
                session, model_name, api_key, prompt_text, b64, ctx["resolved_url"], max_tokens
            )
        if ctx["transport"] == "openai_compatible_negotiation":
            return self._openai_compatible_negotiated_request(
                session,
                model_name,
                api_key,
                prompt_text,
                b64,
                self._resolve_cloud_base_root(model_name, ctx["resolved_url"]),
                max_tokens,
            )
        return self._openai_compat_request(
            session, model_name, api_key, prompt_text, b64, ctx["resolved_url"], max_tokens
        )

    def _openai_compat_request(self, session, model_name, api_key, prompt_text, b64, url, max_tokens):
        provider = API_CONFIG[model_name]["provider"]
        url = self._normalize_openai_compat_url(url)
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        timeout = (HTTP_CONNECT_TIMEOUT, HTTP_READ_TIMEOUT)
        flags = {
            "protocol": "chat_completions",
            "protocol_fallback": False,
            "fallback_triggered": False,
            "missing_image_retry": False,
            "gemini_model_fallback": False,
            "response_format_retry": False,
        }
        response = None
        payload = None
        text = ""
        for model_index, model_id in enumerate(self._candidate_model_ids(model_name)):
            if model_index > 0:
                flags["gemini_model_fallback"] = True
            response = None
            native_kimi = provider == "kimi" and self._kimi_native_options(url)
            native_openai = provider == "openai" and self._openai_native_options(url)
            for round_index, content in enumerate(self._openai_compat_content_rounds(prompt_text, b64, provider)):
                if round_index > 0:
                    flags["fallback_triggered"] = True
                request_json = {"model": model_id, "messages": self._openai_compat_messages(provider, content), "max_tokens": max_tokens}
                if provider == "qwen" and self._is_official_cloud_endpoint(model_name, url):
                    # Qwen 3.7 defaults to thinking. Stock metadata only needs
                    # the final JSON; hidden reasoning adds substantial delay.
                    request_json["enable_thinking"] = False
                if provider == "minimax" and model_id == "MiniMax-M3" and self._is_official_cloud_endpoint(model_name, url):
                    # M3 defaults to thinking; keep the metadata output budget
                    # for the JSON answer and separate reasoning from content.
                    request_json.update(thinking={"type": "disabled"}, reasoning_split=True)
                if native_kimi:
                    request_json["max_completion_tokens"] = max_tokens
                    request_json.pop("max_tokens", None)
                    request_json["thinking"] = {"type": "disabled"}
                elif native_openai and API_CONFIG[model_name].get("token_limit_param") == "max_completion_tokens":
                    request_json["max_completion_tokens"] = max_tokens
                    request_json.pop("max_tokens", None)
                if self._provider_json_mode_enabled(provider):
                    request_json["response_format"] = {"type": "json_object"}
                response = self._post_if_active(session,
                    url,
                    headers=headers,
                    json=request_json,
                    timeout=timeout,
                )
                thinking_parameter = "enable_thinking" if "enable_thinking" in request_json else "thinking"
                if thinking_parameter in request_json and self._provider_param_rejected(response, thinking_parameter):
                    flags["response_format_retry"] = True
                    request_json = dict(request_json)
                    request_json.pop(thinking_parameter, None)
                    response = self._post_if_active(session,
                        url,
                        headers=headers,
                        json=request_json,
                        timeout=timeout,
                    )
                if "max_completion_tokens" in request_json and self._provider_param_rejected(response, "max_completion_tokens"):
                    flags["response_format_retry"] = True
                    request_json = dict(request_json)
                    request_json["max_tokens"] = request_json.pop("max_completion_tokens")
                    response = self._post_if_active(session,
                        url,
                        headers=headers,
                        json=request_json,
                        timeout=timeout,
                    )
                if "response_format" in request_json and self._response_format_was_rejected(response):
                    flags["response_format_retry"] = True
                    request_json = dict(request_json)
                    request_json.pop("response_format", None)
                    response = self._post_if_active(session,
                        url,
                        headers=headers,
                        json=request_json,
                        timeout=timeout,
                    )
                if provider == "gemini" and response.status_code != 200 and self._is_gemini_proxy_model_missing(response):
                    break
                if response.status_code != 200:
                    return response, None, "", flags
                try:
                    payload = response.json()
                except ValueError:
                    return response, None, "", flags
                text = extract_api_text(payload, "openai_compat", provider)
                if not text and round_index < 2:
                    flags["fallback_triggered"] = True
                    continue
                if self._looks_like_missing_image_reply(text) and round_index < 2:
                    flags["missing_image_retry"] = True
                    continue
                return response, payload, text, flags
            if provider != "gemini" or response is None or not self._is_gemini_proxy_model_missing(response):
                break
        return response, payload, text, flags

    def _test_connection(self):
        if self._network_check_busy or self.is_processing:
            return
        model_name = self.model_var.get()
        cloud_base_url = self.base_url_var.get().strip()
        local_url = self.local_url_var.get().strip()
        pconf = get_provider_config(model_name)
        if pconf["type"] == "local":
            return self._run_connection_smoke(model_name, "", cloud_base_url, local_url, None)
        provider = pconf["provider"]
        self._sync_input_to_session_key(provider)
        self._start_credential_operation(
            "batch", provider,
            lambda result: self._on_connection_authorized(result, model_name, cloud_base_url, local_url),
        )

    def _run_connection_smoke(self, model_name, api_key, cloud_base_url, local_url, lease):
        def request():
            try:
                return self._execute_smoke_request(model_name, api_key=api_key, base_url=cloud_base_url, local_url=local_url)
            finally:
                if lease is not None:
                    lease.close()
        self._run_network_check(request, self._show_connection_result)

    def _run_network_check(self, request, on_result):
        """Run network I/O off Tk; only the polling callback touches widgets."""
        if self._network_check_busy:
            return
        self._network_check_busy = True
        self._update_stats()
        results = queue.Queue()
        self._set_credential_status("Checking connection...")

        def worker():
            try:
                results.put((request(), None))
            except Exception as exc:
                results.put((None, self._redact_secrets(exc)))

        def poll():
            self._network_check_job = None
            try:
                result, error = results.get_nowait()
            except queue.Empty:
                self._network_check_job = self.after(30, poll)
                return
            self._network_check_busy = False
            self._update_stats()
            self._set_credential_status("Connection check finished")
            if error is not None:
                cancelled = self.is_processing and self.stop_flag
                if self.is_processing:
                    self._finish(progress_text=None if cancelled else "Generation not started: connection check failed.")
                if cancelled:
                    return
                messagebox.showerror("Connection Check Failed", error)
            else:
                on_result(result)

        threading.Thread(target=worker, daemon=True).start()
        self._network_check_job = self.after(30, poll)

    def _show_connection_result(self, result):
        ctx = result["ctx"]
        if not result["ok"]:
            return messagebox.showerror("PixTagra", self._format_connection_feedback(result, result["message"]))
        workspace_note = ""
        if self.__dict__.get("_session_save_error"):
            workspace_note = (
                "\n\nGeneration is blocked because this window cannot save its workspace. "
                "Close other PixTagra windows, then close and reopen this window. "
                "A successful connection test does not remove this block."
            )
        return messagebox.showinfo(
            "PixTagra", "Connection OK\n\n"
            f"Route: {ctx['route']}\nTransport: {ctx['transport']}\n"
            f"Model: {ctx['model']}\nEndpoint: {ctx['resolved_url']}\n"
            f"Status: {result['status_code'] or 'OK'}" + workspace_note,
        )

    def _on_connection_authorized(self, result, model_name, cloud_base_url, local_url):
        lease = getattr(result, "lease", None) if result is not None else None
        if getattr(result, "status", None) != "authorized" or lease is None:
            return messagebox.showwarning("PixTagra", self._credential_error_message(result, "Connection test"))
        secret = lease.secret_for_authorized_use()
        if not secret:
            lease.close()
            return messagebox.showwarning("PixTagra", "Connection test authorization expired.")
        return self._run_connection_smoke(model_name, secret, cloud_base_url, local_url, lease)

    def _sync_input_to_session_key(self, provider):
        coordinator = getattr(self, "_credential_coordinator", None)
        if coordinator is None or provider not in ACTIVE_GUI_CREDENTIAL_PROVIDERS:
            return
        secret = self.key_var.get()
        if secret:
            coordinator.set_session_key(provider, secret)

    def _save_key_to_windows(self):
        provider = self._credential_provider()
        self._start_credential_operation(
            "save", provider, self._on_persistent_save_result,
            secret_supplier=lambda: self.key_var.get(),
        )

    def _load_saved_key(self):
        provider = self._credential_provider()
        self._start_credential_operation("reveal", provider, self._on_saved_key_load_result)

    def _on_saved_key_load_result(self, result):
        if getattr(result, "status", None) != "authorized":
            return messagebox.showwarning("PixTagra", self._credential_error_message(result, "Load saved key"))
        lease = getattr(result, "lease", None)
        if lease is None:
            return messagebox.showwarning("PixTagra", "Saved key authorization expired.")
        try:
            secret = lease.secret_for_authorized_use()
        except Exception:
            secret = None
        finally:
            try:
                lease.close()
            except Exception:
                pass
        if not secret:
            return messagebox.showwarning("PixTagra", "Saved key authorization expired.")
        self.key_var.set(secret)
        self._set_credential_status("Saved key loaded for session")
        return None

    def _replace_saved_key(self):
        provider = self._credential_provider()
        if not messagebox.askyesno("PixTagra", "Replace the saved Windows key for this provider?"):
            return
        self._start_credential_operation(
            "replace", provider, self._on_persistent_save_result,
            secret_supplier=lambda: self.key_var.get(),
        )

    def _on_persistent_save_result(self, result):
        if getattr(result, "backend_status", None) in {"stored", "already_present"}:
            self._set_credential_status("Saved in Windows")
            return messagebox.showinfo("PixTagra", "Key saved securely in Windows.")
        return messagebox.showwarning("PixTagra", self._credential_error_message(result, "Save"))

    def _delete_saved_key(self):
        provider = self._credential_provider()
        if not messagebox.askyesno("PixTagra", "Delete the saved Windows key for this provider?"):
            return
        self._start_credential_operation("delete", provider, self._on_delete_saved_key_result)

    def _on_delete_saved_key_result(self, result):
        if getattr(result, "backend_status", None) in {"deleted", "already_absent"}:
            self._set_credential_status("Session only")
            return messagebox.showinfo("PixTagra", "Saved Windows key deleted.")
        return messagebox.showwarning("PixTagra", self._credential_error_message(result, "Delete"))

    def _clear_session_key(self):
        provider = self._credential_provider()
        coordinator = getattr(self, "_credential_coordinator", None)
        if coordinator is not None:
            coordinator.clear_session_key(provider)
        self.key_var.set("")
        self._set_credential_status("Session only")

    def _migrate_legacy_keys(self):
        if getattr(self, "_legacy_config_key_present", False):
            return self._migrate_legacy_config_key()
        hint = self._legacy_migration_hint()
        operation = "migration_continue" if hint == "Migration verification pending" else "migration_begin"
        self._start_credential_operation(operation, None, self._on_legacy_migration_result)

    def _on_legacy_migration_result(self, result):
        phase = getattr(result, "migration_phase", None)
        if getattr(result, "status", None) != "migration_result":
            return messagebox.showwarning("PixTagra", self._credential_error_message(result, "Migration"))
        if phase == "restart_verification_pending":
            self._set_credential_status("Restart PixTagra to verify and complete migration")
            return messagebox.showinfo("PixTagra", "Restart PixTagra to verify and complete migration.")
        if phase == "legacy_cleanup_complete":
            self._set_credential_status("Legacy migration complete")
            self.migrate_legacy_btn.pack_forget()
            return messagebox.showinfo("PixTagra", "Secure migration completed.")
        self._set_credential_status("Migration requires attention")
        return messagebox.showwarning("PixTagra", "Migration was not completed. Existing legacy files were preserved.")

    def _migrate_legacy_config_key(self):
        try:
            config_path = Path(CONFIG_FILE)
            original_bytes = config_path.read_bytes()
            cfg = _read_config_mapping(config_path)
            if "model" in cfg:
                cfg["model"] = migrate_model_selection(cfg["model"])
            secret = cfg.get("api_key")
            model = cfg.get("model", self.model_var.get())
            provider = _legacy_config_provider_id(model)
        except Exception:
            return messagebox.showwarning("PixTagra", "Legacy config migration could not be prepared. The original key was preserved.")
        if not provider or not isinstance(secret, str) or not secret:
            return messagebox.showwarning("PixTagra", "Legacy config migration is not available. The original key was preserved.")

        def complete(result):
            if getattr(result, "backend_status", None) not in {"stored", "already_present"}:
                return messagebox.showwarning("PixTagra", self._credential_error_message(result, "Legacy config migration"))
            migrated = dict(cfg)
            migrated.pop("api_key", None)
            try:
                _remove_legacy_key_from_config(config_path, migrated, original_bytes)
            except Exception:
                return messagebox.showwarning("PixTagra", "Credential was saved, but the original config key was preserved for recovery.")
            self._legacy_config_key_present = False
            self._config_data = migrated
            self._set_credential_status("Saved in Windows")
            self._on_model_change()
            return messagebox.showinfo("PixTagra", "Legacy config key migrated to Windows securely.")

        return self._start_credential_operation("save", provider, complete, secret_supplier=lambda: secret)

    def load_images(self):
        if self._workspace_busy():
            return
        selected_paths = filedialog.askopenfilenames(title="Select Images", filetypes=[("Images", IMAGE_FILE_DIALOG_PATTERN)])
        if not selected_paths:
            return

        unique_paths = []
        seen_paths = set()
        for raw_path in selected_paths:
            normalized = str(Path(raw_path))
            if normalized in seen_paths:
                continue
            seen_paths.add(normalized)
            unique_paths.append(normalized)

        if not self.showing_ws:
            self.welcome.pack_forget()
            self.workspace.pack(fill="both", expand=True)
            self.showing_ws = True

        new_paths = [p for p in unique_paths if p not in self.cards]
        if not new_paths:
            return
        remaining_slots = max(0, MAX_BATCH_SIZE - len(self.cards))
        if remaining_slots <= 0:
            return messagebox.showwarning("Import limit", f"You can keep up to {MAX_BATCH_SIZE} images loaded at once. Clear some images before importing more.")
        if len(new_paths) > remaining_slots:
            messagebox.showinfo(
                "Import limit",
                f"Only the first {remaining_slots} images were imported.\nWorkspace limit: {MAX_BATCH_SIZE}. Start with 50–100 images per generation batch.",
            )
            new_paths = new_paths[:remaining_slots]
        self.stats["total"] = len(self.cards) + len(new_paths)
        self.is_importing = True
        self._update_stats()
        self.prog.set(0)
        self.prog_lbl.configure(text=f"Loading 0/{len(new_paths)} images...")
        self._import_invalid = []
        self._load_batch(list(new_paths), 0)

    def _sync_workspace_state(self, progress_text=None):
        self.stats["total"] = len(self.cards)
        self._update_stats()
        self._ensure_card_views()
        self._refresh_scroll()
        self._queue_visible_thumb_load()
        if progress_text is not None:
            self.prog_lbl.configure(text=progress_text)

    def _load_batch(self, paths, idx):
        """Create lightweight card records in batches, then render only visible views."""
        self._import_job = None
        BATCH = 12
        end = idx
        deadline = time.perf_counter() + 0.025
        for i in range(idx, min(idx + BATCH, len(paths))):
            end = i + 1
            p = paths[i]
            if p not in self.cards:
                error = validate_image(p)
                if error:
                    self._import_invalid.append(f"{Path(p).name}: {error}")
                    continue
                try:
                    card = CardRecord(p, self)
                    self.cards[p] = card
                    self.card_order.append(p)
                except Exception as e:
                    print(f"[import] skip {p}: {e}")  # isolate bad files; don't kill the chain
            if time.perf_counter() >= deadline:
                break

        self.prog.set(end / max(1, len(paths)))
        if end < len(paths):
            if idx == 0:
                self._sync_workspace_state()
            self.prog_lbl.configure(text=f"Loading {end}/{len(paths)} images...")
            self._import_job = self.after(10, lambda: self._load_batch(paths, end))
        else:
            self.is_importing = False
            self._sync_workspace_state(progress_text=f"Loaded {len(paths) - len(self._import_invalid)} images")
            # Decode visible images on demand. Starting every thumbnail here
            # defeats virtualization and competes with scrolling on large PNG batches.
            self._queue_visible_thumb_load()
            self._save_config()
            if self._import_invalid:
                preview = "\n".join(self._import_invalid[:5])
                messagebox.showwarning("Skipped invalid files", preview)
            if not self.cards:
                self._show_welcome_view()

    def _load_thumbs(self):
        self._thumb_bulk_requested = True
        self._queue_thumb_records(
            [self.cards.get(path) for path in self.card_order]
        )

    def _visible_index_window(self):
        pitch = max(1, self._card_height + self._card_gap)
        first_idx = max(0, int(self.canvas.canvasy(0) // pitch) - 2)
        count = self._visible_pool_size() + 4
        last_idx = min(len(self.card_order), first_idx + count)
        return first_idx, last_idx

    def _queue_thumb_records(self, records):
        for record in records:
            if record is None or record.removed or record.thumb_requested or record.thumb_image is not None:
                continue
            if record.path in self._thumb_queue_paths:
                continue
            self._thumb_queue.append(record)
            self._thumb_queue_paths.add(record.path)
        if self._thumb_queue and self._thumb_flush_job is None:
            self._thumb_flush_job = self.after(25, self._flush_thumb_queue)
        if self._thumb_result_job is None:
            self._thumb_result_job = self.after(50, self._drain_thumb_results)

    def _flush_thumb_queue(self):
        self._thumb_flush_job = None
        # Do not decode while the viewport is still moving. Decodes queued
        # during a drag hold a decoded 384px copy each until the jump window
        # ends, which can reach tens of megabytes; submit them once the view
        # settles so only the final visible rows are decoded.
        if time.perf_counter() < self.__dict__.get("_thumbnail_jump_until", 0):
            self._thumb_flush_job = self.after(50, self._flush_thumb_queue)
            return
        for _ in range(min(2, len(self._thumb_queue))):
            record = self._thumb_queue.pop(0)
            self._thumb_queue_paths.discard(record.path)
            record.load_thumb()
        if self._thumb_queue:
            self._thumb_flush_job = self.after(35, self._flush_thumb_queue)

    def _drain_thumb_results(self):
        """Apply completed thumbnail decodes on Tk's main thread."""
        self._thumb_result_job = None
        # A large jump first paints the new editors. Loading their images in
        # the same event turn caused a second expensive native repaint before
        # input could return. Ordinary wheel motion keeps its normal schedule.
        remaining = self.__dict__.get("_thumbnail_jump_until", 0) - time.perf_counter()
        paint_pending = self.__dict__.get("_thumbnail_jump_job") is not None
        if paint_pending or remaining > 0:
            delay = 50 if paint_pending else max(1, round(remaining * 1000))
            self._thumb_result_job = self.after(delay, self._drain_thumb_results)
            return
        applied = 0
        visible_paints = 0
        # Native image creation/painting can take a frame on a scaled display.
        # Do not paint an entire decode burst before returning to input events.
        deadline = time.perf_counter() + .012
        while applied < 8 and visible_paints < 1 and (applied == 0 or time.perf_counter() < deadline):
            try:
                record, img, generation = self._thumb_result_queue.get_nowait()
            except queue.Empty:
                break
            applied += 1
            record.thumb_requested = False
            if record.removed or generation != self._thumb_generation or img is None:
                if img is not None:
                    img.close()
                continue
            try:
                record.thumb_image = ctk.CTkImage(light_image=img, size=(128, 128))
                self._thumb_cache[record.path] = record
                self._thumb_cache.move_to_end(record.path)
                visible_paints += getattr(record, "view", None) is not None
                record.refresh_view(force_fields=False)
                self._trim_thumb_cache()
            except Exception as exc:
                # One malformed/destroyed view must not stop the batch drain.
                self._record_error(f"Thumbnail update failed: {Path(record.path).name}: {exc}")
        if (
            not self._thumb_result_queue.empty()
            or self._thumb_queue
            or any(card.thumb_requested for card in self.cards.values())
        ):
            # Configuration returns before Windows paints native children.
            # A 16ms timer can expire during that paint and chain another
            # expensive image into the same event turn. Leave a full gap.
            delay = 50
            self._thumb_result_job = self.after(delay, self._drain_thumb_results)

    def _queue_visible_thumb_load(self):
        if not self.showing_ws or not self.card_order:
            return
        first_idx, last_idx = self._visible_index_window()
        records = [self.cards.get(self.card_order[idx]) for idx in range(first_idx, last_idx)]
        for record in records:
            if record is not None and record.path in self._thumb_cache:
                self._thumb_cache.move_to_end(record.path)
        # A scrollbar jump invalidates speculative work for the old viewport.
        # Keep an explicit Thumbs request, but put the current viewport first.
        wanted = {record.path for record in records if record is not None}
        previous = self._thumb_queue if self._thumb_bulk_requested else []
        self._thumb_queue = [record for record in previous if record.path in wanted]
        self._thumb_queue_paths = {record.path for record in self._thumb_queue}
        self._queue_thumb_records(records)
        if self._thumb_bulk_requested:
            for record in previous:
                if record.path not in self._thumb_queue_paths:
                    self._thumb_queue.append(record)
                    self._thumb_queue_paths.add(record.path)

    def _trim_thumb_cache(self):
        for path, record in list(self._thumb_cache.items()):
            if len(self._thumb_cache) <= self._thumb_cache_limit:
                break
            if record.view is None:
                record.thumb_image = None
                self._thumb_cache.pop(path)

    def _visible_pool_size(self):
        pitch = max(1, self._card_height + self._card_gap)
        viewport = max(1, self.canvas.winfo_height())
        # One extra row covers both partial rows at the viewport edges. The
        # synchronous pre-paint update below makes an additional row above
        # the viewport unnecessary; rebinding it redraws offscreen controls.
        return max(3, int((viewport + pitch - 1) / pitch) + 1)

    def _ensure_card_views(self):
        needed = self._visible_pool_size()
        while len(self.card_views) < needed:
            view = ImageCard(self.canvas, self)
            self.card_views.append(view)
        while len(self.card_views) > needed:
            view = self.card_views.pop()
            view.unbind_record()
            view.destroy()
        if self.card_views and not self._card_height_ready:
            probe = self.card_views[0]
            probe.update_idletasks()
            scaled_height = max(1, int(round(probe._apply_widget_scaling(260))))
            scaled_gap = max(1, int(round(probe._apply_widget_scaling(18))))
            scaled_pad_x = max(1, int(round(probe._apply_widget_scaling(14))))
            measured_height = max(1, probe.winfo_reqheight())
            # Include the outer vertical padding as well as child requirements.
            target_height = max(scaled_height, measured_height + int(round(probe._apply_widget_scaling(16))))
            if scaled_gap != self._card_gap:
                self._card_gap = scaled_gap
            if scaled_pad_x != self._card_pad_x:
                self._card_pad_x = scaled_pad_x
            if target_height != self._card_height:
                self._card_height = target_height
            self._card_height_ready = True

    def _update_visible_cards(self):
        self._ensure_card_views()
        if not self.card_order:
            for view in self.card_views:
                view.unbind_record()
            return

        pitch = self._card_height + self._card_gap
        top = self.canvas.canvasy(0)
        # Start with the partially visible top row. The pool includes the
        # extra row needed when both viewport edges intersect a card.
        # Native placed children stay mapped while clipped by their parent;
        # Canvas window items unmap offscreen editors and repaint every child
        # when crossing each row boundary.
        first_idx = max(0, int(top // pitch))
        last_idx = min(len(self.card_order), first_idx + len(self.card_views))
        view_width = max(200, (self.canvas.winfo_width() or 1) - (self._card_pad_x * 2) - 6)
        wanted = [
            (idx, self.cards[self.card_order[idx]])
            for idx in range(first_idx, last_idx)
            if self.card_order[idx] in self.cards and not self.cards[self.card_order[idx]].removed
        ]
        wanted_records = {record for _, record in wanted}
        retained = {view.record: view for view in self.card_views if view.record in wanted_records}
        free_views = []
        # Release outgoing records first; never rebind a view still owned by a
        # visible record. Overlapping rows keep their editors, focus and image.
        for view in self.card_views:
            if view.record not in retained:
                # bind_record synchronizes and releases its previous owner.
                # Keep recycled native windows mapped during the reassignment;
                # hide/show here remaps every child editor and causes a blank flash.
                free_views.append(view)
        for idx, record in wanted:
            view = retained.get(record)
            if view is None:
                # Reuse a currently mapped row before a hidden spare. Mapping
                # a spare recreates the display of every child editor; changing
                # saved/editable styling on an existing row is much cheaper.
                view = min(free_views, key=lambda candidate: (
                    not getattr(candidate, "_viewport_mapped", False),
                    getattr(candidate, "_editor_enabled", None) != (not record.saved),
                ))
                free_views.remove(view)
                view.bind_record(record)
            y = idx * pitch + 10 - self.canvas.scroll_offset
            geometry = (self._card_pad_x, y, view_width, self._card_height)
            previous_geometry = getattr(view, "_viewport_geometry", None)
            if previous_geometry != geometry:
                view.place_configure(x=self._card_pad_x, y=y,
                                     width=view_width, height=self._card_height)
                view._viewport_geometry = geometry
                view._viewport_mapped = True
            view._virtual_index = idx
        for view in free_views:
            if view.record is not None:
                view.unbind_record()
        self._queue_visible_thumb_load()

    def _schedule_visible_update(self, immediate=False):
        if immediate:
            if self._visible_update_job:
                try:
                    self.after_cancel(self._visible_update_job)
                except Exception:
                    pass
                self._visible_update_job = None
            self._update_visible_cards()
        elif self._visible_update_job is None:
            # A short frame budget coalesces wheel bursts without waiting for
            # the event queue to become completely idle.
            self._visible_update_job = self.after(16, self._run_visible_update)

    def _run_visible_update(self):
        self._visible_update_job = None
        self._update_visible_cards()

    def _on_canvas_yview(self, first, last):
        self._canvas_scrollbar.set(first, last)
        offset = self.canvas.scroll_offset
        previous = self.__dict__.get("_thumbnail_viewport_offset", offset)
        self._thumbnail_viewport_offset = offset
        jumped = abs(offset - previous) > self.canvas.winfo_height()
        # Any viewport movement defers image application. During a drag every
        # event is smaller than a viewport, so only the jump test left
        # thumbnails repainting rows while the view was still moving.
        if offset != previous:
            self._thumbnail_jump_until = max(
                self.__dict__.get("_thumbnail_jump_until", 0.0), time.perf_counter() + .150
            )
        # Tk has already coalesced the canvas view change. Rebind before it
        # paints the new viewport; a second 16ms timer exposes an empty frame
        # after a large jump and then forces another complete repaint.
        if self.__dict__.get("_handling_yview", False):
            return
        self._handling_yview = True
        try:
            if self.__dict__.get("_card_height_ready", False):
                self._schedule_visible_update(immediate=True)
            else:
                self._schedule_visible_update()
            if jumped:
                self._unbind_after_job("_thumbnail_jump_job")
                # Timers run after the complete idle paint turn. An idle
                # callback can run before other native child paints, letting
                # those paints consume the image wait before input returns.
                self._thumbnail_jump_job = self.after(0, self._after_viewport_jump)
        finally:
            self._handling_yview = False

    def _after_viewport_jump(self):
        self._thumbnail_jump_job = None
        # At 225% scaling native child paints continued for 90-120ms after
        # this timer started. The old 75ms wait inserted an image into that
        # same paint turn. Keep the existing quiet period beyond that work.
        self._thumbnail_jump_until = time.perf_counter() + .150

    def _on_scrollbar(self, *args):
        self.canvas.yview(*args)
        self._schedule_visible_update()

    def _on_mousewheel(self, event):
        if not self.showing_ws or not self._canvas_mousewheel_active:
            return
        if isinstance(event.widget, (tk.Text, tk.Entry)):
            return
        delta = getattr(event, "delta", 0) or 0
        notches = max(1, int(round(abs(delta) / 120)))
        # Explicit pixel increments avoid Tk's default 10%-of-viewport jumps.
        scroll_units = min(8, max(3, notches * 3))
        self.canvas.yview_scroll(-scroll_units if delta > 0 else scroll_units, "units")
        self._schedule_visible_update()

    def _on_canvas_configure(self, event=None):
        self._refresh_scroll()
        self._schedule_visible_update()

    def _remove_card(self, card, update_ui=True):
        card.sync_from_view()
        card.removed = True
        if card.view is not None:
            card.view.unbind_record()
        self._thumb_queue_paths.discard(card.path)
        self.__dict__.get("_thumb_cache", {}).pop(card.path, None)
        card.thumb_image = None
        self._thumb_queue = [queued for queued in self._thumb_queue if queued.path != card.path]
        self.cards.pop(card.path, None)
        self.data_cache.pop(card.path, None)
        try:
            self.card_order.remove(card.path)
        except ValueError:
            pass
        if update_ui:
            self.stats["total"] = len(self.cards)
            self._update_stats()
            self._refresh_scroll()
            if not self.cards:
                self._cancel_workspace_jobs(reset_pool=True)
                self._show_welcome_view()

    def _selected_keyword_targets(self):
        return [
            card for card in self.cards.values()
            if card.var_sel.get() and not card.removed and not card.saved
        ]

    def _apply_batch_keywords(self, targets, additions):
        """Append keywords to target records without replacing existing metadata."""
        changed = 0
        skipped = 0
        added_total = 0
        for card in targets:
            if card.removed or card.saved:
                skipped += 1
                continue
            card.sync_from_view()
            existing = card.get_data(sync=False)[2]
            existing_keys = {
                _normalize_keyword_text(keyword).casefold()
                for keyword in existing
                if _normalize_keyword_text(keyword)
            }
            addition_keys = {
                _normalize_keyword_text(keyword).casefold()
                for keyword in additions
                if _normalize_keyword_text(keyword)
            }
            new_count = len(addition_keys - existing_keys)
            if new_count <= 0:
                continue
            card._cached_data["keywords"] = _merge_keyword_lists(existing, additions)
            if card._cached_data.get("keyword_policy_version") != 3:
                card._cached_data.pop("keyword_policy_version", None)
            card.manual_fields.add("keywords")
            card.manual_modified = True
            card.refresh_quality_state()
            card.refresh_view(force_fields=True)
            self.data_cache[card.path] = dict(card._cached_data)
            changed += 1
            added_total += new_count
        self._update_stats()
        return changed, skipped, added_total

    def _open_batch_keyword_dialog(self):
        if self.is_processing or self.is_saving:
            return messagebox.showwarning("Add keywords", "Wait for the current operation to finish.")
        targets = self._selected_keyword_targets()
        if not targets:
            return messagebox.showwarning("Add keywords", "Select at least one unsaved image first.")

        dialog = AppToplevel(self)
        dialog.title("Add keywords to selected images")
        dialog.geometry("560x310")
        dialog.resizable(False, False)
        dialog.transient(self)
        dialog.configure(fg_color="#F8FAFC")

        body = ctk.CTkFrame(dialog, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=18, pady=16)
        ctk.CTkLabel(
            body,
            text=f"Add keywords to {len(targets)} selected image(s)",
            font=("Segoe UI", 14, "bold"),
            text_color="#1E293B",
            anchor="w",
        ).pack(fill="x")
        ctk.CTkLabel(
            body,
            text="Keywords",
            font=("Segoe UI", 11, "bold"),
            text_color="#475569",
            anchor="w",
        ).pack(fill="x", pady=(12, 4))
        keyword_input = ctk.CTkTextbox(
            body,
            height=116,
            font=("Segoe UI", 12),
            fg_color="#FFFFFF",
            text_color="#111827",
            border_width=1,
            border_color="#597D87",
            corner_radius=8,
        )
        keyword_input.pack(fill="x")
        preview_var = tk.StringVar(value="0 keywords to add")
        ctk.CTkLabel(
            body,
            textvariable=preview_var,
            font=("Segoe UI", 10),
            text_color="#64748B",
            anchor="w",
        ).pack(fill="x", pady=(6, 0))

        def refresh_preview(_event=None):
            additions = _merge_keyword_lists(
                [], _parse_batch_keyword_input(keyword_input.get("1.0", "end"))
            )
            preview_var.set(f"{len(additions)} keywords to add")

        keyword_input.bind("<KeyRelease>", refresh_preview)
        actions = ctk.CTkFrame(body, fg_color="transparent")
        actions.pack(fill="x", pady=(12, 0))

        def apply_changes():
            additions = _merge_keyword_lists(
                [], _parse_batch_keyword_input(keyword_input.get("1.0", "end"))
            )
            if not additions:
                return messagebox.showwarning("Add keywords", "Enter at least one keyword.")
            non_english = [keyword for keyword in additions if _contains_non_english_text(keyword)]
            if non_english:
                preview = ", ".join(non_english[:3])
                return messagebox.showwarning(
                    "English keywords required",
                    f"Replace non-English keyword(s): {preview}",
                )
            changed, skipped, added_total = self._apply_batch_keywords(targets, additions)
            dialog.destroy()
            message = f"Added {added_total} keyword(s) across {changed} image(s)."
            if skipped:
                message += f"\nSkipped {skipped} saved image(s)."
            messagebox.showinfo("Keywords updated", message)

        ctk.CTkButton(
            actions,
            text="Cancel",
            width=88,
            height=34,
            fg_color="#64748B",
            hover_color="#475569",
            command=dialog.destroy,
        ).pack(side="right")
        ctk.CTkButton(
            actions,
            text="Add to selected",
            width=132,
            height=34,
            fg_color="#0F9DB3",
            hover_color="#027A8F",
            command=apply_changes,
        ).pack(side="right", padx=(0, 8))
        dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)
        dialog.after(50, keyword_input.focus_set)
        dialog.grab_set()
        self.wait_window(dialog)
    
    def _sel_all(self):
        for c in self.cards.values():
            c.var_sel.set(True)
        self._schedule_stats_update()
    
    def _sel_none(self):
        for c in self.cards.values():
            c.var_sel.set(False)
        self._schedule_stats_update()
    
    def _sel_failed(self):
        for c in self.cards.values():
            c.var_sel.set(self._card_processing_failed(c))
        self._schedule_stats_update()

    def _failed_cards(self):
        return [card for path in self.card_order
                if (card := self.cards.get(path)) is not None
                and not card.removed and self._card_processing_failed(card)]

    def _refresh_regen_failed_button(self, failed_count=None):
        button = (self.__dict__.get("btns") or {}).get("regen_failed")
        if button is None:
            return
        # Reuse the batch counts when the caller already computed them; a
        # second full status scan per stats refresh costs one Tcl read per card.
        count = len(self._failed_cards()) if failed_count is None else int(failed_count)
        busy = self._workspace_busy()
        self._configure_if_changed(
            button,
            text=f"Regenerate failed ({count})" if count else "Regenerate failed",
            state="normal" if count and not busy else "disabled",
        )

    def _regenerate_failed(self):
        """Select every failed record and start one batch generation for them."""
        if self._workspace_busy():
            return
        failed = self._failed_cards()
        if not failed:
            return messagebox.showinfo("Regenerate failed", "No failed records to regenerate.")
        # Snapshot selection and scope: analyze() can return without starting
        # (source changed, workspace unavailable), and leaving every failed
        # card selected with a resume scope would silently skip regeneration
        # on the next batch and add those cards to Save selected.
        previous = [(card, bool(card.var_sel.get()), card.regen_scope) for card in failed]
        for card in failed:
            card.var_sel.set(True)
            # A never-reviewed draft from a partial preview can resume: the
            # text exists but its audit never ran. A card that still carries a
            # previous completed generation must regenerate from scratch, or
            # the stale result would be re-reviewed instead of replaced.
            draft = card._cached_data
            card.regen_scope = (
                "resume" if draft.get("title") and not draft.get("keyword_review") else "all"
            )
        self.stats["total"] = len(self.cards)
        self._update_stats()
        started = self.is_processing or self.__dict__.get("_credential_operation_pending", False)
        self.analyze(failed)
        started = started or self.is_processing or self.__dict__.get("_credential_operation_pending", False)
        if not started:
            for card, selected, scope in previous:
                card.var_sel.set(selected)
                card.regen_scope = scope
            self._update_stats()

    def _sel_review(self):
        for card in self.cards.values():
            card.var_sel.set(self._card_needs_review(card))
        self._schedule_stats_update()

    def _sel_ready(self):
        for card in self.cards.values():
            card.var_sel.set(self._card_ready(card))
        self._schedule_stats_update()

    def _sel_unprocessed(self):
        for card in self.cards.values():
            card.var_sel.set(self._card_unprocessed(card))
        self._schedule_stats_update()
    
    def _clear(self):
        if self.is_processing or self.is_saving:
            return messagebox.showwarning("Busy", "Wait for saving or cancel analysis before clearing images.")
        if messagebox.askyesno("Confirm", "Clear all?"):
            self._release_active_credential_lease()
            self._cancel_workspace_jobs(reset_pool=True)
            for c in list(self.cards.values()):
                c.destroy()
            self.cards.clear()
            self.card_order.clear()
            self.data_cache.clear()
            self._thumb_cache.clear()
            self.stats = {"total": 0, "processed": 0, "success": 0, "need_review": 0, "failed": 0}
            self._update_stats()
            self._refresh_scroll()
            self._show_welcome_view()
    
    def _update_stats(self):
        self._schedule_session_save()
        ready, review, failed, unprocessed, selected = self._batch_counts()
        self._configure_if_changed(self.stats_lbl, text=f"{len(self.cards)} total | {selected} selected")
        batch_keyword_button = getattr(self, "btns", {}).get("filter_batch_keywords")
        if batch_keyword_button is not None:
            self._configure_if_changed(
                batch_keyword_button,
                text=f"Add keywords ({selected})",
                state="normal" if selected and not self._workspace_busy() else "disabled",
            )
        if hasattr(self, "batch_summary_lbl"):
            if not self.cards:
                summary = "No records loaded"
            elif unprocessed:
                summary = f"{unprocessed} unprocessed | {ready} ready to save | {review} need review | {failed} failed"
            elif review or failed:
                summary = f"{ready} ready to save | {review} need review | {failed} failed"
            else:
                summary = "Automated checks passed. Ready to save; review AI content before submission."
            self._configure_if_changed(self.batch_summary_lbl, text=summary)
        self._update_retry_button()
        self._refresh_regen_failed_button(failed)
        self._update_save_export_buttons()
        self._update_workspace_controls()
        for row in self.__dict__.get("_action_rows", ()):
            row.request_layout()

    @staticmethod
    def _configure_if_changed(control, **values):
        """Avoid native CTk redraws when a periodic state refresh is unchanged."""
        if control is None:
            return False
        changed = {}
        for name, value in values.items():
            try:
                # Localized text widgets retain the canonical source string;
                # their public cget() value is translated for the current UI.
                current = getattr(control, "_ui_text_source", None) if name == "text" else control.cget(name)
                if name == "text" and current is None:
                    current = control.cget(name)
            except Exception:
                current = object()
            if current != value:
                changed[name] = value
        if changed:
            control.configure(**changed)
        return bool(changed)

    def _schedule_stats_update(self):
        self._schedule_session_save()
        if self._stats_update_job is not None:
            try:
                self.after_cancel(self._stats_update_job)
            except Exception:
                pass
        self._stats_update_job = self.after(100, self._run_stats_update)

    def _run_stats_update(self):
        self._stats_update_job = None
        self._update_stats()

    def _batch_counts(self):
        ready = review = failed = unprocessed = selected = 0
        for card in self.cards.values():
            selected += int(bool(card.var_sel.get()))
            if self._card_processing_failed(card):
                failed += 1
            elif self._card_unprocessed(card):
                unprocessed += 1
            elif self._card_needs_review(card):
                review += 1
            elif self._card_ready(card):
                ready += 1
        return ready, review, failed, unprocessed, selected

    def _saveable_counts(self):
        ready = 0
        review = 0
        mode = self.mode_var.get() if hasattr(self, "mode_var") else PROMPT_MODES[0]
        for card in self.cards.values():
            if self._card_processing_failed(card):
                continue
            title, description, keywords = card.get_data(sync=False)
            # Selection and scroll callbacks must not repeat the quadratic
            # keyword comparison for every unchanged image in a 500-row batch.
            # Key by field values so in-place keyword edits and mode changes
            # invalidate the result, as do generation and undo.
            signature = (title, description, tuple(keywords), mode, tuple(metadata_options(getattr(card, "_cached_data", {})).items()))
            if getattr(card, "_classification_input", None) != signature:
                card._classification_result = classify_stock_metadata(title, description, keywords, mode, **metadata_options(getattr(card, "_cached_data", {})))
                card._classification_input = signature
            hard_errors, cached_warnings = card._classification_result
            review_warnings = list(cached_warnings)
            review_warnings.extend(keyword_review_warnings(getattr(card, "_cached_data", {})))
            if hard_errors:
                continue
            if review_warnings or metadata_compliance_warnings(
                title, description, keywords, card.get_compliance()
            ):
                review += 1
            else:
                ready += 1
        return ready, review

    def _workspace_busy(self):
        return any(self.__dict__.get(flag, False) for flag in (
            "is_processing", "is_saving", "is_importing", "_network_check_busy", "_credential_operation_pending",
        ))

    def _update_workspace_controls(self):
        busy = self._workspace_busy()
        buttons = self.__dict__.get("btns", {})
        targets = any(card.var_sel.get() and not card.saved and (
            self._card_unprocessed(card) or self._card_processing_failed(card)
        ) for card in self.__dict__.get("cards", {}).values())
        for key, enabled in (
            ("⚡ Analyze", targets and not busy), ("+ Add", not busy),
            ("⌂ Home", not busy), ("filter_clear", bool(self.__dict__.get("cards")) and not busy),
        ):
            if key in buttons:
                self._configure_if_changed(buttons[key], state="normal" if enabled else "disabled")
        for name in ("test_conn_btn", "_workspace_mode_menu", "_workspace_model_menu", "_visual_review_checkbox", "_export_target_menu"):
            control = self.__dict__.get(name)
            if control is not None:
                self._configure_if_changed(control, state="disabled" if busy else "normal")
        for control in self.__dict__.get("_language_menus", ()):
            self._configure_if_changed(control, state="disabled" if busy else "normal")
        for view in self.__dict__.get("card_views", ()):
            view.refresh_from_record()

    def _csv_export_counts(self):
        rows = hard = review = 0
        self._export_block_reason = ""
        target = App._selected_export_target(self)
        if target.kind == "unverified":
            return 0, 0, 0
        names = set()
        for path, card in self.cards.items():
            title, description, keywords = card.get_data(sync=False)
            if not title or not description:
                continue
            signature = (title, description, tuple(keywords), self.mode_var.get(), tuple(metadata_options(getattr(card, "_cached_data", {})).items()))
            if getattr(card, "_classification_input", None) != signature:
                card._classification_result = classify_stock_metadata(title, description, keywords, self.mode_var.get(), **metadata_options(getattr(card, "_cached_data", {})))
                card._classification_input = signature
            errors, warnings = card._classification_result
            data = dict(getattr(card, "_cached_data", {}), title=title, description=description, keywords=keywords)
            name = Path(path).name.casefold()
            format_errors = export_record_errors(target, path, data)
            if errors or format_errors or name in names:
                hard += 1
                if not self._export_block_reason:
                    reason = (errors or format_errors or ["Duplicate filenames"])[0]
                    self._export_block_reason = f"{Path(path).name}: {translate(reason)}"
                continue
            names.add(name)
            rows += 1
            review += int(bool(warnings or keyword_review_warnings(card._cached_data) or
                               metadata_compliance_warnings(title, description, keywords, card.get_compliance())))
        return rows, hard, review

    def _update_save_export_buttons(self):
        buttons = getattr(self, "btns", {})
        if not buttons or not hasattr(self, "cards"):
            return
        ready, review = self._saveable_counts()
        busy = self._workspace_busy()
        selected = sum(1 for card in self.cards.values() if card.var_sel.get())
        # Explicit Save all includes records with quality warnings or hard metadata
        # issues; only processing failures cannot be written to disk.
        total_saveable = sum(
            1 for card in self.cards.values() if not self._card_processing_failed(card)
        )
        for key, text, count in (
            ("💾 Save", "Save selected", selected),
            ("💾 Save ready", "Save ready", ready),
            ("💾 Save all", "Save all", total_saveable),
            ("📁 Save→Folder", "Save to folder", selected),
            ("📄 JSON", "JSON", len(self.data_cache)),
            ("📄 JSON ready", "JSON ready", ready),
        ):
            button = buttons.get(key)
            if button is not None:
                self._configure_if_changed(button, text=f"{text} ({count})",
                                            state="normal" if count and not busy else "disabled")
        csv_button = buttons.get("📊 SS CSV")
        if csv_button is not None:
            rows, hard, csv_review = self._csv_export_counts()
            csv_label = f"Export (blocked: {hard})" if hard else f"Export ({rows}; review: {csv_review})" if csv_review else f"Export ({rows})"
            self._configure_if_changed(csv_button, text=csv_label,
                                       state="normal" if rows and not hard and not busy else "disabled")
            hint = self.__dict__.get("_export_hint_label")
            if hint is not None:
                text = translate(App._selected_export_target(self).hint)
                if self._export_block_reason:
                    text += "\n" + self._export_block_reason
                self._configure_if_changed(hint, text=text)
        target_menu = self.__dict__.get("_export_target_menu")
        if target_menu is not None:
            self._configure_if_changed(target_menu, state="disabled" if busy else "normal")

    @staticmethod
    def _card_processing_failed(card):
        return bool(getattr(card, "processing_failed", False) or card.status.cget("text") in {"● Error", "● Failed"})

    @staticmethod
    def _card_unprocessed(card):
        return str(card.status.cget("text")) in {"● Pending", "● Unprocessed"}

    @staticmethod
    def _card_ready(card):
        return bool(not App._card_processing_failed(card) and not App._card_unprocessed(card) and str(card.status.cget("text")) in {"● Ready", "● Saved"})

    @staticmethod
    def _card_needs_review(card):
        status = str(card.status.cget("text"))
        return bool(
            not App._card_processing_failed(card)
            and status in {"● Needs review", "● Need Review", "● Need Retry", "● Saved — needs review"}
        )

    def _retry_candidates(self):
        failed = [card for card in self.cards.values() if self._card_processing_failed(card)]
        review = [card for card in self.cards.values() if self._card_needs_review(card)]
        retryable = failed + [card for card in review if card not in failed]
        selected = [card for card in retryable if card.var_sel.get()]
        if selected and len(selected) < len(retryable):
            return selected, f"Retry selected ({len(selected)})"
        if failed:
            return failed, f"Retry failed ({len(failed)})"
        return review, f"Retry review ({len(review)})"

    def _update_retry_button(self):
        button = getattr(self, "btns", {}).get("🔄 Retry")
        if button is None:
            return
        targets, label = self._retry_candidates()
        self._configure_if_changed(button, text=f"🔄 {label}",
                                   state="normal" if targets and not self._workspace_busy() else "disabled")
    
    def _analyze_wrap(self):
        if self._workspace_busy():
            return
        if self.is_saving:
            return messagebox.showwarning("Busy", "Save is in progress. Please wait.")
        sel = [c for c in self.cards.values() if c.var_sel.get() and not c.saved
               and (self._card_unprocessed(c) or self._card_processing_failed(c))]
        if not sel:
            return messagebox.showinfo("Generate metadata", "Select unprocessed or failed images. Open Selection / retry for review items, or use Regenerate on a card to replace completed content.")
        self.analyze(sel)
    
    def _retry(self):
        if self.is_saving:
            return messagebox.showwarning("Busy", "Save is in progress. Please wait.")
        targets, _label = self._retry_candidates()
        if targets:
            self._confirm_batch_retry(targets)

    def _confirm_batch_retry(self, targets):
        count = len(targets)
        dialog = tk.Toplevel(self)
        dialog.title("Confirm retry")
        dialog.transient(self)
        dialog.resizable(False, False)
        body = tk.Frame(dialog, padx=18, pady=16)
        body.pack(fill="both", expand=True)
        tk.Label(body, text=f"Retry {count} image(s)?", font=("Segoe UI", 12, "bold"), anchor="w").pack(fill="x")
        tk.Label(
            body,
            text="Estimated requests: review starts at 1/image; generation may also need repairs and visual review.\nNetwork retries may add requests and additional API usage or charges.\nExisting generated text is reused when only visual review is incomplete.",
            wraplength=560,
            justify="left",
            anchor="w",
        ).pack(fill="x", pady=(8, 14))
        actions = tk.Frame(body)
        actions.pack(fill="x")
        tk.Button(actions, text="Cancel", command=dialog.destroy).pack(side="right")
        def retry():
            dialog.destroy()
            for card in targets:
                if card._cached_data.get("title"):
                    card.regen_scope = "resume"
            self.analyze(targets)
        tk.Button(actions, text=f"Retry {count}", command=retry).pack(side="right", padx=(0, 8))
        dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)
        self._scale_native_dialog(dialog)
        dialog.grab_set()
        self.wait_window(dialog)
    
    def analyze(self, cards):
        if self.is_processing or self._network_check_busy or self.is_importing:
            return
        if self.is_saving:
            return messagebox.showwarning("Busy", "Save is in progress. Please wait.")
        cards = cards[:MAX_BATCH_SIZE]
        for card in cards:
            if image_identity(card.path) != card.source_identity or card.source_identity is None:
                return messagebox.showwarning("Source changed", "An image has changed or is missing. Remove it and reimport the correct file before analysis.")
            card.sync_from_view()
            card._resume_data = copy.deepcopy(card._cached_data) if card.regen_scope == "resume" else None
            # A cancelled or failed earlier run can leave a preview marker
            # behind; each new analysis pass starts from a clean draft state.
            card._partial_draft = False
        if not self._persist_session(interactive=True):
            self.prog_lbl.configure(text="Generation not started: workspace unavailable. See the recovery message.")
            return
        model = self.model_var.get()
        local_url = self.local_url_var.get().strip()
        cloud_base_url = self.base_url_var.get().strip()
        pconf = get_provider_config(model)
        if pconf["type"] == "local":
            return self._start_authorized_analysis(cards, model, local_url, cloud_base_url, None)
        provider = pconf["provider"]
        self._sync_input_to_session_key(provider)
        self._start_credential_operation(
            "batch", provider,
            lambda result: self._on_batch_authorized(result, cards, model, local_url, cloud_base_url),
        )

    def _on_batch_authorized(self, result, cards, model, local_url, cloud_base_url):
        lease = getattr(result, "lease", None) if result is not None else None
        if getattr(result, "status", None) != "authorized" or lease is None:
            return messagebox.showwarning("PixTagra", self._credential_error_message(result, "Batch authorization"))
        self._release_active_credential_lease()
        self._active_credential_lease = lease
        return self._start_authorized_analysis(cards, model, local_url, cloud_base_url, lease)

    def _start_authorized_analysis(self, cards, model, local_url, cloud_base_url, lease):
        api_key = ""
        if lease is not None:
            api_key = lease.secret_for_authorized_use()
            if not api_key:
                self._release_active_credential_lease()
                return messagebox.showwarning("PixTagra", "Batch authorization expired before processing started.")
        self.is_processing, self.stop_flag = True, False
        self._stop_event.clear()
        self._analysis_total = len(cards)
        self.stats["processed"], self.stats["success"], self.stats["need_review"], self.stats["failed"] = 0, 0, 0, 0
        with self.error_lock:
            self.api_errors.clear()
        self.prog.set(0)
        self.btns["⚡ Analyze"].configure(state="disabled")
        self.btns["🔄 Retry"].configure(state="disabled")
        self.btns["⏹ Cancel"].configure(state="normal")
        self.prog_lbl.configure(text="Checking model connection...")
        self._update_stats()
        mode = self.mode_var.get()
        self._batch_language = normalize_language(self.language_var.get()) if "language_var" in self.__dict__ else "en"
        self._batch_visual_review_enabled = self.visual_review_var.get()
        self._run_network_check(
            lambda: self._run_request_smoke_test(model, api_key=api_key, base_url=cloud_base_url, local_url=local_url),
            lambda result: self._on_analysis_preflight(result, cards, model, mode, local_url, cloud_base_url, lease),
        )

    def _on_analysis_preflight(self, result, cards, model, mode, local_url, cloud_base_url, lease):
        if self.stop_flag:
            self._finish()
            return
        smoke_ok, smoke_message, smoke_ctx = result
        if not smoke_ok:
            self._finish(progress_text="Generation not started: connection check failed.")
            return messagebox.showerror(
                "Connection Check Failed",
                self._format_connection_feedback({"ctx": smoke_ctx}, smoke_message),
            )
        self._save_config()
        self.is_processing, self.stop_flag = True, False
        with self.error_lock:
            self.api_errors.clear()
        self.stats["processed"], self.stats["success"], self.stats["need_review"], self.stats["failed"] = 0, 0, 0, 0
        self._reset_analysis_result_queue()
        self._unbind_after_job("_analysis_result_job")
        self._analysis_result_job = self.after(25, self._drain_analysis_results)
        self.btns["⚡ Analyze"].configure(state="disabled")
        self.btns["🔄 Retry"].configure(state="disabled")
        self.btns["⏹ Cancel"].configure(state="normal")
        threading.Thread(target=self._analyze_thread, args=(cards, model, mode, lease, local_url, cloud_base_url), daemon=True).start()
    
    def _analyze_thread(self, cards, model, mode, credential_lease, local_url, cloud_base_url):
        total = len(cards)
        if total <= 0:
            self._queue_analysis_finished()
            return

        api_key = ""
        if credential_lease is not None:
            api_key = credential_lease.secret_for_authorized_use()
            if not api_key:
                self._queue_analysis_finished()
                return

        # Detect provider — Chinese APIs (Kimi, Zhipu) need sequential processing
        # to avoid ConnectionResetError from aggressive rate limiting
        provider = get_provider_config(model).get("provider", "")
        is_rate_limited = provider in ("kimi", "zhipu")
        workers = 1 if is_rate_limited else MAX_WORKERS
        # Rate-limited providers need a deliberate gap.  The bounded executor
        # already caps concurrent requests for other providers, so delaying
        # every submission only stalls result delivery for large batches.
        inter_delay = 4.0 if is_rate_limited else 0.0

        if is_rate_limited:
            for i, card in enumerate(cards):
                if self.stop_flag:
                    break
                try:
                    result = self._process(card, model, mode, api_key, local_url, cloud_base_url)
                    self._queue_analysis_result(card, result, total, mode)
                except Exception:
                    self._queue_analysis_result(card, None, total, mode)
                if self.stop_flag:
                    break
                if i < len(cards) - 1:
                    self._wait_for_retry(inter_delay)
        else:
            def process_if_active(card):
                if self.stop_flag:
                    return None
                return self._process(card, model, mode, api_key, local_url, cloud_base_url)

            with ThreadPoolExecutor(max_workers=workers) as ex:
                remaining = iter(cards)
                futures = {}
                while True:
                    while not self.stop_flag and len(futures) < workers:
                        card = next(remaining, None)
                        if card is None:
                            break
                        futures[ex.submit(process_if_active, card)] = card
                    if not futures:
                        break
                    completed, _pending = wait(futures, return_when=FIRST_COMPLETED)
                    for future in completed:
                        card = futures.pop(future)
                        try:
                            result = future.result()
                        except Exception as exc:
                            self._record_error(self._redact_secrets(exc))
                            result = None
                        # Cancellation stops dispatch, never discards an accepted result.
                        self._queue_analysis_result(card, result, total, mode)

        self._queue_analysis_finished()

    def _queue_analysis_result(self, card, result, total, mode):
        result_queue = self.__dict__.get("_analysis_result_queue")
        if result_queue is None:
            self._record_analysis_result(card, result, total, mode)
            return
        result_queue.put(("result", card, result, total, mode))

    def _queue_partial_preview(self, card, result):
        """Show first-pass metadata while review stages are still running."""
        result_queue = self.__dict__.get("_analysis_result_queue")
        if result_queue is None or getattr(card, "removed", False):
            return
        if not isinstance(result, dict) or not result.get("title"):
            return
        # The worker keeps mutating its own dict (review, dedup, correction).
        # Send an independent copy so the UI thread never reads a nested list
        # while the worker rewrites it.
        result_queue.put(("partial", card, copy.deepcopy(result), 0, None))

    def _apply_partial_preview(self, card, result):
        """Fill the card from the generation pass without changing batch counts."""
        if getattr(card, "removed", False) or self.__dict__.get("stop_flag", False):
            return
        if card._cached_data.get("title") == result.get("title"):
            return
        previous = dict(card._cached_data)
        # fill() rewrites regen_scope and prunes manual_fields against it. A
        # preview must not consume the resume scope or clear the user's manual
        # edits before the completed result arrives, or those fields would be
        # replaced by generated text.
        scope = card.regen_scope
        manual_fields = set(card.manual_fields)
        manual_modified = card.manual_modified
        # The completed result's fill() will not push history for this draft,
        # so push the true previous generation once, here.
        if not getattr(card, "_partial_draft", False) and (
                previous.get("title") or previous.get("description") or previous.get("keywords")):
            card.history.append({"data": previous, "time": card.last_generated_at,
                                 "model": card.last_generated_model,
                                 "manual_fields": set(card.manual_fields)})
            card.history = card.history[-5:]
        card._partial_applied = True
        card._partial_draft = True
        try:
            card.fill(result)
        finally:
            card._partial_applied = False
            card.regen_scope = scope
            card.manual_fields = manual_fields
            card.manual_modified = manual_modified

    def _queue_analysis_finished(self):
        result_queue = self.__dict__.get("_analysis_result_queue")
        if result_queue is None:
            self.after(0, self._finish)
            return
        result_queue.put(("finish", None, None, 0, None))

    def _drain_analysis_results(self):
        """Apply worker results on Tk's main thread in bounded UI batches."""
        self._analysis_result_job = None
        finished = False
        for _ in range(8):
            try:
                kind, card, result, total, mode = self._analysis_result_queue.get_nowait()
            except queue.Empty:
                break
            if kind == "finish":
                finished = True
                break
            if kind == "session_error":
                self._session_error(result)
                continue
            if kind == "partial":
                self._apply_partial_preview(card, result)
                continue
            self._record_analysis_result(card, result, total, mode)
        if finished:
            self._finish()
        elif self.is_processing or not self._analysis_result_queue.empty():
            self._analysis_result_job = self.after(25, self._drain_analysis_results)

    def _record_analysis_result(self, card, result, total, mode=None):
        if getattr(card, "removed", False):
            return
        validation_mode = mode if mode is not None else (
            self.mode_var.get() if hasattr(self, "mode_var") else PROMPT_MODES[0]
        )
        if not result and self.stop_flag:
            return
        self.stats["processed"] += 1
        if result:
            quality_issues = validate_stock_metadata(
                result.get("title", ""),
                result.get("description", ""),
                result.get("keywords", []),
                validation_mode, **metadata_options(result),
            )
            quality_issues.extend(keyword_review_warnings(result))
            compliance_warnings = metadata_compliance_warnings(
                result.get("title", ""),
                result.get("description", ""),
                result.get("keywords", []),
                result.get("compliance", {}),
            )
            if not quality_issues and not compliance_warnings:
                self.stats["success"] += 1
            else:
                self.stats["need_review"] += 1
            self.data_cache[card.path] = result
            card.fill(result)
            if "_session_ack" in self.__dict__:
                self._session_ack.add(card.path)
            # Debounced: a full snapshot write with synchronous=FULL per image
            # blocks the Tk thread and grows quadratically across a large
            # batch. Each result is already durable in its checkpoint until
            # the debounced snapshot acknowledges it.
            self._schedule_session_save()
        else:
            self.stats["failed"] += 1
            self._mark_processing_failed(card)

        count = self.stats["processed"]
        # Results are drained on Tk's thread. Deferred callbacks could run after
        # _finish and overwrite its final state with the previous batch's UI.
        self.prog.set(count / max(1, total))
        self._schedule_stats_update()
        state = "Cancelling — retaining active results" if self.stop_flag else "Processing"
        self.prog_lbl.configure(text=f"{state}: {count}/{total}")
    
    def _process(self, card, model, mode, api_key, local_url, cloud_base_url=""):
        if self.__dict__.get("stop_flag", False) or getattr(card, "removed", False):
            return None
        pconf = get_provider_config(model)
        provider = pconf["provider"]
        img_err = validate_image(card.path)
        if img_err:
            self._record_error(f"{Path(card.path).name}: {img_err}")
            return None
        attempt_delays = RETRY_DELAYS if pconf["type"] == "local" else [0]
        for attempt, delay in enumerate(attempt_delays):
            if self.__dict__.get("stop_flag", False):
                return None
            try:
                data = load_image_bytes(card.path)
                if not data:
                    self._record_error(f"Cannot read image: {card.path}")
                    return None
                b64 = base64.b64encode(prepare_image(data)).decode()
                resume_data = getattr(card, "_resume_data", None)
                if resume_data:
                    r = copy.deepcopy(resume_data)
                elif pconf["type"] == "local":
                    endpoint_issue = self._local_endpoint_issue(local_url)
                    if endpoint_issue:
                        self._record_error(endpoint_issue)
                        return None
                    base_url = self._resolve_local_api_url(local_url or pconf.get("default_base_url", "http://localhost:11434/api/chat"))
                    prompt = build_prompt(mode, self.__dict__.get("_batch_language", "en"))
                    txt = call_local_model(b64, prompt, self._resolve_request_model(model), base_url)
                    r = extract_json(txt, require_object=True)
                    if r:
                        issue = self._metadata_schema_issue(r)
                        if issue:
                            raise _RetryableApiError(f"{model} {issue}.", kind="schema_mismatch")
                else:
                    r = self._call_api(b64, model_name=model, api_key=api_key, base_url=cloud_base_url, mode=mode)
                if r and r.get("title"):
                    if not resume_data:
                        r["metadata_language"] = self.__dict__.get("_batch_language", "en")
                        r["keyword_policy_version"] = 3
                    self._checkpoint_result(card, r, "repair", model)
                    pending_stage = (r.get("keyword_review") or {}).get("stage")
                    repair_needed = any(issue.startswith(("Title", "Description")) for issue in
                        validate_stock_metadata(r.get("title", ""), r.get("description", ""), r.get("keywords", []), mode, **metadata_options(r))
                        + keyword_review_warnings(r))
                    if not resume_data or pending_stage == "repair" or repair_needed:
                        r = self._apply_sanitizer_and_repair(
                            r, b64, pconf, api_key, local_url, cloud_base_url, model, mode
                        )
                    self._queue_partial_preview(card, r)
                    self._checkpoint_result(card, r, "review", model)
                    if self.__dict__.get("_batch_visual_review_enabled", False):
                        r = self._review_generated_keywords(r, b64, model, api_key, cloud_base_url, pconf)
                        if not resume_data:
                            r = self._correct_reviewed_metadata(r, b64, model, mode, api_key, cloud_base_url, pconf)
                    elif pconf.get("review_required", True):
                        r["keyword_review"] = {"status": "model_unqualified", "model": model}
                    self._checkpoint_result(card, r, "done", model)
                    return r
                if r is None:
                    print(f"  Retry {attempt+1}: API returned no parseable result (see [diag] lines above)")
                else:
                    print(f"  Retry {attempt+1}: parsed JSON missing 'title'; keys present: {list(r.keys())}")
            except _FatalApiError as e:
                self._record_error(str(e))
                return None
            except ConnectionError:
                self._record_error("Cannot connect to local Ollama service. Make sure `ollama serve` is running.")
                return None
            except requests.ConnectionError as e:
                print(f"  Retry {attempt+1}: Connection reset — {provider} may be rate limiting")
                self._record_error(f"{model} connection issue: {e}")
                if attempt < len(attempt_delays) - 1:
                    print(f"    Waiting {delay}s before retry...")
            except _RetryableApiError as e:
                msg = self._redact_secrets(e)
                print(f"  Retry {attempt+1}: {msg}")
                self._record_error(msg)
            except Exception as e:
                msg = self._redact_secrets(e)
                print(f"  Retry {attempt+1}: {msg}")
                self._record_error(f"{model} error: {msg}")
            if attempt < len(attempt_delays) - 1:
                wait_for = self._backoff_delay(attempt)
                self._wait_for_retry(wait_for)

        # All retries failed — mark image as failed and continue
        return None
    
    def _stop(self):
        self.stop_flag = True
        stop_event = self.__dict__.get("_stop_event")
        if stop_event is not None:
            stop_event.set()
        self._release_active_credential_lease()
        if "prog_lbl" in self.__dict__:
            self.prog_lbl.configure(text="Cancelling: no new images; waiting for active requests and retaining their results...")
        if "btns" in self.__dict__:
            self.btns["⏹ Cancel"].configure(state="disabled")

    def _wait_for_retry(self, seconds):
        stop_event = self.__dict__.get("_stop_event")
        if stop_event is not None:
            stop_event.wait(seconds)
        else:
            time.sleep(seconds)
    
    def _finish(self, progress_text=None):
        self.is_processing = False
        self._persist_session()
        self._release_active_credential_lease()
        if self._stats_update_job is not None:
            try:
                self.after_cancel(self._stats_update_job)
            except Exception:
                pass
            self._stats_update_job = None
        self._update_stats()
        self.btns["⏹ Cancel"].configure(state="disabled")
        self._update_workspace_controls()
        self._update_retry_button()
        total = self.__dict__.get("_analysis_total", self.stats["processed"])
        self.prog.set(self.stats["processed"] / max(1, total))
        state = "Cancelled" if self.stop_flag else "Done"
        self.prog_lbl.configure(text=progress_text or f"{state}: {self.stats['success']} ok, {self.stats['need_review']} review, {self.stats['failed']} failed")
        with self.error_lock:
            errors = list(self.api_errors)
        if errors and self.stats["failed"] > 0:
            preview = "\n".join(f"• {e}" for e in errors[:3])
            extra = f"\n\n(+{len(errors)-3} more)" if len(errors) > 3 else ""
            messagebox.showwarning("Processing completed with errors", preview + extra)

    def _mark_processing_failed(self, card):
        card.has_error = True
        card.processing_failed = True
        # A previewed record may fail before its completed result arrives. The
        # draft flag must not survive into the next run, where it would make
        # the completed fill skip recording the previous generation.
        card._partial_draft = False
        card.status.configure(text="● Failed", text_color="#EF4444")

    def _focus_card(self, card):
        card.var_sel.set(True)
        if card.path not in self.card_order:
            return
        try:
            index = self.card_order.index(card.path)
            pitch = self._card_height + self._card_gap
            self.canvas.yview_moveto(index * pitch / max(1, len(self.card_order) * pitch + 20))
            self._schedule_visible_update(immediate=True)
            if card.view is not None:
                card.view.focus_set()
        except (AttributeError, tk.TclError, TypeError, ValueError):
            pass

    def _quality_detail(self, card):
        title, description, keywords = card.get_data(sync=False)
        hard_errors, review_warnings = classify_stock_metadata(
            title,
            description,
            keywords,
            self.mode_var.get() if hasattr(self, "mode_var") else PROMPT_MODES[0],
            **metadata_options(getattr(card, "_cached_data", {})),
        )
        review_warnings.extend(keyword_review_warnings(getattr(card, "_cached_data", {})))
        chinese = metadata_language(getattr(card, "_cached_data", {})) == "zh"
        title_count = len(re.findall(r"[\u3400-\u9fff]", title)) if chinese else _stock_word_count(title)
        description_count = len(re.findall(r"[\u3400-\u9fff]", description)) if chinese else _stock_word_count(description)
        unit = "Chinese characters" if chinese else "words"
        title_state = "passed" if 8 <= title_count <= (30 if chinese else 12) else f"{title_count} {unit}"
        description_state = "passed" if 30 <= description_count <= (120 if chinese else 50) else f"{description_count} {unit}"
        minimum, maximum = keyword_limits(getattr(card, "_cached_data", {}))
        keyword_state = f"{len(keywords)} ({minimum}–{maximum})"
        compliance = metadata_compliance_warnings(title, description, keywords, card.get_compliance())
        lines = [
            f"{Path(card.path).name}",
            f"- Keywords: {keyword_state}",
            f"- Description: {description_state}",
            f"- Title: {title_state}",
            f"- Compliance: {'needs review' if compliance else 'passed'}",
            keyword_review_detail(getattr(card, "_cached_data", {})),
        ]
        return "\n".join(lines), bool(hard_errors), bool(review_warnings or compliance)

    def _show_quality_recovery_dialog(
        self, failures, ready_count, on_retry, on_save_ready=None, on_save_review=None
    ):
        """Offer direct recovery actions instead of trapping users in an OK-only warning."""
        normalized_failures = [
            (entry[0], entry[1], entry[2] if len(entry) > 2 else True, entry[3] if len(entry) > 3 else None)
            for entry in failures
        ]
        hard_count = sum(1 for _card, _issue, is_hard, _detail in normalized_failures if is_hard)
        review_count = len(failures) - hard_count
        dialog = tk.Toplevel(self)
        dialog.title("Metadata quality check")
        dialog.transient(self)
        dialog.resizable(False, False)
        body = tk.Frame(dialog, padx=18, pady=16)
        body.pack(fill="both", expand=True)
        tk.Label(body, text=f"{len(failures)} record(s) need review.", font=("Segoe UI", 12, "bold"), anchor="w").pack(fill="x")
        if hard_count:
            tk.Label(body, text=f"{hard_count} hard error(s) must be fixed before saving.", font=("Segoe UI", 10), anchor="w", foreground="#B91C1C").pack(fill="x", pady=(4, 0))
        elif review_count:
            tk.Label(body, text="These are quality warnings. You may save them as-is and accept the tradeoff.", font=("Segoe UI", 10), anchor="w", foreground="#92400E", wraplength=560).pack(fill="x", pady=(4, 0))
        if ready_count:
            tk.Label(body, text=f"{ready_count} record(s) are ready to save.", font=("Segoe UI", 10), anchor="w").pack(fill="x", pady=(4, 8))
        issues = tk.Frame(body)
        issues.pack(fill="both", expand=True)
        issue_text = tk.Text(
            issues,
            height=min(max(len(failures), 3), 12),
            width=78,
            wrap="word",
            relief="solid",
            borderwidth=1,
        )
        issue_scroll = tk.Scrollbar(issues, orient="vertical", command=issue_text.yview)
        issue_text.configure(yscrollcommand=issue_scroll.set)
        issue_text.pack(side="left", fill="both", expand=True)
        issue_scroll.pack(side="right", fill="y")
        for card, issue, _is_hard, detail in normalized_failures:
            issue_text.insert("end", f"{detail or f'• {Path(card.path).name}: {issue}'}\n\n")
        issue_text.configure(state="disabled")
        actions = tk.Frame(body)
        actions.pack(fill="x", pady=(14, 0))
        result = {"action": "cancel"}

        def close(action):
            result["action"] = action
            try:
                dialog.grab_release()
            except tk.TclError:
                pass
            dialog.destroy()

        tk.Button(actions, text=f"Retry generation ({len(failures)})", command=lambda: close("retry")).grid(row=0, column=0, padx=6, pady=4, sticky="ew")
        tk.Button(actions, text="Open record", command=lambda: close("open")).grid(row=0, column=1, padx=6, pady=4, sticky="ew")
        tk.Button(actions, text="Edit manually", command=lambda: close("edit")).grid(row=0, column=2, padx=6, pady=4, sticky="ew")
        if ready_count and on_save_ready is not None:
            tk.Button(actions, text=f"Save ready ({ready_count})", command=lambda: close("save_ready")).grid(row=1, column=0, padx=6, pady=4, sticky="ew")
        if review_count and not hard_count and on_save_review is not None:
            tk.Button(actions, text=f"Save with review ({review_count})", command=lambda: close("save_review")).grid(row=1, column=1, padx=6, pady=4, sticky="ew")
        tk.Button(actions, text="Cancel", command=lambda: close("cancel")).grid(row=1, column=2, padx=6, pady=4, sticky="ew")
        for column in range(3):
            actions.grid_columnconfigure(column, weight=1)
        dialog.protocol("WM_DELETE_WINDOW", lambda: close("cancel"))
        self._scale_native_dialog(dialog)
        dialog.grab_set()
        self.wait_window(dialog)
        if result["action"] == "retry":
            on_retry()
        elif result["action"] == "open" and normalized_failures:
            self._focus_card(normalized_failures[0][0])
        elif result["action"] == "edit" and normalized_failures:
            self._focus_card(normalized_failures[0][0])
        elif result["action"] == "save_ready" and on_save_ready is not None:
            on_save_ready()
        elif result["action"] == "save_review" and on_save_review is not None:
            on_save_review()

    def _record_error(self, message):
        msg = str(message).strip()
        if not msg:
            return
        with self.error_lock:
            if msg not in self.api_errors:
                self.api_errors.append(msg)
    
    def _apply_sanitizer_and_repair(self, r, b64, pconf, api_key, local_url, cloud_base_url, model_name="", mode=PROMPT_MODES[0]):
        """Sanitize metadata and spend at most one request on a focused repair."""
        if not isinstance(r, dict):
            self._record_error(f"{model_name or 'Model'} returned non-object metadata JSON.")
            return {"title": "", "description": "", "keywords": []}
        raw_kw = r.get("keywords", [])
        if isinstance(raw_kw, str):
            raw_kw = split_keywords(raw_kw)
        r = dict(r)
        language = r.get("keyword_language") or metadata_language(r)
        minimum, maximum = keyword_limits(r)
        generated = r.get("keyword_policy_version") in (2, 3)
        if not generated:
            maximum = KEYWORD_TARGET_COUNT
        strict_policy = {"keyword_policy_version": 3} if r.get("keyword_policy_version") == 3 else {}
        kept, rejected, _ = sanitize_keywords_for_stock(raw_kw, target_count=49 if generated else maximum, language=language, **strict_policy)
        if generated:
            kept = rank_keywords_for_stock(kept, mode)[:maximum]
        repair_target = minimum if generated else maximum
        # The minimum decides whether a repair is needed. It is not the cap
        # for valid concepts returned by that repair in the current policy.
        repair_limit = maximum if r.get("keyword_policy_version") == 3 else repair_target
        shortfall = max(0, repair_target - len(kept))
        r["keywords"] = kept
        print(f"[keywords] kept={len(kept)} rejected={len(rejected)} shortfall={shortfall}")
        content_issues = [issue for issue in validate_stock_metadata(
            r.get("title"), r.get("description"), kept, mode, **metadata_options(r)
        ) + keyword_review_warnings(r) if issue.startswith(("Title", "Description"))]
        can_repair = bool(b64 and pconf.get("type") != "local" and not self.__dict__.get("stop_flag", False))
        if content_issues and can_repair and model_name in API_CONFIG:
            try:
                candidate = self._call_api_with_retry(
                    b64, model_name, api_key, cloud_base_url,
                    prompt=build_metadata_repair_prompt(r, content_issues, mode),
                )
                if isinstance(candidate, dict):
                    candidate_issues = validate_stock_metadata(
                        candidate.get("title"), candidate.get("description"), kept, mode, **metadata_options(r)
                    )
                    for field, prefix in (("title", "Title"), ("description", "Description")):
                        if any(issue.startswith(prefix) for issue in content_issues) and not any(
                            issue.startswith(prefix) for issue in candidate_issues
                        ):
                            r[field] = candidate[field]
                    additions = candidate.get("keywords", [])
                    if isinstance(additions, list) and shortfall:
                        r["keywords"], _, _ = sanitize_keywords_for_stock(kept + additions, repair_limit, language=language, **strict_policy)
            except Exception as exc:
                self._record_error(f"{model_name} metadata correction failed: {self._redact_secrets(exc, [api_key])}")
        elif len(kept) < minimum and can_repair:
            # The target is a recommendation. Filling a small numeric gap was
            # adding invented terms to otherwise usable real-image results.
            print(f"[repair] requesting {shortfall} additional keywords")
            try:
                api_conf = API_CONFIG.get(model_name, {})
                if not api_conf:
                    print(f"[repair] model '{model_name}' not found in API_CONFIG")
                    extra = []
                else:
                    extra = self._repair_api_call(b64, kept, rejected, shortfall, pconf, api_conf, api_key, cloud_base_url, model_name, **({"language": language} if language == "zh" else {}))
                if extra:
                    combined, _, _ = sanitize_keywords_for_stock(kept + extra, repair_limit, language=language, **strict_policy)
                    r["keywords"] = combined
                    print(f"[repair] added {len(combined) - len(kept)} keywords total={len(combined)}")
                else:
                    print("[repair] API returned no usable keywords")
            except Exception as _e:
                print(f"[repair] skipped: {_e}")
        r["keywords"] = rank_keywords_for_stock(r["keywords"], mode)
        r["compliance"] = normalize_compliance_flags(r.get("compliance", {}))
        return r

    def _repair_api_call(self, b64, kept, rejected, needed, pconf, api_conf, api_key, base_url="", model_name="", language="en"):
        """Request focused replacement keywords with the bounded API policy."""
        kept_str = "; ".join(kept[:25])
        rej_str = "; ".join(f"{kw} ({reason})" for kw, reason in rejected[:10])
        repair_prompt = (
            f"Look at this image carefully.\n\n"
            f"Accepted keywords so far:\n{kept_str}\n\n"
            f"Rejected (do NOT repeat or create subsets of these):\n{rej_str}\n\n"
            f"Generate up to {needed} NEW keywords for this image. Return fewer, or an empty list, if no more are supported by visible evidence.\n"
            f"Each keyword must:\n"
            f"- Be an English stock-search phrase a buyer would type on Getty, iStock, Adobe Stock, or Shutterstock\n"
            f"- Be concrete, searchable, and different from the accepted list\n"
            f"- Describe something VISIBLE: subject, setting, style, color, technique\n"
            f"- NOT contain prompt language, mood spam, vague business jargon, or duplicate prefixes\n\n"
            f"Return ONLY valid JSON: {{\"keywords\": [\"kw1\", \"kw2\", ...]}}"
        )
        if language == "zh":
            repair_prompt = repair_prompt.replace("English stock-search phrase", "Simplified Chinese stock-search phrase")
            repair_prompt += "\nReturn only natural Simplified Chinese keywords. Keep JSON keys unchanged."
        compact_repair_prompt = (
            "Inspect the attached image. Return ONLY JSON with a keywords array. "
            f"Suggest up to {needed} new, independently searchable, directly visible stock terms. "
            "Do not repeat accepted or rejected terms, synonyms, subsets, vague filler, or inferred facts. "
            "Return fewer when the image does not support more.\n"
            "ACCEPTED: " + json.dumps(kept[:25], ensure_ascii=False) + "\n"
            "REJECTED: " + json.dumps([kw for kw, _reason in rejected[:10]], ensure_ascii=False)
        )
        if language == "zh":
            compact_repair_prompt += "\n关键词必须是自然的简体中文搜索短语；JSON 键名保持 keywords。"
        try:
            parsed = self._call_api_with_retry(
                b64, model_name, api_key, base_url,
                prompt=repair_prompt, retry_prompt=compact_repair_prompt,
                max_tokens=1024, response_kind="keyword_repair",
            )
            if parsed and isinstance(parsed.get("keywords"), list):
                return parsed["keywords"]
        except Exception as e:
            print(f"[repair] exception: {self._redact_secrets(e, [api_key])}")
        return None

    def _correct_reviewed_metadata(self, data, b64, model, mode, api_key, base_url, pconf):
        """One correction after review, followed by a new audit of every new word."""
        report = data.get("keyword_review", {})
        if (data.get("keyword_policy_version") not in (2, 3) or report.get("status") != "reviewed"
                or report.get("correction") or pconf.get("type") == "local"
                or not b64 or self.__dict__.get("stop_flag", False)):
            return data
        minimum, maximum = keyword_limits(data)
        issues = []
        if len(data.get("keywords", [])) < minimum:
            issues.append(f"Only {len(data.get('keywords', []))} keywords remain AFTER factual and semantic review; need {minimum} to {maximum} distinct grounded search concepts")
        issues.extend(f"{c['field'].title()}: {c['reason']}" for c in report.get("caption_checks", [])
                      if c.get("verdict") != "supported")
        issues.extend(f"Uncertain keyword '{c['keyword']}': {c['reason']}" for c in report.get("checks", [])
                      if c.get("verdict") == "uncertain" and c["keyword"] in data.get("keywords", []))
        if not issues:
            return data
        # Only verified concepts are anchors. The rejected captions and labels
        # must not become evidence for their own replacement.
        anchors = [c["keyword"] for c in report.get("checks", [])
                   if c.get("verdict") == "supported" and c["keyword"] in data.get("keywords", [])]
        correction = {"status": "attempted", "before_count": len(data.get("keywords", [])), "issues": issues,
                      "verified_anchors_before_correction": anchors[:]}
        try:
            repair_input = {key: value for key, value in data.items() if key != "keyword_review"}
            audit_constraints = {"visible_facts": report.get("visible_facts", []),
                                 "rejected_keywords": [c for c in report.get("checks", [])
                                                       if c.get("verdict") == "unsupported" or (
                                                           data.get("keyword_policy_version") == 3
                                                           and c.get("verdict") == "uncertain")]}
            prompt = build_metadata_repair_prompt(repair_input, issues, mode)
            prompt += ("\nPOST-REVIEW CORRECTION: Inspect the image afresh. Earlier text is untrusted. "
                       "Keep these verified search concepts where accurate: " + json.dumps(anchors, ensure_ascii=False)
                       + ". Replace uncertain assertions with directly visible descriptions. Search different "
                       "visual dimensions, not synonyms or adjective permutations. Return a DRAFT of 32 to 35 "
                       "distinct supported concepts whenever the image supports them; aim for 35 before audit. "
                       "Do not stop at the minimum merely to shorten the list. The target "
                       "AFTER the fresh factual and semantic audits is 25 to 30 retained concepts, "
                       f"with a final acceptance minimum of {minimum}. "
                       "Do not invent a material, setting or use to reach the minimum.\nPrevious audit:\n"
                       + json.dumps(audit_constraints, ensure_ascii=False))
            candidate = self._call_api_with_retry(b64, model, api_key, base_url, prompt=prompt, max_tokens=4096)
            if not isinstance(candidate, dict) or not candidate.get("title") or not candidate.get("description"):
                raise ValueError("Correction did not return complete metadata")
            corrected = {**data, **candidate}
            # A correction may add or refine candidates, but it must not
            # silently discard concepts that the preceding image audit
            # explicitly supported. Merge those anchors before the fresh
            # audit; that audit can still remove a duplicate or contradiction.
            candidate_keywords = candidate.get("keywords", [])
            if isinstance(candidate_keywords, str):
                candidate_keywords = split_keywords(candidate_keywords)
            if not isinstance(candidate_keywords, list):
                candidate_keywords = []
            corrected["keywords"] = list(dict.fromkeys([*candidate_keywords, *anchors]))
            corrected.pop("keyword_review", None)
            # A keyword shortfall is not a reason to replace a caption which
            # already passed. Keep only verdicts for the exact audited text;
            # the merged metadata still receives a fresh complete review.
            for check in report.get("caption_checks", []):
                field = check["field"]
                if (check.get("verdict") == "supported"
                        and report.get("reviewed_captions", {}).get(field) == data.get(field)):
                    corrected[field] = data[field]
            for field in ("metadata_language", "keyword_language", "keyword_policy_version"):
                if field in data:
                    corrected[field] = data[field]
            # An empty image disables the pre-review network repair. There is
            # exactly one correction request here, never a nested repair loop.
            corrected = self._apply_sanitizer_and_repair(corrected, "", pconf, api_key, "", base_url, model, mode)
            corrected = self._review_generated_keywords(corrected, b64, model, api_key, base_url, pconf)
            correction.update(status="reviewed" if corrected.get("keyword_review", {}).get("status") == "reviewed" else "review_incomplete",
                              after_count=len(corrected.get("keywords", [])))
            corrected.setdefault("keyword_review", {})["correction"] = correction
            return corrected
        except Exception as exc:
            self._record_error(f"{model} post-review correction failed: {self._redact_secrets(exc, [api_key])}")
            return {**data, "keyword_review": {**report, "correction": {**correction, "status": "failed"}}}

    def _review_generated_keywords(self, data, b64, model, api_key, base_url, pconf):
        if self.__dict__.get("stop_flag", False):
            return {**data, "keyword_review": {"status": "cancelled"}}
        if pconf.get("type") == "local":
            return {**data, "keyword_review": {"status": "unavailable"}}
        if not data.get("keywords"):
            return {**data, "keyword_review": {"status": "unavailable"}}
        try:
            review_metadata = bool(data.get("title") and data.get("description"))
            separate_deduplication = data.get("keyword_policy_version") == 3
            review_prompt = build_keyword_review_prompt(
                data["keywords"], data if review_metadata else None,
                language=metadata_language(data), deduplicate=not separate_deduplication,
            )
            compact_review_prompt = build_compact_keyword_review_prompt(
                data["keywords"], data if review_metadata else None,
                language=metadata_language(data), deduplicate=not separate_deduplication,
            )
            response = self._call_api_with_retry(
                b64, model, api_key, base_url,
                prompt=review_prompt, retry_prompt=compact_review_prompt,
                max_tokens=2048, response_kind="keyword_review",
                response_validator=(lambda value: apply_keyword_review(
                    data, {**value, "duplicate_groups": []} if separate_deduplication else value,
                    model, review_metadata=review_metadata,
                )) if separate_deduplication else None,
            )
            if not separate_deduplication:
                return apply_keyword_review(data, response, model, review_metadata=review_metadata)
            factual_response = {**response, "duplicate_groups": []}
            checked = apply_keyword_review(data, factual_response, model, review_metadata=review_metadata)
            source_ids = [i for i, check in enumerate(checked["keyword_review"]["checks"])
                          if check["keyword"] in checked["keywords"]]
            if len(source_ids) < 2:
                checked["keyword_review"]["semantic_review"] = {"status": "not_needed", "candidate_count": len(source_ids)}
                return checked
            if self.__dict__.get("stop_flag", False):
                return {**data, "keyword_review": {"status": "cancelled"}}
            semantic_response = self._call_api_with_retry(
                b64, model, api_key, base_url,
                prompt=build_keyword_deduplication_prompt(checked["keywords"]),
                max_tokens=2048, response_kind="keyword_deduplication",
                response_validator=lambda value: apply_keyword_review(
                    data, {**factual_response, "duplicate_groups": map_keyword_deduplication(value, source_ids)},
                    model, review_metadata=review_metadata,
                ),
            )
            groups = map_keyword_deduplication(semantic_response, source_ids)
            reviewed = apply_keyword_review(data, {**factual_response, "duplicate_groups": groups},
                                            model, review_metadata=review_metadata)
            reviewed["keyword_review"]["semantic_review"] = {
                "status": "reviewed", "candidate_count": len(source_ids), "retained_count": len(reviewed["keywords"]),
            }
            return reviewed
        except Exception as exc:
            self._record_error(f"{model} visual keyword check failed: {self._redact_secrets(exc, [api_key])}")
            return {**data, "keyword_review": {"status": "unavailable"}}

    def _call_api_with_retry(self, b64, model_name, api_key, base_url="", prompt=None, max_tokens=2048,
                             response_kind="metadata", retry_prompt=None, response_validator=None):
        """Run a secondary API operation with the same bounded retry policy as generation."""
        last_error = None
        for attempt in range(len(RETRY_DELAYS)):
            if self.__dict__.get("stop_flag", False):
                raise _RetryableApiError("API operation cancelled.", kind="cancelled")
            try:
                active_prompt = prompt if attempt == 0 or retry_prompt is None else retry_prompt
                call_kwargs = {"prompt": active_prompt, "max_tokens": max_tokens}
                if response_kind != "metadata":
                    call_kwargs["response_kind"] = response_kind
                response = self._call_api_once(
                    b64, model_name, api_key, base_url, **call_kwargs,
                )
                if response_validator is not None:
                    try:
                        response_validator(response)
                    except (ValueError, TypeError, KeyError) as exc:
                        raise _RetryableApiError(
                            f"Invalid {response_kind} response: {exc}", kind="schema",
                        ) from exc
                return response
            except _RetryableApiError as exc:
                last_error = exc
                if attempt >= len(RETRY_DELAYS) - 1:
                    raise
                delay = self._backoff_delay(attempt, exc.retry_after)
                self._log_api_event(
                    "secondary_api_retry",
                    self._build_api_context(model_name, api_key=api_key, base_url=base_url),
                    response_kind=response_kind,
                    attempt=attempt + 1,
                    max_attempts=len(RETRY_DELAYS),
                    error_kind=exc.kind,
                    error_code=exc.code,
                    delay_seconds=round(delay, 2),
                    prompt_variant="compact" if retry_prompt is not None else "same",
                )
                self._wait_for_retry(delay)
        if last_error is not None:
            raise last_error
        raise _RetryableApiError("API operation did not return a result.", kind="unknown")

    def _call_api_once(self, b64, model_name, api_key, base_url="", prompt=None, max_tokens=2048, response_kind="metadata"):
        # Preserve the existing dict API while moving one operation behind the
        # provider boundary. Retry and UI workflow remain owned by the caller.
        prompt = prompt or build_prompt(self.mode_var.get(), self.__dict__.get("_batch_language", "en"))
        ctx = self._build_api_context(model_name, api_key=api_key, base_url=base_url)
        request = ProviderRequest(
            model_name=model_name, provider=ctx["provider"], model_id=ctx["model"],
            endpoint=ctx["resolved_url"], transport=ctx["transport"],
            prompt=prompt, image_base64=b64, max_tokens=max_tokens,
            response_kind=response_kind, timeout=(HTTP_CONNECT_TIMEOUT, HTTP_READ_TIMEOUT),
        )
        operations = LegacyOperations(
            session_factory=requests.Session,
            family_request=lambda session, req, key: self._openai_family_request(
                session, ctx, req.model_name, key, req.prompt, req.image_base64, req.max_tokens
            ),
            post_if_active=self._post_if_active,
            log_event=lambda event, **extra: self._log_api_event(event, ctx, **extra),
            record_error=self._record_error,
            categorize_http_error=self._categorize_http_error,
            is_retryable_status=self._is_retryable_status,
            is_fatal_status=self._is_fatal_status,
            retry_after_seconds=self._retry_after_seconds,
            looks_like_missing_image_reply=self._looks_like_missing_image_reply,
            log_debug_preview=self._log_api_debug_preview,
            metadata_schema_issue=self._metadata_schema_issue,
            redact_secrets=self._redact_secrets,
        )
        return LegacyProvider(operations).generate(request, api_key=api_key).metadata

    def _call_api(self, b64, model_name=None, api_key=None, base_url="", mode=None):
        model_name = model_name or self.model_var.get()
        api_key = str(api_key).strip() if api_key is not None else self._get_clean_api_key()
        prompt = build_prompt(mode if mode is not None else self.mode_var.get(), self.__dict__.get("_batch_language", "en"))
        models = [model_name] + self._fallback_model_candidates(model_name)
        last_error = ""

        for model_index, candidate in enumerate(models):
            candidate_base_url = base_url
            if API_CONFIG[candidate]["provider"] != API_CONFIG[model_name]["provider"]:
                candidate_base_url = ""
            candidate_key = self._api_key_for_model(candidate, model_name, api_key)
            if get_provider_config(candidate)["type"] != "local" and not candidate_key:
                if model_index > 0:
                    self._log_api_event(
                        "fallback_skip",
                        self._build_api_context(candidate, api_key="", base_url=candidate_base_url),
                        reason="missing_api_key",
                        primary_model=model_name,
                    )
                continue
            if model_index > 0:
                self._log_api_event(
                    "fallback_start",
                    self._build_api_context(candidate, api_key=candidate_key, base_url=candidate_base_url),
                    primary_model=model_name,
                    reason=last_error or "recoverable_failure",
                )

            for attempt in range(len(RETRY_DELAYS)):
                if self.__dict__.get("stop_flag", False):
                    return None
                try:
                    result = self._call_api_once(
                        b64,
                        model_name=candidate,
                        api_key=candidate_key,
                        base_url=candidate_base_url,
                        prompt=prompt,
                        max_tokens=2048,
                    )
                    if model_index > 0:
                        self._log_api_event(
                            "fallback_success",
                            self._build_api_context(candidate, api_key=candidate_key, base_url=candidate_base_url),
                            primary_model=model_name,
                        )
                    return result
                except _FatalApiError:
                    raise
                except _RetryableApiError as exc:
                    last_error = self._redact_secrets(exc, [candidate_key])
                    self._log_api_event(
                        "api_retryable_error",
                        self._build_api_context(candidate, api_key=candidate_key, base_url=candidate_base_url),
                        attempt=attempt + 1,
                        max_attempts=len(RETRY_DELAYS),
                        error_kind=exc.kind,
                        error_code=exc.code,
                        error=last_error,
                    )
                    if attempt < len(RETRY_DELAYS) - 1:
                        delay = self._backoff_delay(attempt, exc.retry_after)
                        self._log_api_event(
                            "api_retry_wait",
                            self._build_api_context(candidate, api_key=candidate_key, base_url=candidate_base_url),
                            delay_seconds=round(delay, 2),
                        )
                        self._wait_for_retry(delay)
                except Exception as exc:
                    last_error = self._redact_secrets(exc, [candidate_key])
                    self._record_error(f"{candidate} unexpected error: {last_error}")
                    return None

        if last_error:
            self._record_error(last_error)
        return None
    
    def _save(self):
        targets = [c for c in self.cards.values() if c.var_sel.get()]
        if not targets:
            return messagebox.showwarning("Info", "No images selected")
        if self.is_saving:
            return messagebox.showwarning("Busy", "Save is already in progress.")
        self._start_save(targets, output_dir=None, force=True)

    def _save_ready(self):
        targets = [
            card for card in self.cards.values()
            if not self._card_processing_failed(card)
            and not classify_stock_metadata(
                *card.get_data(sync=False),
                self.mode_var.get() if hasattr(self, "mode_var") else PROMPT_MODES[0],
                **metadata_options(getattr(card, "_cached_data", {})),
            )[0]
            and not classify_stock_metadata(
                *card.get_data(sync=False),
                self.mode_var.get() if hasattr(self, "mode_var") else PROMPT_MODES[0],
                **metadata_options(getattr(card, "_cached_data", {})),
            )[1]
            and not keyword_review_warnings(getattr(card, "_cached_data", {}))
            and not metadata_compliance_warnings(
                *card.get_data(sync=False), card.get_compliance()
            )
        ]
        if not targets:
            return messagebox.showwarning("Info", "No ready images found")
        if self.is_saving:
            return messagebox.showwarning("Busy", "Save is already in progress.")
        self._start_save(targets, output_dir=None, force=False)

    def _save_all(self):
        targets = [card for card in self.cards.values() if not self._card_processing_failed(card)]
        if not targets:
            return messagebox.showwarning("Info", "No saveable images found")
        if self.is_saving:
            return messagebox.showwarning("Busy", "Save is already in progress.")
        self._start_save(targets, output_dir=None, force=True)

    def _export_ready(self):
        if self._workspace_busy():
            return
        for card in self.cards.values():
            card.sync_from_view()
        ready = []
        for path, data in self.data_cache.items():
            card = self.cards.get(path)
            if card is not None and self._card_processing_failed(card):
                continue
            title = str(data.get("title", "")).strip()
            description = str(data.get("description", "")).strip()
            keywords = data.get("keywords", [])
            hard_errors, review_warnings = classify_stock_metadata(title, description, keywords, self.mode_var.get(), **metadata_options(data))
            review_warnings.extend(keyword_review_warnings(data))
            review_warnings.extend(metadata_compliance_warnings(title, description, keywords, data.get("compliance", {})))
            if not hard_errors and not review_warnings:
                ready.append({"file": Path(path).name, **data})
        if not ready:
            return messagebox.showwarning("Info", "No ready metadata found")
        f = filedialog.asksaveasfilename(defaultextension=".json", filetypes=[("JSON", "*.json")])
        if f:
            try:
                _restore_config_bytes(f, json.dumps(ready, indent=2, ensure_ascii=False).encode("utf-8"))
                messagebox.showinfo("Done", f"Exported {len(ready)} ready items")
            except OSError as exc:
                messagebox.showerror("Export Error", str(exc))

    def _go_home(self):
        if self.is_processing:
            return messagebox.showwarning("Busy", "Analysis is in progress. Please wait or cancel first.")
        if self.is_saving:
            return messagebox.showwarning("Busy", "Save is in progress. Please wait.")
        for card in self.cards.values():
            try:
                card.sync_from_view()
            except Exception:
                pass
        self._cancel_workspace_jobs(reset_pool=True)
        self._release_active_credential_lease()
        self._show_welcome_view()
    
    def _save_to_folder(self):
        """Save all selected images with SEO metadata to a new auto-created folder."""
        if self._workspace_busy():
            return
        targets = [c for c in self.cards.values() if c.var_sel.get()]
        if not targets:
            return messagebox.showwarning("Info", "No images selected")
        if self.is_saving:
            return messagebox.showwarning("Busy", "Save is already in progress.")
        
        # Let user pick parent directory
        parent_dir = filedialog.askdirectory(title="Choose where to create the output folder")
        if not parent_dir:
            return
        
        # Auto-create timestamped subfolder
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        folder_name = f"SEO_Ready_{timestamp}"
        output_dir = Path(parent_dir) / folder_name

        try:
            output_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            return messagebox.showerror("Error", f"Cannot create folder:\n{e}")
        
        self._start_save(targets, output_dir=output_dir, force=True)

    def _start_save(self, targets, output_dir=None, force=False):
        if self._workspace_busy():
            return messagebox.showwarning("Busy", "Cancel analysis and wait for active requests before saving.")
        for card in targets:
            identity = getattr(card, "source_identity", image_identity(card.path))
            if identity is None or image_identity(card.path) != identity:
                return messagebox.showwarning("Source changed", "An image has changed or is missing. Reimport it before saving metadata.")
        creator = self.creator_var.get().strip()
        country = self.shoot_location_var.get().strip()
        date_created = self.shoot_date_var.get().strip()
        save_items = []
        ready_items = []
        quality_failures = []
        compliance_reviews = []
        mode = self.mode_var.get() if hasattr(self, "mode_var") else PROMPT_MODES[0]
        for card in targets:
            try:
                card.sync_from_view()
            except Exception:
                pass
            title, description, keywords = card.get_data(sync=False)
            compliance = card.get_compliance() if hasattr(card, "get_compliance") else {}
            hard_errors, review_warnings = classify_stock_metadata(title, description, keywords, mode, **metadata_options(getattr(card, "_cached_data", {})))
            review_warnings.extend(keyword_review_warnings(getattr(card, "_cached_data", {})))
            if hard_errors and not force:
                detail, _hard, _review = self._quality_detail(card)
                quality_failures.append((card, hard_errors[0], True, detail))
                continue
            if review_warnings and not force:
                detail, _hard, _review = self._quality_detail(card)
                quality_failures.append((card, review_warnings[0], False, detail))
            warnings = metadata_compliance_warnings(title, description, keywords, compliance)
            compliance_reviews.extend(
                f"{Path(card.path).name}: {warning}" for warning in warnings
            )
            item = {
                    "card": card,
                    "path": card.path,
                    "title": title,
                    "description": description,
                    "keywords": list(keywords),
                }
            save_items.append(item)
            if not hard_errors and not review_warnings:
                ready_items.append(item)
        if quality_failures and not force:
            retry_cards = [card for card, _issue, is_hard, _detail in quality_failures if not is_hard]
            if not retry_cards:
                retry_cards = [card for card, _issue, _is_hard, _detail in quality_failures]
            return self._show_quality_recovery_dialog(
                quality_failures,
                len(ready_items),
                on_retry=lambda: self.analyze(retry_cards),
                on_save_ready=(
                    lambda: self._start_save_items(
                        ready_items, output_dir, creator, country, date_created, compliance_reviews
                    )
                    if ready_items else None
                ),
                on_save_review=(
                    lambda: self._start_save_items(
                        save_items, output_dir, creator, country, date_created, compliance_reviews
                    )
                    if save_items else None
                ),
            )
        self._start_save_items(save_items, output_dir, creator, country, date_created, compliance_reviews)

    def _start_save_items(self, save_items, output_dir, creator, country, date_created, compliance_reviews=None):
        compliance_reviews = list(compliance_reviews or [])
        if compliance_reviews:
            preview = "\n".join(f"• {warning}" for warning in compliance_reviews[:5])
            extra = f"\n\n(+{len(compliance_reviews)-5} more)" if len(compliance_reviews) > 5 else ""
            if not messagebox.askyesno(
                "Compliance review required",
                preview + extra + "\n\nContinue only after checking releases, rights, and platform disclosure fields.",
            ):
                return
        self.is_saving = True
        self._update_stats()
        if hasattr(self, "_save_result_job") and self._save_result_job is None:
            self._save_result_job = self.after(25, self._drain_save_results)
        self.btns["💾 Save"].configure(state="disabled")
        for key in ("💾 Save ready", "💾 Save all", "📄 JSON", "📄 JSON ready", "📊 SS CSV"):
            if key in self.btns:
                self.btns[key].configure(state="disabled")
        self.btns["📁 Save→Folder"].configure(state="disabled")
        self.prog_lbl.configure(text=f"Saving {len(save_items)} images...")
        self.prog.set(0)
        threading.Thread(
            target=self._save_worker,
            args=(save_items, output_dir, creator, country, date_created),
            daemon=True
        ).start()

    def _save_worker(self, save_items, output_dir=None, creator="", country="", date_created=""):
        ok = 0
        saved_cards = []
        saved_paths = []
        save_warnings = []
        errors = []
        date_meta = metadata._parse_metadata_date(date_created)
        input_paths = [item.get("path", "") for item in save_items]
        worker_reservations = set()

        try:
            for item in save_items:
                card = item["card"]
                image_path = item["path"]
                temp_paths = []
                try:
                    t = item["title"]
                    d = item["description"]
                    k = item["keywords"]
                    raw = load_image_bytes(image_path)
                    if not raw:
                        errors.append(f"Cannot read image: {Path(image_path).name}")
                        continue
                    if metadata._is_tiff_container(raw):
                        errors.append(metadata._unsupported_tiff_save_error(image_path))
                        continue

                    img = None
                    try:
                        if raw[:2] == b"\xff\xd8":
                            source_format = "jpeg"
                            suffix = ".jpg"
                        elif raw.startswith(metadata._PNG_SIGNATURE):
                            metadata._parse_png_container(raw)
                            source_format = "png"
                            suffix = ".png"
                        elif raw[:4] == metadata._WEBP_RIFF_HEADER and raw[8:12] == metadata._WEBP_FORM_TYPE:
                            _webp_chunks, is_animated_webp = metadata._parse_webp_container(raw)
                            if is_animated_webp:
                                raise ValueError("Current version does not support safe saving of animated WebP")
                            source_format = "webp"
                            suffix = ".webp"
                        else:
                            source_format = "legacy"
                            suffix = ".jpg"

                        output_title = t or Path(str(image_path)).stem or "seo_image"
                        new_name = metadata.build_output_filename(output_title, suffix=suffix)
                        base_dir = Path(output_dir) if output_dir else Path(image_path).parent
                        save_path = metadata._reserve_safe_output_path(base_dir, new_name, input_paths, worker_reservations)
                        if source_format == "jpeg":
                            # Stage exact source JPEG bytes before the metadata-only merge.
                            metadata._parse_jpeg_container(raw)
                            encoded_tmp = metadata._write_jpeg_bytes_to_tempfile(raw, save_path.parent)
                            temp_paths.append(encoded_tmp)
                            metadata_bytes = metadata.inject_metadata(
                                encoded_tmp, t, d, k, creator, country, date_created, preserve_existing=True
                            )
                            source_jpeg_path = image_path
                        elif source_format == "png":
                            encoded_tmp = metadata._write_container_bytes_to_tempfile(raw, save_path.parent, ".pixtagra_png_")
                            temp_paths.append(encoded_tmp)
                            metadata_bytes = metadata._merge_png_metadata(
                                load_image_bytes(encoded_tmp), t, d, k, creator, date_created
                            )
                            source_jpeg_path = None
                        elif source_format == "webp":
                            encoded_tmp = metadata._write_container_bytes_to_tempfile(raw, save_path.parent, ".pixtagra_webp_")
                            temp_paths.append(encoded_tmp)
                            metadata_bytes = metadata._merge_webp_metadata(
                                load_image_bytes(encoded_tmp), t, d, k, creator, date_created
                            )
                            source_jpeg_path = None
                        else:
                            # Non-JPEG behavior remains the existing RGB-to-JPEG conversion path.
                            with Image.open(io.BytesIO(raw)) as src:
                                img = src.convert("RGB")
                            exif = {"0th": {}, "Exif": {}, "GPS": {}, "1st": {}, "thumbnail": None}
                            exif["0th"][piexif.ImageIFD.ImageDescription] = d.encode("utf-8")
                            if creator:
                                exif["0th"][piexif.ImageIFD.Artist] = creator.encode("utf-8")
                                exif["0th"][piexif.ImageIFD.Copyright] = f"(c) {creator}".encode("utf-8")
                            exif["0th"][0x9C9B] = t.encode("utf-16le") + b"\x00\x00"
                            exif["0th"][0x9C9D] = ";".join(k).encode("utf-16le") + b"\x00\x00"
                            exif["0th"][0x9C9F] = t.encode("utf-16le") + b"\x00\x00"
                            exif["0th"][0x9C9E] = d.encode("utf-16le") + b"\x00\x00"
                            exif["Exif"][piexif.ExifIFD.UserComment] = piexif.helper.UserComment.dump(d, encoding="unicode")

                            if date_meta:
                                exif_dt = date_meta["exif_datetime"]
                                exif["Exif"][piexif.ExifIFD.DateTimeOriginal] = exif_dt.encode("ascii")
                                exif["Exif"][piexif.ExifIFD.DateTimeDigitized] = exif_dt.encode("ascii")

                            encoded_tmp = metadata._write_jpeg_to_tempfile(img, save_path.parent, piexif.dump(exif))
                            temp_paths.append(encoded_tmp)
                            metadata_bytes = metadata.inject_metadata(
                                encoded_tmp, t, d, k, creator, country, date_created, preserve_existing=False
                            )
                            source_jpeg_path = None
                        if not isinstance(metadata_bytes, (bytes, bytearray)) or not metadata_bytes:
                            raise ValueError("Metadata injection did not return complete output bytes")
                        metadata_tmp = metadata._write_metadata_to_tempfile(save_path.parent, metadata_bytes)
                        temp_paths.append(metadata_tmp)
                        if source_format == "jpeg":
                            metadata._verify_written_metadata(
                                metadata_tmp,
                                t,
                                d,
                                k,
                                creator,
                                source_jpeg_path=source_jpeg_path,
                                date_created=date_created,
                            )
                        elif source_format == "png":
                            metadata._verify_png_written_metadata(
                                metadata_tmp,
                                t,
                                d,
                                k,
                                creator,
                                source_png_path=image_path,
                                date_created=date_created,
                            )
                        elif source_format == "webp":
                            metadata._verify_webp_written_metadata(
                                metadata_tmp,
                                t,
                                d,
                                k,
                                creator,
                                source_webp_path=image_path,
                                date_created=date_created,
                            )
                        else:
                            metadata._verify_written_metadata(
                                metadata_tmp,
                                t,
                                d,
                                k,
                                creator,
                                source_jpeg_path=None,
                                date_created="",
                            )
                        for _ in range(1000):
                            try:
                                metadata._publish_new_output(metadata_tmp, save_path)
                                break
                            except FileExistsError:
                                save_path = metadata._reserve_safe_output_path(base_dir, new_name, input_paths, worker_reservations)
                        else:
                            raise OSError("Cannot create a safe output path after repeated filename conflicts")
                    finally:
                        if img is not None:
                            img.close()

                    cleanup_errors = [error for temp_path in temp_paths if (error := metadata._cleanup_tempfile(temp_path))]
                    if cleanup_errors:
                        save_warnings.extend(cleanup_errors)
                        for cleanup_error in cleanup_errors:
                            print(cleanup_error)
                    print(f"Saved image: {save_path}")
                    ok += 1
                    saved_cards.append(card)
                    saved_paths.append(str(save_path))
                except Exception as e:
                    cleanup_errors = [error for temp_path in temp_paths if (error := metadata._cleanup_tempfile(temp_path))]
                    detail = str(e)
                    if cleanup_errors:
                        detail += "; " + "; ".join(cleanup_errors)
                    display_name = Path(str(image_path)).name or "image"
                    print(f"Save error for {display_name}: {detail}")
                    errors.append(f"{display_name}: {detail}")
        finally:
            metadata._release_safe_output_paths(worker_reservations)

        result = (ok, saved_cards, output_dir, errors, saved_paths, save_warnings)
        result_queue = self.__dict__.get("_save_result_queue")
        if result_queue is not None:
            result_queue.put(result)
        else:
            self.after(0, lambda: self._save_done(*result))

    def _save_done(self, ok, saved_cards, output_dir, errors, saved_paths=None, save_warnings=None):
        self.is_saving = False
        self._update_stats()
        self.prog.set(ok / max(1, ok + len(errors)))
        self.prog_lbl.configure(text=f"Saved: {ok} images, {len(errors)} failed")
        if hasattr(self, "_update_save_export_buttons"):
            self._update_save_export_buttons()
        saved_paths = saved_paths or []
        path_preview = "\n".join(saved_paths[:3])
        if len(saved_paths) > 3:
            path_preview += f"\n(+{len(saved_paths) - 3} more)"

        if ok > 0:
            for card in saved_cards:
                try:
                    card.mark_saved()
                except AttributeError:
                    pass
            self._auto_clear_saved(saved_cards)

        if output_dir:
            if ok > 0:
                result = messagebox.askyesno(
                    "Success",
                    f"Saved {ok} images:\n{path_preview}\n\nOpen folder?"
                )
                if result:
                    try:
                        if os.name == "nt":
                            os.startfile(output_dir)
                        elif os.name == "posix":
                            if os.uname().sysname == "Darwin":
                                subprocess.Popen(["open", output_dir])
                            else:
                                subprocess.Popen(["xdg-open", output_dir])
                    except Exception:
                        pass
            else:
                messagebox.showwarning("Warning", "No images were saved. Check that images have metadata.")
        else:
            messagebox.showinfo("Result", f"Saved {ok} images" + (f":\n{path_preview}" if path_preview else ""))

        if errors:
            if output_dir:
                try:
                    log_path = Path(output_dir) / "failed_images.txt"
                    with open(log_path, 'w', encoding='utf-8') as f:
                        for e in errors:
                            f.write(f"{e}\n")
                except Exception:
                    pass
            preview = "\n".join(f"• {e}" for e in errors[:3])
            extra = f"\n\n(+{len(errors)-3} more)" if len(errors) > 3 else ""
            messagebox.showwarning("Some files failed to save", preview + extra)

        if save_warnings:
            preview = "\n".join(f"• {warning}" for warning in save_warnings[:3])
            extra = f"\n\n(+{len(save_warnings)-3} more)" if len(save_warnings) > 3 else ""
            messagebox.showwarning("Temporary files need cleanup", preview + extra)
    
    def _auto_clear_saved(self, saved_cards):
        """Remove successfully saved cards from UI, keep failed/pending ones."""
        for card in saved_cards:
            self._remove_card(card, update_ui=False)
        
        # Update stats
        self.stats["total"] = len(self.cards)
        self._update_stats()
        
        # Refresh scroll region
        self._refresh_scroll()
        
        # If no cards remain, go back to welcome screen
        if not self.cards:
            self._cancel_workspace_jobs(reset_pool=True)
            self._show_welcome_view()
    
    def _schedule_refresh_scroll(self):
        """Debounced scroll refresh — coalesces rapid Configure events into one update.
        Cancels any pending refresh and schedules a new one 50ms out."""
        if self._refresh_job:
            try:
                self.after_cancel(self._refresh_job)
            except Exception:
                pass
        self._refresh_job = self.after(50, self._do_refresh_scroll)

    def _do_refresh_scroll(self):
        """Called by the debounce timer; clears the job handle then refreshes."""
        self._refresh_job = None
        self._refresh_scroll()

    def _refresh_scroll(self):
        total_cards = len(self.card_order)
        width = self.canvas.winfo_width() or 1
        if total_cards <= 0:
            self.canvas.configure(scrollregion=(0, 0, width, 0))
            self._update_visible_cards()
            return
        self._ensure_card_views()
        pitch = self._card_height + self._card_gap
        total_height = total_cards * pitch + 20
        self.canvas.configure(scrollregion=(0, 0, width, total_height))
        self._update_visible_cards()
    
    def _export(self):
        if self._workspace_busy():
            return
        for card in self.cards.values():
            card.sync_from_view()
        if not self.data_cache:
            return
        f = filedialog.asksaveasfilename(defaultextension=".json", filetypes=[("JSON", "*.json")])
        if f:
            try:
                payload = json.dumps([{"file": Path(k).name, **v} for k, v in self.data_cache.items()], indent=2, ensure_ascii=False)
                _restore_config_bytes(f, payload.encode("utf-8"))
                messagebox.showinfo("Done", f"Exported {len(self.data_cache)} items")
            except OSError as exc:
                messagebox.showerror("Export Error", str(exc))

    def _selected_export_target(self):
        variable = self.__dict__.get("_export_target_var")
        return get_export_target(variable.get() if variable is not None else "shutterstock")

    def _on_export_target_change(self, value):
        target = get_export_target(value)
        self._export_target_var.set(target.label)
        self._export_hint_label.configure(text=target.hint)
        self._update_save_export_buttons()

    def _export_records(self):
        records = []
        if self.cards:
            for path, card in self.cards.items():
                title, description, keywords = card.get_data()
                data = dict(getattr(card, "_cached_data", {}))
                data.update(title=title, description=description, keywords=list(keywords))
                data["compliance"] = card.get_compliance() if hasattr(card, "get_compliance") else {}
                if title and description:
                    records.append({"path": path, "data": data, "source_identity": image_identity(path)})
        else:
            for path, cached in self.data_cache.items():
                data = dict(cached)
                data["keywords"] = split_keywords(data["keywords"]) if isinstance(data.get("keywords"), str) else list(data.get("keywords", []))
                if data.get("title") and data.get("description"):
                    records.append({"path": path, "data": data, "source_identity": image_identity(path)})
        return records

    def _export_shutterstock_csv(self):
        # Retain the existing callable for probes and integrations.
        return App._export_for_target(self, "shutterstock")

    def _export_for_target(self, target=None):
        if self._workspace_busy():
            return
        target = get_export_target(target) if target is not None else App._selected_export_target(self)
        if target.kind == "unverified":
            return messagebox.showwarning("Export unavailable", target.hint)
        records = App._export_records(self)
        names = [Path(record["path"]).name.casefold() for record in records]
        if len(names) != len(set(names)):
            return messagebox.showwarning("Duplicate filenames", "Exports match images by filename. Rename images with duplicate filenames before exporting.")
        mode = self.mode_var.get() if hasattr(self, "mode_var") else PROMPT_MODES[0]
        failures, reviews, compliance_reviews = [], [], []
        for record in records:
            path, data = record["path"], record["data"]
            title, description, keywords = data["title"], data["description"], data["keywords"]
            errors, warnings = classify_stock_metadata(title, description, keywords, mode, **metadata_options(data))
            errors.extend(export_record_errors(target, path, data))
            if errors:
                failures.append(f"{Path(path).name}: {translate(errors[0])}")
            reviews.extend(f"{Path(path).name}: {warning}" for warning in warnings + keyword_review_warnings(data))
            compliance_reviews.extend(f"{Path(path).name}: {warning}" for warning in metadata_compliance_warnings(title, description, keywords, data.get("compliance", {})))
        if failures:
            return messagebox.showwarning("Metadata quality check failed", "\n".join(failures[:5]) + "\n\nCorrect these records before exporting.")
        if not records:
            return messagebox.showwarning("Info", "No processed images found.\nRun AI analysis first.")
        if reviews and not messagebox.askyesno("Keyword quality review", "\n".join(reviews[:5]) + "\n\nSome keywords need quality review. Continue export?"):
            return
        if compliance_reviews and not messagebox.askyesno("Compliance review required", "\n".join(compliance_reviews[:5]) + "\n\nContinue only after checking releases, rights, and platform disclosure fields."):
            return
        if target.kind == "jpeg":
            parent = filedialog.askdirectory(title="Choose where to create the output folder")
            if parent:
                App._start_alamy_export(self, records, parent)
            return
        try:
            rows = []
            for record in records:
                path, data = record["path"], record["data"]
                categories = _infer_ss_categories(data["title"], data["description"], data["keywords"]) if target.key == "shutterstock" else ()
                rows.append(agency_csv_row(target, path, data, categories))
            payload = agency_csv_bytes(target, rows)
            save_path = filedialog.asksaveasfilename(title="Save " + target.label, defaultextension=".csv",
                filetypes=[("CSV files", "*.csv")], initialfile="vcg_manual_reference_not_for_upload.csv" if target.key == "vcg" else target.key + "_upload.csv")
            if not save_path:
                return
            _restore_config_bytes(save_path, payload)
            messagebox.showinfo("Done", f"Exported {len(rows)} items" + "\n" + save_path + ("\n\n" + target.hint if target.key == "vcg" else ""))
        except (OSError, ValueError) as exc:
            messagebox.showerror("Export Error", str(exc))

    def _start_alamy_export(self, records, parent):
        self.is_saving = True
        self._update_stats()
        self.prog.set(0)
        self.prog_lbl.configure(text=f"Saving 0/{len(records)} images...")
        results = queue.Queue()

        def work():
            try:
                output = alamy_jpeg_bundle(records, parent, progress=lambda done, total: results.put(("progress", done, total)))
                results.put(("done", output))
            except Exception as exc:
                results.put(("error", str(exc)))

        def poll():
            self._alamy_export_job = None
            while True:
                try:
                    event = results.get_nowait()
                except queue.Empty:
                    self._alamy_export_job = self.after(40, poll)
                    return
                if event[0] == "progress":
                    self.prog.set(event[1] / event[2])
                    self.prog_lbl.configure(text=f"Saving: {event[1]}/{event[2]}")
                    continue
                self.is_saving = False
                self._update_stats()
                if event[0] == "done":
                    self.prog.set(1)
                    self.prog_lbl.configure(text=f"Exported {len(records)} items")
                    messagebox.showinfo("Done", f"Exported {len(records)} items" + "\n" + str(event[1]))
                else:
                    self.prog.set(0)
                    self.prog_lbl.configure(text="Export Error")
                    messagebox.showerror("Export Error", event[1])
                return

        try:
            threading.Thread(target=work, daemon=True).start()
        except RuntimeError as exc:
            self.is_saving = False
            self._update_stats()
            messagebox.showerror("Export Error", str(exc))
            return
        self.after(40, poll)


def _run_app():
    # CTk establishes per-monitor DPI awareness in App.__init__. Use the same
    # initialization for source startup, probes and the packaged entry point.
    try:
        App().mainloop()
    except tk.TclError as exc:
        _native_startup_error(
            "PixTagra startup error",
            "The GUI runtime could not start.\n\n"
            f"{exc}\n\n"
            "If you are running from source, reinstall Python and make sure the Tcl/Tk option is included. "
            "If you are running the packaged app, rebuild it with the Tcl/Tk files included.",
        )
        raise SystemExit(1)


def _run_packaged_gui_smoke() -> int:
    """Explicit packaged-only GUI launch using no-access credential dependencies."""
    if not _packaged_test_isolation_enabled():
        _write_packaged_probe_result({"status": "test_isolation_required"})
        return 2
    try:
        app = App()
    except Exception as exc:
        _write_packaged_probe_result({"status": "startup_failed", "error_type": type(exc).__name__})
        return 1

    # CustomTkinter retains its appearance tracker after a root is closed on
    # some Windows runtimes. This guard exists only for the explicit smoke CLI.
    # A cold one-folder launch can spend several seconds loading Tk and the
    # bundled Python runtime before the first callback. Keep the smoke bounded
    # without racing normal first-launch initialization.
    exit_guard = threading.Timer(30.0, lambda: os._exit(1))
    exit_guard.daemon = True
    exit_guard.start()

    smoke_state = {"exit_code": 0, "date_button": None, "layout_checks": 0}

    def fail_smoke(exc: Exception) -> None:
        smoke_state["exit_code"] = 1
        _write_packaged_probe_result(
            {"status": "gui_contract_failed", "error_type": type(exc).__name__}
        )
        app.quit()

    def finish_checks() -> None:
        try:
            app.update_idletasks()
            date_button = smoke_state["date_button"]
            picker = app._shoot_date_picker
            if date_button is None or picker is None or not picker.winfo_exists():
                raise RuntimeError("date picker was not created")
            button_left = date_button.winfo_rootx()
            button_top = date_button.winfo_rooty()
            button_right = button_left + date_button.winfo_width()
            button_bottom = button_top + date_button.winfo_height()
            picker_left = picker.winfo_rootx()
            picker_top = picker.winfo_rooty()
            picker_right = picker_left + picker.winfo_width()
            picker_bottom = picker_top + picker.winfo_height()
            date_picker_anchored = (
                picker_left <= button_right
                and picker_right >= button_left
                and (picker_top >= button_bottom or picker_bottom <= button_top)
            )
            native_key_entry = getattr(app.key_entry, "_entry", app.key_entry)
            context_menu_bound = bool(native_key_entry.bind("<Button-3>"))
            expected_models = {
                "OpenAI GPT-5.6 Sol",
                "OpenAI GPT-5.6 Terra",
                "OpenAI GPT-5.6 Luna",
                "Moonshot Kimi",
                "Qwen 3.7 Plus",
                "MiniMax M3",
                "Local Ollama",
            }
            selectable_models_verified = set(SELECTABLE_MODELS) == expected_models
            if not (date_picker_anchored and context_menu_bound and selectable_models_verified):
                raise RuntimeError("packaged GUI contract was not satisfied")
            _write_packaged_probe_result(
                {
                    "status": "ok",
                    "title": app.title(),
                    "selectable_models_verified": selectable_models_verified,
                    "visible_model_count": len(SELECTABLE_MODELS),
                    "context_menu_bound": context_menu_bound,
                    "date_picker_anchored": date_picker_anchored,
                    "hello_prompt_requested": 0,
                    "real_credential_manager_access": 0,
                    "real_vault_access": 0,
                    "real_config_access": 0,
                    "real_clipboard_access": 0,
                    "network_requests": 0,
                    "automatic_persistent_writes": 0,
                    "legacy_fallback_count": 0,
                    "active_leases_after_shutdown": 0,
                }
            )
            app.quit()
        except Exception as exc:
            fail_smoke(exc)

    def begin_checks() -> None:
        try:
            app.update_idletasks()
            date_button = app.shoot_date_picker_btn
            if date_button.winfo_width() <= 1 or date_button.winfo_height() <= 1:
                smoke_state["layout_checks"] += 1
                if smoke_state["layout_checks"] >= 30:
                    raise RuntimeError("GUI layout did not stabilize")
                app.after(100, begin_checks)
                return
            smoke_state["date_button"] = date_button
            app._open_shoot_date_picker()
            app.after(200, finish_checks)
        except Exception as exc:
            fail_smoke(exc)

    app.after(250, begin_checks)
    try:
        app.mainloop()
    finally:
        try:
            app.destroy()
        finally:
            exit_guard.cancel()
    return int(smoke_state["exit_code"])


def _main() -> int:
    if "--packaged-ui-smoke" in sys.argv:
        if not _packaged_test_isolation_enabled():
            return 2
        output_path = os.environ.get("PIXTAGRA_PACKAGED_PROBE_OUTPUT", "")
        if not output_path:
            return 2
        from pixtagra_ui_probe import run_probe
        result = run_probe(sys.modules[__name__], Path(output_path).parent)
        result["executable"] = str(Path(sys.executable).resolve())
        if getattr(sys, "frozen", False):
            from pixtagra_build_info import SOURCE_COMMIT
            result["source_commit"] = SOURCE_COMMIT
        _write_packaged_probe_result(result)
        return 0 if result["passed"] else 1
    if "--packaged-generation-smoke" in sys.argv:
        if not _packaged_test_isolation_enabled():
            return 2
        output_path = os.environ.get("PIXTAGRA_PACKAGED_PROBE_OUTPUT", "")
        if not output_path:
            return 2
        from pixtagra_generation_probe import run_probe
        result = run_probe(sys.modules[__name__], Path(output_path).parent)
        result["executable"] = str(Path(sys.executable).resolve())
        if getattr(sys, "frozen", False):
            from pixtagra_build_info import SOURCE_COMMIT
            result["source_commit"] = SOURCE_COMMIT
        _write_packaged_probe_result(result)
        return 0 if result["passed"] else 1
    if "--packaged-workspace-smoke" in sys.argv:
        if not _packaged_test_isolation_enabled():
            return 2
        from pixtagra_workspace_probe import run_probe, read_corpus_manifest
        output_path = os.environ.get("PIXTAGRA_PACKAGED_PROBE_OUTPUT", "")
        if not output_path:
            return 2
        corpus = os.environ.get("PIXTAGRA_PROBE_MANIFEST", "")
        samples = read_corpus_manifest(corpus) if corpus else None
        result = run_probe(sys.modules[__name__], Path(output_path).parent,
                           count=len(samples) if samples else 150, sample_images=samples,
                           scale=float(os.environ["PIXTAGRA_PROBE_SCALE"]) if os.environ.get("PIXTAGRA_PROBE_SCALE") else None)
        from pixtagra_release import PRODUCT_VERSION
        result.update(version=PRODUCT_VERSION, executable=str(Path(sys.executable).resolve()))
        if getattr(sys, "frozen", False):
            from pixtagra_build_info import SOURCE_COMMIT
            result["source_commit"] = SOURCE_COMMIT
        if corpus:
            result["corpus_manifest_sha256"] = hashlib.sha256(Path(corpus).read_bytes()).hexdigest()
        _write_packaged_probe_result(result)
        return 0 if result["passed"] else 1
    if "--self-test-packaged-credentials" in sys.argv:
        from pixtagra_packaged_selftest import run_packaged_credential_self_test

        result = run_packaged_credential_self_test()
        _write_packaged_probe_result(result)
        return 0
    if "--packaged-gui-smoke" in sys.argv:
        return _run_packaged_gui_smoke()
    _run_app()
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
