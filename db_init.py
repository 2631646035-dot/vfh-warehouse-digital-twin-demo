# -*- coding: utf-8 -*-
"""Build a completely synthetic SQLite database for the public portfolio demo."""

import argparse
import math
import os
import sqlite3
from datetime import date, datetime, timedelta, timezone

from data_loader import DEFAULT_VFH_SCENARIO, ensure_runtime_schema


DB_PATH = os.path.join(os.path.dirname(__file__), "warehouse.db")

DEMO_SKUS = [
    ("DEMO-MW-01", "微波炉 20L", "厨房电器", 49, 38, 29, 13.0, "flexible"),
    ("DEMO-MW-02", "微波炉 32L", "厨房电器", 55, 45, 34, 18.2, "flexible"),
    ("DEMO-TV-01", "电视 43英寸", "影音设备", 108, 14, 66, 10.8, "flexible"),
    ("DEMO-TV-02", "电视 55英寸", "影音设备", 136, 16, 83, 18.5, "floor_only"),
    ("DEMO-AC-01", "窗式空调", "环境电器", 56, 51, 38, 26.0, "flexible"),
    ("DEMO-AC-02", "移动空调", "环境电器", 49, 42, 75, 31.5, "floor_only"),
    ("DEMO-AC-03", "壁挂空调内机", "环境电器", 96, 31, 39, 12.4, "flexible"),
    ("DEMO-RF-01", "双门冰箱", "大家电", 67, 70, 178, 62.0, "floor_only"),
    ("DEMO-RF-02", "多门冰箱", "大家电", 91, 77, 190, 96.0, "floor_only"),
    ("DEMO-RF-04", "小型冰箱", "大家电", 55, 58, 143, 42.0, "floor_only"),
    ("DEMO-WM-01", "滚筒洗衣机", "大家电", 66, 69, 89, 68.0, "floor_only"),
    ("DEMO-WM-02", "波轮洗衣机", "大家电", 62, 67, 102, 48.0, "floor_only"),
    ("DEMO-DW-01", "洗碗机", "厨房电器", 65, 67, 88, 45.0, "floor_only"),
    ("DEMO-OV-01", "嵌入式烤箱", "厨房电器", 66, 65, 67, 38.0, "flexible"),
    ("DEMO-FN-01", "循环风扇", "环境电器", 48, 28, 54, 6.5, "shelf_only"),
    ("DEMO-VC-01", "手持吸尘器", "清洁电器", 76, 18, 25, 4.2, "shelf_only"),
    ("DEMO-VC-02", "扫地机器人", "清洁电器", 48, 40, 15, 5.6, "shelf_only"),
    ("DEMO-KT-01", "电热水壶", "厨房电器", 24, 22, 28, 1.8, "shelf_only"),
    ("DEMO-AF-01", "空气炸锅", "厨房电器", 39, 34, 37, 6.9, "shelf_only"),
    ("DEMO-HP-01", "电磁炉", "厨房电器", 43, 34, 12, 4.1, "shelf_only"),
]


def create_base_tables(conn):
    conn.executescript(
        """
        CREATE TABLE sku_catalog (
            code TEXT PRIMARY KEY,
            name TEXT NOT NULL DEFAULT '',
            category TEXT NOT NULL DEFAULT '',
            length_cm REAL NOT NULL DEFAULT 0,
            width_cm REAL NOT NULL DEFAULT 0,
            height_cm REAL NOT NULL DEFAULT 0,
            weight_kg REAL NOT NULL DEFAULT 0,
            volume_m3 REAL NOT NULL DEFAULT 0,
            storage_type TEXT NOT NULL DEFAULT 'flexible'
                CHECK(storage_type IN ('floor_only','flexible','shelf_only'))
        );

        CREATE TABLE daily_stock (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            sku_code TEXT NOT NULL,
            zone TEXT DEFAULT 'A' CHECK(zone IN ('A','B')),
            qty INTEGER NOT NULL DEFAULT 0,
            storage_type TEXT NOT NULL DEFAULT 'flexible'
                CHECK(storage_type IN ('floor_only','flexible','shelf_only')),
            FOREIGN KEY (sku_code) REFERENCES sku_catalog(code),
            UNIQUE(date, sku_code, zone)
        );

        CREATE TABLE warehouse_params (
            zone TEXT NOT NULL DEFAULT 'A' CHECK(zone IN ('A','B')),
            shelf_total_slots INTEGER DEFAULT 0,
            shelf_used INTEGER DEFAULT 0,
            floor_area REAL DEFAULT 0,
            floor_pallets INTEGER DEFAULT 0,
            width_m REAL DEFAULT 0,
            depth_m REAL DEFAULT 0,
            label TEXT DEFAULT ''
        );

        CREATE INDEX idx_daily_date ON daily_stock(date);
        CREATE INDEX idx_daily_sku ON daily_stock(sku_code);
        """
    )


