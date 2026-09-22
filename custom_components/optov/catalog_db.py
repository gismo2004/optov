"""All access to the controller catalog.

Read-only SQLite queries against the catalog a config entry was set up with: controller
identification, datapoint definitions, translations, display conditions and the profile the
platforms are built from. Nothing here writes to the catalog, and nothing else in the
integration opens it.
"""

import logging
import os
import re
import sqlite3
from contextlib import closing, suppress
from dataclasses import dataclass, field
from typing import Any

try:
    # Real package context (Home Assistant importing custom_components.optov.catalog_db).
    from .conversions import schedule_type
    from .decode import decodes_to_integer, decodes_to_number, raw_bounds
except ImportError:
    # Standalone context: a script adds the integration directory to sys.path and imports
    # this module directly, with no parent package.
    from conversions import schedule_type
    from decode import decodes_to_integer, decodes_to_number, raw_bounds

_LOGGER = logging.getLogger(__name__)

# The name a catalog gets when it arrives without one. Any name is allowed: the config entry
# records which file it was set up with, so several catalogs can live side by side.
CATALOG_FILENAME = "catalog.db"

# The catalog structure this code is written against, as one number.
#
# Users build their own catalogs and update the integration separately, so the two drift apart
# in both directions: an old catalog under a new integration, and a new catalog under an old
# one. Both are checked, because both produce nonsense otherwise -- a missing column raises
# somewhere deep in a query, and a changed meaning does not raise at all.
#
# The compiler writes its own number into `catalog_meta`; this is the structure this code is
# written for. Raise it together with the compiler's whenever the catalog gains something this
# code reads, so that a catalog built before that is reported as behind and the user knows a
# rebuild is due.
#
#   1  level-name key stems on datapoint_defs, catalog_meta itself
#   2  fa_error_codes: the fault texts of the burner automats
CATALOG_SCHEMA_VERSION = 2

# The oldest structure this code still reads. A catalog between the two works, minus what was
# added since, and raises a repair saying so; one below this fails setup, because a query would
# be wrong or would not run. Raise it only when an older catalog would actually be wrong: a
# table or column that is now required, a changed meaning, a different key format.
CATALOG_SCHEMA_MIN = 1


def _ext_label(hw_index: int | None, sw_index: int | None) -> str:
    """The identification extension the way the catalog spells it, or "-" when it is unread."""
    if hw_index is None or sw_index is None:
        return "-"
    return f"{hw_index:02X}{sw_index:02X}"


class UnknownControllerError(ValueError):
    """The controller said who it is and the catalog has no entry that fits.

    Carries the identification extensions the catalog does hold for that System ID, so the
    message can show what the controller would have had to report instead.
    """

    def __init__(
        self,
        sys_id: int,
        hw_index: int | None,
        sw_index: int | None,
        variants: list[str],
    ) -> None:
        self.sys_id = sys_id
        self.hw_index = hw_index
        self.sw_index = sw_index
        self.variants = variants
        super().__init__(
            f"Controller System ID 0x{sys_id:04X} with identification extension "
            f"{_ext_label(hw_index, sw_index)} is not in the catalog"
        )


class CatalogSchemaError(ValueError):
    """A real catalog, but not one this code can read.

    Carries which way round it is, so the message can name the side that needs updating
    instead of saying only that the two disagree.
    """

    def __init__(self, found: int | None) -> None:
        self.found = found
        self.needed = CATALOG_SCHEMA_VERSION
        self.outdated = found is None or found < CATALOG_SCHEMA_MIN
        super().__init__(
            f"catalog schema version {found if found is not None else 'absent'}, "
            f"this integration needs {CATALOG_SCHEMA_MIN} to {CATALOG_SCHEMA_VERSION}"
        )


def schema_readable(version: int | None) -> bool:
    """Whether this code reads a catalog of that structure version at all."""
    return (
        version is not None and CATALOG_SCHEMA_MIN <= version <= CATALOG_SCHEMA_VERSION
    )


def schema_behind(version: int | None) -> bool:
    """Whether a readable catalog was built before the structure this code is written for."""
    return schema_readable(version) and version < CATALOG_SCHEMA_VERSION


# Tab-tree root branch -> entity_category (for organization only).
TIER_CLASSIFICATION: dict[str, str | None] = {
    "Overview": None,
    "PlantOverview": None,
    "Operation": None,
    "Trending": "diagnostic",
    "Statistic": "diagnostic",
    "DiagnosisDiagnosis1": "diagnostic",
    "DiagnosisDiagnosis2": "diagnostic",
    "Lasterror": "diagnostic",
    "Installation": "config",
    "Coding2": "config",
    "CodeAccessLevelTD": "config",
    "Expertlayer": "config",
    "DefaultSettings": "config",
}

DAILY_TIERS = {"Overview", "PlantOverview", "Operation", "Trending", "Statistic"}

KIND_READONLY = 1
KIND_ACTION = 2
KIND_WRITABLE = 3

UNIT_MAP: dict[str, dict[str, str]] = {
    "°C": {"unit": "°C", "device_class": "temperature", "state_class": "measurement"},
    "Grad C": {
        "unit": "°C",
        "device_class": "temperature",
        "state_class": "measurement",
    },
    "K": {"unit": "K"},
    "h": {"unit": "h", "device_class": "duration", "state_class": "total_increasing"},
    "Stunden": {
        "unit": "h",
        "device_class": "duration",
        "state_class": "total_increasing",
    },
    "s": {"unit": "s", "device_class": "duration"},
    "Sekunden": {"unit": "s", "device_class": "duration"},
    "min": {"unit": "min", "device_class": "duration"},
    "Minuten": {"unit": "min", "device_class": "duration"},
    "%": {"unit": "%"},
    "Prozent": {"unit": "%"},
    "bar": {"unit": "bar", "device_class": "pressure"},
    "Bar": {"unit": "bar", "device_class": "pressure"},
    "Bar (absolut)": {"unit": "bar", "device_class": "pressure"},
    "kWh": {"unit": "kWh", "device_class": "energy", "state_class": "total_increasing"},
    "cbm pro h": {"unit": "m³/h", "device_class": "volume_flow_rate"},
}

_DIV_RATIOS = {"div2": 2.0, "div10": 10.0, "div100": 100.0, "div1000": 1000.0}


# Translation lookups go through `translations_p` directly rather than the `translations` view.
#
# The view exists for compatibility and is a UNION ALL of one branch per language over the whole
# pivoted table; the culture arrives as a bound parameter, so SQLite cannot prune the other
# seventeen branches and every join pays for all of them. Measured on this catalog: joining the
# 1,308 datapoints of one controller costs 130 ms through the view and 10 ms against the table,
# and the label map inside get_condition_aliases -- the same join over every datapoint -- was
# 1.5 s of a 7 s setup.
#
# The language is a column name, which cannot be bound, so it is validated against the table's
# own columns and quoted. Anything unknown falls back to English. The columns are read from the
# catalog being queried each time rather than remembered: entries can use different catalogs and
# switch between them at runtime, and the few reads per setup cost nothing measurable.


def _culture_column(conn: sqlite3.Connection, culture: str | None) -> str:
    """A quoted `translations_p` column for a language, validated against the table itself."""
    columns = {
        r[1]
        for r in conn.execute("PRAGMA table_info(translations_p)")
        if r[1] != "text_key"
    }
    wanted = (culture or "en").strip().lower()
    if wanted not in columns:
        # English, then German, then whatever the catalog was built with: a catalog built for
        # a single other language has neither.
        wanted = next(
            (c for c in ("en", "de") if c in columns), min(columns, default="en")
        )
    return f'"{wanted}"'


def _text_join(
    conn: sqlite3.Connection, culture: str | None, alias: str, key_expr: str
) -> str:
    """SQL joining one translated string, as `<alias>.value`. See _culture_column."""
    column = _culture_column(conn, culture)
    return (
        f"LEFT JOIN translations_p {alias}_p ON {alias}_p.text_key = {key_expr} "
        f"LEFT JOIN strings {alias} ON {alias}.id = {alias}_p.{column}"
    )


def _text(conn: sqlite3.Connection, culture: str | None, key: str | None) -> str | None:
    """One translated string for a text key, English if the language has none."""
    if not key:
        return None
    for lang in dict.fromkeys([culture, "en", "de"]):
        column = _culture_column(conn, lang)
        row = conn.execute(
            f"SELECT s.s AS value FROM translations_p p JOIN strings s ON s.id = p.{column} "
            "WHERE p.text_key = ?",
            (key,),
        ).fetchone()
        if row and row["value"]:
            return row["value"]
    return None


