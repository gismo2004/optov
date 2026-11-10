"""A small catalog built by hand, in the structure the compiler writes.

One invented controller with a handful of datapoints, two languages and four display rules:
enough to run identification, the startup probe, the rules and the generated entity set without
a real catalog. Nothing in it comes from a vendor's data.
"""

import sqlite3
from contextlib import closing

SCHEMA_VERSION = 4
SYSTEM_ID = 0x20AB
DEVICE = 1

# Datapoints of the controller.
OUTSIDE, ROOM, MODE, PARTY, CIRCUIT2, ROOM2, SLOPE2, FLOW, MIXER, RUNTIME = range(
    10, 20
)
# Rule inputs that belong to a sibling family: defined, but not linked to this controller.
FOREIGN_CIRCUIT2, FOREIGN_FLOW_HEALTH, FOREIGN_MIXER = 90, 91, 92

_SCHEMA = """
CREATE TABLE catalog_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL) WITHOUT ROWID;
CREATE TABLE devices (
    id INTEGER PRIMARY KEY, system_id TEXT NOT NULL, model TEXT NOT NULL,
    name_key TEXT NOT NULL, identification TEXT, ident_ext TEXT, ident_ext_till TEXT,
    f0 INTEGER, f0_till INTEGER, controller_type INTEGER, error_type INTEGER,
    event_optimisation INTEGER, event_optimisation_exceptions TEXT);
CREATE TABLE circuits (
    device_id INTEGER NOT NULL, circuit TEXT NOT NULL, name_key TEXT NOT NULL,
    PRIMARY KEY (device_id, circuit)) WITHOUT ROWID;
CREATE TABLE groups (
    id INTEGER PRIMARY KEY, device_id INTEGER NOT NULL, parent_id INTEGER,
    address TEXT NOT NULL, name_key TEXT NOT NULL, order_index INTEGER NOT NULL);
CREATE TABLE datapoint_defs (
    id INTEGER PRIMARY KEY, address TEXT NOT NULL, name TEXT NOT NULL,
    name_key TEXT NOT NULL, description_key TEXT, byte_length INTEGER NOT NULL,
    block_length INTEGER NOT NULL, byte_position INTEGER NOT NULL,
    bit_start INTEGER NOT NULL, bit_length INTEGER NOT NULL, conversion TEXT NOT NULL,
    unit TEXT, entity_kind INTEGER NOT NULL, parameter_type TEXT, min_value REAL,
    max_value REAL, stepping REAL, fc_read TEXT, fc_write TEXT, prefix_read TEXT,
    block_factor INTEGER NOT NULL DEFAULT 0, sdk_data_type TEXT, conversion_factor REAL,
    conversion_offset REAL, priority INTEGER, mapping_type INTEGER NOT NULL DEFAULT 0,
    level_text_stem TEXT, level_text_stem_dp TEXT, default_value TEXT, value_type TEXT,
    quick_check INTEGER NOT NULL DEFAULT 0, role TEXT);
CREATE TABLE device_datapoints (
    device_id INTEGER NOT NULL, datapoint_id INTEGER NOT NULL, tier TEXT, circuit TEXT,
    PRIMARY KEY (device_id, datapoint_id)) WITHOUT ROWID;
CREATE TABLE error_codes (
    device_id INTEGER NOT NULL, code TEXT NOT NULL, text_key TEXT NOT NULL,
    PRIMARY KEY (device_id, code)) WITHOUT ROWID;
CREATE TABLE fa_error_codes (
    chip TEXT NOT NULL, code TEXT NOT NULL, text_key TEXT NOT NULL,
    PRIMARY KEY (chip, code)) WITHOUT ROWID;
CREATE TABLE enums (
    event_type_id INTEGER NOT NULL, val_key INTEGER NOT NULL, text_key TEXT NOT NULL,
    PRIMARY KEY (event_type_id, val_key)) WITHOUT ROWID;
CREATE TABLE datapoint_groups (
    event_type_id INTEGER NOT NULL, group_id INTEGER NOT NULL,
    PRIMARY KEY (event_type_id, group_id)) WITHOUT ROWID;
CREATE TABLE display_conditions (
    id INTEGER NOT NULL, device_id INTEGER NOT NULL, target_event_type_id INTEGER,
    target_group_id INTEGER, condition_event_type_id INTEGER NOT NULL, op TEXT NOT NULL,
    compare_value INTEGER NOT NULL, rule_type INTEGER NOT NULL, group_id INTEGER);
CREATE TABLE display_condition_groups (
    id INTEGER NOT NULL, device_id INTEGER NOT NULL, parent_id INTEGER,
    type INTEGER NOT NULL, target_event_type_id INTEGER, target_group_id INTEGER,
    PRIMARY KEY (device_id, id)) WITHOUT ROWID;
CREATE TABLE strings (id INTEGER PRIMARY KEY, s TEXT NOT NULL);
CREATE TABLE translations_p (
    text_key TEXT PRIMARY KEY, "de" INTEGER, "en" INTEGER) WITHOUT ROWID;
CREATE VIEW datapoints AS
    SELECT dd.device_id, d.*, dd.tier, dd.circuit
    FROM device_datapoints dd JOIN datapoint_defs d ON d.id = dd.datapoint_id;
"""