def seed_demo_data(conn):
    today = date.today()
    stock_date = today.isoformat()
    now = datetime.now(timezone.utc).isoformat()

    for index, (code, name, category, length, width, height, weight, storage) in enumerate(DEMO_SKUS):
        volume = round(length * width * height / 1_000_000, 6)
        conn.execute(
            "INSERT INTO sku_catalog VALUES (?,?,?,?,?,?,?,?,?)",
            (code, name, category, length, width, height, weight, volume, storage),
        )
        a_qty = 220 + (index * 137) % 1380
        b_qty = 0 if index % 4 == 0 else 60 + (index * 53) % 360
        conn.execute(
            "INSERT INTO daily_stock(date,sku_code,zone,qty,storage_type) VALUES (?,?,?,?,?)",
            (stock_date, code, "A", a_qty, storage),
        )
        if b_qty:
            conn.execute(
                "INSERT INTO daily_stock(date,sku_code,zone,qty,storage_type) VALUES (?,?,?,?,?)",
                (stock_date, code, "B", b_qty, storage),
            )

    conn.executemany(
        "INSERT INTO warehouse_params VALUES (?,?,?,?,?,?,?,?)",
        [
            ("A", 1800, 1260, 1450, 420, 60, 72, "A区（货主自营仓）"),
            ("B", 1200, 510, 720, 360, 55, 64, "B区（平台前置仓）"),
        ],
    )
    conn.commit()
    conn.close()

    ensure_runtime_schema()
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys=ON")

    for code, _, _, length, width, height, _, _ in DEMO_SKUS:
        normal = max(1, math.floor(120 / length) * math.floor(80 / width))
        rotated = max(1, math.floor(120 / width) * math.floor(80 / length))
        per_layer = max(normal, rotated)
        layers = max(1, math.floor((180 - 15) / height))
        conn.execute(
            """INSERT OR REPLACE INTO sku_pallet_specs
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (code, 120, 80, 180, per_layer, layers, per_layer * layers, 1, now),
        )

    scenario = dict(DEFAULT_VFH_SCENARIO)
    scenario.update(
        country="Demo-Europe",
        monthly_demand_units=9800,
        inbound_containers_month=3,
        b_area_m2=1450,
        b_workers=4,
    )
    conn.execute("DELETE FROM vfh_scenarios")
    import json
    conn.execute(
        "INSERT INTO vfh_scenarios(country,config_json,updated_at) VALUES (?,?,?)",
        ("Demo-Europe", json.dumps(scenario, ensure_ascii=False), now),
    )

    batch_rows = [
        ("DEMO-Europe", "DEMO-A-001", "DEMO-MW-01", today - timedelta(days=78), 480, "A", 0.054),
        ("DEMO-Europe", "DEMO-A-002", "DEMO-TV-02", today - timedelta(days=46), 260, "A", 0.181),
        ("DEMO-Europe", "DEMO-B-003", "DEMO-AC-03", today - timedelta(days=18), 320, "B", 0.116),
        ("DEMO-Europe", "DEMO-B-004", "DEMO-RF-04", today - timedelta(days=12), 140, "B", 0.456),
        ("DEMO-Europe", "DEMO-SEA-005", "DEMO-WM-01", today - timedelta(days=7), 520, "SEA", 0.405),
        ("DEMO-Europe", "DEMO-CUS-006", "DEMO-AF-01", today - timedelta(days=3), 900, "CUSTOMS", 0.049),
    ]
    conn.executemany(
        """INSERT INTO inventory_batches
           (country,batch_id,sku_code,receipt_date,qty,zone,status,unit_volume_m3,updated_at)
           VALUES (?,?,?,?,?,?,'active',?,?)""",
        [(country, batch, sku, received.isoformat(), qty, zone, volume, now)
         for country, batch, sku, received, qty, zone, volume in batch_rows],
    )
    conn.commit()
    conn.close()


def build_database(force=False):
    if os.path.exists(DB_PATH):
        if not force:
            raise SystemExit("warehouse.db 已存在；如需重建演示数据，请运行 python db_init.py --force")
        os.remove(DB_PATH)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys=ON")
    create_base_tables(conn)
    seed_demo_data(conn)
    print(f"Synthetic demo database ready: {DB_PATH}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build synthetic portfolio demo data")
    parser.add_argument("--force", action="store_true", help="replace the existing demo database")
    build_database(parser.parse_args().force)