def _friendly_device_name(
    name_key: str | None, translated: str | None, model: str
) -> str:
    """The product name to show for a controller, from its catalog description.

    The catalog describes each controller with a sentence rather than a name -- "Allgemeine
    Produktbeschreibung: Vitocal-G mit Vitotronic 200 (Typ WO1A) (ab 08/2010)" -- so the
    label in front of the colon is dropped, and so is a trailing validity date, which says
    when the variant started shipping and is noise in a device name. What is left is the
    product: "Vitocal-G mit Vitotronic 200 (Typ WO1A)". Other trailing parentheticals name
    real equipment differences between variants and are kept.

    Falls back to the model code when there is no description, or when the description is
    still an unresolved text key.
    """
    text = (translated or name_key or "").strip()
    if not text or text.startswith("@@"):
        return model
    _, separator, remainder = text.partition(":")
    if separator:
        text = remainder
    text = re.sub(r"\s*\((?:ab|from)\s+\d{2}/\d{4}\)\s*$", "", text)
    text = " ".join(text.split())
    return text or model


def list_catalogs(config_dir: str) -> list[dict[str, Any]]:
    """Every catalog in the directory, with enough about each to tell them apart.

    A catalog built for one controller and a catalog built for all of them look identical from
    the outside, so the count and the languages come from inside the file. Unreadable files are
    left out rather than offered. Blocking I/O; call it from an executor.
    """
    found: list[dict[str, Any]] = []
    try:
        names = sorted(n for n in os.listdir(config_dir) if n.endswith(".db"))
    except OSError:
        return found
    for name in names:
        path = os.path.join(config_dir, name)
        try:
            with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as conn:
                devices = conn.execute("SELECT count(*) FROM devices").fetchone()[0]
                languages = [
                    r[1]
                    for r in conn.execute("PRAGMA table_info(translations_p)")
                    if r[1] != "text_key"
                ]
                version = _schema_version(conn)
        except sqlite3.DatabaseError:
            continue
        # An unusable one is still listed, so that it is visible and can be replaced. How a
        # catalog is described to a person is translated text and is composed by the setup
        # flow from these facts.
        found.append(
            {
                "name": name,
                "path": path,
                "devices": devices,
                "languages": languages,
                "schema_version": version,
                "usable": schema_readable(version),
                "behind": schema_behind(version),
            }
        )
    return found


def ensure_catalog(config_dir: str, filename: str | None = None) -> str:
    """The catalog this controller should use, as a path.

    The catalog is not part of the integration: it is built by the user from their own copy of
    the controller software and uploaded when the integration is added (see the README). It
    lives in `<config>/optov/`, outside the integration's own directory, because an update
    replaces that directory wholesale.

    `filename` is what the config entry recorded, so two controllers can use two different
    catalogs -- someone who built one per controller rather than one for all of them. Without
    it, a single catalog in the directory is used, and anything else is an error the caller
    turns into a question. Blocking I/O; call it from an executor.
    """
    if filename:
        path = os.path.join(config_dir, filename)
        if not os.path.exists(path):
            raise FileNotFoundError(f"catalog {filename} is no longer in {config_dir}")
        return path
    catalogs = list_catalogs(config_dir)
    if not catalogs:
        raise FileNotFoundError(f"no catalog in {config_dir}")
    if len(catalogs) > 1:
        raise ValueError(f"{len(catalogs)} catalogs in {config_dir}; none chosen")
    return catalogs[0]["path"]


def install_catalog(source: str, config_dir: str, filename: str | None = None) -> str:
    """Put a catalog the user supplied in place, after checking that it is one.

    `source` is the uploaded file and `filename` the name it arrived with, so several
    catalogs can sit side by side under names their owner recognises. Current builds are plain
    SQLite files; a compressed one from an older build is unpacked here, once, rather than at
    every start.

    It is verified before it replaces anything: the result has to be a SQLite database with
    the tables this integration reads and at least one controller in it. A file that fails is
    rejected here, where the message can say so. Blocking I/O; call it from an executor.
    """
    import shutil
    import tempfile

    os.makedirs(config_dir, exist_ok=True)
    name = _catalog_name(filename or CATALOG_FILENAME)
    target = os.path.join(config_dir, name)
    with tempfile.TemporaryDirectory(dir=config_dir) as staging:
        candidate = os.path.join(staging, name)
        with open(source, "rb") as fh:
            compressed = fh.read(6).startswith(b"\xfd7zXZ")
        if compressed:
            _unpack_to(source, candidate)
        else:
            shutil.copyfile(source, candidate)
        _verify(candidate)
        _settle_journal(candidate)
        os.replace(candidate, target)
    _LOGGER.info(
        "Catalog installed: %s (%.1f MB)", target, os.path.getsize(target) / 1024 / 1024
    )
    return target


def remove_catalog(config_dir: str, name: str) -> None:
    """Delete a catalog file, and whatever SQLite may have left beside it. Blocking I/O.

    Only a plain `.db` file name directly inside the catalog directory is accepted, so a name
    arriving from a form cannot point anywhere else. Whether an entry still uses the catalog is
    the caller's to check first.
    """
    if not name or name != os.path.basename(name) or not name.endswith(".db"):
        raise ValueError(f"not a catalog file name: {name!r}")
    path = os.path.join(config_dir, name)
    os.remove(path)
    for suffix in ("-wal", "-shm", "-journal"):
        with suppress(FileNotFoundError):
            os.remove(path + suffix)
    absolute = os.path.abspath(path)
    for key in [k for k in _ALIAS_CACHE if k[0] == absolute]:
        del _ALIAS_CACHE[key]
    _LOGGER.info("Catalog deleted: %s", path)


def _catalog_name(filename: str) -> str:
    """A plain `.db` filename, with any directory part and any `.xz` suffix taken off."""
    name = os.path.basename(filename).strip() or CATALOG_FILENAME
    if name.endswith(".xz"):
        name = name[:-3]
    if not name.endswith(".db"):
        name += ".db"
    return name


def _verify(path: str) -> int:
    """Raise unless this is a controller catalog this code can read; else its structure version.

    ValueError for something that is not a catalog at all, CatalogSchemaError for one that is
    but was built to a different structure. The caller tells them apart to say something
    useful: the first is the wrong file, the second is the wrong version of the right file.
    """
    required = {
        "devices",
        "datapoint_defs",
        "device_datapoints",
        "translations_p",
        "strings",
    }
    try:
        with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as conn:
            present = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            missing = required - present
            if missing:
                raise ValueError(
                    f"not a controller catalog; missing {', '.join(sorted(missing))}"
                )
            if not conn.execute("SELECT 1 FROM devices LIMIT 1").fetchone():
                raise ValueError("the catalog describes no controllers")
            version = _schema_version(conn)
            if not schema_readable(version):
                raise CatalogSchemaError(version)
    except sqlite3.DatabaseError as err:
        raise ValueError(f"not a readable database: {err}") from err
    return version


def _schema_version(conn: sqlite3.Connection) -> int | None:
    """The structure version a catalog claims, or None if it claims none.

    A catalog from before versioning existed has no `catalog_meta` at all; so does one whose
    build was interrupted, since the version is the last thing written. Both are "not this
    version" and neither should be read.
    """
    try:
        row = conn.execute(
            "SELECT value FROM catalog_meta WHERE key = 'schema_version'"
        ).fetchone()
    except sqlite3.DatabaseError:
        return None
    try:
        return int(row[0]) if row else None
    except (TypeError, ValueError):
        return None


def catalog_info(path: str) -> dict[str, Any]:
    """What a catalog says about itself, for showing to a person. Blocking I/O.

    Every `catalog_meta` key, the structure version among them, plus the file name, how many
    controllers it describes and the languages it carries.
    """
    with closing(get_db_connection(path)) as conn:
        meta = dict(conn.execute("SELECT key, value FROM catalog_meta").fetchall())
        controllers = conn.execute("SELECT count(*) FROM devices").fetchone()[0]
        languages = sorted(
            r[1]
            for r in conn.execute("PRAGMA table_info(translations_p)")
            if r[1] != "text_key"
        )
    return {
        "file": os.path.basename(path),
        "meta": meta,
        "controllers": controllers,
        "languages": languages,
    }


