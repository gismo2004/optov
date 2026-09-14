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
from contextlib import closing
from typing import Any

from .conversions import schedule_type
from .decode import decodes_to_integer, decodes_to_number

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
# The compiler writes its own number into `catalog_meta`. Raise this one only together with the
# compiler's, and only when an older catalog would actually be wrong: a table or column that is
# now required, a changed meaning, a different key format.
#
#   1  level-name key stems on datapoint_defs, catalog_meta itself
CATALOG_SCHEMA_VERSION = 1


class CatalogSchemaError(ValueError):
    """A real catalog, but not one this code can read.

    Carries which way round it is, so the message can name the side that needs updating
    instead of saying only that the two disagree.
    """

    def __init__(self, found: int | None) -> None:
        self.found = found
        self.needed = CATALOG_SCHEMA_VERSION
        self.outdated = found is None or found < CATALOG_SCHEMA_VERSION
        super().__init__(
            f"catalog schema version {found if found is not None else 'absent'}, "
            f"this integration needs {CATALOG_SCHEMA_VERSION}"
        )


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
# own columns and quoted. Anything unknown falls back to English.
_CULTURE_COLUMNS: set[str] | None = None


def _culture_column(conn: sqlite3.Connection, culture: str | None) -> str:
    """A quoted `translations_p` column for a language, validated against the table itself."""
    global _CULTURE_COLUMNS
    if _CULTURE_COLUMNS is None:
        _CULTURE_COLUMNS = {
            r[1]
            for r in conn.execute("PRAGMA table_info(translations_p)")
            if r[1] != "text_key"
        }
    wanted = (culture or "en").strip().lower()
    if wanted not in _CULTURE_COLUMNS:
        wanted = "en" if "en" in _CULTURE_COLUMNS else "de"
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
        usable = version == CATALOG_SCHEMA_VERSION
        label = f"{name} ({devices} controllers, {'/'.join(languages)})"
        if not usable:
            # Still listed, so that it is visible and can be replaced, but it says what is
            # wrong with it rather than failing after it has been chosen. Which side is behind
            # decides the advice: rebuilding a catalog that is ahead would not help.
            label += (
                " -- newer than this integration, update it"
                if version is not None and version > CATALOG_SCHEMA_VERSION
                else " -- built for an older version, rebuild it"
            )
        found.append(
            {
                "name": name,
                "path": path,
                "devices": devices,
                "languages": languages,
                "schema_version": version,
                "usable": usable,
                "label": label,
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


def _catalog_name(filename: str) -> str:
    """A plain `.db` filename, with any directory part and any `.xz` suffix taken off."""
    name = os.path.basename(filename).strip() or CATALOG_FILENAME
    if name.endswith(".xz"):
        name = name[:-3]
    if not name.endswith(".db"):
        name += ".db"
    return name


def _verify(path: str) -> None:
    """Raise unless this is a controller catalog this code can read.

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
            if version != CATALOG_SCHEMA_VERSION:
                raise CatalogSchemaError(version)
    except sqlite3.DatabaseError as err:
        raise ValueError(f"not a readable database: {err}") from err


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


def check_catalog(path: str) -> None:
    """Raise if this catalog cannot be read by this code. Blocking I/O; use an executor.

    Installing verifies too, but a catalog can also be copied in by hand and an integration
    update can move the goalposts under one that was fine yesterday, so it is checked again
    every time an entry starts.
    """
    _verify(path)


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
    3. Failing both, the candidate that declares no extension at all, which acts as the
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

    # Stage 3 -- the extension-less catch-all.
    for c in candidates:
        if not c["ident_ext"]:
            return c
    return None


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
            return None

        row = _select_variant(candidates, hw_index, sw_index, f0, sys_id & 0xFF)
        if row is None:
            _LOGGER.warning(
                "System ID 0x%04X matches %d controllers but none fits hardware index %s / "
                "software index %s / F0 %s",
                sys_id,
                len(candidates),
                hw_index,
                sw_index,
                f0,
            )
            return None
        if len(candidates) > 1:
            _LOGGER.info(
                "System ID 0x%04X matches %d controllers; selected %s "
                "(hardware index %s, software index %s, F0 %s)",
                sys_id,
                len(candidates),
                row["model"],
                hw_index,
                sw_index,
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
               JOIN datapoints dp ON dc.condition_event_type_id = dp.id AND dp.device_id = dc.device_id
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


_ALIAS_CACHE: dict[tuple[int, str], dict[int, int]] = {}


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
    cached = _ALIAS_CACHE.get((device_id, culture))
    if cached is not None:
        return cached

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
            _ALIAS_CACHE[(device_id, culture)] = {}
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
        _ALIAS_CACHE[(device_id, culture)] = aliases
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


def generate_profile(
    device_id: int,
    db_path: str,
    culture: str,
    probed_values: dict[int, int],
    enabled_tiers: set[str] | None = None,
) -> dict[str, Any]:
    """Dynamically generate entity configurations from SQLite database for an installation."""
    hidden_event_type_ids, hidden_group_ids = evaluate_rules(
        device_id, probed_values, db_path, culture
    )
    # Tiers switched on in the options, on top of the always-on base set. Defaults to nothing
    # extra rather than to DAILY_TIERS: the base set is chosen from the controller's own menu
    # tree, not from tiers, so falling back to a tier list here would silently re-enable a
    # couple of hundred entities the user did not ask for.
    active_tiers = enabled_tiers if enabled_tiers is not None else set()

    with closing(get_db_connection(db_path)) as conn:
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

        # Detect disabled circuits from hidden circuit groups in the circuit tree (ecnsysEventTypeGroupHC)
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
        groups = [dict(r) for r in g_rows]
        group_addrs_by_id = {r["id"]: (r["address"] or "") for r in g_rows}

        profile: dict[str, Any] = {
            "sensors": [],
            "binary_sensors": [],
            "numbers": [],
            "selects": [],
            "switches": [],
            "device_name": device_name,
            "model": model,
            "circuits": circuits,
            "groups": groups,
            "clock_dst": {},
        }

        # Group siblings by address for status-nibble pairing
        by_address: dict[str, list[dict[str, Any]]] = {}
        datapoints = [dict(r) for r in dp_rows]

        clock_settings, clock_dst = _clock_settings(
            datapoints, group_ids_by_et, group_addrs_by_id
        )
        profile["clock_dst"] = clock_dst
        for dp in datapoints:
            by_address.setdefault(dp["address"], []).append(dp)

        for dp in datapoints:
            tier = dp.get("tier")
            if tier not in TIER_CLASSIFICATION:
                continue
            if dp.get("entity_kind") == KIND_ACTION:
                continue

            # Skip raw multi-byte array dumps (e.g. 168-byte EEPROM schedule arrays handled by schedule poller)
            if (
                dp.get("byte_length", 1) > 8
                and (dp.get("parameter_type") or "").strip().lower() == "array"
            ):
                continue

            c = dp.get("circuit")
            if not c:
                g_ids = group_ids_by_et.get(dp["id"], [])
                g_addrs = [group_addrs_by_id.get(gid, "") for gid in g_ids]
                if any(
                    "solaranlage" in a.lower() or a.lower().endswith("~solar")
                    for a in g_addrs
                ):
                    c = "Solar"
                elif any(
                    "warmwasser" in a.lower() or a.lower().endswith("~ww")
                    for a in g_addrs
                ):
                    c = "WW"
                elif any(
                    "~hk1" in a.lower()
                    or "~heizkreis1" in a.lower()
                    or a.lower().endswith("~hc1")
                    for a in g_addrs
                ):
                    c = "HC1"
                elif any(
                    "~hk2" in a.lower()
                    or "~heizkreis2" in a.lower()
                    or a.lower().endswith("~hc2")
                    for a in g_addrs
                ):
                    c = "HC2"
                elif any(
                    "~hk3" in a.lower()
                    or "~heizkreis3" in a.lower()
                    or a.lower().endswith("~hc3")
                    for a in g_addrs
                ):
                    c = "HC3"

            if c and c in hidden_circuits:
                continue

            et_id = dp["id"]
            if et_id in hidden_event_type_ids:
                continue

            g_ids = group_ids_by_et.get(et_id, [])
            if g_ids and all(gid in hidden_group_ids for gid in g_ids):
                continue

            bit_length = dp.get("bit_length", 0)
            if bit_length > 0 and dp.get("entity_kind") == KIND_READONLY:
                # Read-only bit-fields are the sensor-health nibbles, which ride along with the
                # value they describe and are surfaced through its availability instead.
                continue
            # Writable bit-fields are real controls and must be exposed -- operating mode, party
            # and eco all live in bits of a shared register. Writing them needs the whole block
            # read and patched, which async_write_item() handles.

            entity_category = TIER_CLASSIFICATION[tier]
            if et_id in clock_settings:
                entity_category = "config"
            enum_values = enums_by_et.get(et_id, {})
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
                for seg in group_addrs_by_id.get(gid, "").split("~")[1:2]
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
            sensor_category = (
                "diagnostic" if entity_category == "config" else entity_category
            )

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
                # Base set: what the controller itself puts in front of its user. Plus whichever
                # optional tiers the options have switched on, so diagnostics or coding channels
                # can be brought in for a debugging session and dropped again afterwards.
                "enabled_by_default": (
                    ((in_overview or in_trend) and is_measurement)
                    or in_operation
                    or (tier in active_tiers)
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

            # Check for status nibble sibling on same address
            siblings = by_address.get(dp["address"], [])
            status_sibling = next(
                (
                    s
                    for s in siblings
                    if s.get("bit_length", 0) > 0 and s["id"] in enums_by_et
                ),
                None,
            )
            if status_sibling is not None:
                entry["status_bit_start"] = status_sibling.get("bit_start", 0)
                entry["status_bit_length"] = status_sibling.get("bit_length", 0)
                entry["status_options"] = enums_by_et.get(status_sibling["id"], {})

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

            if dp.get("entity_kind") == KIND_WRITABLE:
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
                    if sensor_category:
                        entry["entity_category"] = sensor_category
                    profile["sensors"].append(entry)
                else:
                    entry["div_ratio"] = _div_ratio(dp.get("conversion"))
                    entry.update(_unit_meta(unit_str))
                    if dp.get("max_value") is not None:
                        entry["max"] = dp["max_value"]
                    if dp.get("min_value") is not None:
                        entry["min"] = dp["min_value"]
                    if dp.get("stepping") is not None and dp["stepping"] > 0:
                        entry["step"] = dp["stepping"]
                    profile["numbers"].append(entry)
            else:
                if enum_values:
                    entry["options"] = enum_values
                    if sensor_category:
                        entry["entity_category"] = sensor_category
                    profile["sensors"].append(entry)
                else:
                    ptype = (dp.get("parameter_type") or "").strip().lower()
                    if ptype == "bit":
                        if sensor_category:
                            entry["entity_category"] = sensor_category
                        profile["binary_sensors"].append(entry)
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
                        if sensor_category:
                            entry["entity_category"] = sensor_category
                        profile["sensors"].append(entry)

        # Deduplicate IDs
        platforms = ("sensors", "binary_sensors", "numbers", "selects", "switches")
        all_entries = [e for plat in platforms for e in profile.get(plat, [])]
        counts: dict[str, int] = {}
        for entry in all_entries:
            counts[entry["id"]] = counts.get(entry["id"], 0) + 1
        for entry in all_entries:
            if counts[entry["id"]] > 1:
                entry["id"] = (
                    f"{entry['id']}_{entry['address'].lower().replace('0x', '')}"
                )

        # Filter circuits to only those that have active entities and are not hidden
        active_circuits = {
            e["circuit"]
            for plat in platforms
            for e in profile.get(plat, [])
            if e.get("circuit")
        }
        profile["circuits"] = {
            k: v
            for k, v in circuits.items()
            if k in active_circuits and k not in hidden_circuits
        }

        # Weekly programmes. The catalog marks them with a MappingType, which also fixes the wire
        # layout, the levels and where the level names live -- see conversions.SCHEDULE_TYPES.
        # One programme is one channel: a controller can hold several per circuit (hot water and
        # circulation both belong to the hot-water circuit), so they are keyed by the datapoint
        # rather than by the circuit.
        #
        # A level's name is catalog text like any other. Each programme datapoint carries the
        # key stem to look it up under -- one for the wording shared by its type and one for
        # wording filed under the datapoint itself, which a few programmes have -- and the
        # level number completes the key. Building those keys here instead would put the
        # catalog's own naming into this code, where a rebuilt catalog could not correct it.
        level_texts = _schedule_level_texts(conn, culture, datapoints)
        schedules: dict[str, Any] = {}
        for dp in datapoints:
            stype = schedule_type(dp.get("mapping_type"))
            if stype is None:
                continue
            et_id = dp["id"]
            if et_id in hidden_event_type_ids:
                continue
            # Only programmes the controller puts on its own menus. The catalog also carries
            # alternative layouts of the same programme (a quarter-hour bitmap next to the
            # window list) that no menu references; the controller does not answer for those.
            g_ids = group_ids_by_et.get(et_id, [])
            if not g_ids or all(gid in hidden_group_ids for gid in g_ids):
                continue
            circuit = dp.get("circuit")
            if circuit and circuit in hidden_circuits:
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
                    (
                        level_texts[k]
                        for k in _level_keys(dp, level)
                        if level_texts.get(k)
                    ),
                    str(level),
                )
                modes.append({"value": level, "label": label, "color": color})
            schedules[_slug(dp_name)] = {
                "name": dp.get("pretty_name") or dp_name,
                "circuit": circuit,
                "circuit_name": profile["circuits"].get(circuit, circuit)
                if circuit
                else None,
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
                "fc_read": dp.get("fc_read"),
                "fc_write": dp.get("fc_write"),
            }

        profile["schedules"] = schedules

        return profile