# text key -> (German, English)
_TEXTS = {
    "device": (
        "Beschreibung: Testregelung 100 (ab 01/2020)",
        "Description: Test control 100",
    ),
    "circuit.hc1": ("Heizkreis 1", "Heating circuit 1"),
    "circuit.hc2": ("Heizkreis 2", "Heating circuit 2"),
    "group.general": ("Allgemein", "General"),
    "group.heating": ("Heizen", "Heating"),
    "group.circuit2": ("Heizkreis 2", "Heating circuit 2"),
    "dp.outside": ("Außentemperatur", "Outside temperature"),
    "dp.room": ("Raumtemperatur Soll", "Room temperature setpoint"),
    "dp.room.about": ("Die gewünschte Raumtemperatur.", "The wanted room temperature."),
    "dp.mode": ("Betriebsart", "Operating mode"),
    "dp.party": ("Partybetrieb", "Party mode"),
    "dp.circuit2": ("Heizkreis 2 vorhanden", "Circuit 2 fitted"),
    "dp.room2": ("Raumtemperatur Soll 2", "Room temperature setpoint 2"),
    "dp.slope2": ("Neigung 2", "Slope 2"),
    "dp.flow": ("Vorlauftemperatur", "Flow temperature"),
    # The same wording in English only, as its foreign twin below.
    "dp.mixer": ("Mischer vorhanden", "Mixer fitted"),
    "dp.mixer.foreign": ("Mischer (älteres Modell)", "Mixer fitted"),
    "dp.runtime": ("Mischerlaufzeit", "Mixer runtime"),
    "mode.0": ("Aus", "Off"),
    "mode.1": ("Heizen", "Heating"),
    "mode.2": ("Automatik", "Automatic"),
    "unit.c": ("°C", "°C"),
}


def _datapoint(conn, dp_id, address, name, text, *, linked=True, **fields):
    """One definition; `linked` puts it on the controller, with its tier and circuit."""
    tier, circuit = fields.pop("tier", "Operation"), fields.pop("circuit", None)
    row = {
        "id": dp_id,
        "address": address,
        "name": name,
        "name_key": text,
        "byte_length": 2,
        "block_length": 2,
        "byte_position": 0,
        "bit_start": 0,
        "bit_length": 0,
        "conversion": "NoConversion",
        "entity_kind": 1,
        "parameter_type": "Int",
        "fc_read": "Virtual_READ",
        "fc_write": "undefined",
        **fields,
    }
    conn.execute(
        f"INSERT INTO datapoint_defs ({', '.join(row)}) "
        f"VALUES ({', '.join('?' * len(row))})",
        tuple(row.values()),
    )
    if linked:
        conn.execute(
            "INSERT INTO device_datapoints VALUES (?, ?, ?, ?)",
            (DEVICE, dp_id, tier, circuit),
        )


def _rule(conn, rule_id, target, condition, op, value):
    """Hide `target` while the condition holds: one rule tree with one condition."""
    conn.execute(
        "INSERT INTO display_condition_groups VALUES (?, ?, NULL, 1, ?, NULL)",
        (rule_id, DEVICE, target),
    )
    conn.execute(
        "INSERT INTO display_conditions VALUES (?, ?, ?, NULL, ?, ?, ?, 1, ?)",
        (rule_id, DEVICE, target, condition, op, value, rule_id),
    )