def check_catalog(path: str) -> int:
    """Raise if this catalog cannot be read by this code; else return its structure version.

    Blocking I/O; use an executor. Installing verifies too, but a catalog can also be copied in
    by hand and an integration update can move the goalposts under one that was fine yesterday,
    so it is checked again every time an entry starts. The version comes back so the caller can
    say when a readable catalog is behind.
    """
    return _verify(path)


def _settle_journal(path: str) -> None:
    """Take a catalog out of write-ahead logging, once, while it is still being installed.

    A catalog is only ever read, but a reader of a write-ahead-logged database still creates a
    `-wal` and a `-shm` beside it. In the configuration directory those are litter, and on a
    read-only mount they would stop the catalog being opened at all. Current builds ship
    without it; an older one is converted here rather than at every start.
    """
    # Autocommit: the pragma is refused inside a transaction, which is what the connection
    # would otherwise open on the first statement.
    try:
        conn = sqlite3.connect(path, isolation_level=None)
        try:
            if conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal":
                conn.execute("PRAGMA journal_mode=DELETE")
        finally:
            conn.close()
    except (
        sqlite3.DatabaseError
    ) as err:  # Nothing fatal: the catalog itself is already sound.
        _LOGGER.debug("Could not switch %s out of write-ahead logging: %s", path, err)


def _unpack_to(archive: str, path: str) -> None:
    import lzma
    import shutil

    with lzma.open(archive, "rb") as src, open(path, "wb") as dst:
        shutil.copyfileobj(src, dst, 1024 * 1024)