def build(path, schema_version=SCHEMA_VERSION) -> str:
    """Write the sample catalog to `path` and return the path as text."""
    with closing(sqlite3.connect(path)) as conn:
        conn.executescript(_SCHEMA)
        conn.execute(
            "INSERT INTO catalog_meta VALUES ('schema_version', ?)",
            (str(schema_version),),
        )
        for index, (key, (german, english)) in enumerate(_TEXTS.items()):
            conn.execute("INSERT INTO strings VALUES (?, ?)", (2 * index + 1, german))
            conn.execute("INSERT INTO strings VALUES (?, ?)", (2 * index + 2, english))
            conn.execute(
                "INSERT INTO translations_p VALUES (?, ?, ?)",
                (key, 2 * index + 1, 2 * index + 2),
            )
        conn.execute(
            "INSERT INTO devices (id, system_id, model, name_key, identification) "
            "VALUES (?, '20AB', 'TC100', 'device', '20AB')",
            (DEVICE,),
        )
        conn.executemany(
            "INSERT INTO circuits VALUES (?, ?, ?)",
            [(DEVICE, "HC1", "circuit.hc1"), (DEVICE, "HC2", "circuit.hc2")],
        )
        conn.executemany(
            "INSERT INTO groups VALUES (?, ?, NULL, ?, ?, ?)",
            [
                (1, DEVICE, "TC100~Overview~General", "group.general", 1),
                (2, DEVICE, "TC100~Operation~Heating", "group.heating", 2),
                (3, DEVICE, "TC100~Operation~Circuit2", "group.circuit2", 3),
            ],
        )

        tenths = {"conversion": "Div10", "unit": "unit.c"}
        setting = {"entity_kind": 3, "fc_write": "Virtual_WRITE"}
        limits = {"min_value": 10, "max_value": 30, "stepping": 1}
        _datapoint(
            conn,
            OUTSIDE,
            "0x0101",
            "TC_Outside",
            "dp.outside",
            tier="Overview",
            **tenths,
        )
        _datapoint(
            conn, ROOM, "0x2000", "TC_Room", "dp.room", circuit="HC1",
            description_key="dp.room.about", default_value="20",
            **tenths, **setting, **limits,
        )  # fmt: skip
        _datapoint(
            conn,
            MODE,
            "0x2010",
            "TC_Mode",
            "dp.mode",
            default_value="2",
            byte_length=1,
            block_length=1,
            **setting,
        )
        _datapoint(
            conn,
            PARTY,
            "0x2020",
            "TC_Party",
            "dp.party",
            default_value="0",
            byte_length=1,
            block_length=1,
            bit_length=1,
            **setting,
        )
        _datapoint(
            conn,
            CIRCUIT2,
            "0x7000",
            "TC_Circuit2",
            "dp.circuit2",
            byte_length=1,
            block_length=1,
        )
        _datapoint(
            conn,
            ROOM2,
            "0x3000",
            "TC_Room2",
            "dp.room2",
            circuit="HC2",
            **tenths,
            **setting,
            **limits,
        )
        _datapoint(
            conn,
            SLOPE2,
            "0x3007",
            "TC_Slope2",
            "dp.slope2",
            circuit="HC2",
            **tenths,
            **setting,
        )
        _datapoint(
            conn, FLOW, "0x0105", "TC_Flow", "dp.flow", tier="Overview", **tenths
        )
        _datapoint(
            conn, MIXER, "0x7001", "TC_Mixer", "dp.mixer", byte_length=1, block_length=1
        )
        _datapoint(conn, RUNTIME, "0x7002", "TC_Runtime", "dp.runtime", **setting)
        _datapoint(
            conn, FOREIGN_CIRCUIT2, "0x7100", "TX_Circuit2", "dp.circuit2", linked=False
        )
        _datapoint(
            conn,
            FOREIGN_FLOW_HEALTH,
            "0x0105",
            "TX_SensorStatus_Flow",
            "dp.flow",
            linked=False,
        )
        _datapoint(
            conn, FOREIGN_MIXER, "0x7101", "TX_Mixer", "dp.mixer.foreign", linked=False
        )

        conn.executemany(
            "INSERT INTO datapoint_groups VALUES (?, ?)",
            [(OUTSIDE, 1), (FLOW, 1), (ROOM, 2), (MODE, 2), (PARTY, 2), (CIRCUIT2, 2),
             (MIXER, 2), (RUNTIME, 2), (ROOM2, 3), (SLOPE2, 3)],
        )  # fmt: skip
        conn.executemany(
            "INSERT INTO enums VALUES (?, ?, ?)",
            [(MODE, 0, "mode.0"), (MODE, 1, "mode.1"), (MODE, 2, "mode.2")],
        )
        # Circuit 2's setpoint goes by the controller's own flag, its slope by the sibling
        # family's flag of the same name. The flow sensor's rule names a sensor-health input
        # of the sibling, and the mixer's a flag that shares its name in English only.
        _rule(conn, 1, ROOM2, CIRCUIT2, "eq", 0)
        _rule(conn, 2, SLOPE2, FOREIGN_CIRCUIT2, "eq", 0)
        _rule(conn, 3, FLOW, FOREIGN_FLOW_HEALTH, "eq", 6)
        _rule(conn, 4, RUNTIME, FOREIGN_MIXER, "eq", 0)
        conn.commit()
    return str(path)