def get_db_connection(db_path: str) -> sqlite3.Connection:
    """A read-only connection to one catalog, by path.

    There is no default. No catalog is part of the integration, and which one to open is a
    property of the config entry, so every caller carries the path down from there.
    """
    if not db_path:
        raise ValueError("no catalog path given; it belongs to the config entry")
    conn = sqlite3.connect(f"file:{os.path.abspath(db_path)}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def get_available_languages(db_path: str) -> list[str]:
    """The language codes the catalog carries text in."""
    try:
        with closing(get_db_connection(db_path)) as conn:
            columns = [r[1] for r in conn.execute("PRAGMA table_info(translations_p)")]
            return sorted(c for c in columns if c != "text_key") or ["de", "en"]
    except Exception as err:
        _LOGGER.debug("Could not read available languages from db: %s", err)
        return ["de", "en"]


def _select_variant(
    candidates: list[Any],
    hw_index: int | None,
    sw_index: int | None,
    f0: int | None,
    device_low_byte: int,
) -> Any | None:
    """Pick one controller from those sharing a System ID.

    A System ID on its own is not an identity: across the Optolink catalog 253 controllers
    share about 90 System IDs, and one of those covers fifteen variants whose datapoint counts
    run from 561 to 1062. Two further stages break the tie, in this order:

    1. **F0**, but only when `Device` is 0xC0..0xCB *and* SoftwareIndex >= 200. Exact `f0`
       match first, then an `f0..f0_till` range. Within that range it is the only thing that
       separates several controllers declaring the same extension.
    2. **IdentificationExtension** = `<HardwareIndex:X2><SoftwareIndex:X2>`. Exact match first,
       then the `ident_ext..ident_ext_till` range, comparing the hardware and software bytes
       independently rather than as one 16-bit number.
    3. The software index on its own, but only where every candidate declares the same
       hardware index, so that byte cannot be what separates them.
    4. Failing those, the candidate that declares no extension at all, which acts as the
       catch-all.
    """
    if len(candidates) == 1:
        return candidates[0]

    def _bytes(ext: str | None) -> tuple | None:
        if not ext or len(ext) != 4:
            return None
        try:
            return int(ext[:2], 16), int(ext[2:], 16)
        except ValueError:
            return None

    # Stage 1 -- F0, under its narrow guard.
    if f0 is not None and 0xC0 <= device_low_byte <= 0xCB and (sw_index or 0) >= 200:
        for c in candidates:
            if c["f0"] is not None and c["f0"] >= 0 and f0 == c["f0"]:
                return c
        for c in candidates:
            lo, hi = c["f0"], c["f0_till"]
            if lo is not None and hi is not None and lo >= 0 and lo <= f0 <= hi:
                return c

    # Stage 2 -- IdentificationExtension, exact then range.
    if hw_index is not None and sw_index is not None:
        for c in candidates:
            ext = _bytes(c["ident_ext"])
            if ext and ext[0] == hw_index and ext[1] == sw_index:
                return c
        for c in candidates:
            lo, hi = _bytes(c["ident_ext"]), _bytes(c["ident_ext_till"])
            if lo and hi and lo[0] <= hw_index <= hi[0] and lo[1] <= sw_index <= hi[1]:
                return c

    # Stage 2b -- the software index alone, when the hardware byte cannot be telling variants
    # apart because every candidate declares the same one. A controller reporting a hardware
    # index the catalog never saw is then still one of these variants: the datapoint set
    # follows the software index, which is what the model suffixes name ("Softwarestand 4").
    # Where the hardware indices do differ -- the GWG families, where the byte names the board
    # -- nothing is assumed and the lookup is left to fail.
    if (
        sw_index is not None
        and len({(c["ident_ext"] or "")[:2] for c in candidates}) == 1
    ):
        for c in candidates:
            ext, till = _bytes(c["ident_ext"]), _bytes(c["ident_ext_till"])
            if ext and (ext[1] == sw_index or (till and ext[1] <= sw_index <= till[1])):
                _LOGGER.warning(
                    "Hardware index 0x%02X is not the 0x%02X that every variant of this "
                    "System ID declares; selecting %s by software index 0x%02X alone",
                    hw_index,
                    ext[0],
                    c["model"],
                    sw_index,
                )
                return c

    # Stage 3 -- the extension-less catch-all.
    for c in candidates:
        if not c["ident_ext"]:
            return c
    return None


def _variant_label(candidate: Any) -> str:
    """Name one variant the way the catalog declares it: model and extension range."""
    ext, till = candidate["ident_ext"] or "-", candidate["ident_ext_till"] or ""
    span = f"{ext}..{till}" if till and till != ext else ext
    return f"{candidate['model']} ({span})"


def get_device_by_system_id(
    sys_id: int,
    db_path: str,
    culture: str = "de",
    hw_index: int | None = None,
    sw_index: int | None = None,
    f0: int | None = None,
) -> dict[str, Any] | None:
    """Look up device metadata and translated circuits by DeviceIdent.

    `hw_index`/`sw_index` are DeviceIdent bytes 0x00FA/0x00FB and `f0` is register 0x00F0.
    They are optional so older callers keep working, but without them a System ID that maps to
    several controllers resolves to whichever variant the catch-all rule picks -- see
    _select_variant().
    """
    hex_id = f"{sys_id:04X}".upper()
    dec_id = str(sys_id)
    with closing(get_db_connection(db_path)) as conn:
        candidates = conn.execute(
            "SELECT id, system_id, model, name_key, ident_ext, ident_ext_till, f0, f0_till "
            "FROM devices WHERE system_id = ? OR system_id = ? ORDER BY id",
            (hex_id, dec_id),
        ).fetchall()
        if not candidates:
            raise UnknownControllerError(sys_id, hw_index, sw_index, [])

        row = _select_variant(candidates, hw_index, sw_index, f0, sys_id & 0xFF)
        if row is None:
            raise UnknownControllerError(
                sys_id,
                hw_index,
                sw_index,
                [_variant_label(c) for c in candidates],
            )
        if len(candidates) > 1:
            _LOGGER.info(
                "System ID 0x%04X matches %d controllers; selected %s "
                "(identification extension %s, F0 %s)",
                sys_id,
                len(candidates),
                row["model"],
                _ext_label(hw_index, sw_index),
                f0,
            )

        dev_id = row["id"]
        model = row["model"]
        name_key = row["name_key"]

        device_name = _friendly_device_name(
            name_key, _text(conn, culture, name_key), model
        )

        # Circuits
        circuits: dict[str, str] = {}
        c_rows = conn.execute(
            f"""SELECT c.circuit, COALESCE(t.s, c.name_key) as circuit_name
               FROM circuits c
               {_text_join(conn, culture, "t", "c.name_key")}
               WHERE c.device_id = ?""",
            (dev_id,),
        ).fetchall()
        for r in c_rows:
            circuits[r["circuit"]] = r["circuit_name"]

        return {
            "id": dev_id,
            "system_id": row["system_id"],
            "model": model,
            "device_name": device_name,
            "circuits": circuits,
        }


def get_probe_targets(device_id: int, db_path: str) -> list[dict[str, Any]]:
    """Return distinct equipment configuration registers for self-configuration probe.

    Excludes dynamic runtime sensor health nibbles (SensorStatus, SensorDruckStatus)
    which are evaluated per-datapoint at runtime during polling.
    """
    with closing(get_db_connection(db_path)) as conn:
        rows = conn.execute(
            """SELECT DISTINCT dp.id, dp.address, dp.byte_length, dp.block_length,
                      dp.byte_position, dp.bit_start, dp.bit_length, dp.parameter_type,
                      dp.name, dp.fc_read
               FROM display_conditions dc
               JOIN datapoints dp
                 ON dc.condition_event_type_id = dp.id AND dp.device_id = dc.device_id
               WHERE dc.device_id = ?
                 AND dp.name NOT LIKE '%SensorStatus%'
                 AND dp.name NOT LIKE '%SensorDruckStatus%'""",
            (device_id,),
        ).fetchall()
        targets = {r["id"]: dict(r) for r in rows}

        # Rules whose input belongs to a sibling family are redirected onto this controller's
        # equivalent (see get_condition_aliases). Those equivalents have to be probed too,
        # otherwise the redirect resolves to a datapoint nobody ever read.
        for local_id in set(get_condition_aliases(device_id, db_path=db_path).values()):
            if local_id in targets:
                continue
            row = conn.execute(
                """SELECT dp.id, dp.address, dp.byte_length, dp.block_length,
                          dp.byte_position, dp.bit_start, dp.bit_length, dp.parameter_type,
                          dp.name, dp.fc_read
                   FROM datapoints dp WHERE dp.device_id = ? AND dp.id = ?""",
                (device_id, local_id),
            ).fetchone()
            if row:
                targets[local_id] = dict(row)
        return list(targets.values())


def get_error_history_datapoint(device_id: int, db_path: str) -> dict[str, Any] | None:
    """Locate this device's error-history buffer, entirely from the catalog.

    Every supported family declares exactly one such array datapoint, named `<prefix>Error`
    with a non-zero `block_factor` (the element count). Its `fc_read` decides the wire
    operation and `byte_length / block_factor` gives the size of one entry:

        heat pumps        -> WPRError   @0xA801, 240B / 30 =  8B, Remote_Procedure_Call
        CU401B   (WO1C)    -> WPR3Error  @0xA801, 270B / 30 =  9B, Remote_Procedure_Call
        VScotHO1 (boiler)  -> Error      @0x7507,  90B / 10 =  9B, Virtual_READ

    Matches on the `Error` suffix rather than a hardcoded address, and excludes `*ErrorIndex`
    (a separate, smaller companion buffer).
    """
    with closing(get_db_connection(db_path)) as conn:
        row = conn.execute(
            """SELECT address, name, byte_length, block_factor, fc_read, prefix_read
               FROM datapoints
               WHERE device_id = ?
                 AND block_factor > 0
                 AND name LIKE '%Error'
                 AND name NOT LIKE '%ErrorIndex'
               ORDER BY byte_length DESC LIMIT 1""",
            (device_id,),
        ).fetchone()
        if not row:
            return None

        block_factor = int(row["block_factor"] or 0)
        byte_length = int(row["byte_length"] or 0)
        if block_factor < 1 or byte_length < 1 or byte_length % block_factor:
            _LOGGER.debug(
                "Error-history datapoint %s has unusable geometry (%s bytes / %s entries)",
                row["name"],
                byte_length,
                block_factor,
            )
            return None

        addr_raw = str(row["address"])
        return {
            "address": int(addr_raw, 16)
            if addr_raw.lower().startswith("0x")
            else int(addr_raw),
            "name": row["name"],
            "total_bytes": byte_length,
            "block_factor": block_factor,
            "entry_bytes": byte_length // block_factor,
            "fc_read": row["fc_read"] or "Virtual_READ",
            "prefix_read": row["prefix_read"],
        }


def get_error_codes(
    device_id: int, db_path: str, culture: str = "de"
) -> dict[str, str]:
    """Return {CODE_HEX: localized text} for this device family.

    Fault texts are device-specific: the same code means different things across families
    (on a heat pump `FF` is "Neustart der Regelung"; the generic/boiler table calls it
    "Interner Fehler oder Reset-Taster blockiert"). Always look these up per device.
    """
    with closing(get_db_connection(db_path)) as conn:
        rows = conn.execute(
            """SELECT ec.code AS code, COALESCE(t.value, ec.text_key) AS value
               FROM error_codes ec
               LEFT JOIN translations t
                      ON t.text_key = ec.text_key AND t.culture = ?
               WHERE ec.device_id = ?""",
            (culture.lower(), device_id),
        ).fetchall()
        return {r["code"].upper(): r["value"] for r in rows}


def _address_int(raw: Any) -> int:
    text = str(raw)
    return int(text, 16) if text.lower().startswith("0x") else int(text)


def get_gfa_error_history(device_id: int, db_path: str) -> dict[str, Any] | None:
    """The burner automat's own fault records, and the datapoint that says which automat it is.

    Boilers with a burner automat (Feuerungsautomat) keep its faults apart from the
    controller's: twenty records of nine bytes, one datapoint each (`FehlerHisFA01` to `20`),
    in the same layout as the controller's boiler buffer, plus a one-byte `GFA_Kennung` with
    the automat's chip code. The texts belong to the chip, not to the controller family, so
    the code is read first and the texts looked up under it (see get_fa_error_codes). Both
    parts are required; a controller without them has no burner history.
    """
    with closing(get_db_connection(db_path)) as conn:
        records = conn.execute(
            """SELECT address, byte_length, fc_read FROM datapoints
               WHERE device_id = ? AND name LIKE '%FehlerHisFA__'
               ORDER BY address""",
            (device_id,),
        ).fetchall()
        chip = conn.execute(
            """SELECT address, byte_length, fc_read FROM datapoints
               WHERE device_id = ? AND name LIKE '%GFA_Kennung' LIMIT 1""",
            (device_id,),
        ).fetchone()
    if not records or not chip:
        return None
    entry_bytes = int(records[0]["byte_length"] or 0)
    if entry_bytes < 1 or any(
        int(r["byte_length"] or 0) != entry_bytes for r in records
    ):
        _LOGGER.debug(
            "Burner fault records of device %s differ in size; skipped", device_id
        )
        return None
    return {
        "name": "FehlerHisFA",
        "addresses": [_address_int(r["address"]) for r in records],
        "entry_bytes": entry_bytes,
        "fc_read": records[0]["fc_read"] or "Virtual_READ",
        "chip_address": _address_int(chip["address"]),
        "chip_bytes": int(chip["byte_length"] or 1),
        "chip_fc_read": chip["fc_read"] or "Virtual_READ",
    }


def get_fa_error_codes(chip: str, db_path: str, culture: str = "de") -> dict[str, str]:
    """{CODE_HEX: text} of the burner automat with this chip code.

    Empty for a catalog built before the table existed: the history is then shown with bare
    codes, which is still better than nothing, and a rebuilt catalog fills the texts in.
    """
    with closing(get_db_connection(db_path)) as conn:
        try:
            rows = conn.execute(
                """SELECT f.code AS code, COALESCE(t.value, f.text_key) AS value
                   FROM fa_error_codes f
                   LEFT JOIN translations t
                          ON t.text_key = f.text_key AND t.culture = ?
                   WHERE f.chip = ?""",
                (culture.lower(), chip.upper()),
            ).fetchall()
        except sqlite3.OperationalError:
            return {}
        return {r["code"].upper(): r["value"] for r in rows}


def _condition_holds(probed_value: int | None, op: str, compare: int) -> bool:
    if probed_value is None:
        return False
    if op == "eq":
        return probed_value == compare
    if op == "ne":
        return probed_value != compare
    if op == "gt":
        return probed_value > compare
    if op == "lt":
        return probed_value < compare
    return False


# Per catalog file, controller and language: the file's modification time and the aliases it
# yielded. Keyed by the file because two catalogs can resolve the same controller differently,
# and stamped with its modification time so a catalog replaced in place is not answered from
# the one it replaced; the new result overwrites the old entry rather than adding another.
_ALIAS_CACHE: dict[tuple[str, int, str], tuple[int, dict[int, int]]] = {}


def get_condition_aliases(
    device_id: int, db_path: str, culture: str = "de"
) -> dict[int, int]:
    """Map display-condition inputs that this controller does not have onto ones it does.

    A rule names the datapoint whose value it tests. For a fifth of this controller's rules
    that datapoint belongs to a *sibling* family: the rule hiding "screed drying, heating
    circuit 2" tests an equipment register that only the newer controller generation exposes,
    even though the rule is attached to an older one that holds the same flag at a different
    address. The lookup then finds nothing, the rule never fires, and entities for hardware
    that is not installed stay visible.

    The catalog's own labels bridge this: both entries are called "HK2 vorhanden". Matching on
    the translated label -- and only when it is unambiguous within this device -- resolves the
    cases that matter without inventing name patterns of our own. Anything still unmatched is
    left alone; its rule simply cannot be evaluated.
    """
    path = os.path.abspath(db_path)
    version = os.stat(path).st_mtime_ns
    cache_key = (path, device_id, culture)
    cached = _ALIAS_CACHE.get(cache_key)
    if cached is not None and cached[0] == version:
        return cached[1]

    with closing(get_db_connection(db_path)) as conn:
        foreign = [
            r[0]
            for r in conn.execute(
                """SELECT DISTINCT condition_event_type_id FROM display_conditions
                   WHERE device_id = ? AND condition_event_type_id NOT IN
                     (SELECT datapoint_id FROM device_datapoints WHERE device_id = ?)""",
                (device_id, device_id),
            )
        ]
        if not foreign:
            _ALIAS_CACHE[cache_key] = (version, {})
            return {}

        own: dict[str, list[int]] = {}
        for r in conn.execute(
            f"""SELECT dd.id, COALESCE(t.s, dd.name) AS label
               FROM device_datapoints v
               JOIN datapoint_defs dd ON dd.id = v.datapoint_id
               {_text_join(conn, culture, "t", "dd.name_key")}
               WHERE v.device_id = ?""",
            (device_id,),
        ):
            own.setdefault(r["label"], []).append(r["id"])

        aliases: dict[int, int] = {}
        for fid in foreign:
            row = conn.execute(
                f"""SELECT COALESCE(t.s, dd.name) AS label FROM datapoint_defs dd
                   {_text_join(conn, culture, "t", "dd.name_key")}
                   WHERE dd.id = ?""",
                (fid,),
            ).fetchone()
            if not row:
                continue
            candidates = own.get(row["label"], [])
            if len(candidates) == 1:
                aliases[fid] = candidates[0]

        if aliases:
            _LOGGER.debug(
                "Resolved %d of %d foreign condition inputs by label",
                len(aliases),
                len(foreign),
            )
        _ALIAS_CACHE[cache_key] = (version, aliases)
        return aliases


def evaluate_rules(
    device_id: int,
    probed_values: dict[int, int],
    db_path: str,
    culture: str = "de",
) -> tuple[set[int], set[int]]:
    """Evaluate display condition rules and return (hidden_event_type_ids, hidden_group_ids)."""
    aliases = get_condition_aliases(device_id, db_path, culture)
    with closing(get_db_connection(db_path)) as conn:
        rows = conn.execute(
            """SELECT id, target_event_type_id, target_group_id, condition_event_type_id,
                      op, compare_value, rule_type
               FROM display_conditions
               WHERE device_id = ?""",
            (device_id,),
        ).fetchall()

    rules_by_target: dict[
        tuple[int | None, int | None, int, int], list[dict[str, Any]]
    ] = {}
    for r in rows:
        key = (r["id"], r["target_event_type_id"], r["target_group_id"], r["rule_type"])
        rules_by_target.setdefault(key, []).append(dict(r))

    hidden_event_type_ids: set[int] = set()
    hidden_group_ids: set[int] = set()

    for (_rule_id, target_et, target_g, rule_type), conds in rules_by_target.items():
        holds_list = [
            _condition_holds(
                probed_values.get(
                    aliases.get(
                        c["condition_event_type_id"], c["condition_event_type_id"]
                    )
                ),
                c["op"],
                c["compare_value"],
            )
            for c in conds
        ]
        rule_holds = all(holds_list) if rule_type == 1 else any(holds_list)

        if rule_holds:
            if target_et is not None:
                hidden_event_type_ids.add(target_et)
            if target_g is not None:
                hidden_group_ids.add(target_g)

    return hidden_event_type_ids, hidden_group_ids


def _slug(name: str) -> str:
    s = re.sub(r"[^0-9a-zA-Z]+", "_", name).strip("_").lower()
    return s or "unnamed"


# The vendor's own layout markers, which mean nothing outside its service tool.
_TEXT_MARKERS = re.compile(r"##ecn(?:newline|tab)##")


def _description(dp: dict[str, Any]) -> str | None:
    """The catalog's explanation of what a datapoint does, tidied for display.

    Worth carrying even though nothing in the integration reads it: a coding parameter's
    name says what it is called, not what changing it will do, and the answer is already in
    the catalog. Without this the only way to find out is to open the file by hand.
    """
    text = dp.get("description")
    if not text:
        return None
    text = " ".join(_TEXT_MARKERS.sub(" ", str(text)).split())
    return text or None


def _number_limits(dp: dict[str, Any], div: float) -> dict[str, Any]:
    """What a writable number may be set to, and in what steps.

    The catalog's own limits where it states them. Where it does not -- and it does not for
    well over a third of this family's settings -- the datapoint itself answers: its parameter
    type's width and sign give the range, the conversion's divisor the step. Inventing a range
    instead refuses values the controller accepts and whole steps hide the tenths a scaled
    datapoint is set in.
    """
    limits: dict[str, Any] = {}
    if dp.get("min_value") is not None:
        limits["min"] = dp["min_value"]
    if dp.get("max_value") is not None:
        limits["max"] = dp["max_value"]
    if dp.get("stepping") is not None and dp["stepping"] > 0:
        limits["step"] = dp["stepping"]
    low, high = raw_bounds(dp.get("parameter_type"))
    limits.setdefault("min", round(low / div, 3))
    limits.setdefault("max", round(high / div, 3))
    limits.setdefault("step", round(1 / div, 3))
    return limits


def _as_sensor(entry: dict[str, Any], sensor_category: str | None) -> dict[str, Any]:
    """A sensor may not be `config`; Home Assistant allows that only on writable entities."""
    if sensor_category:
        entry["entity_category"] = sensor_category
    return entry


def _div_ratio(conv: str | None) -> float:
    if not conv:
        return 1.0
    return _DIV_RATIOS.get(conv.strip().lower(), 1.0)


def _unit_meta(raw_unit: str | None) -> dict[str, Any]:
    if not raw_unit:
        return {}
    return UNIT_MAP.get(raw_unit.strip(), {"unit": raw_unit.strip()})


def _clock_settings(
    datapoints: list[dict[str, Any]],
    group_ids_by_et: dict[int, list[int]],
    group_addrs_by_id: dict[int, str],
) -> tuple[set[int], dict[str, Any]]:
    """The datapoints that only exist to configure the clock, so they can be filed as settings.

    A controller keeps its daylight-saving rules -- which month, week and weekday the change
    falls on, twice a year, plus whether to observe it at all -- next to the clock in its
    operating menu. They are set once at commissioning and never touched again, so showing
    them among the everyday controls buries the things that are used daily.

    Finding them needs no names and no addresses. The clock is the one writable datapoint
    carrying a full BCD timestamp (holiday dates carry a BCD *date*, which is a different
    conversion). Its node in the operating menu is where its settings are filed, and
    anything in that node that appears nowhere else in the menu tree exists only for the clock.
    A control that happens to sit there but is also reachable elsewhere -- the V200GW1 files
    "operate all circuits from one" beside its clock -- is referenced twice and stays put.

    Silent where the catalog is: 82 of the 112 controllers with a clock give it no operating
    node at all, and those return nothing.
    """
    clock = next(
        (
            dp
            for dp in datapoints
            if (dp.get("conversion") or "").strip().lower() == "datetimebcd"
            and dp.get("entity_kind") == KIND_WRITABLE
            and (dp.get("fc_write") or "undefined") != "undefined"
        ),
        None,
    )
    if clock is None:
        return set(), {}
    nodes = [
        gid
        for gid in group_ids_by_et.get(clock["id"], [])
        if "~operation~" in group_addrs_by_id.get(gid, "").lower()
    ]
    if len(nodes) != 1:
        return set(), {}
    node = nodes[0]
    members = [
        dp
        for dp in datapoints
        if dp["id"] != clock["id"]
        and group_ids_by_et.get(dp["id"]) == [node]
        and dp.get("entity_kind") == KIND_WRITABLE
    ]
    return {dp["id"] for dp in members}, _dst_rule_map(members)


def _dst_rule_map(members: list[dict[str, Any]]) -> dict[str, Any]:
    """Work out which of the clock's settings is the month, the week and the weekday.

    The catalog does not label the roles, but it does give each field a range, and the three
    ranges are distinct: 1-12 can only be a month, 1-7 only a weekday, and the remaining 1-14
    field is the week. Every controller in the catalog that carries these settings has them in
    exactly that shape -- a two-value switch for whether to observe the change at all, then two
    such triples -- and the two triples are the two changeovers in calendar order, which is the
    order their addresses run in.

    Returns {} rather than a guess if the set does not have that shape.
    """
    toggle = [
        dp
        for dp in members
        if dp.get("min_value") is None and dp.get("max_value") is None
    ]
    fields = [dp for dp in members if dp not in toggle]
    ranges = {(1, 12): "month", (1, 7): "weekday", (1, 14): "week"}
    roles: list[tuple[str, str]] = []
    for dp in sorted(fields, key=lambda d: d["address"]):
        role = ranges.get(
            (int(dp.get("min_value") or 0), int(dp.get("max_value") or 0))
        )
        if role is None:
            return {}
        roles.append((role, dp["address"]))
    if len(roles) != 6 or len(toggle) > 1:
        return {}
    first, second = dict(roles[:3]), dict(roles[3:])
    if sorted(first) != ["month", "week", "weekday"]:
        return {}
    if sorted(second) != ["month", "week", "weekday"]:
        return {}
    rule: dict[str, Any] = {"changeovers": [first, second]}
    if toggle:
        rule["enabled"] = toggle[0]["address"]
    return rule


def _level_keys(datapoint: dict[str, Any], level: int) -> list[str]:
    """The keys a programme's level name may be filed under, most specific first."""
    return [
        f"{stem}.{level}"
        for stem in (
            datapoint.get("level_text_stem_dp"),
            datapoint.get("level_text_stem"),
        )
        if stem
    ]


def _schedule_level_texts(
    conn: sqlite3.Connection, culture: str | None, datapoints: list[dict[str, Any]]
) -> dict[str, str]:
    """Every level name any of these datapoints might use, in one lookup.

    A programme allows at most a handful of levels and a controller has at most a handful of
    programmes, so the candidate keys are few and asking for exactly those beats scanning the
    translation table.
    """
    wanted: set[str] = set()
    for dp in datapoints:
        stype = schedule_type(dp.get("mapping_type"))
        if stype is None:
            continue
        for level in stype["levels"]:
            wanted.update(_level_keys(dp, level))
    if not wanted:
        return {}
    placeholders = ",".join("?" * len(wanted))
    keys = sorted(wanted)
    return {
        r["text_key"]: r["value"]
        for r in conn.execute(
            f"SELECT p.text_key, s.s AS value FROM translations_p p "
            f"JOIN strings s ON s.id = p.{_culture_column(conn, culture)} "
            f"WHERE p.text_key IN ({placeholders})",
            keys,
        ).fetchall()
        if r["value"]
    }


_ENTITY_PLATFORMS = ("sensors", "binary_sensors", "numbers", "selects", "switches")


@dataclass
class _ProfileInputs:
    """Everything the placement of a datapoint needs to know besides the datapoint itself.

    Loaded once per profile from the catalog and the rule evaluation, then read by every stage.
    """

    model: str
    device_name: str
    circuits: dict[str, str]
    hidden_circuits: set[str]
    groups: list[dict[str, Any]]
    group_ids_by_et: dict[int, list[int]]
    group_addrs_by_id: dict[int, str]
    datapoints: list[dict[str, Any]]
    enums_by_et: dict[int, dict[str, str]]
    hidden_event_type_ids: set[int]
    hidden_group_ids: set[int]
    active_tiers: set[str]
    unreachable_fc: set[str]
    # Derived once the datapoints are known.
    by_address: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    clock_settings: set[int] = field(default_factory=set)
    clock_dst: dict[str, Any] = field(default_factory=dict)


def _load_profile_inputs(
    conn: sqlite3.Connection,
    device_id: int,
    culture: str,
    hidden_event_type_ids: set[int],
    hidden_group_ids: set[int],
    active_tiers: set[str],
    unreachable_fc: set[str],
) -> _ProfileInputs:
    """Read what the catalog says about one controller, in the culture asked for."""
    # Get device info
    dev_row = conn.execute(
        "SELECT model, name_key FROM devices WHERE id = ?", (device_id,)
    ).fetchone()
    model = dev_row["model"] if dev_row else "Optolink"
    device_name = _friendly_device_name(
        dev_row["name_key"] if dev_row else None,
        _text(conn, culture, dev_row["name_key"]) if dev_row else None,
        model,
    )

    # Get circuits
    circuits: dict[str, str] = {}
    c_rows = conn.execute(
        f"""SELECT c.circuit, COALESCE(t.s, c.name_key) as circuit_name
           FROM circuits c
           {_text_join(conn, culture, "t", "c.name_key")}
           WHERE c.device_id = ?""",
        (device_id,),
    ).fetchall()
    for r in c_rows:
        circuits[r["circuit"]] = r["circuit_name"]

    # Detect disabled circuits from hidden circuit groups in the circuit tree
    # (ecnsysEventTypeGroupHC)
    circuit_groups = conn.execute(
        """SELECT g.id, g.address
           FROM groups g
           WHERE g.device_id = ? AND g.parent_id = (
               SELECT id FROM groups WHERE device_id = ? AND address LIKE 'ecnsysEventTypeGroupHC%'
           )""",
        (device_id, device_id),
    ).fetchall()
    hidden_circuits: set[str] = set()
    for cg in circuit_groups:
        c_code = cg["address"].split("~")[-1]
        if cg["id"] in hidden_group_ids:
            hidden_circuits.add(c_code)

    # Get datapoint to group links scoped to this device
    dg_rows = conn.execute(
        """SELECT dg.event_type_id, dg.group_id
           FROM datapoint_groups dg
           JOIN groups g ON dg.group_id = g.id AND g.device_id = ?""",
        (device_id,),
    ).fetchall()
    group_ids_by_et: dict[int, list[int]] = {}
    for r in dg_rows:
        group_ids_by_et.setdefault(r["event_type_id"], []).append(r["group_id"])

    # Query all datapoints with translated strings
    dp_rows = conn.execute(
        f"""SELECT dp.*,
                  COALESCE(tn.s, dp.name) as pretty_name,
                  td.s as description,
                  tu.s as translated_unit
           FROM datapoints dp
           {_text_join(conn, culture, "tn", "dp.name_key")}
           {_text_join(conn, culture, "td", "dp.description_key")}
           {_text_join(conn, culture, "tu", "dp.unit")}
           WHERE dp.device_id = ?""",
        (device_id,),
    ).fetchall()

    # Query enums with translations
    enum_rows = conn.execute(
        f"""SELECT e.event_type_id, e.val_key, COALESCE(t.s, e.text_key) as enum_text
           FROM enums e
           JOIN datapoints dp ON e.event_type_id = dp.id AND dp.device_id = ?
           {_text_join(conn, culture, "t", "e.text_key")}
           ORDER BY e.event_type_id, e.val_key""",
        (device_id,),
    ).fetchall()
    enums_by_et: dict[int, dict[str, str]] = {}
    for r in enum_rows:
        enums_by_et.setdefault(r["event_type_id"], {})[str(r["val_key"])] = r[
            "enum_text"
        ]

    # Get groups
    g_rows = conn.execute(
        f"""SELECT g.id, g.parent_id, g.address, g.order_index, COALESCE(t.s, g.name_key) as name
           FROM groups g
           {_text_join(conn, culture, "t", "g.name_key")}
           WHERE g.device_id = ?
           ORDER BY g.order_index""",
        (device_id,),
    ).fetchall()

    inputs = _ProfileInputs(
        model=model,
        device_name=device_name,
        circuits=circuits,
        hidden_circuits=hidden_circuits,
        groups=[dict(r) for r in g_rows],
        group_ids_by_et=group_ids_by_et,
        group_addrs_by_id={r["id"]: (r["address"] or "") for r in g_rows},
        datapoints=[dict(r) for r in dp_rows],
        enums_by_et=enums_by_et,
        hidden_event_type_ids=hidden_event_type_ids,
        hidden_group_ids=hidden_group_ids,
        active_tiers=active_tiers,
        unreachable_fc=unreachable_fc,
    )
    # Group siblings by address for status-nibble pairing
    for dp in inputs.datapoints:
        inputs.by_address.setdefault(dp["address"], []).append(dp)
    inputs.clock_settings, inputs.clock_dst = _clock_settings(
        inputs.datapoints, inputs.group_ids_by_et, inputs.group_addrs_by_id
    )
    return inputs


def _circuit_of(dp: dict[str, Any], inputs: _ProfileInputs) -> str | None:
    """The circuit a datapoint belongs to: its own, or the one its menu branch says."""
    c = dp.get("circuit")
    if c:
        return c
    g_ids = inputs.group_ids_by_et.get(dp["id"], [])
    g_addrs = [inputs.group_addrs_by_id.get(gid, "") for gid in g_ids]
    if any("solaranlage" in a.lower() or a.lower().endswith("~solar") for a in g_addrs):
        return "Solar"
    if any("warmwasser" in a.lower() or a.lower().endswith("~ww") for a in g_addrs):
        return "WW"
    for n in ("1", "2", "3"):
        if any(
            f"~hk{n}" in a.lower()
            or f"~heizkreis{n}" in a.lower()
            or a.lower().endswith(f"~hc{n}")
            for a in g_addrs
        ):
            return f"HC{n}"
    return None


def _place_datapoint(
    dp: dict[str, Any], inputs: _ProfileInputs, profile: dict[str, Any]
) -> None:
    """Decide what one datapoint becomes -- which platform, enabled or not -- or nothing."""
    tier = dp.get("tier")
    if tier not in TIER_CLASSIFICATION:
        return
    if dp.get("entity_kind") == KIND_ACTION:
        return
    if (dp.get("fc_read") or "Virtual_READ") in inputs.unreachable_fc:
        return

    # Skip raw multi-byte array dumps (e.g. 168-byte EEPROM schedule arrays handled by the
    # schedule poller)
    if (
        dp.get("byte_length", 1) > 8
        and (dp.get("parameter_type") or "").strip().lower() == "array"
    ):
        return

    c = _circuit_of(dp, inputs)
    if c and c in inputs.hidden_circuits:
        return

    et_id = dp["id"]
    if et_id in inputs.hidden_event_type_ids:
        return

    g_ids = inputs.group_ids_by_et.get(et_id, [])
    if g_ids and all(gid in inputs.hidden_group_ids for gid in g_ids):
        return

    bit_length = dp.get("bit_length", 0)
    siblings = inputs.by_address.get(dp["address"], [])
    if (
        bit_length > 0
        and dp.get("entity_kind") == KIND_READONLY
        and any(not s.get("bit_length") for s in siblings)
    ):
        # A read-only bit-field beside a full-width value at the same address is that
        # value's sensor-health nibble; it rides along with the value and is surfaced
        # through its availability instead (see status_sibling below). A read-only
        # bit-field on its own is a reading in its own right: relay states, the device
        # status flags, the digital inputs of the diagnosis page all live in single bits
        # of a register that holds nothing else.
        return
    # Writable bit-fields are real controls and must be exposed -- operating mode, party
    # and eco all live in bits of a shared register. Writing them needs the whole block
    # read and patched, which async_write_item() handles.

    entity_category = TIER_CLASSIFICATION[tier]
    if et_id in inputs.clock_settings:
        entity_category = "config"
    enum_values = inputs.enums_by_et.get(et_id, {})
    unit_str = dp.get("translated_unit") or dp.get("unit")

    # What gets switched on out of the box: the controller's own overview page, minus the
    # parameters on it.
    #
    # The catalog preserves the controller's menu tree, so the overview branch is literally
    # the page the unit shows on its display -- Allgemein, WP, one section per heating
    # circuit, Warmwasser, Solaranlage. Using that branch rather than the flat `tier` matters
    # because the tier also sweeps in setpoints (room target, heating-curve slope) that are
    # configuration, not readings. Restricting to read-only entries leaves temperatures and
    # relay states, which is what the display actually shows.
    #
    # Sections for hardware that is not installed have already been dropped above, so on a
    # single-circuit system the heating-circuit 2/3 sections never reach this point.
    branches = {
        seg.lower()
        for gid in g_ids
        for seg in inputs.group_addrs_by_id.get(gid, "").split("~")[1:2]
    }
    # Readings come from the overview page; the everyday controls (hot-water and room
    # setpoints, heating curve, operating mode, holiday) live in the operation menu, which
    # is the controller's own "Bedienung" branch. Both are what the unit puts in front of
    # its user, so both are on by default -- everything deeper stays available but off.
    #
    # The trend tier belongs with them. It is the controller's own graphing set, the
    # fast-moving process values it expects to be watched over time -- return and hot-gas
    # temperatures, suction and condenser pressures -- and the poll scheduler already gives
    # it the shortest interval of any tier. Leaving it out meant the cadence table had an
    # entry for datapoints that could never be in the set, and that a heat pump's return
    # temperature was hidden while its flow temperature was not.
    in_overview = "overview" in branches or "plantoverview" in branches
    in_operation = "operation" in branches
    in_trend = (tier or "") == "Trending"
    is_measurement = dp.get("entity_kind") == KIND_READONLY

    # HA doesn't allow sensors to have entity_category=config, only writable
    # entities (numbers, selects) may be 'config'. For sensors, downgrade to 'diagnostic'.
    sensor_category = "diagnostic" if entity_category == "config" else entity_category

    entry: dict[str, Any] = {
        "id": _slug(dp["name"]),
        "name": dp.get("pretty_name") or dp["name"],
        "address": dp["address"],
        "bytes": dp.get("byte_length", 1),
        "block": dp.get("block_length", 1),
        "byte_position": dp.get("byte_position", 0),
        "bit_start": dp.get("bit_start", 0),
        # Real geometry, not a placeholder. A writable bit-field is a control in its own
        # right and both reading and writing it depend on knowing where it sits; read-only
        # bit-fields never reach here, they are folded into their value's health status.
        "bit_length": bit_length,
        "conversion": dp.get("conversion") or "NoConversion",
        "parameter_type": dp.get("parameter_type"),
        # The wire operation is per-datapoint and is NOT always Virtual_READ -- the
        # VScotHO1 boiler family uses GFA_READ for 94 of its datapoints.
        "fc_read": dp.get("fc_read"),
        "fc_write": dp.get("fc_write"),
        # Only MultOffset consumes these (IntData * factor + offset); decode.py raises
        # rather than substituting 1.0/0.0 when a MultOffset datapoint lacks them.
        "conversion_factor": dp.get("conversion_factor"),
        "conversion_offset": dp.get("conversion_offset"),
        # The catalog's own urgency ranking (lower = more urgent). Drives poll cadence
        # -- see OptolinkCoordinator._target_interval().
        "priority": dp.get("priority"),
        "tier": tier,
        # The controller's own words for this datapoint, shown as a state attribute.
        "description": _description(dp),
        # Base set: what the controller itself puts in front of its user. Plus whichever
        # optional tiers the options have switched on, so diagnostics or coding channels
        # can be brought in for a debugging session and dropped again afterwards.
        "enabled_by_default": (
            ((in_overview or in_trend) and is_measurement)
            or in_operation
            or (tier in inputs.active_tiers)
        ),
        # The base set is what the controller itself puts in front of its user. It is kept
        # separate from enabled_by_default because the poll scheduler treats it differently:
        # base values are read every cycle, anything an optional tier added rotates through
        # the leftover bus budget.
        "base": ((in_overview or in_trend) and is_measurement) or in_operation,
        "_entity_category": entity_category,
        "_sensor_category": sensor_category,
    }
    if entity_category:
        entry["entity_category"] = entity_category
    if c:
        entry["circuit"] = c

    # A full-width value takes the health nibble that shares its address. A bit-field takes
    # none: it would only find itself, or a neighbouring flag in the same register, and read
    # its own 1 as a fault.
    status_sibling = (
        None
        if bit_length
        else next(
            (
                s
                for s in siblings
                if s.get("bit_length", 0) > 0 and s["id"] in inputs.enums_by_et
            ),
            None,
        )
    )
    if status_sibling is not None:
        entry["status_bit_start"] = status_sibling.get("bit_start", 0)
        entry["status_bit_length"] = status_sibling.get("bit_length", 0)
        entry["status_options"] = inputs.enums_by_et.get(status_sibling["id"], {})

    conv_lower = (dp.get("conversion") or "").strip().lower()
    param_type = (dp.get("parameter_type") or "").strip().lower()
    is_datetime_or_str = conv_lower in (
        "datetimebcd",
        "datetime_bcd",
        "datebcd",
        "daytodate",
        "hexbyte2asciibyte",
        "hexbyte2utf16byte",
    ) or param_type in ("array", "string")

    writable = (
        dp.get("entity_kind") == KIND_WRITABLE
        and (dp.get("fc_write") or "undefined") not in inputs.unreachable_fc
    )
    if writable:
        # A writable datapoint one bit wide has exactly two states, so it is a switch --
        # whether or not the catalog bothered to name them. Party mode, eco mode and the
        # one-off hot water run are all this. As a select they took two taps and read
        # "Aus"/"Ein" where Home Assistant already draws a toggle; as a number, which is
        # what the unnamed ones fell through to, they read 0 and 1.
        if bit_length == 1:
            profile["switches"].append(entry)
        elif enum_values:
            entry["options"] = enum_values
            profile["selects"].append(entry)
        elif is_datetime_or_str:
            entry["conversion"] = dp.get("conversion") or "NoConversion"
            profile["sensors"].append(_as_sensor(entry, sensor_category))
        else:
            entry["div_ratio"] = _div_ratio(dp.get("conversion"))
            entry.update(_unit_meta(unit_str))
            entry.update(_number_limits(dp, entry["div_ratio"]))
            profile["numbers"].append(entry)
        return

    if enum_values:
        entry["options"] = enum_values
        profile["sensors"].append(_as_sensor(entry, sensor_category))
    elif param_type == "bit":
        profile["binary_sensors"].append(_as_sensor(entry, sensor_category))
    else:
        entry["div_ratio"] = _div_ratio(dp.get("conversion"))
        entry.update(_unit_meta(unit_str))
        # A reading that decodes to a number is a measurement to Home Assistant
        # whether or not the catalog gives it a unit. Without a state class a
        # unitless one -- a switching-cycle counter, a performance factor -- is
        # treated as text: drawn as a timeline, never graphed, no statistics.
        # Nothing in the catalog tells a counter from a gauge, so both get
        # `measurement`, which graphs either correctly. Whole numbers are shown
        # without the ".0" the scaling path leaves on them.
        conversion = dp.get("conversion")
        if not is_datetime_or_str and decodes_to_number(conversion):
            entry.setdefault("state_class", "measurement")
            if decodes_to_integer(conversion):
                entry["display_precision"] = 0
        profile["sensors"].append(_as_sensor(entry, sensor_category))


def _dedupe_ids(profile: dict[str, Any]) -> None:
    """Two datapoints with one name: both ids get their address appended."""
    all_entries = [e for plat in _ENTITY_PLATFORMS for e in profile.get(plat, [])]
    counts: dict[str, int] = {}
    for entry in all_entries:
        counts[entry["id"]] = counts.get(entry["id"], 0) + 1
    for entry in all_entries:
        if counts[entry["id"]] > 1:
            entry["id"] = f"{entry['id']}_{entry['address'].lower().replace('0x', '')}"


def _active_circuits(profile: dict[str, Any], inputs: _ProfileInputs) -> dict[str, str]:
    """Only the circuits that have entities and are not hidden."""
    active = {
        e["circuit"]
        for plat in _ENTITY_PLATFORMS
        for e in profile.get(plat, [])
        if e.get("circuit")
    }
    return {
        k: v
        for k, v in inputs.circuits.items()
        if k in active and k not in inputs.hidden_circuits
    }


def _schedules(
    conn: sqlite3.Connection,
    culture: str,
    inputs: _ProfileInputs,
    circuits: dict[str, str],
) -> dict[str, Any]:
    """The weekly programmes the controller puts on its own menus.

    The catalog marks them with a MappingType, which also fixes the wire layout, the levels and
    where the level names live -- see conversions.SCHEDULE_TYPES. One programme is one channel:
    a controller can hold several per circuit (hot water and circulation both belong to the
    hot-water circuit), so they are keyed by the datapoint rather than by the circuit.

    A level's name is catalog text like any other. Each programme datapoint carries the key stem
    to look it up under -- one for the wording shared by its type and one for wording filed
    under the datapoint itself, which a few programmes have -- and the level number completes
    the key. Building those keys here instead would put the catalog's own naming into this
    code, where a rebuilt catalog could not correct it.
    """
    level_texts = _schedule_level_texts(conn, culture, inputs.datapoints)
    schedules: dict[str, Any] = {}
    for dp in inputs.datapoints:
        stype = schedule_type(dp.get("mapping_type"))
        if stype is None:
            continue
        et_id = dp["id"]
        if et_id in inputs.hidden_event_type_ids:
            continue
        # Only programmes the controller puts on its own menus. The catalog also carries
        # alternative layouts of the same programme (a quarter-hour bitmap next to the
        # window list) that no menu references; the controller does not answer for those.
        g_ids = inputs.group_ids_by_et.get(et_id, [])
        if not g_ids or all(gid in inputs.hidden_group_ids for gid in g_ids):
            continue
        circuit = dp.get("circuit")
        if circuit and circuit in inputs.hidden_circuits:
            continue
        block_length = dp.get("block_length") or 0
        block_factor = dp.get("block_factor") or 0
        if block_length % 7:
            _LOGGER.debug(
                "Programme %s has %d bytes, not a whole week; skipped",
                dp["name"],
                block_length,
            )
            continue
        # The bare datapoint name ("NKU_Tagesprogramm_HK1"), which is both the key and what
        # the per-programme label texts are filed under.
        dp_name = str(dp["name"]).rsplit(".", 1)[-1]
        address = dp["address"]
        # One entry per level the programme allows: its number, the catalog's name for it on
        # this programme, and a colour. The colour is published because it is catalog data; a
        # frontend is free to ignore it, and the bundled card does, so that it can follow the
        # Home Assistant theme instead.
        modes = []
        for level, color in zip(stype["levels"], stype["colors"], strict=True):
            label = next(
                (level_texts[k] for k in _level_keys(dp, level) if level_texts.get(k)),
                str(level),
            )
            modes.append({"value": level, "label": label, "color": color})
        schedules[_slug(dp_name)] = {
            "name": dp.get("pretty_name") or dp_name,
            "circuit": circuit,
            "circuit_name": circuits.get(circuit, circuit) if circuit else None,
            "address": address,
            "format": stype["format"],
            "day_bytes": block_length // 7,
            "windows_per_day": stype["windows"],
            "minutes_per_step": stype["step"],
            "default_level": stype["default"],
            "levels": list(stype["levels"]),
            "modes": modes,
            "block_length": block_length,
            "block_factor": block_factor,
            # Says how the address advances from one record to the next; see
            # optolink.address_step_bytes().
            "mapping_type": dp.get("mapping_type"),
            "fc_read": dp.get("fc_read"),
            "fc_write": dp.get("fc_write"),
        }
    return schedules


def generate_profile(
    device_id: int,
    db_path: str,
    culture: str,
    probed_values: dict[int, int],
    enabled_tiers: set[str] | None = None,
    unreachable_fc: set[str] | None = None,
) -> dict[str, Any]:
    """The entity set for one installation: what the catalog says, minus what the rules hide.

    `unreachable_fc` are catalog FCRead/FCWrite values this link has no telegram for (see
    optolink.unreachable_function_codes). A datapoint that cannot be read is left out entirely,
    and one that cannot be written becomes a reading rather than a control that always fails.
    """
    hidden_event_type_ids, hidden_group_ids = evaluate_rules(
        device_id, probed_values, db_path, culture
    )
    with closing(get_db_connection(db_path)) as conn:
        inputs = _load_profile_inputs(
            conn,
            device_id,
            culture,
            hidden_event_type_ids,
            hidden_group_ids,
            # Tiers switched on in the options, on top of the always-on base set. Defaults to
            # nothing extra rather than to DAILY_TIERS: the base set is chosen from the
            # controller's own menu tree, not from tiers, so falling back to a tier list here
            # would silently re-enable a couple of hundred entities the user did not ask for.
            enabled_tiers if enabled_tiers is not None else set(),
            unreachable_fc or set(),
        )
        profile: dict[str, Any] = {
            **{platform: [] for platform in _ENTITY_PLATFORMS},
            "device_name": inputs.device_name,
            "model": inputs.model,
            "circuits": inputs.circuits,
            "groups": inputs.groups,
            "clock_dst": inputs.clock_dst,
        }
        for dp in inputs.datapoints:
            _place_datapoint(dp, inputs, profile)
        _dedupe_ids(profile)
        profile["circuits"] = _active_circuits(profile, inputs)
        profile["schedules"] = _schedules(conn, culture, inputs, profile["circuits"])
        return profile
